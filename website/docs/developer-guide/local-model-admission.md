---
title: "Local Model Admission (Experimental)"
description: "Explicit shared-slot enrollment at the physical local request boundary"
---

# Local Model Admission (Experimental)

This opt-in source feature lets participating applications share one local model
request slot. It is not enabled by default, a machine-wide inference firewall, a
RAM budget, or an automatic recovery mechanism. It does not select a model,
change a prompt, authorize tools, or enable paid fallback.

## Supported boundary

Only explicitly enrolled, direct HTTP loopback Ollama OpenAI chat-completion
routes are supported. Use literal `127.0.0.1` or `::1`, an exact `/v1` base path,
and exact model tags. `localhost`, LAN hosts, proxies, other API paths and local
Messages/Responses protocols are not interchangeable enrollment names. An
unsupported API mode on an enrolled backend is rejected, not silently unguarded.

The policy is read from the owning profile's `config.yaml` when a client is built.
An absent section or explicit `enabled: false` preserves existing behavior.
Malformed/unreadable policy cannot be treated as disabled for loopback clients;
unrelated cloud clients retain their existing behavior.

The following is a deployment template, **not an installation command**. The
paths, digest and model tag must be replaced with a reviewed deployment contract:

```yaml
local_model_admission:
  enabled: true
  coordinator_source: /absolute/canonical/path/local_model_lane.py
  coordinator_sha256: "<64 lowercase hex characters of reviewed source>"
  lane_root: /absolute/canonical/private/shared-lane
  routes:
    - base_url: http://127.0.0.1:11434/v1
      protocol: ollama-openai-v1
      # Optional, separately reviewed native nonstream /api/chat callers only:
      allow_native_chat: false
      # Fail closed pending a supported deployment/capacity verifier (see below).
      require_capacity_qualification: true
      models:
        - "<exact served model tag>"
```

A native `/api/chat` caller must pass `native_chat=True` to
`guarded_client_kwargs` and have `allow_native_chat: true` on this same exact
backend. Both sides are required. This client can send only nonstream native
chat (`stream: false` explicitly); it cannot send OpenAI completions, Responses,
other native endpoints, or requests for unenrolled models. Missing/disabled
enrollment raises a terminal refusal, never an unguarded native client. This
does not change ordinary SDK clients' absent-policy behavior.

### Required capacity qualification

`require_capacity_qualification: true` currently **refuses every request** on
that enrolled route with `local_model_capacity_unqualified`, before acquiring
the shared slot or dispatching inference. There is no supported positive
qualification implementation yet. This is an explicit safety stop, not a
working memory allocator, automated RAM check or promise of a smaller model.
It preserves the nonretryable reason through native/auxiliary clients, API runs
and desktop mailbox delivery; it does not select a cloud fallback.

Omitting this flag or setting it to boolean `false` retains the existing
serialization-only behavior, **without RAM protection**. Other value types
are invalid rather than silently disabling the requirement. An absent/disabled
whole admission section still preserves existing routing. As with all enrollment,
activation requires coordinated restart; cached clients do not adopt this flag
live. No route is enabled or reconfigured by adding this source feature.

Why not accept a number of free bytes as proof? Ollama's effective loading
configuration includes parallel slots, context, batch size, cache settings,
placement and inherited model components. Its metadata endpoints do not fully
establish those settings for the running process. The existing Hermes managed
runtime owns llama-server, not this Ollama process. A future capacity verifier
must bind a reviewed full memory envelope to the actual backend process
incarnation, hardware, exact model manifest, inherited configuration and bounded
workload, then compare fresh available memory under the shared slot. A settings
file hash, empty model list or matching displayed context alone is insufficient.

