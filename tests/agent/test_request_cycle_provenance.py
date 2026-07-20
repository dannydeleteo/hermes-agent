"""Unit and restart/replay tests for Hermes structured request-cycle receipts."""
from __future__ import annotations

import sqlite3
import hashlib
import hmac
import os
import secrets
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.request_cycle_provenance import (
    REQUEST_CYCLE_SCHEMA,
    attach_gateway_cycle_envelope,
    canonical_envelope_bytes,
    checksum,
    envelope_issues,
    generate_key_file,
    load_hmac_key,
    metadata_from_message,
    new_request_cycle,
    refresh_tool_result_checksum,
    SIGNATURE_PREFIX,
    sign_envelope,
    stamp_assistant_tool_calls,
    tool_result_message,
    verify_envelope_signature,
)
from hermes_state import SCHEMA_SQL, SessionDB


@pytest.fixture(autouse=True)
def cycle_hmac_key(monkeypatch, tmp_path):
    path = tmp_path / "request-cycle.key"
    path.write_text(secrets.token_hex(32))
    path.chmod(0o600)
    monkeypatch.setenv("HERMES_REQUEST_CYCLE_HMAC_KEY_FILE", str(path))


def test_cycle_stamps_user_call_and_receipt_without_prompt_text():
    cycle = new_request_cycle()
    assert cycle["schema"] == REQUEST_CYCLE_SCHEMA
    assert cycle["signature"].startswith(SIGNATURE_PREFIX)
    agent = SimpleNamespace(_request_cycle=cycle)
    assistant = {"role": "assistant", "tool_calls": [{
        "id": "call-1", "function": {"name": "read_file", "arguments": '{"path":"/approved/x.txt"}'},
    }]}
    stamp_assistant_tool_calls(assistant, cycle)
    call_meta = assistant["tool_calls"][0]["hermes_tool_provenance"]
    assert call_meta["request_cycle_id"] == cycle["request_cycle_id"]
    assert call_meta["tool_call_id"] == "call-1"
    assert call_meta["tool_name"] == "read_file"
    assert call_meta["arguments_checksum"].startswith("sha256:")
    assert call_meta["signature"].startswith(SIGNATURE_PREFIX)

    receipt = tool_result_message(
        agent, tool_call_id="call-1", tool_name="read_file",
        arguments={"path": "/approved/x.txt"}, content="unique", execution_status="executed",
    )
    assert receipt["name"] == receipt["tool_name"] == "read_file"
    assert receipt["hermes_tool_provenance"]["request_cycle_id"] == cycle["request_cycle_id"]
    assert receipt["hermes_tool_provenance"]["result_checksum"]
    assert receipt["hermes_tool_provenance"]["execution_status"] == "executed"
    receipt["content"] += "\n\nUser guidance: stay concise"
    refresh_tool_result_checksum(receipt)
    assert receipt["hermes_tool_provenance"]["result_checksum"]
    assert receipt["hermes_tool_provenance"]["signature"].startswith(SIGNATURE_PREFIX)


def test_cycle_envelope_is_structured_and_local_gateway_only():
    cycle = new_request_cycle()
    local = SimpleNamespace(_request_cycle=cycle, base_url="http://127.0.0.1:11515/v1")
    kwargs = {"messages": [{"role": "user", "content": "hello"}]}
    attach_gateway_cycle_envelope(local, kwargs)
    assert kwargs["extra_body"]["hermes_request_cycle"] == cycle
    assert cycle["request_cycle_id"] not in kwargs["messages"][0]["content"]

    remote = SimpleNamespace(_request_cycle=cycle, base_url="https://example.invalid/v1")
    remote_kwargs = {"messages": []}
    attach_gateway_cycle_envelope(remote, remote_kwargs)
    assert "extra_body" not in remote_kwargs


def test_process_restart_replay_preserves_metadata_in_sessiondb(tmp_path: Path):
    path = tmp_path / "state.db"
    cycle = new_request_cycle()
    agent = SimpleNamespace(_request_cycle=cycle)
    user = {"role": "user", "content": "read it", "hermes_request_cycle": cycle}
    assistant = {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call-1", "function": {"name": "read_file", "arguments": '{"path":"/approved/x.txt"}'},
    }]}
    stamp_assistant_tool_calls(assistant, cycle)
    receipt = tool_result_message(
        agent, tool_call_id="call-1", tool_name="read_file",
        arguments={"path": "/approved/x.txt"}, content="unique", execution_status="executed",
    )

    db = SessionDB(path)
    db.create_session("s1", "test")
    for message in (user, assistant, receipt):
        db.append_message(
            "s1", message["role"], message.get("content"), tool_name=message.get("tool_name"),
            tool_calls=message.get("tool_calls"), tool_call_id=message.get("tool_call_id"),
            metadata=metadata_from_message(message),
        )
    db.close()

    restarted = SessionDB(path)
    replayed = restarted.get_messages_as_conversation("s1")
    restarted.close()
    assert replayed[0]["hermes_request_cycle"] == cycle
    assert replayed[1]["tool_calls"][0]["hermes_tool_provenance"]["request_cycle_id"] == cycle["request_cycle_id"]
    assert replayed[2]["name"] == "read_file"
    assert replayed[2]["hermes_tool_provenance"]["result_checksum"] == receipt["hermes_tool_provenance"]["result_checksum"]


