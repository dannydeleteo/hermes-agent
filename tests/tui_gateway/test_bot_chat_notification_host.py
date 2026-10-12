"""Actual dispatcher/owner guard/profile scope/SQLite; supplied owner/auth metadata.

These are isolated source tests, NOT a real WebSocket, native owner, installation,
Mac durability, provider-budget or transcript-delivery qualification.
"""
from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
import threading
from types import SimpleNamespace
import uuid

import pytest

from gateway.bot_chat_notification_store import SQLiteReceiptStore
from gateway.bot_chat_notifications import Caller, Notification, Refused, RefusalCode, TargetRef
from hermes_cli.active_sessions import ActiveSessionLease
from tui_gateway import bot_chat_notification_host as host_module
from tui_gateway import server
from tui_gateway.bot_chat_notification_host import OwnerAdmission, observe_owner, prepare_owner


def _forbidden(*args, **kwargs):
    raise AssertionError("notification RPC must not execute, discover, or fetch usage")


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from tools import bot_live_delivery as mailbox
    from tui_gateway import launch_profile_policy

    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_hermes_home", tmp_path / "a")
    monkeypatch.setattr(launch_profile_policy, "_snapshot", None)
    monkeypatch.setattr("agent.secret_scope._MULTIPLEX_ACTIVE", False)
    for name in ("_run_prompt_submit", "_make_agent", "_ensure_active_session_slot", "_get_usage"):
        monkeypatch.setattr(server, name, _forbidden)
    for name in ("find_canonical_live_owner", "find_canonical_owner", "deliver_to_live_owner",
                 "claim_pending_delivery", "read_delivery_result"):
        monkeypatch.setattr(mailbox, name, _forbidden)
    clock = [1000.0]
    rows = {}
    for name in ("a", "b"):
        home = tmp_path / name
        home.mkdir(mode=0o700)
        (home / "config.yaml").write_text("terminal:\n  backend: local\n", encoding="utf-8")
        (home / ".env").write_text(f"NOTIFICATION_SCOPE_KEY={name}\n", encoding="utf-8")
        directory = home / "runtime" / "bot_chat_notifications"
        directory.mkdir(mode=0o700, parents=True)
        SQLiteReceiptStore(directory, create=True, capacity=8)
        transport = SimpleNamespace(auth_identity={"provider": "test", "user_id": "owner"},
                                    write=lambda frame: True)
        sid, key = f"live-{name}", f"chat-{name}"
        lease = ActiveSessionLease(lease_id=f"lease-{name}", session_id=key, surface="desktop")
        session = dict(session_key=key, profile_home=str(home), transport=transport,
                       history_lock=threading.RLock(), active_session_lease=lease, agent=object())
        server._sessions[sid] = session
        principal = server._methods_browser_control._principal_digest(transport.auth_identity)
        admission = OwnerAdmission(sid, key, lease.lease_id, home, "device-test", name,
                                   f"scope-{name}", principal, 1500.0)
        with server._session_turn_admission(session) as admitted:
            assert admitted
            host = prepare_owner(session, admission, clock=lambda: clock[0])
        owner = dict(profile_home=str(home), session_id=key, lease_id=lease.lease_id, live_session_id=sid)
        rows[name] = SimpleNamespace(sid=sid, session=session, transport=transport, host=host,
                                     directory=directory, home=home, owner=owner, principal=principal)

    def call(name, operation, *, via=None, **params):
        row = rows[name]
        transport = via or row.transport
        completed, frames = threading.Event(), []
        original_write = transport.write

        def write(frame):
            frames.append(frame)
            completed.set()
            return True

        transport.write = write
        try:
            # Only the RPC is forbidden to hydrate. The repository's fixture
            # teardown restores unrelated profile caches after this call ends.
            with monkeypatch.context() as request_patch:
                request_patch.setattr("hermes_cli.env_loader.hydrate_profile_secret_sources", _forbidden)
                response = server.dispatch({"id": "receipt-test", "method": f"bot_notifications.{operation}",
                                            "params": {"session_id": row.sid, **params}}, transport=transport)
                if response is not None:
                    return response
                assert completed.wait(10), "notification RPC did not return"
                return frames[-1]
        finally:
            transport.write = original_write

    def register(name="a"):
        row = rows[name]
        params = dict(owner_opt_in=True, expires_at=1400.0)
        if row.host.requested is None:
            assert call(name, "register", **params)["error"]["data"]["reason"] == "unavailable"
        with server._session_turn_admission(row.session):
            observe_owner(row.sid, row.session, row.owner)
        return call(name, "register", **params)["result"]["target"]

    yield SimpleNamespace(rows=rows, clock=clock, call=call, register=register)