On Ollama 0.34.3 the OpenAI chat-completion conversion does not forward
`options.num_ctx`; that API needs a model-configured context parameter. The
separate native API can honor its request options but still needs the same
deployment/capacity proof. See the pinned
[OpenAI conversion](https://github.com/ollama/ollama/blob/v0.34.3/openai/openai.go),
[loading scheduler](https://github.com/ollama/ollama/blob/v0.34.3/server/sched.go),
[option resolution](https://github.com/ollama/ollama/blob/v0.34.3/server/routes.go)
and [context configuration](https://docs.ollama.com/api/openai-compatibility#setting-the-local-context-size).
Even a qualified preflight is cooperative admission, not an OS memory reservation
against other applications or unenrolled callers. Backend recovery remains a
separate reviewed operation; this refusal neither acquires nor clears a marker.

All participating profiles/applications must use the **same machine-user lane
root**, not separate profile directories. The coordinator must implement the
reviewed `LocalModelLane(root).acquire(owner=...)` lease contract with `release()`
and `abandon()`, and `LaneBusy` / `LaneRecoveryRequired` exceptions. The adapter
executes only bytes matching the pinned digest from a canonical regular file
owned by this user and not group/world writable. Digest changes are deployments,
not automatic trust updates.

## Worker lifecycle

The HTTP transport acquires immediately before physical dispatch. The raw
response body owns the lease across SDK consumption, daemon-worker survivors
and caller cancellation. The selected client disables SDK/transport retries,
redirects and environment proxy routing. Caller-injected HTTP clients are
rejected on enrolled routes because their hidden routing cannot be established.

Release requires bounded, unambiguous raw JSON for the requested model, Ollama's
fingerprint, one terminal choice and clean raw EOF. Streaming also requires a
consistent response ID, valid SSE framing and `[DONE]`. The chunk containing
`[DONE]` is withheld until raw EOF: OpenAI Python 2.24.0 otherwise stops reading
at that marker. Earlier text can still stream normally. Response headers,
rendered text, synthesized partial replies, socket closure and thread/process
exit are not evidence of backend completion.

Native chat uses its own bounded strict-JSON evidence: the exact requested
model, boolean `done: true`, `done_reason` of `stop` or `length`, no error or
remote-host/model identity, and clean raw EOF. A finished but truncated or
otherwise unusable answer releases physical ownership; the caller must still
reject its quality. Native chat never relies on OpenAI fingerprints or SSE.

An error, cancellation, early close or unproven reply abandons the lease without
clearing the durable marker. The same transport stays fenced even if callers
override the SDK retry count. Native and auxiliary recovery stop before retry or
cloud fallback. A later intentional auxiliary call may construct a fresh client;
it must reacquire the same lane and cannot clear uncertainty. Retiring a cached
client does not close another worker's transport.

A caller rejecting a yielded chunk may raise its own cancellation exception.
After response closure, `find_admission_error(exc, client=...)` preserves any
denial actually recorded by that request's transport. It does not infer
uncertainty from every post-completion cancellation or bad answer.

A synthetic nonstream watchdog timeout carries the original request client's
admission identity even if the owner switches models while that worker survives.
Classifying only the live primary client would lose the local safety stop when
the newly selected model is remote.

## Activation and recovery gates

This is **restart-only enrollment**, not a live policy switch. Existing clients
may remain cached and unenrolled. Before enabling or changing it, settle old
workers and coordinate every owning runtime: desktop sessions, phone workers,
auxiliary workers and other participating applications. Updating only a phone
process does not upgrade the desktop owner of a mailbox-delivered chat.

Never clear lane state based on a dead PID, elapsed time, an empty model list,
a disconnected socket, or a finished UI task. There is deliberately no force
unlock here. Backend settlement/recovery requires its separately reviewed
operator workflow. A surviving worker can still release when it later provides
valid completion; an abandoned uncertain attempt cannot infer that proof.

Separate applications, phone checkers and arbitrary tools posting directly to
Ollama are not automatically enrolled. A reviewed caller must adopt the guarded
client, preserve typed failure/retry metadata, and prove its physical request
lifetime. Source support alone is not deployment, rendered UI acceptance, a RAM
budget or an approved backend-recovery operation.

### Memory readings are not admission

The existing `hermes_cli.local_runtime.hardware.probe_budget()` supplies a live
hardware estimate. On macOS, missing, malformed, impossible or failed memory
readings contribute zero available bytes; a genuine zero is not replaced with
half of physical RAM. A complete `vm_stat` snapshot must declare a supported
4 KiB or 16 KiB page size. Free, inactive and speculative queues are counted
once; the separate purgeable count is not added again.

This is only a probe correction. `planning=True` intentionally prices total
capacity for the catalog. Existing managed-runtime launch/growth paths still
use capacity and do not receive a live UMA launch budget. Direct Ollama requests
do not consume this probe at all. Neither the corrected reading nor a unit-level
physics refusal qualifies a model/context combination or closes the RAM gate.
Do not clear a recovery marker or start a model on that evidence alone.

## Verification and compatibility

The source was exercised with OpenAI Python 2.24.0 and HTTPX 0.28.1. The accepted
wire contract is grounded in Ollama 0.34.3's
[response formatter](https://github.com/ollama/ollama/blob/v0.34.3/openai/openai.go)
and [completion writer](https://github.com/ollama/ollama/blob/v0.34.3/middleware/openai.go).
The wrapper uses [HTTPX's public transport API](https://www.python-httpx.org/advanced/transports/#custom-transports);
the terminal-marker behavior comes from the
[pinned SDK implementation](https://github.com/openai/openai-python/blob/v2.24.0/src/openai/_streaming.py).
New backend/SDK versions require replaying completion, cancellation and retry
proofs before enrollment, not assuming generic OpenAI compatibility is enough.

Use `scripts/run_tests.sh` with the four `tests/agent/test_local_model_*` /
`test_auxiliary_local_model_admission.py` files and `test_native_chat_admission.py`.
When running older provider
regressions from a worktree whose Git metadata is under the real Hermes home,
add `-p tests.agent.local_model_test_support`: it redirects only updater lookup
to a temporary fixture, leaving the real-home I/O guard intact. The separate
coordinator repository holds the controlled-HTTP, native-worker, cross-process
integration proof; no real model generation is needed for that test.
