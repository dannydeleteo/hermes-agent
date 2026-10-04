"""Opt-in admission for explicitly enrolled direct Ollama routes (not locality guesses).

The coordinator is deployment-owned, shared with other participating applications.
No profile, credential, model selection, or live service is changed by this module.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
import types


class LocalModelAdmissionError(RuntimeError):
    """Terminal for this attempt: never a reason to retry or choose a paid fallback."""

    def __init__(self, code="unavailable"):
        messages = {
            "busy": "Another local model request is still running. No new request was sent.",
            "recovery_required": "Local model completion is uncertain. New local work is paused for review; no cloud fallback was used.",
            "unavailable": "Local model admission could not be verified. No new request was sent.",
        }
        self.code = code if code in messages else "unavailable"
        super().__init__(messages[self.code])


def find_admission_error(exc, *, client=None):
    seen, pending = set(), [exc]
    while pending and len(seen) < 32:
        current = pending.pop()
        if id(current) in seen or not isinstance(current, BaseException):
            continue
        seen.add(id(current))
        if isinstance(current, LocalModelAdmissionError):
            return current
        pending.extend((current.__context__, current.__cause__))
    # A caller can stop while consuming a yielded chunk. Its exception is not
    # raised by the transport, but response.close() has already recorded the
    # abandoned body's denial. Preserve only observed denial, not a guess that
    # every unusable answer or post-completion cancellation is uncertain.
    if is_guarded_client(client):
        code = client._client._transport.admission.denied
        if code is not None:
            return LocalModelAdmissionError(code)
    return None


def raise_if_admission_error(exc):
    error = find_admission_error(exc)
    if error is not None:
        raise error


def is_guarded_client(client):
    from agent.local_model_transport import LocalTransport, AsyncLocalTransport
    return isinstance(getattr(getattr(client, "_client", None), "_transport", None),
                      (LocalTransport, AsyncLocalTransport))


def guarded_client_needs_rebuild(client):
    """A failed attempt stays fenced; a later intentional call may build a new client.

    Never close a surviving worker here. Its references/body still own cleanup.
    """
    return is_guarded_client(client) and client._client._transport.admission.denied is not None


def terminal_admission_error(exc, client):
    """Also fence watchdog errors synthesized before the physical worker unwinds."""
    error = find_admission_error(exc)
    if error is not None:
        return error
    if is_guarded_client(client):
        return LocalModelAdmissionError("recovery_required")
    return None


def _canonical_endpoint(value):
    import httpx
    if not isinstance(value, str):
        raise ValueError("endpoint must be text")
    parsed = httpx.URL(value)
    if (parsed.scheme != "http" or parsed.host not in {"127.0.0.1", "::1"}
            or parsed.userinfo or parsed.query or parsed.fragment
            or parsed.raw_path.rstrip(b"/") != b"/v1"):
        raise ValueError("only explicit direct loopback Ollama endpoints are supported")
    return str(parsed).rstrip("/")


def _selected_policy(base_url):
    import httpx
    try:
        candidate = httpx.URL(str(base_url))
    except (httpx.InvalidURL, ValueError):
        return None
    # Invalid local settings must not alter an unrelated cloud/subscription route.
    if candidate.scheme != "http" or candidate.host not in {"127.0.0.1", "::1"}:
        return None
    try:
        from hermes_cli.config import FailedConfigRead, load_config_readonly
        config = load_config_readonly()
        if isinstance(config, FailedConfigRead):
            raise ValueError("cannot establish enrollment from an unreadable config")
        cfg = config.get("local_model_admission")
    except Exception:
        raise LocalModelAdmissionError() from None
    if cfg is None or (isinstance(cfg, dict) and cfg.get("enabled") is False):
        return None
    try:
        if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
            raise ValueError("explicit boolean enablement required")
        routes = cfg["routes"]
        if not isinstance(routes, list) or not routes:
            raise ValueError("explicit routes required")
        selected = None
        endpoints = set()
        for route in routes:
            endpoint = _canonical_endpoint(route["base_url"])
            configured = httpx.URL(endpoint)
            if (configured.scheme, configured.host, configured.port) != (candidate.scheme, candidate.host, candidate.port):
                continue
            # Same backend with a query, alternate path, or unsupported wire is not
            # an unrelated route; it must not get an unguarded client.
            if _canonical_endpoint(str(candidate)) != endpoint:
                raise ValueError("unsupported path on enrolled backend")
            models = route["models"]
            native_chat = route.get("allow_native_chat", False)
            if (route.get("protocol") != "ollama-openai-v1" or endpoint in endpoints
                    or type(native_chat) is not bool
                    or not isinstance(models, list) or not models
                    or any(not isinstance(model, str) or not model.strip() for model in models)):
                raise ValueError("invalid route")
            endpoints.add(endpoint)
            selected = (endpoint, frozenset(models), native_chat)
        if selected is None:
            return None
        source, root, digest = cfg["coordinator_source"], cfg["lane_root"], cfg["coordinator_sha256"]
        for path in (source, root):
            if not isinstance(path, str) or not Path(path).is_absolute() or str(Path(path).resolve()) != path:
                raise ValueError("canonical absolute paths required")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("reviewed source digest required")
        return _Policy(selected[0], selected[1], source, digest, root, selected[2])
    except (KeyError, TypeError, ValueError, OSError, httpx.InvalidURL):
        raise LocalModelAdmissionError() from None


def validate_local_api_mode(base_url, api_mode):
    if _selected_policy(base_url) is not None and api_mode not in (None, "", "chat_completions"):
        raise LocalModelAdmissionError()


def _load_coordinator(source, digest):
    """Execute checked bytes, not a second read or a timestamp-valid stale .pyc."""
    try:
        path = Path(source)
        info = path.lstat()
        if (path.resolve() != path or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid() or info.st_mode & 0o022):
            raise ValueError("untrusted coordinator source")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("coordinator changed")
        module = types.ModuleType("checked_local_model_lane")
        module.__file__ = source
        exec(compile(raw, source, "exec"), module.__dict__)
        return module
    except Exception:
        raise LocalModelAdmissionError() from None


@dataclass(frozen=True)
class _Policy:
    base_url: str
    models: frozenset[str]
    source: str
    digest: str
    root: str
    allow_native_chat: bool = False

    def acquire(self):
        module = _load_coordinator(self.source, self.digest)
        try:
            return module.LocalModelLane(Path(self.root)).acquire(owner="hermes-native")
        except module.LaneBusy:
            raise LocalModelAdmissionError("busy") from None
        except module.LaneRecoveryRequired:
            raise LocalModelAdmissionError("recovery_required") from None
        except Exception:
            raise LocalModelAdmissionError() from None


def guarded_client_kwargs(kwargs, *, async_mode=False, native_chat=False):
    """None leaves remote/unconfigured clients unchanged; selected routes fail closed.

    An existing HTTP client cannot be inspected reliably for mounts, proxy retries,
    or hidden routing. Reject injection on enrolled routes instead of bypassing it.

    Native /api/chat callers must explicitly request this separate wire and have
    allow_native_chat: true on the same enrolled /v1 backend. Unlike ordinary SDK
    clients, they never receive an unguarded fallback when enrollment is absent.
    """
    policy = _selected_policy(kwargs.get("base_url", ""))
    if native_chat and (policy is None or not policy.allow_native_chat):
        raise LocalModelAdmissionError()
    if policy is None:
        return None
    if "http_client" in kwargs:
        raise LocalModelAdmissionError()
    import httpx
    from agent.local_model_transport import LocalTransport, AsyncLocalTransport
    transport_cls = AsyncLocalTransport if async_mode else LocalTransport
    transport = transport_cls(policy, native_chat=native_chat)
    cls = httpx.AsyncClient if async_mode else httpx.Client
    client = cls(transport=transport, trust_env=False, follow_redirects=False,
                 timeout=httpx.Timeout(connect=15, read=None, write=15, pool=10))
    return {**kwargs, "http_client": client, "max_retries": 0}
