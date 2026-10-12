"""Owner-local STORE/STATUS connection; never a second chat executor.

Installation must explicitly supply OwnerAdmission while holding the real owner's
turn guard. That trusted host input is NOT authenticated by constructing this
dataclass. No runtime currently supplies it: absent admission is unavailable.
The existing consumer volunteers its already-resolved owner metadata only after
an authenticated registration request. RPCs never discover/claim a native owner.

First proof: register -> store -> status -> reopen the same store -> replay.
This is notification persistence, not transcript delivery or a model answer.
Native installation/identity qualification and any WAKE policy remain separate.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import math
from pathlib import Path
import time
import uuid

from gateway.bot_chat_notification_store import SQLiteReceiptStore
from gateway.bot_chat_notifications import (
    BotChatNotifications, Caller, EndpointBinding, Grant, Notification, Permission,
    Refused, RefusalCode, Registration, TargetRef,
)

_HOST_KEY = "_bot_chat_notification_host"
_OWNER_MAX_AGE = 30.0


def _finite(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


@dataclass(frozen=True)
class OwnerAdmission:
    """Private, installer-admitted identity. Never accepted as RPC parameters."""
    live_session_id: str
    session_id: str
    lease_id: str
    profile_home: Path
    device_id: str
    profile_id: str
    scope_id: str
    principal_id: str
    expires_at: float


class _WakeDisabled:
    @contextmanager
    def hold(self, envelope):
        raise Refused(RefusalCode.PERMISSION)
        yield  # context-manager protocol; no permit can be minted here

    def revalidate(self, envelope, permit):
        raise Refused(RefusalCode.PERMISSION)

    def admit(self, envelope, *, before_submit):
        raise Refused(RefusalCode.PERMISSION)

    def read(self, delivery_id):
        raise Refused(RefusalCode.PERMISSION)


class NotificationHost:
    """Ephemeral exact-owner registration over the ONE existing receipt ledger.

    All methods run under the owner's existing history/retirement guard. On owner
    or generation replacement, install a new host; never adopt previous receipts
    into the new binding. Trusted same-process host, not a hostile-code sandbox.
    """
    def __init__(self, session: dict, admission: OwnerAdmission, *, clock=time.time):
        if (not isinstance(admission, OwnerAdmission)
                or not isinstance(admission.profile_home, Path)
                or not admission.profile_home.is_absolute()
                or ".." in admission.profile_home.parts
                or not _finite(admission.expires_at)
                or any(not isinstance(value, str) or not value.strip() or len(value) > 256
                       for key, value in asdict(admission).items()
                       if key not in {"profile_home", "expires_at"})):
            raise Refused(RefusalCode.INVALID)
        self.session = session
        self.admission = admission
        self.lease = session.get("active_session_lease")
        self.profile_home = session.get("profile_home")
        self.clock = clock
        self.requested = None
        self.observed_at = None
        self.connection = None
        self.invalidated = False

    def _current(self, sid: str, session: dict) -> float:
        a, lease = self.admission, self.lease
        now = self.clock()
        if (self.invalidated or session is not self.session or sid != a.live_session_id
                or session.get("active_session_lease") is not lease or lease is None
                or getattr(lease, "enabled", False) is not True
                or getattr(lease, "released", True) is not False
                or getattr(lease, "lease_id", None) != a.lease_id
                or a.lease_id.startswith("borrowed:")
                or getattr(lease, "session_id", None) != a.session_id
                or session.get("session_key") != a.session_id
                or session.get("profile_home") != self.profile_home
                or self.profile_home not in (None, str(a.profile_home))
                or session.get("_closing") or session.get("_finalized")):
            self.invalidated = True
            raise Refused(RefusalCode.STALE)
        if not _finite(now) or now >= a.expires_at:
            raise Refused(RefusalCode.EXPIRED)
        return now

    def observe(self, sid: str, session: dict, owner: dict | None) -> None:
        """Receive metadata from the maintained poller's existing resolution, no I/O."""
        self.observed_at = None
        if self.requested is None:
            return
        try:
            now = self._current(sid, session)
        except Refused:
            return
        a = self.admission
        expected = dict(profile_home=str(a.profile_home), session_id=a.session_id,
                        lease_id=a.lease_id, live_session_id=a.live_session_id)
        if not isinstance(owner, dict) or any(owner.get(k) != v for k, v in expected.items()):
            return
        self.observed_at = now

    def _authority(self, sid, session, transport, principal):
        from hermes_constants import get_hermes_home

        now = self._current(sid, session)
        if (principal != self.admission.principal_id
                or Path(get_hermes_home()) != self.admission.profile_home):
            raise Refused(RefusalCode.FOREIGN)
        if self.requested is not None and self.requested[0] is not transport:
            raise Refused(RefusalCode.FOREIGN)
        return now

    def register(self, sid, session, transport, principal, *, owner_opt_in, expires_at):
        now = self._authority(sid, session, transport, principal)
        if owner_opt_in is not True:
            raise Refused(RefusalCode.PERMISSION)
        if not _finite(expires_at) or not now < expires_at <= self.admission.expires_at:
            raise Refused(RefusalCode.EXPIRED)
        request = (transport, float(expires_at))
        if self.requested is not None and self.requested != request:
            raise Refused(RefusalCode.CONFLICT)
        self.requested = request
        self._fresh(now)
        if self.connection is None:
            a = self.admission
            binding = EndpointBinding(
                endpoint_id=uuid.uuid4().hex, generation=uuid.uuid4().hex,
                scope_id=a.scope_id, route_class="local", device_id=a.device_id,
                profile_id=a.profile_id, session_id=a.session_id, lease_id=a.lease_id,
                live_session_id=a.live_session_id, expires_at=float(expires_at))
            registration = Registration(binding, (Grant(
                principal, a.scope_id, frozenset({Permission.STORE, Permission.STATUS}),
                float(expires_at)),), owner_opt_in=True)
            # Installer creates the private directory and initializes the accepted
            # ledger. Enrollment never creates/repairs files or another state.db.
            store = SQLiteReceiptStore(a.profile_home / "runtime" / "bot_chat_notifications")
            disabled = _WakeDisabled()
            self.connection = BotChatNotifications(
                registration=registration, receipts=store, mailbox=disabled,
                admission_gate=disabled, clock=self.clock)
        binding = self.connection.registration.binding
        return {"target": asdict(TargetRef(binding.endpoint_id, binding.generation)),
                "permissions": ["store", "status"], "expires_at": binding.expires_at,
                "wake_enabled": False, "meaning": "notification_storage_only"}

    def _fresh(self, now):
        if self.observed_at is None or not 0 <= now - self.observed_at < _OWNER_MAX_AGE:
            raise Refused(RefusalCode.UNAVAILABLE)

    def receipt(self, operation, sid, session, transport, principal, *, target,
                notification_id, text=None, expires_at=None):
        now = self._authority(sid, session, transport, principal)
        self._fresh(now)
        if self.connection is None:
            raise Refused(RefusalCode.UNREGISTERED)
        caller = Caller(principal, self.admission.scope_id)
        ref = TargetRef(**target)
        if operation == "store":
            notification = Notification(notification_id, text, "store", expires_at)
            receipt = self.connection.admit(caller, ref, notification)
        else:
            stored = self.connection.inspect(caller, ref, notification_id)
            receipt, notification = stored.admission(), stored.notification
        return {"receipt": asdict(receipt), "text": notification.text,
                "expires_at": notification.expires_at,
                "meaning": "notification_storage_only"}


