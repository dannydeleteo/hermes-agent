"""Native Ollama checker wire shares ownership, not OpenAI completion syntax."""
import json

import httpx
import openai
import pytest

from tests.agent.test_local_model_admission import MODEL, URL, admission  # noqa: F401


def payload(**changes):
    return {"model": MODEL, "done": True, "done_reason": "stop",
            "message": {"role": "assistant", "content": "checked"}, **changes}


def native_request(sdk, **changes):
    return sdk._client.post(URL.removesuffix("/v1") + "/api/chat", json={
        "model": MODEL, "stream": False,
        "messages": [{"role": "user", "content": "supplied text"}], **changes})


def client(module):
    return openai.OpenAI(**module.guarded_client_kwargs(
        {"api_key": "fixture", "base_url": URL}, native_chat=True))


@pytest.mark.parametrize("setting", [None, False, "true", 1])
def test_native_wire_requires_separate_explicit_enrollment(admission, setting):
    module, lane, cfg = admission
    cfg["local_model_admission"]["routes"][0]["allow_native_chat"] = setting
    with pytest.raises(module.LocalModelAdmissionError):
        client(module)
    assert lane.acquisitions == 0


def test_native_wire_does_not_silently_run_when_guard_disabled(admission):
    module, lane, cfg = admission
    cfg["local_model_admission"]["enabled"] = False
    with pytest.raises(module.LocalModelAdmissionError):
        client(module)
    assert lane.acquisitions == 0


@pytest.mark.parametrize("reason", ["stop", "length"])
def test_physical_native_completion_releases_even_if_answer_quality_is_unusable(admission, monkeypatch, reason):
    module, lane, cfg = admission
    cfg["local_model_admission"]["routes"][0]["allow_native_chat"] = True
    dispatched = []

    def handle(request):
        assert lane.held
        dispatched.append(request)
        return httpx.Response(200, headers={"content-type": "application/json"},
                              stream=httpx.ByteStream(json.dumps(payload(done_reason=reason)).encode()))

    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: httpx.MockTransport(handle))
    with client(module) as sdk:
        assert native_request(sdk).json()["done"] is True
        assert not lane.held and not lane.uncertain
        # This opt-in client cannot be repurposed to send an unreviewed wire.
        with pytest.raises(Exception) as caught:
            sdk.chat.completions.create(model=MODEL, messages=[])
        assert module.find_admission_error(caught.value).code == "unavailable"
    assert len(dispatched) == 1


@pytest.mark.parametrize("changes", [{"model": "wrong"}, {"done": False}, {"done": 1},
    {"done_reason": "unknown"}, {"error": "bad"}, {"remote_host": "https://remote.example"}])
def test_unmatched_native_reply_keeps_durable_recovery_barrier(admission, monkeypatch, changes):
    module, lane, cfg = admission
    cfg["local_model_admission"]["routes"][0]["allow_native_chat"] = True
    dispatched = []

    def handle(request):
        dispatched.append(request)
        return httpx.Response(200, headers={"content-type": "application/json"},
                              stream=httpx.ByteStream(json.dumps(payload(**changes)).encode()))

    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: httpx.MockTransport(handle))
    with client(module) as sdk:
        with pytest.raises(module.LocalModelAdmissionError) as caught:
            native_request(sdk)
        assert caught.value.code == "recovery_required"
    with client(module) as sdk:
        with pytest.raises(module.LocalModelAdmissionError) as caught:
            native_request(sdk)
        assert caught.value.code == "recovery_required"
    assert lane.uncertain and len(dispatched) == 1


@pytest.mark.parametrize("changes", [{"stream": True}, {"stream": None}, {"model": "not-enrolled"}])
def test_unsupported_native_request_never_dispatches(admission, monkeypatch, changes):
    module, lane, cfg = admission
    cfg["local_model_admission"]["routes"][0]["allow_native_chat"] = True
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: httpx.MockTransport(
        lambda request: pytest.fail("invalid request reached transport")))
    with client(module) as sdk:
        with pytest.raises(module.LocalModelAdmissionError):
            native_request(sdk, **changes)
    assert lane.acquisitions == 0
