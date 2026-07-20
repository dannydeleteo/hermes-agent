"""Signed structured request-cycle provenance for Hermes tool loops.

This module is deliberately byte-compatible with the model gateway's
``agents.core.tool_provenance`` contract.  It carries execution evidence as
structured data, never as prompt text, so transcript reordering cannot grant
or remove execution authority.

The HMAC key is a 32-byte value encoded as exactly 64 lowercase hexadecimal
characters in a regular, owner-owned ``0600`` file.  The file path may be
overridden *only* by ``HERMES_REQUEST_CYCLE_HMAC_KEY_FILE`` for isolated test
or deployment staging; the key material is never accepted through an
environment variable, printed, logged, or serialized.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


REQUEST_CYCLE_SCHEMA = "hermes.request-cycle.v1"
TOOL_CALL_SCHEMA = "hermes.tool-call.v1"
TOOL_RECEIPT_SCHEMA = "hermes.tool-receipt.v1"

SIGNATURE_FIELD = "signature"
SIGNATURE_PREFIX = "hmac-sha256:"
HMAC_KEY_FILE_ENV = "HERMES_REQUEST_CYCLE_HMAC_KEY_FILE"
DEFAULT_HMAC_KEY_FILE = Path.home() / ".hermes" / "request-cycle-hmac.key"
KEY_FILE_MODE = 0o600
KEY_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SIGNATURE_RE = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
CYCLE_ID_RE = re.compile(r"^rc_[0-9a-f]{32}$")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_EXECUTION_STATUSES = frozenset({"executed", "blocked", "cancelled", "refused", "error"})

_UNSIGNED_FIELDS: dict[str, tuple[str, ...]] = {
    REQUEST_CYCLE_SCHEMA: ("schema", "request_cycle_id", "created_at"),
    TOOL_CALL_SCHEMA: (
        "schema", "request_cycle_id", "tool_call_id", "tool_name",
        "arguments_checksum", "created_at",
    ),
    TOOL_RECEIPT_SCHEMA: (
        "schema", "request_cycle_id", "tool_call_id", "tool_name",
        "arguments_checksum", "execution_status", "result_checksum", "created_at",
    ),
}


class DuplicateJSONKey(ValueError):
    """Raised when a JSON object contains duplicate member names."""


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJSONKey("duplicate JSON object key")
        result[key] = value
    return result


def _strict_json_loads(value: str) -> Any:
    return json.loads(value, object_pairs_hook=_reject_duplicate_pairs)


def utc_now() -> str:
    """Return the protocol's fixed UTC/RFC3339 seconds-precision timestamp."""
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not TIMESTAMP_RE.fullmatch(value):
        return False
    try:
        _dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _canonical_json(value: Any, *, parse_json_string: bool) -> str:
    """Canonical JSON: UTF-8, no whitespace, sorted keys, no NaN/Infinity.

    For tool arguments only, a valid JSON string is canonicalized as its JSON
    value so Hermes's parsed execution arguments match the assistant's raw
    OpenAI arguments.  Invalid or duplicate-key JSON stays a *string*, which
    is unambiguous and cannot collide with an object value.
    """
    if parse_json_string and isinstance(value, str):
        try:
            value = _strict_json_loads(value)
        except (TypeError, ValueError):
            pass
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def checksum(value: Any, *, parse_json_string: bool = False) -> str:
    """Return ``sha256:<lowercase hex>`` over canonical UTF-8 JSON."""
    try:
        blob = _canonical_json(value, parse_json_string=parse_json_string)
    except (TypeError, ValueError):
        blob = json.dumps(str(value), ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8", "strict")).hexdigest()


def _unsigned_fields(schema: Any) -> tuple[str, ...] | None:
    return _UNSIGNED_FIELDS.get(schema) if isinstance(schema, str) else None


def envelope_issues(envelope: Any, *, allow_missing_signature: bool = False) -> tuple[str, ...]:
    """Return deterministic, secret-free contract issue codes for an envelope.

    Unknown fields are rejected, schema versions are exact, all required
    values are non-null strings, and a supplied signature has fixed syntax.
    This is deliberately stricter than merely signing arbitrary dictionaries.
    """
    if not isinstance(envelope, dict):
        return ("ENVELOPE_NOT_OBJECT",)
    fields = _unsigned_fields(envelope.get("schema"))
    if fields is None:
        return ("UNSUPPORTED_SCHEMA",)
    expected = set(fields)
    allowed = set(fields) | {SIGNATURE_FIELD}
    if not allow_missing_signature:
        expected.add(SIGNATURE_FIELD)
    actual = set(envelope)
    issues: list[str] = []
    if actual - allowed:
        issues.append("UNKNOWN_FIELD")
    if expected - actual:
        issues.append("MISSING_FIELD")
    if issues:
        return tuple(issues)

    if not isinstance(envelope.get("request_cycle_id"), str) or not CYCLE_ID_RE.fullmatch(envelope["request_cycle_id"]):
        issues.append("INVALID_REQUEST_CYCLE_ID")
    if not _valid_timestamp(envelope.get("created_at")):
        issues.append("INVALID_TIMESTAMP")
    if envelope.get("schema") != REQUEST_CYCLE_SCHEMA:
        call_id = envelope.get("tool_call_id")
        tool_name = envelope.get("tool_name")
        if not isinstance(call_id, str) or not call_id or len(call_id) > 512:
            issues.append("INVALID_TOOL_CALL_ID")
        if not isinstance(tool_name, str) or not tool_name or len(tool_name) > 256:
            issues.append("INVALID_TOOL_NAME")
        if not isinstance(envelope.get("arguments_checksum"), str) or not SHA256_RE.fullmatch(envelope["arguments_checksum"]):
            issues.append("INVALID_ARGUMENTS_CHECKSUM")
    if envelope.get("schema") == TOOL_RECEIPT_SCHEMA:
        if envelope.get("execution_status") not in _EXECUTION_STATUSES:
            issues.append("INVALID_EXECUTION_STATUS")
        if not isinstance(envelope.get("result_checksum"), str) or not SHA256_RE.fullmatch(envelope["result_checksum"]):
            issues.append("INVALID_RESULT_CHECKSUM")
    if SIGNATURE_FIELD in envelope and (not isinstance(envelope.get(SIGNATURE_FIELD), str)
                                        or not SIGNATURE_RE.fullmatch(envelope[SIGNATURE_FIELD])):
        issues.append("INVALID_SIGNATURE_FORMAT")
    elif not allow_missing_signature:
        signature = envelope.get(SIGNATURE_FIELD)
        if not isinstance(signature, str) or not SIGNATURE_RE.fullmatch(signature):
            issues.append("INVALID_SIGNATURE_FORMAT")
    return tuple(issues)


def canonical_envelope_bytes(envelope: dict[str, Any]) -> bytes:
    """Exact HMAC input: unsigned known fields, sorted compact UTF-8 JSON.

    ``signature`` is excluded.  No Unicode normalization is performed.  The
    logical envelope must pass strict v1 field validation before it can be
    signed, so unknown/missing fields cannot create cross-runtime ambiguity.
    """
    unsigned = {key: value for key, value in envelope.items() if key != SIGNATURE_FIELD}
    issues = envelope_issues(unsigned, allow_missing_signature=True)
    if issues:
        raise ValueError("invalid request-cycle envelope")
    fields = _unsigned_fields(unsigned["schema"])
    assert fields is not None
    canonical = {field: unsigned[field] for field in fields}
    return _canonical_json(canonical, parse_json_string=False).encode("utf-8", "strict")


def _key_path() -> Path:
    return Path(os.environ.get(HMAC_KEY_FILE_ENV, str(DEFAULT_HMAC_KEY_FILE))).expanduser()


def load_hmac_key() -> tuple[bytes | None, str | None]:
    """Load an approved local key, returning only secret-free failure codes.

    The path is protected against symlinks and checked both before and after
    opening.  The parent need not be unreadable (``~/.hermes`` may be 0755),
    but it must be owned by this user and not group/other writable; secrecy
    and integrity come from the regular owner-only 0600 key file.
    """
    path = _key_path()
    try:
        parent = path.parent.lstat()
    except OSError:
        return None, "KEY_PARENT_UNAVAILABLE"
    if stat.S_ISLNK(parent.st_mode):
        return None, "KEY_PARENT_SYMLINK"
    if not stat.S_ISDIR(parent.st_mode):
        return None, "KEY_PARENT_NOT_DIRECTORY"
    if parent.st_uid != os.geteuid():
        return None, "KEY_PARENT_OWNER_MISMATCH"
    if stat.S_IMODE(parent.st_mode) & 0o022:
        return None, "KEY_PARENT_WRITABLE_BY_OTHERS"
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None, "KEY_MISSING"
    except OSError:
        return None, "KEY_UNREADABLE"
    if stat.S_ISLNK(before.st_mode):
        return None, "KEY_SYMLINK"
    if not stat.S_ISREG(before.st_mode):
        return None, "KEY_NOT_REGULAR"
    if before.st_uid != os.geteuid():
        return None, "KEY_OWNER_MISMATCH"
    if stat.S_IMODE(before.st_mode) != KEY_FILE_MODE:
        return None, "KEY_MODE_MISMATCH"

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None, "KEY_UNREADABLE"
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid()
                or stat.S_IMODE(opened.st_mode) != KEY_FILE_MODE
                or opened.st_dev != before.st_dev or opened.st_ino != before.st_ino):
            return None, "KEY_CHANGED_DURING_OPEN"
        chunks: list[bytes] = []
        while sum(len(chunk) for chunk in chunks) <= 64:
            part = os.read(fd, 65 - sum(len(chunk) for chunk in chunks))
            if not part:
                break
            chunks.append(part)
        raw = b"".join(chunks)
    except OSError:
        return None, "KEY_UNREADABLE"
    finally:
        os.close(fd)
    try:
        encoded = raw.decode("ascii")
    except UnicodeDecodeError:
        return None, "KEY_MALFORMED"
    if not KEY_HEX_RE.fullmatch(encoded):
        return None, "KEY_MALFORMED"
    return bytes.fromhex(encoded), None


