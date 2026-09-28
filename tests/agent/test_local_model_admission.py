"""Physical local-request ownership through real SDK/HTTPX clients, without inference."""
import asyncio
import json
import threading
from types import SimpleNamespace

import httpx
import openai
import pytest


MODEL = "local-test:exact"
URL = "http://127.0.0.1:11434/v1"


class Busy(RuntimeError):
    pass


class Uncertain(RuntimeError):
    pass


class LaneBoundary:
    """The external coordinator's contract; cross-process proof lives with that coordinator."""
    held = False
    uncertain = False
    acquisitions = 0

    def acquire(self, *, owner):
        if self.held:
            raise Busy()
        if self.uncertain:
            raise Uncertain()
        self.held = True
        self.acquisitions += 1
        return SimpleNamespace(release=self.release, abandon=self.abandon)

    def release(self):
        assert self.held
        self.held = False

    def abandon(self):
        self.held = False
        self.uncertain = True


@pytest.fixture
def admission(monkeypatch, tmp_path):
    from agent import local_model_admission as admission
    from hermes_cli import config

    lane = LaneBoundary()
    cfg = {"model": {"context_length": 64000}, "local_model_admission": {
        "enabled": True,
        "coordinator_source": str((tmp_path / "coordinator.py").resolve()),
        "coordinator_sha256": "a" * 64,
        "lane_root": str((tmp_path / "lane").resolve()),
        "routes": [{"base_url": URL, "protocol": "ollama-openai-v1", "models": [MODEL]}],
    }}
    monkeypatch.setattr(config, "load_config_readonly", lambda: cfg)
    monkeypatch.setattr(admission, "_load_coordinator", lambda *_: SimpleNamespace(
        LocalModelLane=lambda root: lane, LaneBusy=Busy, LaneRecoveryRequired=Uncertain))
    return admission, lane, cfg


def completion(*, stream=False, model=MODEL, reason="stop"):
    return {"id": "chatcmpl-controlled", "object": "chat.completion.chunk" if stream else "chat.completion",
            "created": 1, "model": model, "system_fingerprint": "fp_ollama", "choices": [
                {"index": 0, "finish_reason": reason,
                 "delta" if stream else "message": {"role": "assistant", "content": "hello"}}]}


def wire_response(*, stream=False, payload=None, ending=True):
    payload = payload or completion(stream=stream)
    body = json.dumps(payload).encode()
    if stream:
        body = b"data: " + body + b"\n\n" + (b"data: [DONE]\n\n" if ending else b"")
    return httpx.Response(200, headers={"content-type": "text/event-stream" if stream else "application/json"},
                          stream=httpx.ByteStream(body))


@pytest.mark.parametrize("async_mode,stream", [(False, False), (False, True), (True, False), (True, True)])
def test_physical_completion_releases_but_uncertain_response_does_not(admission, monkeypatch, async_mode, stream):
    module, lane, _ = admission
    dispatched = []
    bad = False

    def handle(request):
        assert lane.held, "actual dispatch must already own the lane"
        dispatched.append(json.loads(request.content))
        return wire_response(stream=stream, payload=completion(stream=stream, model="wrong" if bad else MODEL))

    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: httpx.MockTransport(handle))
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(handle))

    def client():
        options = module.guarded_client_kwargs({"api_key": "test", "base_url": URL}, async_mode=async_mode)
        return (openai.AsyncOpenAI if async_mode else openai.OpenAI)(**options)

    async def request_async():
        async with client() as sdk:
            result = await sdk.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "test"}], stream=stream)
            if stream:
                return [chunk async for chunk in result]
            return result

    def request():
        if async_mode:
            return asyncio.run(request_async())
        with client() as sdk:
            result = sdk.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "test"}], stream=stream)
            return list(result) if stream else result

    assert request()
    assert not lane.held and not lane.uncertain
    bad = True
    with pytest.raises(Exception) as caught:
        request()
    assert module.find_admission_error(caught.value).code == "recovery_required"
    assert lane.uncertain and not lane.held
    with pytest.raises(Exception) as caught:
        request()
    assert module.find_admission_error(caught.value).code == "recovery_required"
    assert len(dispatched) == 2, "uncertain backend cannot receive a third request"


