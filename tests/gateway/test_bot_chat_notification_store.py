"""Real disposable SQLite at the ReceiptStore seam; no owner or provider calls.

Canonical runner qualification is separate from native Mac durability acceptance.
"""
from dataclasses import asdict, replace
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import uuid

import pytest

from gateway.bot_chat_notification_store import SQLiteReceiptStore
from gateway.bot_chat_notifications import (
    BotChatNotifications, Caller, EndpointBinding, Grant, Notification, Permission,
    Receipt, ReceiptState, RefusalCode, Refused, Registration, TargetRef,
)

pytestmark = pytest.mark.platforms("posix")


def receipt(*, intent="wake", generation="first", notification_id=None, text="Retain this result."):
    caller = Caller("reviewer", "local-scope")
    binding = EndpointBinding("bot", generation, caller.scope_id, "local", "device", "profile",
                              "existing-session", "lease", "live-owner", 300.0)
    notification = Notification(notification_id or str(uuid.uuid4()), text, intent, 200.0)

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False, allow_nan=False).encode()).hexdigest()

    payload = {"text": text, "intent": intent, "expires_at": 200.0}
    identity = {"domain": "hermes.bot-chat-notification.v1", "notification_id": notification.notification_id,
                "caller": asdict(caller), "binding": asdict(binding)}
    return Receipt(caller, binding, notification, digest(payload), digest(identity),
                   ReceiptState.STORED if intent == "store" else ReceiptState.UNKNOWN, 100.0, 100.0)


def key(row):
    return row.caller.principal_id, row.caller.scope_id, row.notification_id


@pytest.fixture
def private_dir(tmp_path):
    directory = (tmp_path / "receipts").resolve()
    directory.mkdir(mode=0o700)
    return directory


def refused(code, operation):
    with pytest.raises(Refused) as caught:
        operation()
    assert caught.value.code is code
    assert str(caught.value) == code.value


def test_reopen_retains_one_key_across_generations_without_retargeting(private_dir):
    original = receipt(intent="store")
    store = SQLiteReceiptStore(private_dir, create=True, capacity=2)
    assert store.read(key(original)) is None
    assert store.reserve(key(original), original) == (True, original)

    reopened = SQLiteReceiptStore(private_dir)
    changed = receipt(intent="store", generation="second", notification_id=original.notification_id)
    assert reopened.durable is True
    assert reopened.read(key(original)) == original
    assert reopened.reserve(key(changed), changed) == (False, original)
    assert store.read(key(original)) == original


def test_compare_and_swap_returns_actual_winner_and_retains_terminal_tombstone(private_dir):
    store = SQLiteReceiptStore(private_dir, create=True, capacity=1)
    original = receipt()
    assert store.reserve(key(original), original) == (True, original)
    peer = SQLiteReceiptStore(private_dir)
    claimed = replace(original, state=ReceiptState.CLAIMED, updated_at=101.0)
    terminal = replace(original, state=ReceiptState.NOT_SUBMITTED, updated_at=102.0)
    assert peer.replace(key(original), original, claimed) == claimed
    assert store.replace(key(original), original, terminal) == claimed
    settled = replace(claimed, state=ReceiptState.SETTLED, updated_at=103.0, result="Finished.")
    assert store.replace(key(original), claimed, settled) == settled
    assert SQLiteReceiptStore(private_dir).read(key(original)) == settled
    another = receipt()
    refused(RefusalCode.CAPACITY, lambda: peer.reserve(key(another), another))
    assert peer.reserve(key(original), original) == (False, settled)


