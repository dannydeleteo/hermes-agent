"""Structured request-cycle provenance for Hermes tool loops.

The model gateway is a validator, never a tool executor.  This module stamps
the structured evidence that lets it distinguish a current Hermes tool result
from reordered historical context without relying on message position.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import os
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


REQUEST_CYCLE_SCHEMA = "hermes.request-cycle.v1"
TOOL_CALL_SCHEMA = "hermes.tool-call.v1"
TOOL_RECEIPT_SCHEMA = "hermes.tool-receipt.v1"
SIGNATURE_FIELD = "signature"
SIGNATURE_PREFIX = "hmac-sha256:"
HMAC_KEY_ENV = "HERMES_REQUEST_CYCLE_HMAC_KEY"
HMAC_KEY_FILE_ENV = "HERMES_REQUEST_CYCLE_HMAC_KEY_FILE"
DEFAULT_HMAC_KEY_FILE = Path.home() / ".hermes" / "request-cycle-hmac.key"


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any, *, parse_json_string: bool) -> str:
    if parse_json_string and isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            pass
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def checksum(value: Any, *, parse_json_string: bool = False) -> str:
    """Return a stable, typed SHA-256 checksum for arguments or a tool result."""
    try:
        blob = _canonical_json(value, parse_json_string=parse_json_string)
    except (TypeError, ValueError):
        blob = json.dumps(str(value), ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()


def _hmac_key() -> bytes | None:
    """Load the local shared key without ever logging or serializing it.

    The key is provisioned only as part of the explicit two-service activation
    procedure.  If it is absent, Hermes still carries structure but cannot
    produce execution-authorizing evidence; the gateway fails closed.
    """
    inline = os.environ.get(HMAC_KEY_ENV)
    if inline:
        return inline.encode("utf-8")
    key_file = Path(os.environ.get(HMAC_KEY_FILE_ENV, str(DEFAULT_HMAC_KEY_FILE)))
    try:
        key = key_file.read_bytes().strip()
    except OSError:
        return None
    return key or None


def _signature_payload(envelope: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in envelope.items() if key != SIGNATURE_FIELD}
    return _canonical_json(unsigned, parse_json_string=False).encode("utf-8")


def sign_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Add or refresh the local HMAC binding for an envelope in-place."""
    key = _hmac_key()
    if key is None:
        envelope.pop(SIGNATURE_FIELD, None)
        return envelope
    digest = hmac.new(key, _signature_payload(envelope), hashlib.sha256).hexdigest()
    envelope[SIGNATURE_FIELD] = SIGNATURE_PREFIX + digest
    return envelope


def new_request_cycle() -> dict[str, str]:
    """Create one opaque cycle identifier at the start of a Hermes user request."""
    cycle: dict[str, Any] = {
        "schema": REQUEST_CYCLE_SCHEMA,
        "request_cycle_id": "rc_" + uuid.uuid4().hex,
        "created_at": utc_now(),
    }
    sign_envelope(cycle)
    return cycle


def active_cycle(agent: Any) -> dict[str, str] | None:
    cycle = getattr(agent, "_request_cycle", None)
    if not isinstance(cycle, dict):
        return None
    if cycle.get("schema") != REQUEST_CYCLE_SCHEMA:
        return None
    if not isinstance(cycle.get("request_cycle_id"), str) or not cycle["request_cycle_id"]:
        return None
    return cycle


def stamp_assistant_tool_calls(message: dict[str, Any], cycle: dict[str, str] | None) -> None:
    """Attach request-cycle evidence to every assistant tool call in-place."""
    if not cycle:
        return
    for tool_call in message.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        call_id = tool_call.get("id") or tool_call.get("call_id")
        function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
        name = function.get("name")
        if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
            continue
        provenance = {
            "schema": TOOL_CALL_SCHEMA,
            "request_cycle_id": cycle["request_cycle_id"],
            "tool_call_id": call_id,
            "tool_name": name,
            "arguments_checksum": checksum(function.get("arguments"), parse_json_string=True),
            "created_at": utc_now(),
        }
        tool_call["hermes_tool_provenance"] = sign_envelope(provenance)


def tool_result_message(
    agent: Any,
    *,
    tool_call_id: str,
    tool_name: str,
    arguments: Any,
    content: Any,
    execution_status: str,
) -> dict[str, Any]:
    """Build the sole structured receipt carried with a Hermes tool result."""
    message: dict[str, Any] = {
        "role": "tool",
        "name": tool_name,
        # ``tool_name`` is the persisted SessionDB field; ``name`` is the
        # OpenAI-compatible message field. Store both so replay is lossless.
        "tool_name": tool_name,
        "content": content,
        "tool_call_id": tool_call_id,
    }
    cycle = active_cycle(agent)
    if cycle:
        provenance = {
            "schema": TOOL_RECEIPT_SCHEMA,
            "request_cycle_id": cycle["request_cycle_id"],
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "arguments_checksum": checksum(arguments, parse_json_string=True),
            "execution_status": execution_status,
            "result_checksum": checksum(content),
            "created_at": utc_now(),
        }
        message["hermes_tool_provenance"] = sign_envelope(provenance)
    return message


def refresh_tool_result_checksum(message: dict[str, Any]) -> None:
    """Rebind a receipt after Hermes adds bounded internal guidance/truncation.

    The receipt is built after execution, but Hermes may append a steer marker
    or enforce a result-size budget before the next model request.  Recompute
    only the result checksum so the receipt always covers exactly the content
    that reaches the gateway; all identity fields remain immutable.
    """
    receipt = message.get("hermes_tool_provenance")
    if isinstance(receipt, dict) and receipt.get("schema") == TOOL_RECEIPT_SCHEMA:
        receipt["result_checksum"] = checksum(message.get("content"))
        sign_envelope(receipt)


def attach_gateway_cycle_envelope(agent: Any, api_kwargs: dict[str, Any]) -> None:
    """Carry the active cycle outside prompt text to the local model gateway only.

    OpenAI's Python SDK merges ``extra_body`` into the JSON request body.  The
    local model-gateway is the validator that consumes this envelope; remote
    providers never receive the private Hermes metadata.
    """
    cycle = active_cycle(agent)
    if not cycle:
        return
    parsed = urlparse(str(getattr(agent, "base_url", "") or ""))
    if parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port != 11515:
        return
    extra_body = api_kwargs.get("extra_body")
    if not isinstance(extra_body, dict):
        extra_body = {}
    extra_body = dict(extra_body)
    extra_body["hermes_request_cycle"] = dict(cycle)
    api_kwargs["extra_body"] = extra_body


def metadata_from_message(message: dict[str, Any]) -> dict[str, Any]:
    """Return only the durable provenance metadata intended for SessionDB."""
    out: dict[str, Any] = {}
    for key in ("hermes_request_cycle", "hermes_tool_provenance"):
        value = message.get(key)
        if isinstance(value, dict):
            out[key] = value
    return out


__all__ = [
    "REQUEST_CYCLE_SCHEMA", "TOOL_CALL_SCHEMA", "TOOL_RECEIPT_SCHEMA",
    "active_cycle", "attach_gateway_cycle_envelope", "checksum",
    "metadata_from_message", "new_request_cycle", "stamp_assistant_tool_calls",
    "tool_result_message", "refresh_tool_result_checksum", "sign_envelope", "utc_now",
]
