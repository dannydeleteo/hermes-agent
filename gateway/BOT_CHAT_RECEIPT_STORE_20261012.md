# Bot Chat receipt store — source handoff, October 12, 2026

Status: capability-detection successor; fresh canonical execution and independent
review pending. The original draft failed its canonical gate, as recorded below.

Runtime impact: none. This change does not register a chat, deliver a message,
start a model, change a schedule, or install anything in the running Hermes.

## Outcome and scope

This component is intended to keep a notification's receipt across restarts so
an uncertain acknowledgement cannot become permission to do the work again.
The first component proof is one supplied notification stored in a disposable
SQLite database, reopened, and retrieved through the existing wrapper's status
operation. The owner-visible Bot Chat connection is a later, separately owned
step; a source component is not a completed owner workflow.

Integration owner: Codex Root, GreekFire Finance - Roadmap. Author lane: Assess
Hermes session performance. Sole claimed paths:

- `gateway/bot_chat_notification_store.py`
- `tests/gateway/test_bot_chat_notification_store.py`
- this dated handoff

Base: fetched GitHub `main`, `a50c5d2ccae79c1cee18f55f0fd703d38983b5bd`,
tree `daa5f59cb2cebf1240ba3ae0b54d60ebed5bab78`.
Original branch: `codex/bot-chat-receipt-store-20261012`.
The source commit/tree and final three file hashes are supplied with the frozen
handoff; a document cannot contain its own final commit hash.

No host, registration getter, admission gate, mailbox hook, provider policy,
API, CLI, dependency, daemon, or runtime importer is added. The existing wrapper
and its original 32 tests are unchanged.

## Public component contract

`SQLiteReceiptStore(directory, create=True, capacity=N)` creates only a new
`receipts.sqlite3` file inside an explicitly supplied, existing empty directory.
The directory must be owned by the current user with mode 0700; the file is 0600.
`SQLiteReceiptStore(directory)` reopens existing validated state. An optional
capacity on reopen checks equality; it does not resize the ledger. The constructor
does not discover a profile, choose a default location, or open `state.db`.

The three operations implement the existing `ReceiptStore` protocol:

- `read(key)` returns the typed receipt or absence, without model authority.
- `reserve(key, receipt)` atomically returns `(created, actual_receipt)`.
  The key is `(principal, scope, notification UUID)`, excluding generation and
  target. A conflicting-generation reserve returns the retained original; the
  existing wrapper decides the replay conflict.
- `replace(key, expected, receipt)` performs exact expected-value comparison
  under the write transaction and returns the actual winner if another writer
  advanced it. Identity, arrival and terminal receipts cannot be overwritten;
  an existing claim cannot regress. No stored notification becomes a wake.

Capacity is persisted and checked transactionally. Full capacity refuses new
keys without evicting uncertain, claimed, terminal or `NOT_SUBMITTED` records.
There is no delete, prune, expiry sweep, automatic repair or resend operation.
Keep a failed ledger intact for the recovery owner; do not substitute a new
empty database to clear an error.

## Privacy, format and durability boundaries

The store rejects symlink components, unsafe ancestor ownership/writability,
non-private directories/files, non-owner files, hard links, unexpected sidecars
and changed directory/database inodes. Sticky shared ancestors may contain the
private owned directory. It does not chmod existing material or follow a profile
alias. The directory must remain exclusively managed by the trusted host.
This is not isolation from malicious same-user/root code or arbitrary filesystem
replacement during an operation; native lifecycle qualification remains separate.

Receipt JSON is versioned, canonical and capped at 256 KiB per record, with the
wrapper's 16 KiB UTF-8 text/result bounds, strict typed identifiers, finite
timestamps, exact hashes and conservative evidence labels. Unsupported versions,
duplicate/unknown fields, malformed data and identity inconsistencies fail closed.
Record SQL uses bound parameters. The exact two-table schema and format marker
are validated; unknown databases are not migrated or adopted.

Connections are short-lived, use `mode=rw` after exclusive file creation, and
keep SQLite transactions limited to store work. No database connection crosses
a mailbox or owner-lock boundary. Writes use DELETE journaling,
`synchronous=EXTRA`, requested `fullfsync`, and directory synchronization before
acknowledgement. Temporary SQL storage is memory-only on an eligible SQLite
build; rollback-journal material stays beside the private database and its
permissions are checked before commit.

