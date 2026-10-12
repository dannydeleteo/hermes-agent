"""Disconnected contract tests: only in-memory receipts, mailbox and admission gate.

May run as stdlib unittest without the repository's runtime-importing conftest.
No fake's durability declaration is evidence of real persistence or provider budgets.
"""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from gateway.bot_chat_notifications import (
    AdmissionPermit, BotChatNotifications, Caller, EndpointBinding, Grant,
    MailboxReceipt, Notification, Permission, ReceiptState, RefusalCode, Refused, Registration,
    TargetRef,
)


class MemoryReceipts:
    """Simulated atomic durable store shared across adapter instances, not disk I/O."""
    durable = True

    def __init__(self):
        self.rows = {}
        self.lock = threading.RLock()
        self.reserve_error = None
        self.replace_error = None
        self.before_replace = lambda key, expected, proposed: None

    def read(self, key):
        with self.lock:
            return self.rows.get(key)

    def reserve(self, key, receipt):
        if self.reserve_error is not None:
            raise self.reserve_error
        with self.lock:
            existing = self.rows.get(key)
            if existing is not None:
                return False, existing
            self.rows[key] = receipt
            return True, receipt

    def replace(self, key, expected, receipt):
        if self.replace_error is not None:
            raise self.replace_error
        self.before_replace(key, expected, receipt)
        with self.lock:
            current = self.rows[key]
            if current == expected:
                self.rows[key] = receipt
            return self.rows[key]


class FakeMailbox:
    def __init__(self):
        self.rows = {}
        self.admissions = []
        self.reads = []
        self.fail_after_store = False
        self.read_error = None
        self.transform = lambda receipt: receipt

    def admit(self, envelope):
        self.admissions.append(envelope)
        result = MailboxReceipt(
            envelope.delivery_id, envelope.binding, envelope.payload_digest,
            ReceiptState.QUEUED,
        )
        self.rows[envelope.delivery_id] = result
        if self.fail_after_store:
            raise RuntimeError("private backend detail must not escape")
        return self.transform(result)

    def read(self, delivery_id):
        self.reads.append(delivery_id)
        if self.read_error is not None:
            raise self.read_error
        return self.rows.get(delivery_id)


class FakeGate:
    def __init__(self):
        self.entries = []
        self.overrides = {}
        self.before_yield = lambda: None
        self.exit_error = None

    @contextmanager
    def hold(self, envelope):
        self.entries.append(envelope)
        self.before_yield()
        yield replace(AdmissionPermit(
            binding=envelope.binding, payload_digest=envelope.payload_digest,
            delivery_id=envelope.delivery_id,
            target_state="ready", capacity_state="available", usage_state="available",
            budget_state="reserved", reservation_id="fake-reservation",
        ), **self.overrides)
        if self.exit_error is not None:
            raise self.exit_error