def _note(target, text="A retained source needs review."):
    return dict(target=target, notification_id=str(uuid.uuid4()), text=text, expires_at=1300.0)


def _reason(response):
    return response["error"]["data"]["reason"]


def test_register_store_status_reopen_and_replay(runtime):
    r = runtime
    target = r.register()
    note = _note(target)
    stored = r.call("a", "store", **note)["result"]
    assert stored["text"] == note["text"]
    assert stored["receipt"]["state"] == "stored"
    assert stored["meaning"] == "notification_storage_only"
    assert stored["receipt"]["provider_budget_enforcement"] == "unverified"
    assert stored["receipt"]["evidence_kind"] == "injected-contract"
    r.rows["a"].host.connection.receipts = SQLiteReceiptStore(r.rows["a"].directory)
    status = r.call("a", "status", target=target, notification_id=note["notification_id"])
    assert status["result"] == stored
    assert r.call("a", "store", **note)["result"] == stored
    assert r.register() == target  # repeated enrollment does not remint the generation
    assert not (r.rows["a"].home / "runtime" / "bot_live_delivery").exists()
    assert not (r.rows["a"].home / "state.db").exists()


def test_missing_host_is_unavailable_and_never_initializes_storage(runtime):
    row = runtime.rows["a"]
    row.session.pop("_bot_chat_notification_host")
    before = set(row.home.rglob("*"))
    assert _reason(runtime.call("a", "register", owner_opt_in=True, expires_at=1400.0)) == "unavailable"
    assert set(row.home.rglob("*")) == before


def test_missing_ledger_requires_installation_not_automatic_repair(runtime):
    row = runtime.rows["a"]
    retained = row.home / "retained-receipts"
    row.directory.rename(retained)
    params = dict(owner_opt_in=True, expires_at=1400.0)
    assert _reason(runtime.call("a", "register", **params)) == "unavailable"
    observe_owner(row.sid, row.session, row.owner)
    assert _reason(runtime.call("a", "register", **params)) == "storage_unavailable"
    assert not row.directory.exists()
    assert (retained / "receipts.sqlite3").is_file()


def test_launch_profile_none_uses_only_the_explicit_admitted_home(runtime, monkeypatch):
    row = runtime.rows["a"]
    monkeypatch.setenv("HERMES_HOME", str(row.home))
    row.session["profile_home"] = None  # normal launch-profile session representation
    row.session.pop("_bot_chat_notification_host")
    with server._session_turn_admission(row.session):
        row.host = prepare_owner(row.session, row.host.admission, clock=lambda: runtime.clock[0])
    target = runtime.register()
    assert runtime.call("a", "store", **_note(target))["result"]["receipt"]["state"] == "stored"


def test_registration_requires_opt_in_then_current_owner_metadata(runtime):
    r, row = runtime, runtime.rows["a"]
    assert _reason(r.call("a", "register", owner_opt_in=False, expires_at=1400.0)) == "permission_denied"
    observe_owner(row.sid, row.session, row.owner)
    assert row.host.observed_at is None  # no volunteer before the explicit request
    assert _reason(r.call("a", "register", owner_opt_in=True, expires_at=1400.0)) == "unavailable"
    observe_owner(row.sid, row.session, {**row.owner, "lease_id": "other"})
    assert row.host.observed_at is None
    assert r.register()


