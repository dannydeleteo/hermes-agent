"""Enrolled auxiliary requests stop at local admission instead of changing routes."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


@pytest.fixture
def local_route(tmp_path, monkeypatch):
    import hermes_yaml as yaml
    from agent import auxiliary_client as aux
    from agent import local_model_admission as admission

    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
                "https_proxy", "http_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    source = tmp_path / "coordinator.py"
    coordinator_text = "# External coordinator boundary is supplied by this test.\n"
    source.write_text(coordinator_text)
    lane_root = tmp_path / "lane"
    lane_root.mkdir(mode=0o700)
    local_base = "http://127.0.0.1:11434/v1"
    config = {
        "model": {"provider": "cloud-fallback", "model": "cloud-model"},
        "custom_providers": [{"name": "cloud-fallback", "base_url": "https://cloud.test/v1", "api_key": "test"}],
        "auxiliary": {"title_generation": {
            "provider": "ollama", "model": "local-model:tag", "base_url": local_base,
            "fallback_chain": [{"provider": "cloud-fallback", "model": "cloud-model"}],
        }},
        "local_model_admission": {
            "enabled": True, "coordinator_source": str(source),
            "coordinator_sha256": hashlib.sha256(coordinator_text.encode()).hexdigest(),
            "lane_root": str(lane_root),
            "routes": [{"base_url": local_base, "protocol": "ollama-openai-v1", "models": ["local-model:tag"]}],
        },
    }
    (profile / "config.yaml").write_text(yaml.safe_dump(config))
    state = SimpleNamespace(busy=True, acquisitions=0, dispatches=[], request_ids=[], releases=0, abandoned=0)

    class LaneBusy(RuntimeError):
        pass

    class LaneUnavailable(RuntimeError):
        pass

    class LaneRecoveryRequired(LaneUnavailable):
        pass

    class Lease:
        invocation_id = "a" * 32

        def check(self):
            pass

        def release(self):
            state.releases += 1

        def abandon(self):
            state.abandoned += 1

    class LocalModelLane:
        def __init__(self, root):
            assert Path(root) == lane_root

        def acquire(self, *, owner):
            state.acquisitions += 1
            if state.busy:
                raise LaneBusy("test lane busy")
            return Lease()

    coordinator = SimpleNamespace(LocalModelLane=LocalModelLane, LaneBusy=LaneBusy,
                                  LaneUnavailable=LaneUnavailable, LaneRecoveryRequired=LaneRecoveryRequired)
    monkeypatch.setattr(admission, "_load_coordinator", lambda *args, **kwargs: coordinator)

    class ChunkStream(httpx.SyncByteStream, httpx.AsyncByteStream):
        def __init__(self, chunks):
            self.chunks = chunks

        def __iter__(self):
            yield from self.chunks

        async def __aiter__(self):
            for chunk in self.chunks:
                yield chunk

    def send(_transport, request):
        body = json.loads(request.content)
        state.dispatches.append((str(request.url), body))
        state.request_ids.append(request.headers.get("x-request-id"))
        if body.get("stream"):
            chunk = {"id": "test", "object": "chat.completion.chunk", "created": 1,
                     "system_fingerprint": "fp_ollama", "model": body["model"],
                     "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": None}]}
            first = f"data: {json.dumps(chunk)}\n\n".encode()
            chunk["choices"][0]["finish_reason"] = "stop"
            last = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=ChunkStream([first, last]))
        payload = {"id": "test", "object": "chat.completion", "created": 1, "system_fingerprint": "fp_ollama",
                   "model": body["model"], "choices": [{"index": 0, "finish_reason": "stop",
                   "message": {"role": "assistant", "content": "ok"}}]}
        return httpx.Response(200, headers={"content-type": "application/json"},
                              stream=httpx.ByteStream(json.dumps(payload).encode()))

    async def asend(transport, request):
        return send(transport, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", asend)
    aux.shutdown_cached_clients()
    yield SimpleNamespace(state=state, config=config, profile=profile, aux=aux, admission=admission,
                          base_url=local_base)
    aux.shutdown_cached_clients()


@pytest.mark.parametrize("failure,mode", [
    ("busy", "sync"), ("busy", "async"), ("busy", "stream"),
    ("busy", "progress"), ("busy", "async_progress"),
    ("deadline", "progress"), ("deadline", "async_progress"),
    ("configuration", "sync"), ("configuration", "async"),
    ("unsupported_mode", "sync"), ("unsupported_mode", "async"),
    ("capacity", "sync"), ("capacity", "async"), ("capacity", "stream"),
    ("capacity", "progress"), ("capacity", "async_progress"),
])
def test_local_auxiliary_failure_never_retries_or_dispatches_to_cloud(local_route, failure, mode):
    route = local_route
    if failure == "capacity":
        import hermes_yaml as yaml
        route.config["local_model_admission"]["routes"][0]["require_capacity_qualification"] = True
        (route.profile / "config.yaml").write_text(yaml.safe_dump(route.config))
    if failure == "unsupported_mode":
        import hermes_yaml as yaml
        route.config["custom_providers"].append({"name": "local-messages", "model": "local-model:tag",
            "base_url": route.base_url, "api_key": "test", "api_mode": "anthropic_messages"})
        route.config["auxiliary"]["title_generation"] = {"provider": "local-messages", "model": "local-model:tag"}
        (route.profile / "config.yaml").write_text(yaml.safe_dump(route.config))
    if failure == "configuration":
        import hermes_yaml as yaml
        route.config["local_model_admission"]["lane_root"] = "not-an-absolute-directory"
        route.config["auxiliary"]["title_generation"] = {
            "provider": "missing-provider", "model": "missing-model",
            "fallback_chain": [
                {"provider": "ollama", "model": "local-model:tag", "base_url": route.base_url},
                {"provider": "cloud-fallback", "model": "cloud-model"},
            ],
        }
        (route.profile / "config.yaml").write_text(yaml.safe_dump(route.config))
    route.state.busy = failure == "busy"
    kwargs = {"task": "title_generation", "messages": [{"role": "user", "content": "Keep this input."}]}
    with pytest.raises(route.admission.LocalModelAdmissionError) as caught:
        with (route.aux.aux_progress_hook((lambda: None) if "progress" in mode else None),
              route.aux.aux_stream_deadline(0.0 if failure == "deadline" else None)):
            if mode.startswith("async"):
                asyncio.run(route.aux.async_call_llm(**kwargs))
            elif mode == "stream":
                list(route.aux.call_llm(**kwargs, stream=True))
            else:
                route.aux.call_llm(**kwargs)
    assert caught.value.code == {"busy": "busy", "deadline": "recovery_required",
                                 "configuration": "unavailable", "unsupported_mode": "unavailable",
                                 "capacity": "capacity_unqualified"}[failure]
    assert route.state.acquisitions == (0 if failure in {"configuration", "unsupported_mode", "capacity"} else 1)
    if failure == "deadline":
        assert len(route.state.dispatches) == 1
        assert route.state.dispatches[0][0].startswith(route.base_url)
        assert route.state.abandoned == 1
    else:
        assert route.state.dispatches == []


@pytest.mark.parametrize("async_mode", [False, True])
def test_auxiliary_profile_routes_keep_request_content_and_local_ownership(local_route, monkeypatch, async_mode):
    import hermes_yaml as yaml

    route = local_route
    route.state.busy = False
    second_home = route.profile.parent / "second-profile"
    second_home.mkdir()
    remote_config = copy.deepcopy(route.config)
    remote_config["auxiliary"]["title_generation"] = {
        "provider": "custom", "model": "cloud-model", "base_url": "https://cloud.test/v1", "api_key": "test"}
    (second_home / "config.yaml").write_text(yaml.safe_dump(remote_config))
    messages = [{"role": "user", "content": "Keep this input."}]
    for profile in (route.profile, second_home, route.profile):
        monkeypatch.setenv("HERMES_HOME", str(profile))
        kwargs = {"task": "title_generation", "messages": messages}
        response = (asyncio.run(route.aux.async_call_llm(**kwargs)) if async_mode
                    else route.aux.call_llm(**kwargs))
        assert response.choices[0].message.content == "ok"
    assert [url for url, _ in route.state.dispatches] == [
        route.base_url + "/chat/completions", "https://cloud.test/v1/chat/completions",
        route.base_url + "/chat/completions"]
    assert all(body["messages"] == messages for _, body in route.state.dispatches)
    assert route.state.acquisitions == route.state.releases == 2
    assert route.state.abandoned == 0


@pytest.mark.parametrize("async_mode", [False, True])
def test_later_auxiliary_call_rebuilds_busy_client_after_competitor_finishes(local_route, async_mode):
    import hermes_yaml as yaml

    route = local_route
    route.config["model"]["default_headers"] = {"X-Request-ID": "keep-owner-request-id"}
    (route.profile / "config.yaml").write_text(yaml.safe_dump(route.config))
    messages = [{"role": "user", "content": "Keep this input."}]
    kwargs = {"task": "title_generation", "messages": messages}

    def competitor_finishes(caught):
        assert caught.value.code == "busy"
        assert route.state.acquisitions == 1
        assert route.state.dispatches == []
        assert route.state.releases == route.state.abandoned == 0
        # The external owner finishes cleanly; the denied caller never releases it.
        route.state.busy = False

    if async_mode:
        async def run():
            with pytest.raises(route.admission.LocalModelAdmissionError) as caught:
                await route.aux.async_call_llm(**kwargs)
            competitor_finishes(caught)
            return await route.aux.async_call_llm(**kwargs)

        # Both intentional calls share one live loop, so loop replacement cannot
        # accidentally hide reuse of the denied cached client.
        response = asyncio.run(run())
    else:
        with pytest.raises(route.admission.LocalModelAdmissionError) as caught:
            route.aux.call_llm(**kwargs)
        competitor_finishes(caught)
        response = route.aux.call_llm(**kwargs)

    assert response.choices[0].message.content == "ok"
    assert route.state.acquisitions == 2
    assert route.state.releases == 1
    assert route.state.abandoned == 0
    assert len(route.state.dispatches) == 1
    url, body = route.state.dispatches[0]
    assert url == route.base_url + "/chat/completions"
    assert body["messages"] == messages
    assert body["model"] == "local-model:tag"
    assert route.state.request_ids == ["keep-owner-request-id"]