class BotChatNotificationTests(unittest.TestCase):
    def setUp(self):
        self.binding = EndpointBinding(
            endpoint_id="hermes-bot", generation="generation-one", scope_id="local-scope",
            route_class="local", device_id="mac", profile_id="helper",
            session_id="existing-chat", lease_id="lease-one", live_session_id="live-one",
            expires_at=300.0,
        )
        self.caller = Caller("codex", "local-scope")
        self.target = TargetRef("hermes-bot", "generation-one")
        self.notification = Notification(
            "12345678-1234-4234-8234-123456789abc", "Please review the supplied result.",
            intent="store", expires_at=200.0,
        )
        self.grant = Grant("codex", "local-scope", frozenset({Permission.STORE, Permission.STATUS}), 300.0)
        self.receipts, self.mailbox, self.gate = MemoryReceipts(), FakeMailbox(), FakeGate()
        self.now = 100.0
        self.adapter = self.make_adapter()

    def make_adapter(self, **overrides):
        values = dict(
            registration=Registration(self.binding, (self.grant,), owner_opt_in=True),
            receipts=self.receipts, mailbox=self.mailbox, admission_gate=self.gate,
            clock=lambda: self.now,
        )
        values.update(overrides)
        return BotChatNotifications(**values)

    def wake_adapter(self, **overrides):
        grant = replace(self.grant, permissions=frozenset(Permission))
        return self.make_adapter(
            registration=Registration(self.binding, (grant,), owner_opt_in=True),
            **overrides,
        )

    def assert_refused(self, code, operation):
        with self.assertRaises(Refused) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code.value)

    def test_store_and_inspect_are_not_model_wake_authority(self):
        arrival = self.adapter.admit(self.caller, self.target, self.notification)
        receipt = self.adapter.inspect(self.caller, self.target, self.notification.notification_id)
        self.assertEqual(arrival.state, ReceiptState.STORED)
        self.assertEqual(arrival.evidence_kind, "injected-contract")
        self.assertEqual(arrival.provider_budget_enforcement, "unverified")
        self.assertEqual(receipt.notification_id, self.notification.notification_id)
        self.assertEqual(receipt.state, ReceiptState.STORED)
        self.assertEqual(receipt.notification, self.notification)
        self.assertEqual(arrival.delivery_id, receipt.delivery_id)
        self.assertRegex(arrival.delivery_id, r"^[0-9a-f]{64}$")
        self.assertEqual(self.adapter.admit(self.caller, self.target, self.notification), arrival)
        self.assertEqual(self.mailbox.admissions, [])
        self.assertEqual(self.mailbox.reads, [])
        self.assertEqual(self.gate.entries, [])

    def test_guarded_wake_and_status_follow_one_exact_receipt(self):
        adapter = self.wake_adapter()
        notification = replace(self.notification, intent="wake")
        arrival = adapter.admit(self.caller, self.target, notification)
        self.assertEqual(arrival.state, ReceiptState.QUEUED)
        self.assertEqual(len(self.gate.entries), 1)
        self.assertEqual(len(self.mailbox.admissions), 1)
        self.assertEqual(self.mailbox.admissions[0].notification, notification)
        self.assertFalse(hasattr(arrival, "result"))

        delivery_id = arrival.delivery_id
        self.mailbox.rows[delivery_id] = replace(self.mailbox.rows[delivery_id], state=ReceiptState.CLAIMED)
        self.now = 110.0
        claimed = adapter.inspect(self.caller, self.target, notification.notification_id)
        self.assertEqual(claimed.state, ReceiptState.CLAIMED)
        self.assertEqual(claimed.updated_at, 110.0)
        self.mailbox.rows[delivery_id] = replace(
            self.mailbox.rows[delivery_id], state=ReceiptState.SETTLED, result="Reviewed supplied text.",
        )
        settled = adapter.inspect(self.caller, self.target, notification.notification_id)
        self.assertEqual(settled.state, ReceiptState.SETTLED)
        self.assertEqual(settled.result, "Reviewed supplied text.")
        self.assertEqual(settled.evidence_kind, "injected-contract")
        self.assertEqual(settled.provider_budget_enforcement, "unverified")
        self.assertEqual(adapter.admit(self.caller, self.target, notification), settled.admission())
        self.assertEqual(len(self.gate.entries), 1)
        self.assertEqual(len(self.mailbox.admissions), 1)
        self.assertEqual(self.mailbox.reads, [delivery_id, delivery_id])

    def test_wrong_notification_from_store_is_not_a_status_result(self):
        self.adapter.admit(self.caller, self.target, self.notification)
        key = self.caller.principal_id, self.caller.scope_id, self.notification.notification_id
        other = replace(self.notification, notification_id="32345678-1234-4234-8234-123456789abc")
        self.receipts.rows[key] = replace(self.receipts.rows[key], notification=other)
        with self.assertRaises(Refused) as refused:
            self.adapter.inspect(self.caller, self.target, self.notification.notification_id)
        self.assertEqual(refused.exception.code, RefusalCode.CONFLICT)
        self.assertEqual(self.mailbox.reads, [])

    def test_store_wake_and_status_are_separate_permissions(self):
        for permissions in ({Permission.STATUS}, {Permission.WAKE}, {Permission.STORE}):
            with self.subTest(permissions=permissions):
                grant = replace(self.grant, permissions=frozenset(permissions))
                adapter = self.make_adapter(registration=Registration(self.binding, (grant,), True))
                self.assert_refused(RefusalCode.PERMISSION, lambda: adapter.admit(
                    self.caller, self.target, replace(self.notification, intent="wake"),
                ))
        stored = self.adapter.admit(self.caller, self.target, self.notification)
        grant = replace(self.grant, permissions=frozenset({Permission.STORE}))
        store_only = self.make_adapter(registration=Registration(self.binding, (grant,), True))
        self.assert_refused(RefusalCode.PERMISSION, lambda: store_only.inspect(
            self.caller, self.target, self.notification.notification_id,
        ))
        grant = replace(self.grant, permissions=frozenset({Permission.STATUS}))
        status_only = self.make_adapter(registration=Registration(self.binding, (grant,), True))
        self.assertEqual(status_only.inspect(self.caller, self.target, self.notification.notification_id).admission(), stored)
        self.assertEqual(self.mailbox.admissions, [])
        self.assertEqual(self.gate.entries, [])

    def test_registration_identity_expiry_and_durability_fail_closed(self):
        cases = [
            (RefusalCode.UNREGISTERED, Registration(self.binding, (self.grant,)), self.caller, self.target),
            (RefusalCode.PERMISSION, Registration(self.binding, (self.grant,), True), Caller("claude", "local-scope"), self.target),
            (RefusalCode.FOREIGN, Registration(self.binding, (self.grant,), True), Caller("codex", "windows-scope"), self.target),
            (RefusalCode.STALE, Registration(self.binding, (self.grant,), True), self.caller, replace(self.target, generation="old")),
            (RefusalCode.STALE, Registration(self.binding, (self.grant,), True), self.caller, replace(self.target, endpoint_id="other")),
            (RefusalCode.EXPIRED, Registration(replace(self.binding, expires_at=100), (self.grant,), True), self.caller, self.target),
            (RefusalCode.PERMISSION, Registration(self.binding, (replace(self.grant, expires_at=100),), True), self.caller, self.target),
            (RefusalCode.STALE, Registration(replace(self.binding, lease_id=""), (self.grant,), True), self.caller, self.target),
            (RefusalCode.STALE, Registration(replace(self.binding, route_class="generic_api"), (self.grant,), True), self.caller, self.target),
        ]
        for code, registration, caller, target in cases:
            with self.subTest(code=code, registration=registration):
                adapter = self.make_adapter(registration=registration)
                self.assert_refused(code, lambda: adapter.admit(caller, target, self.notification))
        self.receipts.durable = False
        self.assert_refused(RefusalCode.NONDURABLE, lambda: self.adapter.admit(self.caller, self.target, self.notification))
        self.assertEqual(self.mailbox.admissions, [])
        self.assertEqual(self.gate.entries, [])
        self.assertEqual(self.receipts.rows, {})

    def test_gate_refuses_unsafe_or_unknown_admission_without_reserving_work(self):
        adapter = self.wake_adapter()
        notification = replace(self.notification, intent="wake")
        cases = [
            ("target_state", "busy", RefusalCode.BUSY),
            ("target_state", "approval_wait", RefusalCode.APPROVAL),
            ("target_state", "stale", RefusalCode.STALE),
            ("target_state", "unavailable", RefusalCode.UNAVAILABLE),
            ("target_state", "unknown", RefusalCode.UNKNOWN),
            ("target_state", "unexpected", RefusalCode.UNKNOWN),
            ("capacity_state", "blocked", RefusalCode.CAPACITY),
            ("capacity_state", "unknown", RefusalCode.CAPACITY),
            ("usage_state", "blocked", RefusalCode.USAGE),
            ("usage_state", "unknown", RefusalCode.USAGE),
            ("budget_state", "unavailable", RefusalCode.BUDGET),
            ("budget_state", "unknown", RefusalCode.BUDGET),
            ("reservation_id", "", RefusalCode.BUDGET),
            ("binding", replace(self.binding, lease_id="different-lease"), RefusalCode.STALE),
            ("binding", replace(self.binding, live_session_id="different-live"), RefusalCode.STALE),
            ("payload_digest", "0" * 64, RefusalCode.STALE),
            ("delivery_id", "0" * 64, RefusalCode.STALE),
        ]
        for field, value, code in cases:
            with self.subTest(field=field, value=value):
                self.gate.overrides = {field: value}
                self.assert_refused(code, lambda: adapter.admit(self.caller, self.target, notification))
        self.assertEqual(self.mailbox.admissions, [])
        self.assertEqual(self.receipts.rows, {})

    def test_expiry_is_rechecked_after_entering_the_guard(self):
        adapter = self.wake_adapter()
        self.gate.before_yield = lambda: setattr(self, "now", 220.0)
        self.assert_refused(RefusalCode.EXPIRED, lambda: adapter.admit(
            self.caller, self.target, replace(self.notification, intent="wake"),
        ))
        self.assertEqual(self.mailbox.admissions, [])
        self.assertEqual(self.receipts.rows, {})

    def test_changed_payload_or_target_cannot_reuse_a_notification_uuid(self):
        self.adapter.admit(self.caller, self.target, self.notification)
        for changes in ({"text": self.notification.text + " "}, {"intent": "wake"}, {"expires_at": 201.0}):
            with self.subTest(changes=changes):
                self.assert_refused(RefusalCode.CONFLICT, lambda: self.wake_adapter().admit(
                    self.caller, self.target, replace(self.notification, **changes),
                ))
        for field in ("endpoint_id", "generation", "device_id", "profile_id", "session_id", "lease_id", "live_session_id"):
            with self.subTest(field=field):
                binding = replace(self.binding, **{field: "changed"})
                adapter = self.make_adapter(registration=Registration(binding, (self.grant,), True))
                target = TargetRef(binding.endpoint_id, binding.generation)
                self.assert_refused(RefusalCode.CONFLICT, lambda: adapter.admit(self.caller, target, self.notification))
        self.assertEqual(self.mailbox.admissions, [])

    def test_cross_device_scope_needs_its_own_exact_grant_and_identity(self):
        local = self.adapter.admit(self.caller, self.target, self.notification)
        binding = replace(self.binding, scope_id="windows-scope", route_class="cross_device", device_id="windows")
        windows_caller = Caller("codex", "windows-scope")
        adapter = self.make_adapter(registration=Registration(binding, (self.grant,), True))
        self.assert_refused(RefusalCode.PERMISSION, lambda: adapter.admit(windows_caller, self.target, self.notification))
        grant = replace(self.grant, scope_id="windows-scope")
        adapter = self.make_adapter(registration=Registration(binding, (grant,), True))
        windows = adapter.admit(windows_caller, self.target, self.notification)
        self.assertNotEqual(local.delivery_id, windows.delivery_id)
        self.assertEqual(len(self.receipts.rows), 2)
        self.assert_refused(RefusalCode.FOREIGN, lambda: adapter.inspect(self.caller, self.target, self.notification.notification_id))

    def test_invalid_notification_is_refused_without_backend_access(self):
        cases = [
            {"notification_id": self.notification.notification_id.upper()},
            {"notification_id": self.notification.notification_id.replace("-", "")},
            {"notification_id": "not-a-uuid"}, {"text": " "}, {"text": "x" * 16385},
            {"text": "é" * 8193}, {"text": "bad\ud800"}, {"intent": ["wake"]},
            {"expires_at": float("nan")}, {"expires_at": float("inf")},
            {"expires_at": True}, {"expires_at": 0}, {"expires_at": 10 ** 400},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                self.assert_refused(RefusalCode.INVALID, lambda: self.adapter.admit(
                    self.caller, self.target, replace(self.notification, **changes),
                ))
        self.assertEqual(self.receipts.rows, {})
        self.assertEqual(self.mailbox.admissions, [])
        self.assertEqual(self.gate.entries, [])

    def test_uncertain_handoff_restart_and_expiry_never_trigger_a_second_dispatch(self):
        adapter = self.wake_adapter()
        notification = replace(self.notification, intent="wake")
        self.mailbox.fail_after_store = True
        arrival = adapter.admit(self.caller, self.target, notification)
        self.assertEqual(arrival.state, ReceiptState.UNKNOWN)
        restarted = self.wake_adapter()
        self.now = 240.0  # Notification expired, binding and grants still current.
        self.assertEqual(restarted.admit(self.caller, self.target, notification), arrival)
        self.assertEqual(restarted.inspect(self.caller, self.target, notification.notification_id).state, ReceiptState.QUEUED)
        self.mailbox.rows[arrival.delivery_id] = replace(self.mailbox.rows[arrival.delivery_id], state=ReceiptState.CLAIMED)
        claimed = restarted.inspect(self.caller, self.target, notification.notification_id)
        self.assertEqual(claimed.state, ReceiptState.CLAIMED)
        self.mailbox.rows.clear()
        self.assert_refused(RefusalCode.UNKNOWN, lambda: restarted.inspect(self.caller, self.target, notification.notification_id))
        self.assertEqual(restarted.admit(self.caller, self.target, notification), claimed.admission())
        self.now = 301.0
        self.assert_refused(RefusalCode.EXPIRED, lambda: restarted.admit(self.caller, self.target, notification))
        self.assertEqual(len(self.mailbox.admissions), 1)
        self.assertEqual(len(self.gate.entries), 1)

    def test_atomic_reservation_admits_only_once_across_two_competing_adapters(self):
        barrier = threading.Barrier(2, timeout=3)

        class CompetingReceipts(MemoryReceipts):
            def read(self, key):
                result = super().read(key)
                barrier.wait()
                return result

        receipts = CompetingReceipts()
        adapters = [self.wake_adapter(receipts=receipts), self.wake_adapter(receipts=receipts)]
        results, errors = [], []

        def admit(adapter):
            try:
                results.append(adapter.admit(self.caller, self.target, replace(self.notification, intent="wake")))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=admit, args=(adapter,)) for adapter in adapters]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].delivery_id, results[1].delivery_id)
        self.assertEqual(len(receipts.rows), 1)
        self.assertEqual(len(self.mailbox.admissions), 1)

    def test_storage_failure_or_capacity_refusal_cannot_dispatch(self):
        adapter = self.wake_adapter()
        for error, code in ((OSError("private path"), RefusalCode.STORAGE), (Refused(RefusalCode.CAPACITY), RefusalCode.CAPACITY)):
            with self.subTest(code=code):
                self.receipts.reserve_error = error
                self.assert_refused(code, lambda: adapter.admit(
                    self.caller, self.target, replace(self.notification, intent="wake"),
                ))
        self.assertEqual(self.mailbox.admissions, [])
        self.assertEqual(self.receipts.rows, {})

    def test_projection_write_failure_retains_uncertainty_and_inspection_recovers_without_wake(self):
        adapter = self.wake_adapter()
        notification = replace(self.notification, intent="wake")
        self.receipts.replace_error = OSError("private path")
        arrival = adapter.admit(self.caller, self.target, notification)
        self.assertEqual(arrival.state, ReceiptState.UNKNOWN)
        self.assertEqual(adapter.admit(self.caller, self.target, notification), arrival)
        self.assert_refused(RefusalCode.STORAGE, lambda: adapter.inspect(self.caller, self.target, notification.notification_id))
        self.receipts.replace_error = None
        recovered = adapter.inspect(self.caller, self.target, notification.notification_id)
        self.assertEqual(recovered.state, ReceiptState.QUEUED)
        self.assertEqual(len(self.mailbox.admissions), 1)

    def test_error_after_guard_exit_does_not_erase_dispatch_reservation(self):
        adapter = self.wake_adapter()
        notification = replace(self.notification, intent="wake")
        self.gate.exit_error = RuntimeError("private guard error")
        self.assert_refused(RefusalCode.UNKNOWN, lambda: adapter.admit(self.caller, self.target, notification))
        replay = adapter.admit(self.caller, self.target, notification)
        self.assertEqual(replay.state, ReceiptState.QUEUED)
        self.assertEqual(len(self.mailbox.admissions), 1)
        self.assertEqual(len(self.gate.entries), 1)

    def test_status_only_permission_never_enters_a_guard_or_admits_mail(self):
        notification = replace(self.notification, intent="wake")
        arrival = self.wake_adapter().admit(self.caller, self.target, notification)
        grant = replace(self.grant, permissions=frozenset({Permission.STATUS}))
        status_only = self.make_adapter(registration=Registration(self.binding, (grant,), True))
        self.gate.overrides = {"target_state": "busy", "budget_state": "unavailable"}
        self.mailbox.rows[arrival.delivery_id] = replace(self.mailbox.rows[arrival.delivery_id], state=ReceiptState.CLAIMED)
        self.assertEqual(status_only.inspect(self.caller, self.target, notification.notification_id).state, ReceiptState.CLAIMED)
        self.assertEqual(len(self.gate.entries), 1)
        self.assertEqual(len(self.mailbox.admissions), 1)
        self.assertEqual(self.mailbox.reads, [arrival.delivery_id])

    def test_missing_or_foreign_mailbox_receipt_never_leaks_a_result_or_repeats_work(self):
        adapter = self.wake_adapter()
        notification = replace(self.notification, intent="wake")
        self.mailbox.fail_after_store = True
        arrival = adapter.admit(self.caller, self.target, notification)
        valid = self.mailbox.rows[arrival.delivery_id]
        invalid = [
            None, replace(valid, delivery_id="0" * 64), replace(valid, payload_digest="0" * 64),
            replace(valid, binding=replace(self.binding, live_session_id="different")),
            replace(valid, state=ReceiptState.STORED), replace(valid, state=ReceiptState.UNKNOWN),
            replace(valid, state="settled", result="private result from an invalid receipt"),
            replace(valid, state=ReceiptState.SETTLED, result=None),
        ]
        for receipt in invalid:
            with self.subTest(receipt=receipt):
                self.mailbox.rows[arrival.delivery_id] = receipt
                self.assert_refused(RefusalCode.UNKNOWN, lambda: adapter.inspect(self.caller, self.target, notification.notification_id))
                self.assertEqual(adapter.admit(self.caller, self.target, notification).state, ReceiptState.UNKNOWN)
        self.mailbox.read_error = OSError("private read failure")
        self.assert_refused(RefusalCode.UNKNOWN, lambda: adapter.inspect(self.caller, self.target, notification.notification_id))
        self.assertEqual(len(self.mailbox.admissions), 1)

    def test_claim_never_regresses_and_terminal_result_is_bounded_and_immutable(self):
        adapter = self.wake_adapter()
        notification = replace(self.notification, intent="wake")
        arrival = adapter.admit(self.caller, self.target, notification)
        queued = self.mailbox.rows[arrival.delivery_id]
        self.mailbox.rows[arrival.delivery_id] = replace(queued, state=ReceiptState.CLAIMED)
        claimed = adapter.inspect(self.caller, self.target, notification.notification_id)
        self.mailbox.rows[arrival.delivery_id] = queued
        self.assertEqual(adapter.inspect(self.caller, self.target, notification.notification_id), claimed)
        self.mailbox.rows[arrival.delivery_id] = replace(queued, state=ReceiptState.SETTLED, result="é" * 9000)
        settled = adapter.inspect(self.caller, self.target, notification.notification_id)
        self.assertEqual(settled.result, "é" * 8192)
        self.assertTrue(settled.result_truncated)
        reads = list(self.mailbox.reads)
        self.mailbox.rows[arrival.delivery_id] = replace(queued, state=ReceiptState.FAILED, result="changed")
        self.assertEqual(adapter.inspect(self.caller, self.target, notification.notification_id), settled)
        self.assertEqual(self.mailbox.reads, reads)
        self.assertEqual(adapter.admit(self.caller, self.target, notification), settled.admission())
        self.assertEqual(len(self.mailbox.admissions), 1)

    def test_malformed_admission_receipt_does_not_poison_the_unknown_reservation(self):
        self.mailbox.transform = lambda receipt: replace(receipt, state="queued")
        notification = replace(self.notification, intent="wake")
        adapter = self.wake_adapter()
        arrival = adapter.admit(self.caller, self.target, notification)
        self.assertEqual(arrival.state, ReceiptState.UNKNOWN)
        key = self.caller.principal_id, self.caller.scope_id, notification.notification_id
        self.assertIs(self.receipts.rows[key].state, ReceiptState.UNKNOWN)
        self.assertEqual(adapter.inspect(self.caller, self.target, notification.notification_id).state, ReceiptState.QUEUED)
        self.assertEqual(len(self.mailbox.admissions), 1)

    def test_invalid_utf8_result_is_unknown_not_a_corrupt_terminal_receipt(self):
        adapter = self.wake_adapter()
        notification = replace(self.notification, intent="wake")
        arrival = adapter.admit(self.caller, self.target, notification)
        self.mailbox.rows[arrival.delivery_id] = replace(
            self.mailbox.rows[arrival.delivery_id], state=ReceiptState.SETTLED, result="bad\ud800",
        )
        self.assert_refused(RefusalCode.UNKNOWN, lambda: adapter.inspect(self.caller, self.target, notification.notification_id))
        self.assertEqual(adapter.admit(self.caller, self.target, notification).state, ReceiptState.QUEUED)
        self.assertEqual(len(self.mailbox.admissions), 1)

    def test_inconsistent_stored_receipt_cannot_leak_a_result_or_upgrade_evidence(self):
        self.adapter.admit(self.caller, self.target, self.notification)
        key = self.caller.principal_id, self.caller.scope_id, self.notification.notification_id
        valid = self.receipts.rows[key]
        for changes in ({"result": "foreign output"}, {"result_truncated": True},
                        {"evidence_kind": "actual-session"}, {"provider_budget_enforcement": "verified"}):
            with self.subTest(changes=changes):
                self.receipts.rows[key] = replace(valid, **changes)
                self.assert_refused(RefusalCode.STORAGE, lambda: self.adapter.inspect(
                    self.caller, self.target, self.notification.notification_id,
                ))
        self.assertEqual(self.mailbox.reads, [])

    def test_compare_and_swap_preserves_a_competing_newer_terminal_receipt(self):
        adapter = self.wake_adapter()
        notification = replace(self.notification, intent="wake")
        arrival = adapter.admit(self.caller, self.target, notification)
        self.mailbox.rows[arrival.delivery_id] = replace(self.mailbox.rows[arrival.delivery_id], state=ReceiptState.CLAIMED)

        def concurrent_completion(key, expected, proposed):
            self.receipts.rows[key] = replace(expected, state=ReceiptState.SETTLED, result="first terminal result")

        self.receipts.before_replace = concurrent_completion
        final = adapter.inspect(self.caller, self.target, notification.notification_id)
        self.assertEqual(final.state, ReceiptState.SETTLED)
        self.assertEqual(final.result, "first terminal result")
        self.assertEqual(len(self.mailbox.admissions), 1)

    def test_compare_and_swap_cannot_substitute_a_different_payload_under_the_same_uuid(self):
        adapter = self.wake_adapter()
        notification = replace(self.notification, intent="wake")
        arrival = adapter.admit(self.caller, self.target, notification)
        self.mailbox.rows[arrival.delivery_id] = replace(self.mailbox.rows[arrival.delivery_id], state=ReceiptState.CLAIMED)
        changed = replace(notification, text="another payload")
        serialized = json.dumps({"text": changed.text, "intent": changed.intent, "expires_at": changed.expires_at},
                                sort_keys=True, separators=(",", ":"), ensure_ascii=False)

        def corrupt_compare_and_swap(key, expected, proposed):
            self.receipts.rows[key] = replace(expected, notification=changed,
                                             payload_digest=hashlib.sha256(serialized.encode()).hexdigest())

        self.receipts.before_replace = corrupt_compare_and_swap
        self.assert_refused(RefusalCode.CONFLICT, lambda: adapter.inspect(self.caller, self.target, notification.notification_id))
        self.assertEqual(len(self.mailbox.admissions), 1)

    def test_equivalent_timestamp_serialization_preserves_stable_identity(self):
        arrival = self.adapter.admit(self.caller, self.target, self.notification)
        binding = replace(self.binding, expires_at=300)
        adapter = self.make_adapter(registration=Registration(binding, (self.grant,), True))
        replay = adapter.admit(self.caller, self.target, replace(self.notification, expires_at=200))
        self.assertEqual(replay, arrival)

    def test_non_boolean_storage_creation_flag_cannot_authorize_dispatch(self):
        class MalformedStore(MemoryReceipts):
            def reserve(self, key, receipt):
                _, actual = super().reserve(key, receipt)
                return "false", actual

        adapter = self.wake_adapter(receipts=MalformedStore())
        self.assert_refused(RefusalCode.STORAGE, lambda: adapter.admit(
            self.caller, self.target, replace(self.notification, intent="wake"),
        ))
        self.assertEqual(self.mailbox.admissions, [])

    def test_compare_and_swap_cannot_regress_a_claim(self):
        adapter = self.wake_adapter()
        notification = replace(self.notification, intent="wake")
        arrival = adapter.admit(self.caller, self.target, notification)
        self.mailbox.rows[arrival.delivery_id] = replace(self.mailbox.rows[arrival.delivery_id], state=ReceiptState.CLAIMED)
        adapter.inspect(self.caller, self.target, notification.notification_id)
        self.mailbox.rows[arrival.delivery_id] = replace(self.mailbox.rows[arrival.delivery_id], state=ReceiptState.SETTLED)

        def regressing_compare_and_swap(key, expected, proposed):
            self.receipts.rows[key] = replace(expected, state=ReceiptState.UNKNOWN)

        self.receipts.before_replace = regressing_compare_and_swap
        self.assert_refused(RefusalCode.STORAGE, lambda: adapter.inspect(self.caller, self.target, notification.notification_id))
        self.assertEqual(len(self.mailbox.admissions), 1)


if __name__ == "__main__":
    unittest.main()