def test_existing_poller_volunteers_without_additional_owner_lookup(runtime, monkeypatch):
    from tools import bot_live_delivery as mailbox
    r, row = runtime, runtime.rows["a"]
    assert _reason(r.call("a", "register", owner_opt_in=True, expires_at=1400.0)) == "unavailable"
    # This test supplies controlled owner metadata to the EXISTING native lookup.
    # No private registry/DB is opened and the native queue has no delivery.
    (row.home / "runtime" / "bot_live_delivery").mkdir(mode=0o700)
    observed = []
    monkeypatch.setattr(mailbox, "find_canonical_live_owner", lambda home: observed.append(home) or row.owner)
    monkeypatch.setattr(mailbox, "claim_pending_delivery", lambda *args: None)
    assert server._poll_bot_live_delivery_once(row.sid, row.session) is False
    assert observed == [row.home]
    assert r.call("a", "register", owner_opt_in=True, expires_at=1400.0)["result"]["wake_enabled"] is False
    monkeypatch.setattr(mailbox, "find_canonical_live_owner", lambda home: None)
    assert server._poll_bot_live_delivery_once(row.sid, row.session) is False
    assert row.host.observed_at is None


@pytest.mark.parametrize("identity", [None, {"provider": "test", "user_id": ""},
                                     {"provider": "test", "user_id": "stranger"}])
def test_missing_or_foreign_identity_cannot_enroll(runtime, identity):
    row = runtime.rows["a"]
    row.transport.auth_identity = identity
    assert "error" in runtime.call("a", "register", owner_opt_in=True, expires_at=1400.0)
    assert row.host.requested is None


def test_internal_identity_cannot_enroll(runtime):
    from hermes_cli.dashboard_auth.ws_tickets import INTERNAL_PROVIDER, INTERNAL_USER_ID
    runtime.rows["a"].transport.auth_identity = {"provider": INTERNAL_PROVIDER, "user_id": INTERNAL_USER_ID}
    assert "error" in runtime.call("a", "register", owner_opt_in=True, expires_at=1400.0)


def test_attached_mirror_is_not_the_enrolled_transport(runtime):
    from tui_gateway.transport import FanoutTransport
    target = runtime.register()
    row = runtime.rows["a"]
    mirror = SimpleNamespace(auth_identity=dict(row.transport.auth_identity), write=lambda frame: True)
    row.session["transport"] = FanoutTransport(row.transport, mirror)
    assert _reason(runtime.call("a", "store", via=mirror, **_note(target))) == RefusalCode.FOREIGN.value
    row.session["transport"] = mirror
    assert "error" in runtime.call("a", "store", **_note(target))


@pytest.mark.parametrize("mutation", ["lease", "missing", "released", "borrowed", "disabled", "session", "home", "closing"])
def test_changed_owner_fails_closed(runtime, mutation):
    target = runtime.register()
    row = runtime.rows["a"]
    session, lease = row.session, row.session["active_session_lease"]
    if mutation == "lease":
        session["active_session_lease"] = replace(lease)
    elif mutation == "missing":
        session.pop("active_session_lease")
    elif mutation == "released":
        lease.released = True
    elif mutation == "borrowed":
        lease.lease_id = "borrowed:lease"
    elif mutation == "disabled":
        lease.enabled = False
    elif mutation == "session":
        session["session_key"] = "new-compression-tip"
    elif mutation == "home":
        session["profile_home"] = str(runtime.rows["b"].home)
    else:
        session["_closing"] = True
    assert _reason(runtime.call("a", "store", **_note(target))) == RefusalCode.STALE.value


def test_replaced_session_cannot_reuse_host(runtime):
    target = runtime.register()
    row = runtime.rows["a"]
    server._sessions[row.sid] = dict(row.session)
    assert _reason(runtime.call("a", "store", **_note(target))) == RefusalCode.STALE.value


def test_current_mapping_is_rechecked_after_actual_owner_guard(runtime, monkeypatch):
    target = runtime.register()
    row = runtime.rows["a"]
    original = server._session_turn_admission

    @contextmanager
    def retiring(session):
        with original(session) as admitted:
            assert session["history_lock"]._is_owned()
            server._sessions[row.sid] = dict(session)
            yield admitted

    monkeypatch.setattr(server, "_session_turn_admission", retiring)
    assert _reason(runtime.call("a", "store", **_note(target))) == RefusalCode.STALE.value


@pytest.mark.parametrize("elapsed", [30.0, -1.0])
def test_stale_or_future_owner_observation_is_not_authority(runtime, elapsed):
    target = runtime.register()
    runtime.clock[0] += elapsed
    assert _reason(runtime.call("a", "store", **_note(target))) == "unavailable"