def prepare_owner(session: dict, admission: OwnerAdmission, *, clock=time.time) -> NotificationHost:
    """Trusted installation seam; caller holds the real turn guard. No I/O/opt-in.

    A native caller/identity is still an installation dependency. RPC clients
    cannot call this seam or provide an OwnerAdmission. Never replace a live host.
    """
    if _HOST_KEY in session:
        raise Refused(RefusalCode.CONFLICT)
    host = NotificationHost(session, admission, clock=clock)
    host._current(admission.live_session_id, session)
    session[_HOST_KEY] = host
    return host


def observe_owner(sid: str, session: dict, owner: dict | None) -> None:
    host = session.get(_HOST_KEY)
    if isinstance(host, NotificationHost):
        host.observe(sid, session, owner)


def request(operation, sid, session, transport, principal, params):
    host = session.get(_HOST_KEY)
    if not isinstance(host, NotificationHost):
        raise Refused(RefusalCode.UNAVAILABLE)
    if operation == "register":
        return host.register(sid, session, transport, principal,
                             owner_opt_in=params["owner_opt_in"], expires_at=params["expires_at"])
    return host.receipt(operation, sid, session, transport, principal,
                        target=params["target"], notification_id=params["notification_id"],
                        text=params.get("text"), expires_at=params.get("expires_at"))