def test_native_worker_and_outer_recovery_do_not_bypass_admission(admission, monkeypatch):
    module, lane, _ = admission
    # This test is about provider workers, never updater recovery of a checkout.
    from hermes_cli import _early_recovery
    monkeypatch.setattr(_early_recovery, "restore_interrupted_pull", lambda: False)
    from run_agent import AIAgent
    from agent.turn_api_error import handle_api_error

    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: httpx.MockTransport(
        lambda request: wire_response()))
    agent = AIAgent(api_key="test", base_url=URL, model=MODEL, provider="custom", quiet_mode=True,
                    skip_context_files=True, skip_memory=True, enabled_toolsets=[], max_iterations=1)
    agent.api_mode = "chat_completions"
    try:
        assert module.is_guarded_client(agent.client)
        response = agent._interruptible_api_call({"model": MODEL, "messages": [{"role": "user", "content": "test"}]})
        assert response.choices[0].message.content == "hello"
        assert lane.acquisitions == 1 and not lane.held
        # The watchdog can return before the physical worker has any exception to unwrap.
        # The actual enrolled primary client is sufficient to stop routing recovery.
        def forbidden(*args, **kwargs):
            pytest.fail("an enrolled local timeout must never enter retry/fallback recovery")
        monkeypatch.setattr("agent.turn_api_error.recover_before_classification", forbidden)
        verdict = handle_api_error(
            agent, api_error=TimeoutError("watchdog deadline"), _retry=None, thinking_spinner=None,
            messages=[], api_messages=[], api_kwargs={}, system_message=None, active_system_prompt=None,
            conversation_history=[], approx_tokens=1, retry_count=0, max_retries=3,
            compression_attempts=0, max_compression_attempts=1, api_call_count=1,
            api_request_id="original-request-id", api_start_time=0, effective_task_id="original-task-id", turn_id="original-turn-id")
        assert verdict.action == "return"
        assert verdict.result["failure_reason"] == "local_model_recovery_required"
        assert verdict.result["failure_retryable"] is False
    finally:
        agent.client.close()


def test_native_stream_never_turns_uncertainty_into_partial_success(admission, monkeypatch):
    module, lane, _ = admission
    from hermes_cli import _early_recovery
    monkeypatch.setattr(_early_recovery, "restore_interrupted_pull", lambda: False)
    from run_agent import AIAgent
    dispatched, deltas = [], []

    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"data: " + json.dumps(completion(stream=True, reason=None)).encode() + b"\n\n"
            raise httpx.ReadError("controlled disconnect after visible text")

    def handle(request):
        dispatched.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=BrokenStream())

    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: httpx.MockTransport(handle))
    agent = AIAgent(api_key="test", base_url=URL, model=MODEL, provider="custom", quiet_mode=True,
                    skip_context_files=True, skip_memory=True, enabled_toolsets=[], max_iterations=1,
                    stream_delta_callback=deltas.append)
    try:
        with pytest.raises(module.LocalModelAdmissionError):
            agent._interruptible_streaming_api_call({"model": MODEL, "messages": [{"role": "user", "content": "test"}]})
        assert deltas == ["hello"]
        assert len(dispatched) == 1 and lane.uncertain
    finally:
        agent.client.close()