def test_grant_expiry_retains_receipt_without_extending_access(runtime):
    target = runtime.register()
    note = _note(target)
    assert "result" in runtime.call("a", "store", **note)
    runtime.clock[0] = 1400.0
    row = runtime.rows["a"]
    observe_owner(row.sid, row.session, row.owner)
    assert _reason(runtime.call("a", "status", target=target, notification_id=note["notification_id"])) == "expired"
    assert row.host.connection.receipts.read((row.principal, "scope-a", note["notification_id"])) is not None


def test_retarget_changed_payload_and_unknown_status(runtime):
    target = runtime.register()
    note = _note(target)
    assert "result" in runtime.call("a", "store", **note)
    assert _reason(runtime.call("a", "store", **{**note, "text": "different"})) == "replay_conflict"
    assert _reason(runtime.call("a", "store", **{**note, "target": {**target, "generation": "other"}})) == RefusalCode.STALE.value
    assert _reason(runtime.call("a", "status", target=target, notification_id=str(uuid.uuid4()))) == "not_found"


@pytest.mark.parametrize("field,value", [("profile", "b"), ("principal_id", "owner"),
                                         ("intent", "wake"), ("path", "/private/foreign")])
def test_no_client_selected_authority_or_wake(runtime, field, value):
    note = _note(runtime.register())
    assert runtime.call("a", "store", **{**note, field: value})["error"]["code"] == 4000


@pytest.mark.parametrize("change", [{"text": ""}, {"expires_at": float("inf")},
                                    {"expires_at": True}, {"notification_id": "not-a-uuid"},
                                    {"text": "\N{SNOWMAN}" * 6000}])
def test_invalid_notification_is_refused(runtime, change):
    note = _note(runtime.register())
    assert "error" in runtime.call("a", "store", **{**note, **change})


def test_wrapper_and_dependencies_cannot_wake(runtime):
    target = runtime.register()
    row = runtime.rows["a"]
    wrapper = row.host.connection
    with pytest.raises(Refused) as refused:
        wrapper.admit(Caller(row.principal, "scope-a"), TargetRef(**target),
                      Notification(str(uuid.uuid4()), "not a prompt", "wake", 1300.0))
    assert refused.value.code == RefusalCode.PERMISSION
    with pytest.raises(Refused):
        with wrapper.admission_gate.hold(None):
            pytest.fail("wake permit must not exist")
    assert runtime.call("a", "wake")["error"]["code"] == -32601


def test_two_profile_homes_are_bound_a_b_a_without_ambient_leak(runtime, monkeypatch):
    from agent.secret_scope import get_secret, set_multiplex_active
    from hermes_constants import get_hermes_home
    from tools.terminal_scope import get_terminal_scope

    r = runtime
    monkeypatch.setenv("NOTIFICATION_SCOPE_KEY", "ambient-not-a-profile")
    before = dict(os.environ)
    set_multiplex_active(True)
    targets = {name: r.register(name) for name in ("a", "b")}
    observed = []
    original = host_module.request

    def scoped(*args, **kwargs):
        assert threading.current_thread() is not threading.main_thread()
        assert get_terminal_scope() is not None
        assert args[2]["history_lock"]._is_owned()
        observed.append((str(get_hermes_home()), get_secret("NOTIFICATION_SCOPE_KEY")))
        return original(*args, **kwargs)

    monkeypatch.setattr(host_module, "request", scoped)
    same_uuid = str(uuid.uuid4())
    try:
        for name in ("a", "b", "a"):
            note = {**_note(targets[name], f"{name}-only"), "notification_id": same_uuid}
            assert r.call(name, "store", **note)["result"]["text"] == f"{name}-only"
        assert observed == [(str(r.rows[n].home), n) for n in ("a", "b", "a")]
        assert "error" in r.call("b", "status", target=targets["a"], notification_id=same_uuid)
        assert r.call("a", "status", target=targets["a"], notification_id=same_uuid)["result"]["text"] == "a-only"
        assert dict(os.environ) == before
    finally:
        set_multiplex_active(False)
