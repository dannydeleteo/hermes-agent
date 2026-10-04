"""The raw backend proof is stricter than a rendered answer or SDK stream end."""
import json

import pytest

from agent.local_model_completion import CompletionProof, MAX_BYTES, strict_json


def response(**changes):
    return {
        "id": "chatcmpl-proof", "object": "chat.completion.chunk",
        "model": "fixture:exact", "system_fingerprint": "fp_ollama",
        "choices": [{"index": 0, "finish_reason": "stop", "delta": {"content": "ok"}}],
        **changes,
    }


@pytest.mark.parametrize("ending", [b"\n", b"\r", b"\r\n"])
def test_completion_survives_every_split_and_empty_chunk(ending):
    lines = json.dumps(response(), indent=2).encode().splitlines()
    wire = ending.join(b"data: " + line for line in lines) + ending * 2
    wire += b"data: [DONE]" + ending * 2
    for split in range(len(wire) + 1):
        proof = CompletionProof("fixture:exact", True)
        proof.feed(wire[:split])
        proof.feed(b"")
        proof.feed(wire[split:])
        proof.finish()


@pytest.mark.parametrize("mutation", [
    {"model": "other"}, {"id": ""}, {"system_fingerprint": "proxy"},
    {"object": "chat.completion"}, {"error": "failed"}, {"choices": []},
    {"choices": [{"index": True, "finish_reason": "stop"}]},
    {"choices": [{"index": 0, "finish_reason": "unknown"}]},
])
def test_mismatched_completion_cannot_release(mutation):
    proof = CompletionProof("fixture:exact", True)
    with pytest.raises(ValueError):
        proof.feed(b"data: " + json.dumps(response(**mutation)).encode() + b"\n\n")


@pytest.mark.parametrize("wire", [
    b"data: [DONE]\n\n", b"data: {}\n\n", b"event: message\n\n",
])
def test_unsupported_or_premature_event_cannot_release(wire):
    with pytest.raises(ValueError):
        CompletionProof("fixture:exact", True).feed(wire)


@pytest.mark.parametrize("tail", [b"", b"data: [DONE]", b"data: [DONE]\n\ntrailing"])
def test_terminal_choice_without_complete_raw_framing_cannot_release(tail):
    proof = CompletionProof("fixture:exact", True)
    proof.feed(b"data: " + json.dumps(response()).encode() + b"\n\n" + tail)
    with pytest.raises(ValueError):
        proof.finish()


@pytest.mark.parametrize("wire", [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}'])
def test_non_json_or_ambiguous_fields_rejected(wire):
    with pytest.raises(ValueError):
        strict_json(wire)


def test_size_is_bounded_before_accumulating_body():
    proof = CompletionProof("fixture:exact", False)
    proof.total = MAX_BYTES
    with pytest.raises(ValueError):
        proof.feed(b"x")
    assert proof.buffer == b""
