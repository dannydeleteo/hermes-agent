"""Bounded raw completion evidence, before Hermes/SDK can synthesize a final reply.

Only the explicitly enrolled direct Ollama chat protocol is accepted, not a
generic OpenAI-compatible proxy. Protocol sources:
https://github.com/ollama/ollama/blob/v0.34.3/openai/openai.go
https://github.com/ollama/ollama/blob/v0.34.3/middleware/openai.go
"""
import json
import re


MAX_BYTES = 16 * 1024 * 1024


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("non-JSON constant")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)


class NativeChatCompletionProof:
    """Physical completion only; accepting the answer is the caller's decision.

    Native nonstream contract: https://docs.ollama.com/api/chat and
    https://github.com/ollama/ollama/blob/v0.34.3/api/types.go (ChatResponse).
    The transport calls finish only after clean raw EOF, never on headers/text.
    """
    streaming = False
    done = False

    def __init__(self, model):
        self.model = model
        self.buffer = bytearray()

    def feed(self, chunk):
        if len(self.buffer) + len(chunk) > MAX_BYTES:
            raise ValueError("completion evidence exceeds bound")
        self.buffer.extend(chunk)

    def finish(self):
        response = strict_json(self.buffer)
        if (not isinstance(response, dict) or response.get("model") != self.model
                or response.get("done") is not True or "error" in response
                or response.get("done_reason") not in ("stop", "length")
                or response.get("remote_host") or response.get("remote_model")):
            raise ValueError("unmatched or incomplete native backend response")


class CompletionProof:
    def __init__(self, model, streaming):
        self.model, self.streaming = model, streaming
        self.total = 0
        self.buffer = b""
        self.data = []
        self.identity = None
        self.terminal = False
        self.done = False
        self.pending_cr = False

    def feed(self, chunk):
        if not chunk:
            return
        self.total += len(chunk)
        if self.total > MAX_BYTES:
            raise ValueError("completion evidence exceeds bound")
        if not self.streaming:
            self.buffer += chunk
            return
        # SSE permits CR, LF, and CRLF (including split CRLF). Parse each before
        # yielding raw bytes: the SDK would otherwise see a DONE we failed to fence.
        if self.pending_cr and chunk.startswith(b"\n"):
            chunk = chunk[1:]
        self.pending_cr = False
        self.buffer += chunk
        while (separator := re.search(br"[\r\n]", self.buffer)) is not None:
            index = separator.start()
            line, ending = self.buffer[:index], self.buffer[index:index + 1]
            self.buffer = self.buffer[index + 1:]
            if ending == b"\r":
                self.pending_cr = not self.buffer
                if self.buffer.startswith(b"\n"):
                    self.buffer = self.buffer[1:]
            if not line:
                self._event()
            elif self.done:
                raise ValueError("data after terminal marker")
            elif line.startswith(b"data:"):
                self.data.append(line[5:].removeprefix(b" "))
            elif not line.startswith(b":"):
                raise ValueError("unsupported SSE field")

    def _event(self):
        if not self.data:
            return
        data, self.data = b"\n".join(self.data), []
        if data == b"[DONE]":
            if self.done or not self.terminal:
                raise ValueError("terminal marker without completion")
            self.done = True
            return
        if self.done:
            raise ValueError("data after terminal marker")
        self._response(strict_json(data))

    def _response(self, response):
        expected = "chat.completion.chunk" if self.streaming else "chat.completion"
        if (not isinstance(response, dict) or response.get("model") != self.model
                or response.get("object") != expected or "error" in response
                or response.get("system_fingerprint") != "fp_ollama"
                or not isinstance(response.get("id"), str) or not response["id"]):
            raise ValueError("unmatched backend response")
        if self.identity is None:
            self.identity = response["id"]
        if response["id"] != self.identity:
            raise ValueError("response identity changed")
        choices = response.get("choices")
        if self.streaming and choices == [] and isinstance(response.get("usage"), dict):
            return
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ValueError("single completion required")
        choice = choices[0]
        if type(choice.get("index")) is not int or choice["index"] != 0 or self.terminal:
            raise ValueError("unexpected choice after completion")
        reason = choice.get("finish_reason")
        if reason in ("stop", "length", "tool_calls"):
            self.terminal = True
        elif not self.streaming or reason is not None:
            raise ValueError("backend completion unproven")

    def finish(self):
        if self.streaming:
            if self.buffer or self.data or not self.done:
                raise ValueError("truncated stream")
        else:
            self._response(strict_json(self.buffer))
        if not self.terminal:
            raise ValueError("backend completion unproven")
