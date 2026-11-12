"""Atomic manager correction; all boards here are disposable native fixtures."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    import hermes_constants
    hermes_constants._cached_default_hermes_root = None
    kb._INITIALIZED_PATHS.clear()
    conn = kbc.connect()
    tid = kb.create_task(conn, title="original", assignee="author", initial_status="blocked")
    conn.execute("UPDATE tasks SET block_kind='dependency', block_recurrences=2, consecutive_failures=3, last_failure_error='retained' WHERE id=?", (tid,))
    yield conn, tid
    conn.close()


def correct(conn, tid, **kw):
    from hermes_cli.kanban_idle_hold import reclassify_idle_hold
    return reclassify_idle_hold(conn, tid, expected_status="blocked", expected_kind="dependency", kind="needs_input", reason="FOR COREY: review retained attachment", **kw)


@pytest.mark.parametrize("fields", [
    {"current_run_id": 987}, {"claim_lock": "stale", "claim_expires": 1},
    {"claim_lock": "live", "claim_expires": 9999999999},
    {"worker_pid": 123}, {"worker_started_at": "retained"},
    {"status": "running"}, {"status": "ready"}, {"block_kind": "capability"},
])
def test_stale_live_or_changed_state_refused_without_writes(board, fields):
    conn, tid = board
    # current_run_id is not FK constrained; synthetic stale pointer must fail closed.
    conn.execute("UPDATE tasks SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?", (*fields.values(), tid))
    before = conn.total_changes
    assert correct(conn, tid) is False
    assert conn.total_changes == before


@pytest.mark.parametrize("own", [True, False])
def test_dispatcher_worker_cannot_correct_even_own_idle_card(board, monkeypatch, own):
    conn, tid = board
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid if own else "t_other")
    with pytest.raises(PermissionError, match="board-manager"):
        correct(conn, tid)


@pytest.mark.parametrize("done,resume,expected", [(False, "ready", "todo"), (True, "ready", "ready"), (True, "review", "review")])
def test_dependency_normalizes_from_current_parents(board, done, resume, expected):
    from hermes_cli.kanban_idle_hold import reclassify_idle_hold
    conn, tid = board
    parent = kb.create_task(conn, title="parent")
    kb.link_tasks(conn, parent, tid)
    if done:
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    kb._append_event(conn, tid, "blocked", {"source_status": resume})
    assert reclassify_idle_hold(conn, tid, expected_status="blocked", expected_kind="dependency", kind="dependency", reason="current upstream state")
    task = kb.get_task(conn, tid)
    assert task.status == expected
    assert task.block_recurrences == 2


def test_owner_hold_is_never_dispatchable_and_preserves_evidence(board):
    conn, tid = board
    conn.execute("UPDATE tasks SET consecutive_failures=0 WHERE id=?", (tid,))
    kb.add_comment(conn, tid, author="author", body="retained history")
    before = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    history = list(conn.execute("SELECT * FROM task_events WHERE task_id=?", (tid,)))
    statements = []
    conn.set_trace_callback(statements.append)
    assert correct(conn, tid)
    after = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    assert after == {**before, "block_kind": "needs_input"}
    events = list(conn.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY id", (tid,)))
    assert events[:-1] == history
    payload = json.loads(events[-1]["payload"])
    assert payload["previous_kind"] == "dependency"
    assert payload["kind"] == "needs_input"
    assert payload["reason"].startswith("FOR COREY:")
    assert events[-1]["kind"] == "blocked"
    assert not any("SET status = 'ready'" in s for s in statements)
    kb.recompute_ready(conn)
    assert kb.get_task(conn, tid).status == "blocked"


def test_supported_manager_action_returns_exact_readback(board, monkeypatch, capsys):
    from hermes_cli.kanban_idle_hold import main
    conn, tid = board
    assert main(["--task-id", tid, "--expected-status", "blocked", "--expected-kind", "dependency", "--kind", "needs_input", "--reason", "FOR COREY: existing decision inbox"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"ok": True, "task_id": tid, "status": "blocked", "block_kind": "needs_input"}
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    assert main(["--task-id", tid, "--expected-status", "blocked", "--expected-kind", "needs_input", "--kind", "dependency", "--reason", "denied"]) == 2
    assert kb.get_task(conn, tid).status == "blocked"


def test_delegated_child_refused(board):
    from agent.delegation_context import delegated_child_context
    conn, tid = board
    with delegated_child_context(), pytest.raises(PermissionError):
        correct(conn, tid)


def test_attachment_history_runs_and_siblings_untouched(board, tmp_path):
    conn, tid = board
    attachment = tmp_path / "cut.txt"
    attachment.write_bytes(b"original episode bytes")
    kb.add_attachment(conn, tid, filename="cut.txt", stored_path=str(attachment), size=attachment.stat().st_size)
    other = kb.create_task(conn, title="untouched inbox", initial_status="blocked")
    conn.execute("INSERT INTO task_runs(task_id,status,started_at,ended_at,worker_pid,summary) VALUES (?,'blocked',1,2,123,'original terminal evidence')", (tid,))
    tables = ["task_attachments", "task_comments", "task_runs", "task_links"]
    before = {t: list(conn.execute(f"SELECT * FROM {t}")) for t in tables}
    sibling = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (other,)).fetchone())
    assert correct(conn, tid)
    assert before == {t: list(conn.execute(f"SELECT * FROM {t}")) for t in tables}
    assert dict(conn.execute("SELECT * FROM tasks WHERE id=?", (other,)).fetchone()) == sibling
    assert attachment.read_bytes() == b"original episode bytes"


def test_racing_claim_committed_before_cas_is_refused(board):
    import concurrent.futures
    import threading
    conn, tid = board
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    waiting = threading.Event()
    def correction():
        import sqlite3
        other = sqlite3.connect(path, isolation_level=None, timeout=5)
        other.row_factory = sqlite3.Row
        waiting.set()
        try:
            return correct(other, tid)
        finally:
            other.close()
    conn.execute("BEGIN IMMEDIATE")
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(correction)
        assert waiting.wait(3)
        conn.execute("UPDATE tasks SET claim_lock='racing', claim_expires=9999999999 WHERE id=?", (tid,))
        conn.commit()
        assert future.result(timeout=5) is False
    assert kb.get_task(conn, tid).block_kind == "dependency"


def test_audit_failure_rolls_back_correction(board, monkeypatch):
    conn, tid = board
    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr(kb, "_append_event", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        correct(conn, tid)
    assert kb.get_task(conn, tid).block_kind == "dependency"


@pytest.mark.parametrize("status,ended", [("running", None), ("running", 1), ("blocked", None)])
def test_orphan_nonterminal_run_is_not_idle(board, status, ended):
    conn, tid = board
    conn.execute("INSERT INTO task_runs(task_id,status,started_at,ended_at) VALUES (?,?,1,?)", (tid, status, ended))
    before = conn.total_changes
    assert correct(conn, tid) is False
    assert conn.total_changes == before


def test_nested_transaction_is_not_committed(board):
    conn, tid = board
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(RuntimeError, match="already inside"):
        correct(conn, tid)
    assert conn.in_transaction
    conn.rollback()


def test_owner_hold_refuses_native_claim_and_stale_retry(board):
    conn, tid = board
    assert correct(conn, tid)
    changes = conn.total_changes
    assert correct(conn, tid) is False
    assert conn.total_changes == changes
    assert kb.claim_task(conn, tid, claimer="competing dispatcher") is None


@pytest.mark.parametrize("args", [
    {"expected_status": "ready"}, {"expected_kind": "wrong"},
    {"kind": "wrong"}, {"reason": "  "},
])
def test_invalid_contract_rejected(board, args):
    from hermes_cli.kanban_idle_hold import reclassify_idle_hold
    conn, tid = board
    params = dict(expected_status="blocked", expected_kind="dependency", kind="needs_input", reason="FOR COREY: review")
    with pytest.raises(ValueError):
        reclassify_idle_hold(conn, tid, **{**params, **args})