def test_two_connections_compete_for_one_reservation_and_one_cas_winner(private_dir):
    stores = [SQLiteReceiptStore(private_dir, create=True, capacity=2), SQLiteReceiptStore(private_dir)]
    original = receipt()
    alternate = receipt(notification_id=original.notification_id, generation="alternative")
    start = threading.Barrier(2, timeout=5)

    def reserve_one(index):
        start.wait()
        row = (original, alternate)[index]
        return stores[index].reserve(key(row), row)

    with ThreadPoolExecutor(max_workers=2) as pool:
        reservations = list(pool.map(reserve_one, (0, 1)))
    assert sorted(created for created, _ in reservations) == [False, True]
    winner = reservations[0][1]
    assert reservations[1][1] == winner
    assert SQLiteReceiptStore(private_dir).read(key(original)) == winner

    start = threading.Barrier(2, timeout=5)
    proposals = [replace(winner, state=ReceiptState.CLAIMED, updated_at=101.0),
                 replace(winner, state=ReceiptState.NOT_SUBMITTED, updated_at=102.0)]

    def replace_one(index):
        start.wait()
        return stores[index].replace(key(winner), winner, proposals[index])

    with ThreadPoolExecutor(max_workers=2) as pool:
        observed = list(pool.map(replace_one, (0, 1)))
    assert observed[0] == observed[1]
    assert observed[0] in proposals
    assert SQLiteReceiptStore(private_dir).read(key(winner)) == observed[0]


def test_competing_distinct_keys_cannot_overbook_capacity(private_dir):
    stores = [SQLiteReceiptStore(private_dir, create=True, capacity=1), SQLiteReceiptStore(private_dir)]
    rows = [receipt(), receipt()]
    start = threading.Barrier(2, timeout=5)

    def reserve_one(index):
        start.wait()
        try:
            return stores[index].reserve(key(rows[index]), rows[index])
        except Refused as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve_one, (0, 1)))
    assert outcomes.count(RefusalCode.CAPACITY) == 1
    winner = next(result[1] for result in outcomes if type(result) is tuple)
    assert SQLiteReceiptStore(private_dir).read(key(winner)) == winner
    loser = next(row for row in rows if row != winner)
    assert stores[0].read(key(loser)) is None


@pytest.mark.parametrize("state", [ReceiptState.UNKNOWN, ReceiptState.NOT_SUBMITTED])
def test_capacity_and_dedup_survive_reopen_without_eviction(private_dir, state):
    store = SQLiteReceiptStore(private_dir, create=True, capacity=1)
    original = receipt()
    store.reserve(key(original), original)
    retained = replace(original, state=state, updated_at=101.0) if state != original.state else original
    store.replace(key(original), original, retained)
    reopened = SQLiteReceiptStore(private_dir)
    another = receipt()
    refused(RefusalCode.CAPACITY, lambda: reopened.reserve(key(another), another))
    assert reopened.reserve(key(original), original) == (False, retained)
    refused(RefusalCode.STORAGE, lambda: SQLiteReceiptStore(private_dir, capacity=2))
    refused(RefusalCode.STORAGE, lambda: SQLiteReceiptStore(private_dir, create=True, capacity=1))
    assert SQLiteReceiptStore(private_dir).read(key(original)) == retained


def test_real_sqlite_write_lock_refuses_without_losing_or_creating_receipts(private_dir):
    store = SQLiteReceiptStore(private_dir, create=True, capacity=2, lock_timeout=0)
    original, next_row = receipt(), receipt()
    store.reserve(key(original), original)
    locker = sqlite3.connect(private_dir / "receipts.sqlite3", isolation_level=None)
    try:
        locker.execute("BEGIN IMMEDIATE")
        refused(RefusalCode.STORAGE, lambda: store.reserve(key(next_row), next_row))
        refused(RefusalCode.STORAGE, lambda: store.replace(
            key(original), original, replace(original, state=ReceiptState.CLAIMED, updated_at=101.0)))
    finally:
        locker.rollback()
        locker.close()
    reopened = SQLiteReceiptStore(private_dir)
    assert reopened.read(key(original)) == original
    assert reopened.read(key(next_row)) is None


