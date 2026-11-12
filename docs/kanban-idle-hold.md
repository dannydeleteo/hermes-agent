# Atomic idle-hold correction (source contract)

This amendment adds an explicit **board-manager-only** action, not a new worker
capability and not a replacement for normal block/unblock. It is source-only
until independently reviewed at the exact head, accepted by the unchanged
canonical merge gate and installed by the existing sole installer.

## Supported action after qualified installation

Run from the installed runtime using its managed Python in an existing authorized
board-manager execution context (never a dispatcher worker, even for its own card):

```text
<managed-python> -m hermes_cli.kanban_idle_hold --board <board-slug> \
  --task-id <original-task-id> --expected-status blocked \
  --expected-kind dependency --kind needs_input \
  --reason 'FOR COREY: review the retained attachment in the existing decision inbox'
```

This is a dedicated, supported Python module CLI, not a `hermes kanban` subcommand
or a raw SQL operator recipe. The same module exports `reclassify_idle_hold` for
manager integrations. No existing tool registration or dispatcher scope changes.
Do not strip worker environment/lineage or forge another profile to run it.

Both expected fields are mandatory. `--expected-kind null` explicitly means SQL
NULL; all other kinds must be native `VALID_BLOCK_KINDS`. A nonblank reason is
mandatory. Exit 0 means applied, 1 means stale/unsafe state (no writes), 2 means
bad input or worker/delegate refusal. Successful/refused CAS returns JSON with
`ok`, `task_id`, actual read-back `status` and `block_kind`; a readback is not a
creative decision, worker acknowledgment, install receipt or delivery completion.

## Atomic unit and refusal policy

Within the existing guarded `write_txn` (`BEGIN IMMEDIATE`), inspect current task,
parents and resumable phase; compare expected previous status/kind; update only
status/kind; append native audit event; commit together. No intermediate READY
state for an owner hold. Native `blocked` events keep needs_input/capability/
transient holds sticky to `recompute_ready`; `dependency_wait` preserves resumable
review history. Audit includes previous/new status and kind, new reason,
`reclassified_in_place: true`, and resume phase. Event and correction roll back
together on error. Existing history is append-only, never rewritten.

Refuse unless status is BLOCKED, current_run_id is NULL, and **all** claim_lock,
claim_expires, worker_pid and worker_started_at fields are NULL. Refuse any orphan
run whose status is running or ended_at is NULL, even without a current pointer.
This deliberately
also refuses expired/stale claim remnants: it neither guesses process liveness nor
reclaims/ends runs. Any stale-remnant recovery is a separate incumbent-supported
action. A racing claim writer either commits first and makes the CAS refuse, or
waits behind the correction transaction. Existing claim code cannot claim a
blocked owner hold. Already-open caller transactions are refused by the native
transaction guard; this action never commits unrelated work.

For new `dependency`, current incomplete parents yield TODO; all terminal or no
parents yield READY, or REVIEW when existing native lifecycle history says review.
Unlike `block_task(kind='dependency')`, this explicit correction does not silently
turn a completed/no-parent dependency into a new human hold. Parent inspection is
in the same write transaction. Later parent changes use ordinary native policy.

Correction is not a new failure, fresh retry or another unblock/reblock loop.
Preserve block_recurrences, consecutive_failures, last_failure_error, runs,
identity, assignee, attachments/bytes, comments, links and all other task fields.
Refusals append nothing. Ordinary block/unblock semantics are unchanged.

## Delivery obligations (not discharged here)

Source is fork-main based; installed runtime has a different actual head. Qualify
identical additive module/tests in a standalone snapshot of installed HEAD plus
retained dirty compatibility files. No shared DB/dispatcher/tool files are edited
or replaced. Do not transplant whole fork files onto the installed runtime.

Canonical manual proposal for the delivery owner: archive the current canonical
manual, append a dated section explaining the command, manager-only restriction,
CAS refusal/readback and separate episode decisions, then run its real guide-index
check. This document is a source proposal, not an update to a retired manual and
not a claim that the canonical index passed.

After review, the chair retains canonical gate, attributable Root intake, sole
installation, manual/index and fresh supported final-action readbacks on the
original records. No original episode action is performed in source tests.
