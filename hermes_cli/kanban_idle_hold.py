"""Supported board-manager correction for idle blocked holds, never a worker action.

No schema migration and no change to normal block/unblock policy. See
``docs/kanban-idle-hold.md`` for the deliberately narrow action contract.
"""
from __future__ import annotations

import os
import sqlite3

from hermes_cli import kanban_db as kb


def reclassify_idle_hold(
    conn: sqlite3.Connection, task_id: str, *, expected_status: str,
    expected_kind: str | None, kind: str, reason: str,
) -> bool:
    """CAS an idle blocked hold; False means stale/unsafe state, with no writes.

    Refuse even expired claim remnants rather than guessing worker liveness.
    Correcting metadata is not another block cycle: counters/evidence survive.
    """
    from agent.delegation_context import is_delegated_child_process_context

    if os.environ.get("HERMES_KANBAN_TASK") or is_delegated_child_process_context():
        raise PermissionError("idle hold correction is board-manager-only, not a worker action")
    if expected_status != "blocked":
        raise ValueError("expected_status must be blocked")
    if expected_kind is not None and expected_kind not in kb.VALID_BLOCK_KINDS:
        raise ValueError("invalid expected_kind")
    if kind not in kb.VALID_BLOCK_KINDS:
        raise ValueError("invalid kind")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("a nonblank audited reason is required")
    # The native transaction guard rejects delegation and already-open txns;
    # BEGIN IMMEDIATE serializes parent inspection, CAS and audit with claims.
    with kb.write_txn(conn):
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or row["status"] != expected_status or row["block_kind"] != expected_kind:
            return False
        if any(row[field] is not None for field in (
            "current_run_id", "claim_lock", "claim_expires", "worker_pid", "worker_started_at",
        )):
            return False
        if conn.execute(
            "SELECT 1 FROM task_runs WHERE task_id=? "
            "AND (status='running' OR ended_at IS NULL) LIMIT 1", (task_id,),
        ).fetchone() is not None:
            return False
        status = "blocked"
        resume_status = kb._resume_status_from_events(conn, task_id)
        if kind == "dependency":
            status = kb._landing_status_after_parents(conn, task_id)
            if status == "ready" and resume_status == "review":
                status = "review"
        changed = conn.execute(
            "UPDATE tasks SET status=?, block_kind=? WHERE id=? AND status=? "
            "AND block_kind IS ? AND current_run_id IS NULL AND claim_lock IS NULL "
            "AND claim_expires IS NULL AND worker_pid IS NULL AND worker_started_at IS NULL",
            (status, kind, task_id, expected_status, expected_kind),
        ).rowcount
        if changed != 1:
            return False
        event_kind = "dependency_wait" if kind == "dependency" else "blocked"
        kb._append_event(conn, task_id, event_kind, {
            "reclassified_in_place": True,
            "previous_status": expected_status, "previous_kind": expected_kind,
            "status": status, "kind": kind, "reason": reason,
            "resume_status": resume_status,
        })
    return True


def main(argv: list[str] | None = None) -> int:
    """Standalone manager action; no worker/tool authority is added."""
    import argparse
    import json
    import sys
    from hermes_cli import kanban_db_connect as kbc

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", default=None)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--expected-status", required=True, choices=["blocked"])
    parser.add_argument("--expected-kind", required=True,
                        choices=["null", *sorted(kb.VALID_BLOCK_KINDS)])
    parser.add_argument("--kind", required=True, choices=sorted(kb.VALID_BLOCK_KINDS))
    parser.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    conn = None
    try:
        # Refuse before connecting: even schema initialization is a mutation.
        from agent.delegation_context import is_delegated_child_process_context
        if os.environ.get("HERMES_KANBAN_TASK") or is_delegated_child_process_context():
            raise PermissionError("idle hold correction is board-manager-only, not a worker action")
        conn = kbc.connect(board=args.board)
        ok = reclassify_idle_hold(
            conn, args.task_id, expected_status=args.expected_status,
            expected_kind=None if args.expected_kind == "null" else args.expected_kind,
            kind=args.kind, reason=args.reason,
        )
        task = kb.get_task(conn, args.task_id)
        print(json.dumps({"ok": ok, "task_id": args.task_id,
                          "status": task.status if task else None,
                          "block_kind": task.block_kind if task else None}))
        return 0 if ok else 1
    except (PermissionError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