@pytest.mark.parametrize("finish", [True, False])
def test_cancelled_native_owner_cannot_release_a_surviving_worker(admission, monkeypatch, finish):
    module, lane, _ = admission
    from hermes_cli import _early_recovery
    monkeypatch.setattr(_early_recovery, "restore_interrupted_pull", lambda: False)
    from run_agent import AIAgent
    entered, continue_backend, backend_finished = threading.Event(), threading.Event(), threading.Event()
    dispatched, outcome, physical_threads = [], [], []

    class SurvivingBody(httpx.SyncByteStream):
        def __iter__(self):
            physical_threads.append(threading.current_thread())
            entered.set()
            assert continue_backend.wait(10), "test backend was not released"
            try:
                if finish:
                    yield json.dumps(completion()).encode()
                else:
                    raise httpx.ReadError("controlled backend disconnect")
            finally:
                backend_finished.set()

    def handle(request):
        dispatched.append(request)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=SurvivingBody())

    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: httpx.MockTransport(handle))
    agent = AIAgent(api_key="test", base_url=URL, model=MODEL, provider="custom", quiet_mode=True,
                    skip_context_files=True, skip_memory=True, enabled_toolsets=[], max_iterations=1)

    def caller():
        try:
            agent._interruptible_api_call({"model": MODEL, "messages": [{"role": "user", "content": "test"}]})
        except BaseException as exc:
            outcome.append(exc)

    worker = threading.Thread(target=caller)
    worker.start()
    try:
        assert entered.wait(5)
        agent._interrupt_requested = True
        worker.join(3)
        assert not worker.is_alive() and isinstance(outcome[0], InterruptedError)
        assert lane.held and not backend_finished.is_set()
        with openai.OpenAI(**module.guarded_client_kwargs({"api_key": "test", "base_url": URL})) as contender:
            with pytest.raises(Exception) as caught:
                contender.chat.completions.create(model=MODEL, messages=[])
            assert module.find_admission_error(caught.value).code == "busy"
        assert len(dispatched) == 1
    finally:
        continue_backend.set()
        worker.join(3)
        assert backend_finished.wait(3)
        # Join this actual physical worker, not the caller or unrelated daemon threads.
        physical_threads[0].join(3)
        assert not physical_threads[0].is_alive()
        agent.client.close()
    assert not lane.held
    assert lane.uncertain is not finish


@pytest.mark.parametrize("blocked", ["busy", "transport_error"])
def test_sdk_retry_override_cannot_dispatch_after_first_denial(admission, monkeypatch, blocked):
    module, lane, _ = admission
    dispatched = []
    lane.held = blocked == "busy"
    original_acquire = lane.acquire

    def release_competitor_after_denial(**kwargs):
        try:
            return original_acquire(**kwargs)
        finally:
            if blocked == "busy":
                lane.held = False

    lane.acquire = release_competitor_after_denial

    def handle(request):
        dispatched.append(request)
        raise httpx.ReadError("controlled missing backend completion")

    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: httpx.MockTransport(handle))
    with openai.OpenAI(**module.guarded_client_kwargs({"api_key": "test", "base_url": URL})) as sdk:
        with pytest.raises(Exception) as caught:
            sdk.with_options(max_retries=2).chat.completions.create(model=MODEL, messages=[])
        assert module.find_admission_error(caught.value).code == ("busy" if blocked == "busy" else "recovery_required")
        assert len(dispatched) == (0 if blocked == "busy" else 1)


@pytest.mark.parametrize("base_url", [URL, "HTTP://127.0.0.1:11434/v1", "http://127.0.0.1:011434/v1", "http://127.0.0.1:11434/./v1"])
def test_httpx_equivalent_local_routes_cannot_bypass_selection(admission, base_url):
    module, _, _ = admission
    options = module.guarded_client_kwargs({"api_key": "test", "base_url": base_url})
    assert options is not None
    options["http_client"].close()


def test_unrelated_cloud_route_ignores_invalid_local_configuration(admission):
    module, _, cfg = admission
    cfg["local_model_admission"]["routes"][0]["models"] = []
    assert module.guarded_client_kwargs({"api_key": "test", "base_url": "https://api.openai.com/v1"}) is None