def test_existing_sessiondb_is_reconciled_with_provenance_metadata_column(tmp_path: Path):
    """A restart upgrades an existing state DB without a destructive migration."""
    path = tmp_path / "legacy-state.db"
    legacy_schema = SCHEMA_SQL.replace(",\n    metadata TEXT\n);", "\n);")
    assert legacy_schema != SCHEMA_SQL
    conn = sqlite3.connect(path)
    conn.executescript(legacy_schema)
    conn.close()

    db = SessionDB(path)
    try:
        columns = {row[1] for row in db._conn.execute("PRAGMA table_info(messages)").fetchall()}
        assert "metadata" in columns
    finally:
        db.close()


def _fixed_cycle():
    return {
        "schema": REQUEST_CYCLE_SCHEMA,
        "request_cycle_id": "rc_0123456789abcdef0123456789abcdef",
        "created_at": "2026-07-20T01:02:03Z",
    }


def test_canonical_contract_is_exact_unambiguous_and_rejects_unknown_fields():
    cycle = _fixed_cycle()
    expected = (
        b'{"created_at":"2026-07-20T01:02:03Z",'
        b'"request_cycle_id":"rc_0123456789abcdef0123456789abcdef",'
        b'"schema":"hermes.request-cycle.v1"}'
    )
    assert canonical_envelope_bytes(cycle) == expected
    sign_envelope(cycle)
    assert cycle["signature"].startswith(SIGNATURE_PREFIX) and len(cycle["signature"]) == len(SIGNATURE_PREFIX) + 64
    assert checksum('{"b":2,"a":1}', parse_json_string=True) == checksum({"a": 1, "b": 2})
    assert checksum('{"a":1,"a":2}', parse_json_string=True) != checksum({"a": 2})
    assert envelope_issues({**cycle, "unexpected": True}, allow_missing_signature=True) == ("UNKNOWN_FIELD",)
    assert envelope_issues({**cycle, "created_at": "2026-07-20T01:02:03+00:00"}, allow_missing_signature=True) == ("INVALID_TIMESTAMP",)


def test_key_lifecycle_is_strict_and_never_prints_key_material(monkeypatch, tmp_path: Path, capsys):
    parent = tmp_path / "key-parent"
    parent.mkdir(mode=0o700)
    key_path = parent / "request-cycle.key"
    monkeypatch.setenv("HERMES_REQUEST_CYCLE_HMAC_KEY_FILE", str(key_path))
    generate_key_file(key_path)
    mode = stat.S_IMODE(key_path.stat().st_mode)
    key, issue = load_hmac_key()
    assert mode == 0o600 and issue is None and key is not None and len(key) == 32
    fingerprint = hashlib.sha256(key).hexdigest()[:16]
    assert len(fingerprint) == 16
    assert capsys.readouterr().out == capsys.readouterr().err == ""

    key_path.chmod(0o644)
    assert load_hmac_key()[1] == "KEY_MODE_MISMATCH"
    key_path.chmod(0o600)
    key_path.write_text("not-a-key")
    assert load_hmac_key()[1] == "KEY_MALFORMED"
    key_path.unlink()
    os.symlink(parent / "elsewhere", key_path)
    assert load_hmac_key()[1] == "KEY_SYMLINK"
    key_path.unlink()
    linked_parent = tmp_path / "linked-parent"
    os.symlink(parent, linked_parent)
    monkeypatch.setenv("HERMES_REQUEST_CYCLE_HMAC_KEY_FILE", str(linked_parent / "request-cycle.key"))
    assert load_hmac_key()[1] == "KEY_PARENT_SYMLINK"


def test_rotation_invalidates_old_receipts_fail_closed(monkeypatch, tmp_path: Path):
    key_path = tmp_path / "request-cycle.key"
    key_path.write_text(secrets.token_hex(32))
    key_path.chmod(0o600)
    monkeypatch.setenv("HERMES_REQUEST_CYCLE_HMAC_KEY_FILE", str(key_path))
    cycle = new_request_cycle()
    assert verify_envelope_signature(cycle)[0]
    old_signature = cycle["signature"]
    key_path.write_text(secrets.token_hex(32))
    key_path.chmod(0o600)
    ok, issue = verify_envelope_signature(cycle)
    assert not ok and issue == "SIGNATURE_MISMATCH" and cycle["signature"] == old_signature


def test_secure_generation_uses_a_nonrepeating_256_bit_key(tmp_path: Path):
    parent = tmp_path / "keys"
    parent.mkdir(mode=0o700)
    first, second = parent / "first.key", parent / "second.key"
    generate_key_file(first)
    generate_key_file(second)
    first_key, first_issue = _load_key_at(first)
    second_key, second_issue = _load_key_at(second)
    assert first_issue is second_issue is None
    assert first_key != second_key and len(first_key) == len(second_key) == 32


def _load_key_at(path: Path):
    old = os.environ.get("HERMES_REQUEST_CYCLE_HMAC_KEY_FILE")
    os.environ["HERMES_REQUEST_CYCLE_HMAC_KEY_FILE"] = str(path)
    try:
        return load_hmac_key()
    finally:
        if old is None:
            os.environ.pop("HERMES_REQUEST_CYCLE_HMAC_KEY_FILE", None)
        else:
            os.environ["HERMES_REQUEST_CYCLE_HMAC_KEY_FILE"] = old