SQLite documents that [EXTRA synchronizes the journal directory after unlink](https://www.sqlite.org/pragma.html#pragma_synchronous),
while [fullfsync is platform-dependent](https://www.sqlite.org/pragma.html#pragma_fullfsync).
Its [temporary-file rules](https://www.sqlite.org/tempfiles.html) distinguish
memory temporaries from disk-backed transaction journals. Configuring these
settings does not qualify actual Mac F_FULLFSYNC, power-loss recovery or an
installed filesystem. Those claims remain unverified.

A failed commit acknowledgement may mean the row was saved. The store reports
a sanitized storage refusal and retains the database; reopening returns the
durable truth. It never turns that uncertainty into another dispatch. Storage
errors do not expose raw paths or backend error strings through `Refused`.

## Original draft verification, before Root's canonical run

Completed: source-only Python AST parsing of the new module and test file.
This did not import or execute them. No red/green test result is claimed.

Pending: the mandatory `scripts/run_tests.sh` run on Root's separately qualified
contained source environment, and independent source review. No alternative
pytest/unittest run, environment creation, dependency installation, or native
probe was performed by this lane.

The tests are written to use actual disposable SQLite files and reopenings,
competing connections, write-lock refusal, commit failure before/after the real
commit, directory-sync acknowledgement failure, private journal observations,
bounded capacity, unsafe paths/ownership, malformed data and wrapper STORE/STATUS
integration. Boundary fault injection wraps real SQLite transactions; it is not
evidence of a real power interruption. One alternate-ownership test requires the
isolated root test user and explicitly skips otherwise. No operating system is
mocked. Existing wrapper tests and runner-isolation tests retain their own scope;
their earlier Linux success does not execute these new tests.

Original draft hashes, preserved at `d07297fa6b601956622f9d50f1d428c476dcb85d`:

- store: `928179b211d47bd8c84b0d6ba4ac9fead4b262f9c877952b2d8dc3e49a716d31`
- new tests: `716630c9b0ccd56481fbd7f8b40e63bf518111e741d1ef6489421fb128245cf0`
- unchanged wrapper: `0d60fabdb4fc1153286721a0b264d846f4dd45e74a5f48094670f7de77ee2a20`
- unchanged wrapper tests: `e9c58efae08e0aec9a5b0de883aa26a61c2b6046dea7e61841f62cf219b85d9c`

## Next owner action

Root reviews and executes the frozen source using its approved canonical gate,
then decides source integration. Registration, the real owner's turn guard,
usage/budget authority and pre-submit/claim expiry hooks remain separate claims.
Native WAKE, native maintenance/timeout/database qualification, no-force and
Source101 holds remain unchanged. Do not use this component as a reason to probe
an owner or install a host.

No canonical Corey Guide changed: this disconnected draft adds no owner-visible
operation or supported recovery route. Root owns the eventual manual update,
Last verified date and owner-guide check when an owner-facing connection is
accepted. This technical handoff is not a competing owner manual.

## Forward correction — guard-compatible capability detection

Root executed the mandatory canonical runner on the exact original draft in its
network-disabled Linux container. The retained control and output are in the
private `hermes-receipt-store-canonical-20261012` receipt folder. The result was
59 passed, 40 failed and one deselected: unchanged wrapper 32 and isolation 22
passed; the new store had five negative-case passes and 40 constructor failures.
Those negative passes do not qualify directory protections that the constructor
never reached. The separate ownership case was not run.

The retained first ordinary create/store/reopen test fails at `_directory_fd`
before database creation. `os.open in os.supports_dir_fd` and the corresponding
`os.stat` membership check compare function identity, not actual support.
`tests/conftest.py` installs `HomeIOGuard`; `tests/home_io_guard.py` wraps these
functions, checks the supplied path and forwards `dir_fd` to the existing OS
implementation. It does not replace the original function objects in
`os.supports_dir_fd`. The source check therefore refused a supported guarded
operation. This is the retained RED evidence; the held head was not rerun.

Successor branch: `codex/bot-chat-receipt-store-fd-20261012`, based directly on
the immutable original `d07297fa6b601956622f9d50f1d428c476dcb85d`.
Only the same three claimed paths change:

- Remove function-identity membership as a prerequisite. Keep required hard
  no-follow flags and the existing checked descriptor walk, inode, ownership,
  private-file and transaction checks. Every OS call still goes through the
  installed guards; nothing unwraps, replaces or bypasses them.
- Map narrowly unsupported operations (`NotImplementedError`, alongside the
  existing OS/type errors) to the typed storage refusal. Do not catch general
  `RuntimeError`, `AssertionError` or pytest safety tripwires.
- Give the four existing long-text cases short diagnostic IDs. Inputs and
  assertions are unchanged; no test coverage is removed or added.

Successor source hashes:

- store: `b7fe3a39c9f034cad059935f3f1bffdc8a224d014171f6f00742f049715fde6b`
- tests: `ff62f1e3bf84c0d673ee28895ee79d586abb3728e0e63b83f157a0d24faa32fe`

Source-only AST parsing and diff checks pass. No successor execution result is
claimed. Root next runs the ordinary create/store/reopen case under the unchanged
canonical harness, then the broader approved scope only after that positive
case passes. The separate ownership gate remains pending. All native, provider,
budget, host-binding, manual, Source101 and installation limits above remain.


## Root integration verification — October 12, 2026

Reviewed successor: `7a77463ce53e5a2791a971a84a316bcb2c22b076`, tree
`1756fa78f8b152baa4be63590b54fef22a028fee`. Independent delta review and
execution closure cleared the disconnected component for source integration.
The original `d07297f` failure remains retained; its function-identity preflight
is replaced with guarded actual descriptor-relative calls and narrowly sanitized
unsupported-operation errors. No guard, old wrapper, dependency or caller changed.

The mandatory canonical runner passed 100 unique cases in a pinned, isolated
Linux arm64 environment. First, an actual create/store/reopen test passed.
Then 45 new-store, 32 unchanged-wrapper and 22 unchanged-isolation cases passed
with all container capabilities dropped. The one alternate-ownership case ran
separately and passed with only CAP_CHOWN effective; its container had no host
mounts, network or privileged mode. The first positive repeats one broad case
and is not counted as a 101st unique test. All 19 source/guard/dependency pins
matched before and after execution. The existing environment retained its 89
lock-matched distributions; no new dependency, provider or model was requested.

This is a selected Linux source gate, not a full repository or native Mac gate.
Mac full-fsync and power-loss recovery, real caller/owner registration, admission
gate, mailbox hooks, usage policy, runtime installation and ordinary delivery
remain unqualified. The store still has no runtime importer or default path.
Corey's canonical Hermes and collaboration manuals describe source preparation
separately from installed behavior; their October 12 verification and index
check accompany integration. Original author branches and history are preserved.