@pytest.mark.parametrize("failure", ["failed_read", "exception"])
def test_unreadable_policy_is_not_silently_disabled_for_local_requests(admission, monkeypatch, failure):
    module, _, _ = admission
    from hermes_cli import config

    def unreadable():
        if failure == "exception":
            raise OSError("controlled unreadable config")
        return config.FailedConfigRead({}, error=ValueError("controlled malformed YAML"))

    monkeypatch.setattr(config, "load_config_readonly", unreadable)
    with pytest.raises(module.LocalModelAdmissionError):
        module.guarded_client_kwargs({"api_key": "test", "base_url": URL})
    assert module.guarded_client_kwargs({"api_key": "test", "base_url": "https://api.openai.com/v1"}) is None


def test_enrolled_ollama_route_cannot_be_rebuilt_as_unguarded_messages_client(admission):
    module, _, _ = admission
    from agent.anthropic_adapter import build_anthropic_client
    with pytest.raises(module.LocalModelAdmissionError):
        build_anthropic_client(api_key="fixture", base_url=URL)


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("reason", [None, "stop"])
def test_cr_delimited_done_cannot_hide_failed_raw_stream(admission, monkeypatch, async_mode, reason):
    module, lane, _ = admission

    class BrokenCRStream(httpx.SyncByteStream, httpx.AsyncByteStream):
        def __iter__(self):
            yield b"data: " + json.dumps(completion(stream=True, reason=reason)).encode() + b"\n\n"
            yield b"data: [DONE]\r\r"
            raise httpx.ReadError("disconnect that SDK must not silently hide")

        async def __aiter__(self):
            for chunk in self:
                yield chunk

    def handle(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=BrokenCRStream())

    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: httpx.MockTransport(handle))
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(handle))
    options = module.guarded_client_kwargs({"api_key": "test", "base_url": URL}, async_mode=async_mode)

    async def invoke():
        async with openai.AsyncOpenAI(**options) as sdk:
            stream = await sdk.chat.completions.create(model=MODEL, messages=[], stream=True)
            return [chunk async for chunk in stream]

    with pytest.raises(Exception) as caught:
        if async_mode:
            asyncio.run(invoke())
        else:
            with openai.OpenAI(**options) as sdk:
                list(sdk.chat.completions.create(model=MODEL, messages=[], stream=True))
    assert module.find_admission_error(caught.value).code == "recovery_required"
    assert lane.uncertain and not lane.held


@pytest.mark.parametrize("async_mode", [False, True])
def test_early_stream_close_preserves_uncertainty(admission, monkeypatch, async_mode):
    module, lane, _ = admission
    handler = lambda request: wire_response(stream=True)
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(handler))
    options = module.guarded_client_kwargs({"api_key": "test", "base_url": URL}, async_mode=async_mode)

    async def invoke():
        async with openai.AsyncOpenAI(**options) as sdk:
            result = await sdk.chat.completions.create(model=MODEL, messages=[], stream=True)
            assert lane.held
            await result.close()

    if async_mode:
        asyncio.run(invoke())
    else:
        with openai.OpenAI(**options) as sdk:
            result = sdk.chat.completions.create(model=MODEL, messages=[], stream=True)
            assert lane.held
            result.close()
    assert lane.uncertain and not lane.held


def test_async_task_cancellation_while_reading_retains_recovery_barrier(admission, monkeypatch):
    module, lane, _ = admission

    async def invoke():
        entered = asyncio.Event()

        class PendingBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                await asyncio.Event().wait()
                yield b"unreachable"

        def handle(request):
            assert lane.held
            return httpx.Response(200, headers={"content-type": "application/json"}, stream=PendingBody())

        monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(handle))
        options = module.guarded_client_kwargs({"api_key": "test", "base_url": URL}, async_mode=True)
        async with openai.AsyncOpenAI(**options) as sdk:
            request = asyncio.create_task(sdk.chat.completions.create(model=MODEL, messages=[]))
            await asyncio.wait_for(entered.wait(), timeout=3)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        assert lane.uncertain and not lane.held

    asyncio.run(invoke())


