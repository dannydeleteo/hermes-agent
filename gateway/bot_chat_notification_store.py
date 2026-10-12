"""Private SQLite ReceiptStore; deliberately disconnected from any live owner.

The trusted host supplies an existing, exclusively owned 0700 directory. Explicit
creation never adopts, repairs or replaces an existing database. Opening an
existing store never initializes missing state. No profile discovery, migration,
expiry eviction, retry, mailbox, provider, or background worker lives here.

This is a same-user trusted filesystem boundary, not a sandbox against a hostile
process running as that user/root. Such a process can also modify Hermes itself.
Paths/inodes/permissions are checked before and after operations; callers must
not rename or replace this dedicated directory while it is in use.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

from gateway.bot_chat_notifications import (
    Caller, EndpointBinding, Notification, Receipt, ReceiptState, RefusalCode, Refused,
)

_FILENAME = "receipts.sqlite3"
_MAX_RECORD_BYTES = 256 * 1024
_MAX_CAPACITY = 100_000
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}")
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
_TERMINAL = frozenset({ReceiptState.SETTLED, ReceiptState.FAILED,
                       ReceiptState.CANCELLED, ReceiptState.AMBIGUOUS})
_NEXT = {
    ReceiptState.UNKNOWN: _TERMINAL | {ReceiptState.QUEUED, ReceiptState.CLAIMED, ReceiptState.NOT_SUBMITTED},
    ReceiptState.QUEUED: _TERMINAL | {ReceiptState.CLAIMED},
    ReceiptState.CLAIMED: _TERMINAL,
}
_SCHEMA = {
    "metadata": "CREATE TABLE metadata (singleton INTEGER PRIMARY KEY CHECK(singleton = 1), "
                "version INTEGER NOT NULL CHECK(version = 1), capacity INTEGER NOT NULL "
                "CHECK(capacity BETWEEN 1 AND 100000))",
    "receipts": "CREATE TABLE receipts (principal TEXT NOT NULL, scope TEXT NOT NULL, "
                "notification_id TEXT NOT NULL, delivery_id TEXT NOT NULL UNIQUE, payload BLOB NOT NULL "
                "CHECK(typeof(payload) = 'blob' AND length(payload) <= 262144), "
                "PRIMARY KEY(principal, scope, notification_id)) WITHOUT ROWID",
}


@contextmanager
def _storage_errors():
    try:
        yield
    except Refused:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError, RecursionError, NotImplementedError):
        raise Refused(RefusalCode.STORAGE) from None


def _require(condition):
    if not condition:
        raise Refused(RefusalCode.STORAGE)


def _identifier(value):
    return type(value) is str and _IDENTIFIER.fullmatch(value) is not None


def _timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _text(value):
    return type(value) is str and len(value) <= 16384 and len(value.encode("utf-8")) <= 16384


def _key(value):
    _require(type(value) is tuple and len(value) == 3)
    _require(_identifier(value[0]) and _identifier(value[1]) and type(value[2]) is str)
    _require(str(uuid.UUID(value[2])) == value[2])
    return value


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _encode(row):
    _require(type(row) is Receipt)
    _require(type(row.caller) is Caller and type(row.binding) is EndpointBinding
             and type(row.notification) is Notification and type(row.state) is ReceiptState)
    _key((row.caller.principal_id, row.caller.scope_id, row.notification_id))
    binding = asdict(row.binding)
    _require(all(_identifier(value) for field, value in binding.items() if field != "expires_at"))
    _require(row.binding.route_class in {"local", "cross_device"}
             and row.binding.scope_id == row.caller.scope_id)
    _require(all(_timestamp(value) for value in (
        row.binding.expires_at, row.notification.expires_at, row.arrived_at, row.updated_at)))
    _require(row.updated_at >= row.arrived_at)
    _require(_text(row.notification.text) and bool(row.notification.text.strip())
             and type(row.notification.intent) is str and row.intent in {"store", "wake"})
    _require((row.intent == "store") == (row.state is ReceiptState.STORED))
    _require(_text(row.result) and type(row.result_truncated) is bool)
    _require(row.state in _TERMINAL or (not row.result and not row.result_truncated))
    _require(type(row.evidence_kind) is str and row.evidence_kind == "injected-contract"
             and type(row.provider_budget_enforcement) is str and row.provider_budget_enforcement == "unverified")
    _require(all(type(value) is str and _HEX_DIGEST.fullmatch(value) for value in (
        row.delivery_id, row.payload_digest)))
    payload = {"text": row.notification.text, "intent": row.intent,
               "expires_at": float(row.notification.expires_at)}
    binding["expires_at"] = float(row.binding.expires_at)
    identity = {"domain": "hermes.bot-chat-notification.v1", "notification_id": row.notification_id,
                "caller": asdict(row.caller), "binding": binding}
    _require(row.payload_digest == hashlib.sha256(_json(payload)).hexdigest()
             and row.delivery_id == hashlib.sha256(_json(identity)).hexdigest())
    encoded = _json({"version": 1, "receipt": asdict(row)})
    _require(len(encoded) <= _MAX_RECORD_BYTES)
    return encoded


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        _require(name not in result)
        result[name] = value
    return result


def _decode(encoded):
    _require(type(encoded) is bytes and len(encoded) <= _MAX_RECORD_BYTES)
    value = json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_object)
    _require(type(value) is dict and set(value) == {"version", "receipt"}
             and type(value["version"]) is int and value["version"] == 1)
    fields = value["receipt"]
    _require(type(fields) is dict)
    fields = dict(fields)
    fields["caller"] = Caller(**fields["caller"])
    fields["binding"] = EndpointBinding(**fields["binding"])
    fields["notification"] = Notification(**fields["notification"])
    fields["state"] = ReceiptState(fields["state"])
    row = Receipt(**fields)
    # Reject unknown/missing fields, duplicate keys, nonfinite numbers and alternate
    # encodings instead of silently normalizing an unrecognized stored record.
    _require(_encode(row) == encoded)
    return row


def _same_notification(left, right):
    return (left.caller, left.binding, left.notification, left.payload_digest, left.delivery_id, left.arrived_at) == (
        right.caller, right.binding, right.notification, right.payload_digest, right.delivery_id, right.arrived_at)


class SQLiteReceiptStore:
    """One bounded ledger with explicit creation and no connections kept alive.

    ``create=True, capacity=N`` creates only a new file in an existing empty
    private directory. Omit both when reopening; the original capacity persists.
    Passing capacity on reopen checks equality, never resizes the ledger.

    Successful writes use DELETE journaling with synchronous=EXTRA, request
    fullfsync and sync the directory before acknowledgement. These settings are
    not proof of Mac F_FULLFSYNC, power-loss recovery or production installation.
    """
    durable = True

    def __init__(self, directory: Path, *, create: bool = False,
                 capacity: int | None = None, lock_timeout: float = 1.0):
        with _storage_errors():
            _require(type(create) is bool)
            _require(type(lock_timeout) in (int, float) and math.isfinite(lock_timeout) and 0 <= lock_timeout <= 30)
            _require(capacity is None or (type(capacity) is int and 1 <= capacity <= _MAX_CAPACITY))
            _require(not create or capacity is not None)
            self.directory = Path(directory)
            _require(self.directory.is_absolute() and ".." not in self.directory.parts)
            self._directory_identity = None
            self._file_identity = None
            self._timeout = lock_timeout
            with self._directory_fd() as directory_fd:
                if create:
                    _require(not os.listdir(directory_fd))
                    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
                    file_fd = os.open(_FILENAME, flags, 0o600, dir_fd=directory_fd)
                    try:
                        os.fchmod(file_fd, 0o600)  # Only this newly created, exclusive file.
                        os.fsync(file_fd)
                    finally:
                        os.close(file_fd)
                    os.fsync(directory_fd)
                self._check_files(directory_fd)
                with self._connection(directory_fd) as connection:
                    if create:
                        connection.execute("BEGIN IMMEDIATE")
                        for statement in _SCHEMA.values():
                            connection.execute(statement)
                        connection.execute("PRAGMA application_id = 0x48424e31")
                        connection.execute("PRAGMA user_version = 1")
                        connection.execute("INSERT INTO metadata VALUES (1, 1, ?)", (capacity,))
                        self._commit(connection, directory_fd)
                    self.capacity = self._schema_capacity(connection, capacity)

    @contextmanager
    def _directory_fd(self):
        # Guard/decorator wrappers change callable identity, not dir_fd support.
        # Use the guarded operations below; unsupported calls fail closed.
        _require(all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC", "geteuid")))
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(self.directory.anchor, flags)
        try:
            for component in self.directory.parts[1:]:
                parent = os.fstat(descriptor)
                _require(parent.st_uid in {0, os.geteuid()})
                _require(not parent.st_mode & 0o022 or bool(parent.st_mode & stat.S_ISVTX))
                child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            current = os.fstat(descriptor)
            _require(current.st_uid == os.geteuid() and stat.S_IMODE(current.st_mode) == 0o700)
            identity = current.st_dev, current.st_ino
            _require(self._directory_identity is None or self._directory_identity == identity)
            self._directory_identity = identity
            yield descriptor
            current = os.stat(self.directory, follow_symlinks=False)
            _require(stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == identity
                     and current.st_uid == os.geteuid() and stat.S_IMODE(current.st_mode) == 0o700)
        finally:
            os.close(descriptor)

    def _check_files(self, directory_fd):
        names = set(os.listdir(directory_fd))
        _require(_FILENAME in names and names <= {_FILENAME, _FILENAME + "-journal"})
        for name in names:
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                if name == _FILENAME + "-journal":  # Another completed SQLite transaction.
                    continue
                raise
            _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                     and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)
            if name == _FILENAME:
                identity = info.st_dev, info.st_ino
                _require(self._file_identity is None or identity == self._file_identity)
                self._file_identity = identity
        actual = os.stat(self.directory / _FILENAME, follow_symlinks=False)
        _require((actual.st_dev, actual.st_ino) == self._file_identity)

    @contextmanager
    def _connection(self, directory_fd):
        self._check_files(directory_fd)
        # rw, not rwc: disappearance may never create an empty replacement ledger.
        connection = sqlite3.connect((self.directory / _FILENAME).as_uri() + "?mode=rw",
                                     uri=True, timeout=self._timeout, isolation_level=None)
        try:
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _MAX_RECORD_BYTES + 2048)
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA temp_store = MEMORY")
            connection.execute("PRAGMA synchronous = EXTRA")
            connection.execute("PRAGMA fullfsync = ON")
            _require(connection.execute("PRAGMA journal_mode").fetchone() == ("delete",))
            _require(connection.execute("PRAGMA synchronous").fetchone() == (3,))
            _require(connection.execute("PRAGMA fullfsync").fetchone() == (1,))
            _require(connection.execute("PRAGMA temp_store").fetchone() == (2,))
            _require(connection.execute("PRAGMA trusted_schema").fetchone() == (0,))
            options = {row[0] for row in connection.execute("PRAGMA compile_options")}
            _require(bool(options & {"TEMP_STORE=1", "TEMP_STORE=2", "TEMP_STORE=3"}))
            self._check_files(directory_fd)
            yield connection
            self._check_files(directory_fd)
        finally:
            try:
                if connection.in_transaction:
                    connection.rollback()
            finally:
                connection.close()

    def _schema_capacity(self, connection, expected):
        _require(connection.execute("PRAGMA application_id").fetchone() == (0x48424E31,))
        _require(connection.execute("PRAGMA user_version").fetchone() == (1,))
        schema = list(connection.execute("SELECT type, name, sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*'"))
        _require(len(schema) == len(_SCHEMA)
                 and all(kind == "table" and _SCHEMA.get(name) == sql for kind, name, sql in schema))
        metadata = list(connection.execute("SELECT singleton, version, capacity FROM metadata"))
        _require(len(metadata) == 1)
        singleton, version, capacity = metadata[0]
        _require(singleton == 1 and version == 1 and type(capacity) is int and 1 <= capacity <= _MAX_CAPACITY)
        _require(expected is None or capacity == expected)
        _require(connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] <= capacity)
        return capacity

    def _commit(self, connection, directory_fd):
        self._check_files(directory_fd)
        connection.commit()
        # Failure after commit is an uncertain acknowledgement, never permission
        # to delete a receipt, retry admission, or replace the ledger.
        os.fsync(directory_fd)
        self._check_files(directory_fd)

    @contextmanager
    def _operation(self, *, write=False):
        with _storage_errors(), self._directory_fd() as directory_fd, self._connection(directory_fd) as connection:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            self._schema_capacity(connection, self.capacity)
            yield connection
            if write:
                self._commit(connection, directory_fd)

    @staticmethod
    def _read(connection, key):
        found = connection.execute(
            "SELECT delivery_id, payload FROM receipts WHERE principal = ? AND scope = ? AND notification_id = ?",
            key).fetchone()
        if found is None:
            return None, None
        delivery_id, encoded = found
        row = _decode(encoded)
        _require((row.caller.principal_id, row.caller.scope_id, row.notification_id) == key
                 and row.delivery_id == delivery_id)
        return row, encoded

    def read(self, key: tuple[str, str, str]) -> Receipt | None:
        with _storage_errors():
            _key(key)
            with self._operation() as connection:
                return self._read(connection, key)[0]

    def reserve(self, key: tuple[str, str, str], receipt: Receipt) -> tuple[bool, Receipt]:
        with _storage_errors():
            _key(key)
            encoded = _encode(receipt)
            _require(key == (receipt.caller.principal_id, receipt.caller.scope_id, receipt.notification_id))
            with self._operation(write=True) as connection:
                actual, _ = self._read(connection, key)
                if actual is not None:
                    return False, actual
                _require(receipt.state in {ReceiptState.STORED, ReceiptState.UNKNOWN})
                if connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] >= self.capacity:
                    raise Refused(RefusalCode.CAPACITY)
                connection.execute("INSERT INTO receipts VALUES (?, ?, ?, ?, ?)", (*key, receipt.delivery_id, encoded))
                return True, receipt

    def replace(self, key: tuple[str, str, str], expected: Receipt, receipt: Receipt) -> Receipt:
        with _storage_errors():
            _key(key)
            _encode(expected)
            encoded = _encode(receipt)
            _require(all(key == (row.caller.principal_id, row.caller.scope_id, row.notification_id)
                         for row in (expected, receipt)))
            with self._operation(write=True) as connection:
                actual, previous = self._read(connection, key)
                _require(actual is not None)
                if actual != expected:
                    return actual
                if actual == receipt:
                    return actual
                if (not _same_notification(actual, receipt) or receipt.updated_at < actual.updated_at
                        or receipt.state not in _NEXT.get(actual.state, frozenset())):
                    raise Refused(RefusalCode.CONFLICT)
                changed = connection.execute(
                    "UPDATE receipts SET payload = ? WHERE principal = ? AND scope = ? AND notification_id = ? AND payload = ?",
                    (encoded, *key, previous))
                _require(changed.rowcount == 1)
                return receipt
