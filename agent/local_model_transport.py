"""Request-local ownership that survives caller cancellation, SDK retries and streams.

HTTPX public transport API: https://www.python-httpx.org/advanced/transports/#custom-transports
The SDK stops at DONE without requesting EOF; hold that chunk until raw exhaustion:
https://github.com/openai/openai-python/blob/v2.24.0/src/openai/_streaming.py
"""
import threading

import httpx

from agent.local_model_admission import LocalModelAdmissionError
from agent.local_model_completion import CompletionProof, MAX_BYTES, strict_json


class _Admission:
    def __init__(self, policy):
        self.policy = policy
        self.lock = threading.Lock()
        self.denied = None

    def begin(self, request):
        with self.lock:
            if self.denied is not None:
                raise LocalModelAdmissionError(self.denied)
            try:
                if request.method != "POST" or str(request.url) != self.policy.base_url + "/chat/completions":
                    raise LocalModelAdmissionError()
                if len(request.content) > MAX_BYTES:
                    raise LocalModelAdmissionError()
                body = strict_json(request.content)
                if (not isinstance(body, dict) or body.get("model") not in self.policy.models
                        or type(body.get("stream", False)) is not bool
                        or type(body.get("n", 1)) is not int or body.get("n", 1) != 1):
                    raise LocalModelAdmissionError()
                proof = CompletionProof(body["model"], body.get("stream", False))
                lease = self.policy.acquire()
                return _Ownership(self, lease, proof)
            except Exception as exc:
                self.denied = exc.code if isinstance(exc, LocalModelAdmissionError) else "unavailable"
                raise LocalModelAdmissionError(self.denied) from exc

    def refuse_future(self):
        with self.lock:
            self.denied = "recovery_required"


class _Ownership:
    def __init__(self, admission, lease, proof):
        self.admission, self.lease, self.proof = admission, lease, proof
        self.lock = threading.Lock()

    def headers(self, response):
        expected = "text/event-stream" if self.proof.streaming else "application/json"
        if (response.status_code != 200 or response.headers.get("content-type", "").split(";")[0] != expected
                or response.headers.get("content-encoding", "identity") != "identity"):
            raise LocalModelAdmissionError("recovery_required")

    def complete(self):
        self.proof.finish()
        with self.lock:
            if self.lease is None:
                raise LocalModelAdmissionError("recovery_required")
            lease, self.lease = self.lease, None
            try:
                lease.release()
            except Exception as exc:
                self.admission.refuse_future()
                raise LocalModelAdmissionError("recovery_required") from exc

    def abandon(self):
        with self.lock:
            if self.lease is not None:
                lease, self.lease = self.lease, None
                self.admission.refuse_future()
                lease.abandon()


class _Body(httpx.SyncByteStream):
    def __init__(self, inner, ownership):
        self.inner, self.ownership = inner, ownership

    def __iter__(self):
        tail = []
        try:
            for chunk in self.inner:
                self.ownership.proof.feed(chunk)
                if self.ownership.proof.done:
                    tail.append(chunk)
                else:
                    yield chunk
            self.ownership.complete()
            yield from tail
        except BaseException as exc:
            self.ownership.abandon()
            if isinstance(exc, Exception):
                raise LocalModelAdmissionError("recovery_required") from exc
            raise

    def close(self):
        self.ownership.abandon()
        self.inner.close()


class _AsyncBody(httpx.AsyncByteStream):
    def __init__(self, inner, ownership):
        self.inner, self.ownership = inner, ownership

    async def __aiter__(self):
        tail = []
        try:
            async for chunk in self.inner:
                self.ownership.proof.feed(chunk)
                if self.ownership.proof.done:
                    tail.append(chunk)
                else:
                    yield chunk
            self.ownership.complete()
            for chunk in tail:
                yield chunk
        except BaseException as exc:
            self.ownership.abandon()
            if isinstance(exc, Exception):
                raise LocalModelAdmissionError("recovery_required") from exc
            raise

    async def aclose(self):
        self.ownership.abandon()
        await self.inner.aclose()


class LocalTransport(httpx.BaseTransport):
    def __init__(self, policy):
        self.admission = _Admission(policy)
        self.inner = httpx.HTTPTransport(retries=0, trust_env=False)

    @property
    def _pool(self):
        # Existing native abort shuts sockets down, never closes another thread's FD.
        return getattr(self.inner, "_pool", None)

    def handle_request(self, request):
        ownership = self.admission.begin(request)
        response = None
        try:
            response = self.inner.handle_request(request)
            ownership.headers(response)
            response.stream = _Body(response.stream, ownership)
            return response
        except BaseException as exc:
            ownership.abandon()
            if response is not None:
                response.close()
            if isinstance(exc, Exception):
                raise LocalModelAdmissionError("recovery_required") from exc
            raise

    def close(self):
        self.inner.close()


class AsyncLocalTransport(httpx.AsyncBaseTransport):
    def __init__(self, policy):
        self.admission = _Admission(policy)
        self.inner = httpx.AsyncHTTPTransport(retries=0, trust_env=False)

    async def handle_async_request(self, request):
        ownership = self.admission.begin(request)
        response = None
        try:
            response = await self.inner.handle_async_request(request)
            ownership.headers(response)
            response.stream = _AsyncBody(response.stream, ownership)
            return response
        except BaseException as exc:
            ownership.abandon()
            if response is not None:
                await response.aclose()
            if isinstance(exc, Exception):
                raise LocalModelAdmissionError("recovery_required") from exc
            raise

    async def aclose(self):
        await self.inner.aclose()