@pytest.mark.parametrize("status,headers", [
    (302, {"location": URL}), (503, {}),
    (200, {"content-type": "text/plain"}),
    (200, {"content-type": "application/json", "content-encoding": "gzip"}),
])
def test_unproven_response_headers_abandon_without_redirect_or_retry(admission, monkeypatch, status, headers):
    module, lane, _ = admission
    dispatched = []

    def handle(request):
        dispatched.append(request)
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(b"unproven"))

    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: httpx.MockTransport(handle))
    options = module.guarded_client_kwargs({"api_key": "test", "base_url": URL})
    with openai.OpenAI(**options) as sdk:
        with pytest.raises(Exception) as caught:
            sdk.chat.completions.create(model=MODEL, messages=[])
    assert module.find_admission_error(caught.value).code == "recovery_required"
    assert lane.uncertain and len(dispatched) == 1


def test_enrolled_route_rejects_caller_supplied_http_client(admission):
    module, lane, _ = admission
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("no dispatch"))) as supplied:
        with pytest.raises(module.LocalModelAdmissionError):
            module.guarded_client_kwargs({"base_url": URL, "http_client": supplied})
    assert lane.acquisitions == 0


def test_model_switch_cannot_erase_surviving_local_request_admission(admission, monkeypatch):
    module, lane, cfg = admission
    from hermes_cli import config, _early_recovery
    monkeypatch.setattr(config, "load_config", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(_early_recovery, "restore_interrupted_pull", lambda: False)
    from run_agent import AIAgent
    from agent.chat_completion_nonstream import _NonStreamRequest
    from agent.turn_api_error import handle_api_error

    entered, resume = threading.Event(), threading.Event()
    outcomes, sends = [], []

    class HeldBody(httpx.SyncByteStream):
        def __iter__(self):
            entered.set()
            assert resume.wait(10)
            yield json.dumps(completion()).encode()

    def handle(request):
        sends.append(str(request.url))
        assert request.url.host == "127.0.0.1", "no cloud dispatch for the old local attempt"
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=HeldBody())

    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: httpx.MockTransport(handle))
    monkeypatch.setattr("agent.model_metadata.get_model_context_length", lambda *args, **kwargs: 64000)
    agent = AIAgent(api_key="test", base_url=URL, model=MODEL, provider="custom", quiet_mode=True,
                    skip_context_files=True, skip_memory=True, enabled_toolsets=[], max_iterations=1)
    request = _NonStreamRequest(agent, {"model": MODEL, "messages": [{"role": "user", "content": "test"}]})
    request.wd.stale_timeout = .15

    def caller():
        try:
            request.run()
        except BaseException as exc:
            outcomes.append(exc)

    caller_thread = threading.Thread(target=caller)
    caller_thread.start()
    try:
        assert entered.wait(3)
        agent.switch_model("cloud-test", "custom", api_key="fixture", base_url="https://cloud.invalid/v1",
                           api_mode="chat_completions")
        caller_thread.join(5)
        assert not caller_thread.is_alive() and outcomes
        assert request.thread.is_alive() and lane.held
        assert not module.is_guarded_client(agent.client)
        error = module.find_admission_error(outcomes[0])
        assert error is not None and error.code == "recovery_required"

        def forbidden(*args, **kwargs):
            pytest.fail("the old local attempt must not enter the new provider's recovery")

        monkeypatch.setattr("agent.turn_api_error.recover_before_classification", forbidden)
        verdict = handle_api_error(
            agent, api_error=outcomes[0], _retry=None, thinking_spinner=None,
            messages=[], api_messages=[], api_kwargs=request.api_kwargs, system_message=None, active_system_prompt=None,
            conversation_history=[], approx_tokens=1, retry_count=0, max_retries=3,
            compression_attempts=0, max_compression_attempts=1, api_call_count=1,
            api_request_id="same-request", api_start_time=0, effective_task_id="same-task", turn_id="same-turn")
        assert verdict.action == "return" and verdict.result["failure_retryable"] is False
        assert len(sends) == 1 and lane.held
    finally:
        resume.set()
        caller_thread.join(3)
        request.thread.join(3)
        assert not request.thread.is_alive()
        agent.client.close()
    assert not lane.held and not lane.uncertain