@pytest.mark.parametrize("failure", ["before_commit", "after_commit", "directory_sync"])
def test_failed_acknowledgement_is_sanitized_and_reopen_reports_durable_truth(private_dir, monkeypatch, failure):
    store = SQLiteReceiptStore(private_dir, create=True, capacity=2)
    row = receipt()
    real_connect = sqlite3.connect

    class CommitFailure(sqlite3.Connection):
        def commit(self):
            if failure == "after_commit":
                super().commit()
            raise sqlite3.OperationalError("private backend detail")

    with monkeypatch.context() as patch:
        if failure == "directory_sync":
            def fail_sync(descriptor):
                raise OSError("private backend detail")
            patch.setattr(os, "fsync", fail_sync)
        else:
            patch.setattr(sqlite3, "connect", lambda *args, **kwargs: real_connect(
                *args, **kwargs, factory=CommitFailure))
        refused(RefusalCode.STORAGE, lambda: store.reserve(key(row), row))

    reopened = SQLiteReceiptStore(private_dir)
    if failure == "before_commit":
        assert reopened.read(key(row)) is None
        assert reopened.reserve(key(row), row) == (True, row)
    else:
        assert reopened.read(key(row)) == row
        assert reopened.reserve(key(row), row) == (False, row)


def test_private_journal_and_durable_connection_settings_at_real_commit(private_dir, monkeypatch):
    store = SQLiteReceiptStore(private_dir, create=True, capacity=2)
    row = receipt(text="x" * 16384)
    real_connect = sqlite3.connect
    observations = []

    class ObservedCommit(sqlite3.Connection):
        def commit(self):
            journal = private_dir / "receipts.sqlite3-journal"
            observations.append((stat.S_IMODE(journal.stat().st_mode), journal.stat().st_uid,
                                 self.execute("PRAGMA synchronous").fetchone(),
                                 self.execute("PRAGMA fullfsync").fetchone(),
                                 self.execute("PRAGMA temp_store").fetchone(),
                                 self.execute("PRAGMA journal_mode").fetchone()))
            return super().commit()

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", lambda *args, **kwargs: real_connect(
            *args, **kwargs, factory=ObservedCommit))
        assert store.reserve(key(row), row) == (True, row)
    assert observations == [(0o600, os.geteuid(), (3,), (1,), (2,), ("delete",))]
    assert stat.S_IMODE(private_dir.stat().st_mode) == 0o700
    assert [(entry.name, stat.S_IMODE(entry.stat().st_mode)) for entry in private_dir.iterdir()] == [
        ("receipts.sqlite3", 0o600)]
    assert SQLiteReceiptStore(private_dir).read(key(row)) == row


@pytest.mark.parametrize("location", ["relative", "missing", "public", "symlink", "ancestor_symlink"])
def test_unsafe_directory_is_refused_without_creation_or_permission_repair(private_dir, tmp_path, location):
    selected = private_dir
    if location == "relative":
        selected = Path("receipt-store-relative-not-authorized")
    elif location == "missing":
        selected = tmp_path / "missing"
    elif location == "public":
        private_dir.chmod(0o755)
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(private_dir, target_is_directory=True)
        selected = alias
        if location == "ancestor_symlink":
            (private_dir / "child").mkdir(mode=0o700)
            selected = alias / "child"
    refused(RefusalCode.STORAGE, lambda: SQLiteReceiptStore(selected, create=True, capacity=1))
    assert not (private_dir / "receipts.sqlite3").exists()
    if location == "public":
        assert stat.S_IMODE(private_dir.stat().st_mode) == 0o755
    if location == "missing":
        assert not selected.exists()


@pytest.mark.parametrize("unsafe", ["file_mode", "file_symlink", "hard_link", "journal_symlink", "wal", "shm"])
def test_unsafe_database_or_sidecar_is_not_followed_or_repaired(private_dir, tmp_path, unsafe):
    store = SQLiteReceiptStore(private_dir, create=True, capacity=2)
    row = receipt()
    store.reserve(key(row), row)
    database = private_dir / "receipts.sqlite3"
    outside = tmp_path / "outside"
    outside.write_bytes(b"unrelated test material")
    original_outside = outside.read_bytes()
    if unsafe == "file_mode":
        database.chmod(0o640)
    elif unsafe == "file_symlink":
        database.rename(tmp_path / "preserved.sqlite3")
        database.symlink_to(outside)
    elif unsafe == "hard_link":
        os.link(database, tmp_path / "linked.sqlite3")
    elif unsafe == "journal_symlink":
        (private_dir / "receipts.sqlite3-journal").symlink_to(outside)
    else:
        (private_dir / ("receipts.sqlite3-" + unsafe)).write_bytes(b"")
    refused(RefusalCode.STORAGE, lambda: store.read(key(row)))
    refused(RefusalCode.STORAGE, lambda: SQLiteReceiptStore(private_dir))
    assert outside.read_bytes() == original_outside
    if unsafe == "file_mode":
        assert stat.S_IMODE(database.stat().st_mode) == 0o640