def generate_key_file(path: str | Path) -> None:
    """Create a new 256-bit key atomically without returning or printing it.

    Existing paths are never overwritten; rotation is an explicit operational
    action rather than an implicit side effect of service startup.
    """
    target = Path(path).expanduser()
    parent = target.parent.lstat()
    if (stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o022):
        raise OSError("key parent does not meet ownership requirements")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(target, flags, KEY_FILE_MODE)
    try:
        os.fchmod(fd, KEY_FILE_MODE)
        raw = secrets.token_hex(32).encode("ascii")
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)


def sign_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Seal a valid envelope in-place, or leave it unsigned on any key failure."""
    envelope.pop(SIGNATURE_FIELD, None)
    key, _status = load_hmac_key()
    if key is None:
        return envelope
    try:
        payload = canonical_envelope_bytes(envelope)
    except (TypeError, ValueError):
        return envelope
    envelope[SIGNATURE_FIELD] = SIGNATURE_PREFIX + hmac.new(key, payload, hashlib.sha256).hexdigest()
    return envelope


def verify_envelope_signature(envelope: Any) -> tuple[bool, str | None]:
    """Verify a structured envelope with no secret-bearing error information."""
    issues = envelope_issues(envelope)
    if issues:
        return False, issues[0]
    assert isinstance(envelope, dict)
    key, status = load_hmac_key()
    if key is None:
        return False, status
    try:
        expected = SIGNATURE_PREFIX + hmac.new(key, canonical_envelope_bytes(envelope), hashlib.sha256).hexdigest()
    except (TypeError, ValueError):
        return False, "INVALID_ENVELOPE"
    return hmac.compare_digest(envelope[SIGNATURE_FIELD], expected), "SIGNATURE_MISMATCH"


def new_request_cycle() -> dict[str, Any]:
    """Create one opaque cycle identifier at the start of a Hermes user request."""
    cycle: dict[str, Any] = {
        "schema": REQUEST_CYCLE_SCHEMA,
        "request_cycle_id": "rc_" + uuid.uuid4().hex,
        "created_at": utc_now(),
    }
    return sign_envelope(cycle)


def active_cycle(agent: Any) -> dict[str, Any] | None:
    cycle = getattr(agent, "_request_cycle", None)
    if not isinstance(cycle, dict) or cycle.get("schema") != REQUEST_CYCLE_SCHEMA:
        return None
    if not isinstance(cycle.get("request_cycle_id"), str) or not cycle["request_cycle_id"]:
        return None
    return cycle


def stamp_assistant_tool_calls(message: dict[str, Any], cycle: dict[str, Any] | None) -> None:
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
    """Build the structured receipt carried with a Hermes tool result."""
    message: dict[str, Any] = {
        "role": "tool",
        "name": tool_name,
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
    """Rebind a receipt after bounded internal guidance/truncation is added."""
    receipt = message.get("hermes_tool_provenance")
    if isinstance(receipt, dict) and receipt.get("schema") == TOOL_RECEIPT_SCHEMA:
        receipt["result_checksum"] = checksum(message.get("content"))
        sign_envelope(receipt)


def attach_gateway_cycle_envelope(agent: Any, api_kwargs: dict[str, Any]) -> None:
    """Carry the active cycle outside prompt text to the local gateway only."""
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
    """Return only durable provenance metadata intended for SessionDB."""
    out: dict[str, Any] = {}
    for key in ("hermes_request_cycle", "hermes_tool_provenance"):
        value = message.get(key)
        if isinstance(value, dict):
            out[key] = value
    return out


__all__ = [
    "DEFAULT_HMAC_KEY_FILE", "HMAC_KEY_FILE_ENV", "KEY_FILE_MODE", "REQUEST_CYCLE_SCHEMA",
    "SIGNATURE_FIELD", "SIGNATURE_PREFIX", "TOOL_CALL_SCHEMA", "TOOL_RECEIPT_SCHEMA",
    "active_cycle", "attach_gateway_cycle_envelope", "canonical_envelope_bytes", "checksum",
    "envelope_issues", "generate_key_file", "load_hmac_key", "metadata_from_message",
    "new_request_cycle", "refresh_tool_result_checksum", "sign_envelope", "stamp_assistant_tool_calls",
    "tool_result_message", "utc_now", "verify_envelope_signature",
]
