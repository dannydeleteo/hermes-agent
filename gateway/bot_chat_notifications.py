"""Disconnected, injected notification seam for a volunteered canonical Bot Chat.

No runtime import, path discovery, HTTP route or production dependency is wired here.
The host must authenticate Caller outside message content and construct Registration
from the owner's exact opt-in. It must retain receipt keys across binding generations.
Only TargetRef is message-supplied; native lease/session identity stays Hermes-owned.

The future mailbox adapter wraps tools.bot_live_delivery's admit/read operations;
it resolves profile_home privately and checks native receipts before projecting them.
AdmissionGate must hold the real owner's turn guard and enforce/reserve the approved
budget/capacity through claim. A supplied 'reserved' permit is NOT provider enforcement.
This slice proves injected policy only, not authentication, disk durability, actual
session delivery, provider budgets or runtime safety. No production gate exists here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import Enum
import hashlib
import json
import math
import re
from typing import Callable, ContextManager, Protocol
import uuid


class Permission(str, Enum):
    STORE = "store"
    WAKE = "wake"
    STATUS = "status"


class ReceiptState(str, Enum):
    STORED = "stored"
    NOT_SUBMITTED = "not_submitted"
    UNKNOWN = "unknown"
    QUEUED = "queued"
    CLAIMED = "claimed"
    SETTLED = "settled"
    FAILED = "failed"
    CANCELLED = "cancelled"
    AMBIGUOUS = "ambiguous"


class RefusalCode(str, Enum):
    INVALID = "invalid_request"
    UNREGISTERED = "unregistered"
    STALE = "stale_binding"
    FOREIGN = "foreign_scope"
    PERMISSION = "permission_denied"
    EXPIRED = "expired"
    NONDURABLE = "nondurable"
    STORAGE = "storage_unavailable"
    CONFLICT = "replay_conflict"
    NOT_FOUND = "not_found"
    BUSY = "busy"
    APPROVAL = "approval_wait"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
    CAPACITY = "capacity_blocked"
    USAGE = "usage_blocked"
    BUDGET = "budget_unavailable"


class Refused(RuntimeError):
    """Typed pre-admission refusal; exception text never includes backend/private data."""
    def __init__(self, code: RefusalCode):
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True)
class Caller:
    principal_id: str
    scope_id: str


@dataclass(frozen=True)
class TargetRef:
    endpoint_id: str
    generation: str


@dataclass(frozen=True)
class EndpointBinding:
    endpoint_id: str
    generation: str
    scope_id: str
    route_class: str
    device_id: str
    profile_id: str
    session_id: str
    lease_id: str
    live_session_id: str
    expires_at: float


@dataclass(frozen=True)
class Grant:
    principal_id: str
    scope_id: str
    permissions: frozenset[Permission]
    expires_at: float


@dataclass(frozen=True)
class Registration:
    binding: EndpointBinding
    grants: tuple[Grant, ...]
    owner_opt_in: bool = False


@dataclass(frozen=True)
class Notification:
    notification_id: str
    text: str
    intent: str
    expires_at: float


@dataclass(frozen=True)
class Envelope:
    caller: Caller
    binding: EndpointBinding
    notification: Notification
    payload_digest: str
    delivery_id: str

    @property
    def key(self) -> tuple[str, str, str]:
        # Exclude target/generation: retargeting an uncertain UUID must conflict.
        return self.caller.principal_id, self.caller.scope_id, self.notification.notification_id


@dataclass(frozen=True)
class AdmissionReceipt:
    notification_id: str
    delivery_id: str
    payload_digest: str
    generation: str
    state: ReceiptState
    arrived_at: float
    updated_at: float
    evidence_kind: str = "injected-contract"
    provider_budget_enforcement: str = "unverified"


@dataclass(frozen=True)
class Receipt:
    caller: Caller
    binding: EndpointBinding
    notification: Notification
    payload_digest: str
    delivery_id: str
    state: ReceiptState
    arrived_at: float
    updated_at: float
    result: str = ""
    result_truncated: bool = False
    # Deliberately not 'actual-session': an injected receipt alone proves neither.
    evidence_kind: str = "injected-contract"
    provider_budget_enforcement: str = "unverified"

    @property
    def notification_id(self) -> str:
        return self.notification.notification_id

    @property
    def intent(self) -> str:
        return self.notification.intent

    def admission(self) -> AdmissionReceipt:
        return AdmissionReceipt(
            self.notification_id, self.delivery_id, self.payload_digest,
            self.binding.generation, self.state, self.arrived_at, self.updated_at,
        )


@dataclass(frozen=True)
class MailboxReceipt:
    delivery_id: str
    binding: EndpointBinding
    payload_digest: str
    state: ReceiptState
    result: str = ""


@dataclass(frozen=True)
class AdmissionPermit:
    binding: EndpointBinding
    payload_digest: str
    delivery_id: str
    target_state: str = "unknown"
    capacity_state: str = "unknown"
    usage_state: str = "unknown"
    budget_state: str = "unavailable"
    reservation_id: str = ""


class ReceiptStore(Protocol):
    """Trusted store: atomic durable reserve/CAS, retained across adapter restarts.

    reserve returns (created, actual_receipt). It may not forget an uncertain or
    claimed record, or prune terminal deduplication keys into permission to rerun.
    It must enforce bounded receipt capacity atomically; fail before dispatch if full.
    NOT_SUBMITTED is a retained local non-submission tombstone, never cancellation
    of native work or permission to reuse/remint the notification UUID.
    Implementations may raise Refused(CAPACITY), or fail with STORAGE. No live store
    implementation is supplied in this source-only slice.
    """
    durable: bool

    def read(self, key: tuple[str, str, str]) -> Receipt | None: ...
    def reserve(self, key: tuple[str, str, str], receipt: Receipt) -> tuple[bool, Receipt]: ...
    def replace(self, key: tuple[str, str, str], expected: Receipt, receipt: Receipt) -> Receipt: ...


class Mailbox(Protocol):
    """Trusted synchronous submitter; no mailbox side effect before the callback.

    Invoke before_submit exactly once at the actual admission/commit boundary,
    after any blocking preparation, and propagate its refusal without submitting.
    Its returned permit is bound to this delivery and must be honored through
    native claim together with expiry/owner/usage/budget checks. Do not retain the
    callback or treat it as renewable authority. No native implementation exists.
    """
    def admit(self, envelope: Envelope, *,
              before_submit: Callable[[], AdmissionPermit]) -> MailboxReceipt: ...
    def read(self, delivery_id: str) -> MailboxReceipt | None: ...


class AdmissionGate(Protocol):
    """Trusted guard held through reservation and dispatch, never acquired by status.

    The host must revalidate the exact native owner under this guard and reserve
    capacity/usage/budget for this delivery ID. A future consumer must honor that
    reservation through claim. Exiting must release unused reservations, including
    competing identical requests. This module does not implement those guarantees.
    """
    def hold(self, envelope: Envelope) -> ContextManager[AdmissionPermit]: ...
    def revalidate(self, envelope: Envelope, permit: AdmissionPermit) -> AdmissionPermit:
        """Freshly check the SAME held reservation, never acquire/renew another.

        Recheck owner, capacity, usage and budget while the original guard is held.
        The host must not echo an earlier permit as proof of current authority.
        """
        ...


_MAILBOX_TERMINAL = frozenset({ReceiptState.SETTLED, ReceiptState.FAILED,
                               ReceiptState.CANCELLED, ReceiptState.AMBIGUOUS})
_TERMINAL = _MAILBOX_TERMINAL | {ReceiptState.NOT_SUBMITTED}
_NEXT = {
    ReceiptState.UNKNOWN: _MAILBOX_TERMINAL | {ReceiptState.QUEUED, ReceiptState.CLAIMED},
    ReceiptState.QUEUED: _MAILBOX_TERMINAL | {ReceiptState.CLAIMED},
    ReceiptState.CLAIMED: _MAILBOX_TERMINAL,
}
_TARGET_REFUSALS = {
    "busy": RefusalCode.BUSY,
    "approval_wait": RefusalCode.APPROVAL,
    "unavailable": RefusalCode.UNAVAILABLE,
    "stale": RefusalCode.STALE,
}


def _identifier(value: str) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}", value) is not None


def _timestamp(value: float) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0
    except OverflowError:
        return False


def _bounded_utf8(value: str) -> bool:
    if not isinstance(value, str) or len(value) > 16384:
        return False
    try:
        return len(value.encode("utf-8")) <= 16384
    except UnicodeError:
        return False


def _result_excerpt(value: str) -> tuple[str, bool]:
    try:
        encoded = value[:16384].encode("utf-8")
    except UnicodeError:
        raise Refused(RefusalCode.UNKNOWN) from None
    excerpt = encoded[:16384].decode("utf-8", errors="ignore")
    return excerpt, len(excerpt) < len(value)


def _uuid(value: str) -> None:
    try:
        valid = isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        valid = False
    if not valid:
        raise Refused(RefusalCode.INVALID)


def _digest(value: dict) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


class BotChatNotifications:
    """Two operations over supplied dependencies; construction performs no I/O."""
    def __init__(self, *, registration: Registration, receipts: ReceiptStore,
                 mailbox: Mailbox, admission_gate: AdmissionGate, clock: Callable[[], float]):
        self.registration = registration
        self.receipts = receipts
        self.mailbox = mailbox
        self.admission_gate = admission_gate
        self.clock = clock

    def _authorize(self, caller: Caller, target: TargetRef, permissions: frozenset[Permission]) -> float:
        registration, binding = self.registration, self.registration.binding
        if registration.owner_opt_in is not True:
            raise Refused(RefusalCode.UNREGISTERED)
        if any(not _identifier(value) for key, value in asdict(binding).items() if key != "expires_at"):
            raise Refused(RefusalCode.STALE)
        now = self.clock()
        if (not _timestamp(now) or not _timestamp(binding.expires_at)
                or binding.route_class not in {"local", "cross_device"}
                or target != TargetRef(binding.endpoint_id, binding.generation)):
            raise Refused(RefusalCode.STALE)
        if now >= binding.expires_at:
            raise Refused(RefusalCode.EXPIRED)
        if not _identifier(caller.principal_id) or caller.scope_id != binding.scope_id:
            raise Refused(RefusalCode.FOREIGN)
        allowed = any(
            grant.principal_id == caller.principal_id and grant.scope_id == caller.scope_id
            and _timestamp(grant.expires_at) and now < grant.expires_at
            and permissions <= grant.permissions for grant in registration.grants
        )
        if not allowed:
            raise Refused(RefusalCode.PERMISSION)
        if self.receipts.durable is not True:
            raise Refused(RefusalCode.NONDURABLE)
        return now

    def _envelope(self, caller: Caller, notification: Notification) -> Envelope:
        _uuid(notification.notification_id)
        if (not _bounded_utf8(notification.text) or not notification.text.strip()
                or notification.intent not in ("store", "wake") or not _timestamp(notification.expires_at)):
            raise Refused(RefusalCode.INVALID)
        payload = {"text": notification.text, "intent": notification.intent, "expires_at": float(notification.expires_at)}
        binding_identity = asdict(self.registration.binding)
        binding_identity["expires_at"] = float(self.registration.binding.expires_at)
        identity = {"domain": "hermes.bot-chat-notification.v1", "notification_id": notification.notification_id,
                    "caller": asdict(caller), "binding": binding_identity}
        return Envelope(caller, self.registration.binding, notification, _digest(payload), _digest(identity))

    def _read(self, key: tuple[str, str, str]) -> Receipt | None:
        try:
            receipt = self.receipts.read(key)
        except Exception:
            raise Refused(RefusalCode.STORAGE) from None
        return None if receipt is None else self._check_receipt(receipt, Caller(key[0], key[1]), key[2])

    def _check_receipt(self, receipt: Receipt, caller: Caller, notification_id: str) -> Receipt:
        if not isinstance(receipt, Receipt) or not isinstance(receipt.notification, Notification):
            raise Refused(RefusalCode.STORAGE)
        if (receipt.caller != caller or receipt.binding != self.registration.binding
                or receipt.notification_id != notification_id):
            raise Refused(RefusalCode.CONFLICT)
        envelope = self._envelope(caller, receipt.notification)
        self._replay(receipt, envelope)
        if (not isinstance(receipt.state, ReceiptState)
                or (receipt.intent == "store") != (receipt.state == ReceiptState.STORED)
                or not _timestamp(receipt.arrived_at) or not _timestamp(receipt.updated_at)
                or receipt.updated_at < receipt.arrived_at or not _bounded_utf8(receipt.result)
                or not isinstance(receipt.result_truncated, bool)
                or receipt.evidence_kind != "injected-contract" or receipt.provider_budget_enforcement != "unverified"):
            raise Refused(RefusalCode.STORAGE)
        if receipt.state not in _MAILBOX_TERMINAL and (receipt.result or receipt.result_truncated):
            raise Refused(RefusalCode.STORAGE)
        return receipt

    @staticmethod
    def _replay(receipt: Receipt, envelope: Envelope) -> AdmissionReceipt:
        if (receipt.caller != envelope.caller or receipt.binding != envelope.binding
                or receipt.notification != envelope.notification or receipt.payload_digest != envelope.payload_digest
                or receipt.delivery_id != envelope.delivery_id):
            raise Refused(RefusalCode.CONFLICT)
        return receipt.admission()

    def _reserve(self, envelope: Envelope, state: ReceiptState, now: float) -> tuple[bool, Receipt]:
        proposed = Receipt(
            envelope.caller, envelope.binding, envelope.notification,
            envelope.payload_digest, envelope.delivery_id, state, now, now,
        )
        try:
            created, actual = self.receipts.reserve(envelope.key, proposed)
        except Refused:
            raise
        except Exception:
            raise Refused(RefusalCode.STORAGE) from None
        if not isinstance(created, bool):
            raise Refused(RefusalCode.STORAGE)
        self._check_receipt(actual, envelope.caller, envelope.notification.notification_id)
        self._replay(actual, envelope)
        if created and actual != proposed:
            raise Refused(RefusalCode.STORAGE)
        return created, actual

    @staticmethod
    def _permit(permit: AdmissionPermit, envelope: Envelope) -> None:
        if (not isinstance(permit, AdmissionPermit)
                or permit.binding != envelope.binding or permit.payload_digest != envelope.payload_digest
                or permit.delivery_id != envelope.delivery_id):
            raise Refused(RefusalCode.STALE)
        if permit.target_state != "ready":
            raise Refused(_TARGET_REFUSALS.get(permit.target_state, RefusalCode.UNKNOWN))
        for actual, expected, refusal in (
            (permit.capacity_state, "available", RefusalCode.CAPACITY),
            (permit.usage_state, "available", RefusalCode.USAGE),
            (permit.budget_state, "reserved", RefusalCode.BUDGET),
        ):
            if actual != expected:
                raise Refused(refusal)
        if not _identifier(permit.reservation_id):
            raise Refused(RefusalCode.BUDGET)

    def _project(self, receipt: Receipt, observed: MailboxReceipt, now: float) -> Receipt:
        if (not isinstance(observed, MailboxReceipt) or observed.binding != receipt.binding
                or observed.delivery_id != receipt.delivery_id or observed.payload_digest != receipt.payload_digest
                or not isinstance(observed.state, ReceiptState) or observed.state not in _NEXT[ReceiptState.UNKNOWN]
                or not isinstance(observed.result, str)):
            raise Refused(RefusalCode.UNKNOWN)
        # Status may race with another inspector. Never regress a claim or replace
        # a terminal result; the store's compare-and-swap provides the final fence.
        if observed.state not in _NEXT.get(receipt.state, frozenset()):
            return receipt
        result, truncated = _result_excerpt(observed.result) if observed.state in _TERMINAL else ("", False)
        projected = replace(receipt, state=observed.state, updated_at=max(now, receipt.updated_at),
                            result=result, result_truncated=truncated)
        key = receipt.caller.principal_id, receipt.caller.scope_id, receipt.notification_id
        try:
            actual = self.receipts.replace(key, receipt, projected)
        except Exception:
            raise Refused(RefusalCode.STORAGE) from None
        self._check_receipt(actual, receipt.caller, receipt.notification_id)
        self._replay(actual, self._envelope(receipt.caller, receipt.notification))
        if actual.state != receipt.state and actual.state not in _NEXT.get(receipt.state, frozenset()):
            raise Refused(RefusalCode.STORAGE)
        return actual

    def _not_submitted(self, receipt: Receipt) -> AdmissionReceipt:
        """Retain this UUID after a known pre-submission refusal, never cancel work.

        A failed/uncertain CAS keeps UNKNOWN. A competing verified mailbox state
        wins over this proposed local terminal state; neither case permits retry.
        """
        try:
            now = self.clock()
            if not _timestamp(now):
                now = receipt.updated_at
            proposed = replace(receipt, state=ReceiptState.NOT_SUBMITTED,
                               updated_at=max(now, receipt.updated_at))
            key = receipt.caller.principal_id, receipt.caller.scope_id, receipt.notification_id
            actual = self.receipts.replace(key, receipt, proposed)
            self._check_receipt(actual, receipt.caller, receipt.notification_id)
            self._replay(actual, self._envelope(receipt.caller, receipt.notification))
            if actual == proposed or actual.state in _NEXT.get(receipt.state, frozenset()):
                return actual.admission()
        except Exception:
            pass
        return receipt.admission()

    def _dispatch_authority(self, envelope: Envelope, target: TargetRef,
                            permit: AdmissionPermit) -> tuple[float, AdmissionPermit]:
        current = self.admission_gate.revalidate(envelope, permit)
        if self.registration.binding != envelope.binding:
            raise Refused(RefusalCode.STALE)
        # Revalidation itself may block. Read the clock/grants AFTER it returns.
        now = self._authorize(envelope.caller, target, frozenset({Permission.STORE, Permission.WAKE}))
        if now >= envelope.notification.expires_at:
            raise Refused(RefusalCode.EXPIRED)
        self._permit(current, envelope)
        if current.reservation_id != permit.reservation_id:
            raise Refused(RefusalCode.BUDGET)
        return now, current

    def _wake(self, envelope: Envelope, target: TargetRef) -> AdmissionReceipt:
        try:
            with self.admission_gate.hold(envelope) as permit:
                self._permit(permit, envelope)
                now = self._authorize(envelope.caller, target, frozenset({Permission.STORE, Permission.WAKE}))
                if now >= envelope.notification.expires_at:
                    raise Refused(RefusalCode.EXPIRED)
                created, receipt = self._reserve(envelope, ReceiptState.UNKNOWN, now)
                if not created:
                    return receipt.admission()
                # A durable reserve may block beyond any admission deadline.
                # Recheck before even invoking the wake-capable dependency.
                try:
                    now, _ = self._dispatch_authority(envelope, target, permit)
                except Exception:
                    return self._not_submitted(receipt)
                # Reserve BEFORE dispatch. Even a callback that writes and then
                # throws must never be invoked again for this notification UUID.
                boundary_open, boundary_used, submission_authorized = True, False, False
                boundary_refusal = None

                def before_submit() -> AdmissionPermit:
                    nonlocal boundary_used, submission_authorized, boundary_refusal, now
                    if not boundary_open or boundary_used:
                        raise Refused(RefusalCode.UNKNOWN)
                    boundary_used = True
                    try:
                        now, current = self._dispatch_authority(envelope, target, permit)
                    except Refused as exc:
                        boundary_refusal = exc
                        raise
                    except Exception:
                        boundary_refusal = Refused(RefusalCode.UNKNOWN)
                        raise boundary_refusal from None
                    submission_authorized = True
                    return current

                try:
                    observed = self.mailbox.admit(envelope, before_submit=before_submit)
                    if not submission_authorized:
                        raise Refused(RefusalCode.UNKNOWN)
                    receipt = self._project(receipt, observed, now)
                except Exception as exc:
                    if exc is boundary_refusal and not submission_authorized:
                        return self._not_submitted(receipt)
                    # No callback proof, a swallowed refusal, or any failure
                    # after authorization remains UNKNOWN, not cancellation.
                finally:
                    boundary_open = False
                return receipt.admission()
        except Refused:
            raise
        except Exception:
            raise Refused(RefusalCode.UNKNOWN) from None

    def admit(self, caller: Caller, target: TargetRef, notification: Notification) -> AdmissionReceipt:
        """Save arrival or request one guarded wake; replay never calls the mailbox.

        A returned state describes the retained admission, not current native
        health. The same UUID with different content/target is always a conflict.
        'unknown' means inspect this exact UUID; never mint another to retry it.
        'not_submitted' is a durable pre-submission refusal, not a native cancel.
        Neither state authorizes another dispatch of this notification.
        """
        required = frozenset({Permission.STORE, Permission.WAKE} if notification.intent == "wake" else {Permission.STORE})
        now = self._authorize(caller, target, required)
        envelope = self._envelope(caller, notification)
        existing = self._read(envelope.key)
        if existing is not None:
            return self._replay(existing, envelope)
        if now >= notification.expires_at:
            raise Refused(RefusalCode.EXPIRED)
        if notification.intent == "wake":
            return self._wake(envelope, target)
        _, receipt = self._reserve(envelope, ReceiptState.STORED, now)
        return self._replay(receipt, envelope)

    def inspect(self, caller: Caller, target: TargetRef, notification_id: str) -> Receipt:
        """Read one authorized receipt, optionally refreshing it without wake authority.

        Failed native reads raise UNKNOWN and leave the retained evidence intact.
        updated_at records a state transition, not a successful polling heartbeat.
        No result is available without STATUS; terminal excerpts may be truncated.
        """
        now = self._authorize(caller, target, frozenset({Permission.STATUS}))
        _uuid(notification_id)
        receipt = self._read((caller.principal_id, caller.scope_id, notification_id))
        if receipt is None:
            raise Refused(RefusalCode.NOT_FOUND)
        if receipt.caller != caller or receipt.binding != self.registration.binding:
            raise Refused(RefusalCode.CONFLICT)
        if receipt.state == ReceiptState.STORED or receipt.state in _TERMINAL:
            return receipt
        try:
            observed = self.mailbox.read(receipt.delivery_id)
        except Exception:
            raise Refused(RefusalCode.UNKNOWN) from None
        return self._project(receipt, observed, now)
