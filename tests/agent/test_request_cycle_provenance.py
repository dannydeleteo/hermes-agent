"""Unit and restart/replay tests for Hermes structured request-cycle receipts."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.request_cycle_provenance import (
    REQUEST_CYCLE_SCHEMA,
    attach_gateway_cycle_envelope,
    metadata_from_message,
    new_request_cycle,
    refresh_tool_result_checksum,
    SIGNATURE_PREFIX,
    stamp_assistant_tool_calls,
    tool_result_message,
)
from hermes_state import SCHEMA_SQL, SessionDB


@pytest.fixture(autouse=True)
def cycle_hmac_key(monkeypatch):
    monkeypatch.setenv("HERMES_REQUEST_CYCLE_HMAC_KEY", "unit-test-request-cycle-key")


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