def test_nonowner_directory_and_file_are_refused(private_dir):
    if os.geteuid() != 0:
        pytest.skip("Real alternate ownership requires the isolated root test user; no UID is mocked.")
    store = SQLiteReceiptStore(private_dir, create=True, capacity=1)
    database = private_dir / "receipts.sqlite3"
    os.chown(database, 1, -1)
    try:
        refused(RefusalCode.STORAGE, lambda: SQLiteReceiptStore(private_dir))
    finally:
        os.chown(database, 0, -1)
    os.chown(private_dir, 1, -1)
    try:
        refused(RefusalCode.STORAGE, lambda: store.read(key(receipt())))
    finally:
        os.chown(private_dir, 0, -1)


def test_replaced_directory_or_file_cannot_reset_a_bound_store(private_dir, tmp_path):
    store = SQLiteReceiptStore(private_dir, create=True, capacity=1)
    row = receipt()
    store.reserve(key(row), row)
    retained = tmp_path / "retained-directory"
    private_dir.rename(retained)
    private_dir.mkdir(mode=0o700)
    refused(RefusalCode.STORAGE, lambda: store.reserve(key(row), row))
    assert not list(private_dir.iterdir())
    assert SQLiteReceiptStore(retained).read(key(row)) == row

    replacement = SQLiteReceiptStore(private_dir, create=True, capacity=1)
    replacement_file = tmp_path / "replacement.sqlite3"
    (private_dir / "receipts.sqlite3").rename(replacement_file)
    # Move the preserved database into that path: valid SQLite is still a different inode.
    (retained / "receipts.sqlite3").rename(private_dir / "receipts.sqlite3")
    refused(RefusalCode.STORAGE, lambda: replacement.read(key(row)))
    assert SQLiteReceiptStore(private_dir).read(key(row)) == row


@pytest.mark.parametrize("change", [
    {"state": "unknown"}, {"updated_at": float("nan")}, {"arrived_at": True},
    {"payload_digest": "0" * 64}, {"delivery_id": "f" * 64},
    {"result": "not a terminal result"}, {"result_truncated": 1},
    {"evidence_kind": "actual-session"}, {"provider_budget_enforcement": "verified"},
])
def test_malformed_typed_receipt_cannot_reserve_a_key(private_dir, change):
    store = SQLiteReceiptStore(private_dir, create=True, capacity=1)
    row = receipt()
    refused(RefusalCode.STORAGE, lambda: store.reserve(key(row), replace(row, **change)))
    assert store.read(key(row)) is None


@pytest.mark.parametrize("text", ["x" * 16385, "é" * 8193, "\ud800", " "])
def test_invalid_or_unbounded_text_is_not_persisted(private_dir, text):
    store = SQLiteReceiptStore(private_dir, create=True, capacity=1)
    row = receipt()
    bad = replace(row, notification=replace(row.notification, text=text))
    refused(RefusalCode.STORAGE, lambda: store.reserve(key(row), bad))
    assert store.read(key(row)) is None


@pytest.mark.parametrize("damage", ["missing", "truncated"])
def test_missing_or_truncated_database_is_never_reinitialized(private_dir, tmp_path, damage):
    store = SQLiteReceiptStore(private_dir, create=True, capacity=1)
    row = receipt()
    store.reserve(key(row), row)
    database = private_dir / "receipts.sqlite3"
    backup = tmp_path / "retained.sqlite3"
    database.rename(backup)
    retained = backup.read_bytes()
    if damage == "truncated":
        database.write_bytes(b"")
        database.chmod(0o600)
    refused(RefusalCode.STORAGE, lambda: store.read(key(row)))
    refused(RefusalCode.STORAGE, lambda: SQLiteReceiptStore(private_dir))
    assert backup.read_bytes() == retained
    if damage == "missing":
        assert not database.exists()
    else:
        assert database.read_bytes() == b""


@pytest.mark.parametrize("corruption", ["truncated", "unknown_version", "duplicate_key", "extra_field", "database_version"])
def test_corrupt_or_unrecognized_stored_records_fail_closed_without_repair(private_dir, corruption):
    store = SQLiteReceiptStore(private_dir, create=True, capacity=1)
    row = receipt()
    store.reserve(key(row), row)
    database = private_dir / "receipts.sqlite3"
    with sqlite3.connect(database) as writer:
        if corruption == "database_version":
            writer.execute("PRAGMA user_version = 2")
        else:
            encoded = json.dumps({"version": 1, "receipt": asdict(row)}, sort_keys=True,
                                 separators=(",", ":"), ensure_ascii=False).encode()
            altered = {
                "truncated": b"{",
                "unknown_version": encoded.replace(b'"version":1', b'"version":2'),
                "duplicate_key": b'{"version":1,' + encoded[1:],
                "extra_field": b'{"unexpected":true,' + encoded[1:],
            }[corruption]
            writer.execute("UPDATE receipts SET payload = ?", (altered,))
    original_bytes = database.read_bytes()
    refused(RefusalCode.STORAGE, lambda: store.read(key(row)))
    refused(RefusalCode.STORAGE, lambda: store.reserve(key(row), row))
    assert database.read_bytes() == original_bytes


def test_parameterized_text_and_terminal_immutability(private_dir):
    store = SQLiteReceiptStore(private_dir, create=True, capacity=2)
    row = receipt(text="'); DROP TABLE receipts; -- 📨")
    assert store.reserve(key(row), row) == (True, row)
    cancelled = replace(row, state=ReceiptState.CANCELLED, updated_at=101.0, result="Stopped.")
    assert store.replace(key(row), row, cancelled) == cancelled
    refused(RefusalCode.CONFLICT, lambda: store.replace(key(row), cancelled, row))
    altered_terminal = replace(cancelled, result="Changed.", updated_at=102.0)
    refused(RefusalCode.CONFLICT, lambda: store.replace(key(row), cancelled, altered_terminal))
    assert store.read(key(row)) == cancelled
    another = receipt(intent="store")
    assert store.reserve(key(another), another) == (True, another)


def test_real_store_integrates_store_and_status_without_a_wake_dependency(private_dir):
    row = receipt(intent="store")
    store = SQLiteReceiptStore(private_dir, create=True, capacity=2)

    class ForbiddenWake:
        def __getattr__(self, name):
            raise AssertionError("Store/status must not use the wake-capable dependency")

    binding = row.binding
    grants = (Grant(row.caller.principal_id, row.caller.scope_id,
                    frozenset({Permission.STORE, Permission.STATUS}), 300.0),)

    def adapter(ledger):
        return BotChatNotifications(registration=Registration(binding, grants, True), receipts=ledger,
                                    mailbox=ForbiddenWake(), admission_gate=ForbiddenWake(), clock=lambda: 100.0)

    target = TargetRef(binding.endpoint_id, binding.generation)
    first = adapter(store).admit(row.caller, target, row.notification)
    reopened = adapter(SQLiteReceiptStore(private_dir))
    assert first.state is ReceiptState.STORED
    assert reopened.inspect(row.caller, target, row.notification_id) == row
    assert reopened.admit(row.caller, target, row.notification) == first
    refused(RefusalCode.PERMISSION, lambda: reopened.admit(
        row.caller, target, replace(row.notification, intent="wake")))
