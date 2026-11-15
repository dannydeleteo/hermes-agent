# Owner project local-gate acceptance

The owner policy extends the existing typed commit-status declaration to every
current and future repository in the `dannydeleteo` owner namespace. It is not
an exemption for third-party repositories or for a PR's own policy file.

An accepted default-branch `.github/kanban-required-checks.json` may use:

```json
{
  "schema": "hermes.kanban-required-checks.v2",
  "repository": "dannydeleteo/example-project",
  "base_branch": "main",
  "required_checks": [
    {"context": "local-gate", "kind": "commit_status", "creator_login": "dannydeleteo"}
  ],
  "advisory_checks": []
}
```

The repository and branch fields must match the actual accepted default target;
the default branch need not be named main. The policy blob is authenticated by
its Git blob hash at the current accepted default commit, never the proposed
head. The caller must support typed commit-status provenance. The newest actual
status must have the exact repository/head URL, creator, positive integer ID and
success state. A check-run cannot impersonate it. Policy, PR head and base pins
are rechecked after evidence collection. Existing lifecycle ownership/reclaim
fences still guard durable completion.

The accepted declaration applies even if rule APIs become readable. Real
server-required checks are always unioned with it, retaining their app pins.
A plan-limited branch-rule HTTP403 is not proof of a blocking protection and
requires no subscription purchase. Other authorization failures remain real
failures. This code does not modify server settings or manufacture statuses.

Readable server rules on a stacked, nondefault target retain their original
server-check gate; an author-controlled branch cannot supply fallback authority.
A missing optional default declaration does not erase genuine server checks.
Malformed declarations fail closed. Neither no declaration/no checks nor a
missing/failing/foreign/stale status becomes a pass.

The existing PM declaration and Woodshed's separate
`hermes.kanban-required-checks.woodshed-local.v1` declaration remain supported.
The latter has no advisory exemption. Legacy v1 declarations remain app-pinned.

A valid declaration is not proof that an arbitrary test command is safe or that
an installation happened. The canonical integration owner must independently
review the exact full source head, run documented project-specific checks on the
actual merge result with the latest target, recheck the pins, and publish the
genuine local-gate result. No free-form command runner, direct-main push,
force-push, administrative bypass, new subscription or change to no-remote
source-acceptance routes is introduced here.

Synthetic API and real disposable SQLite tests cover owned projects, future
project names, non-main defaults, stacked targets, rule-read availability,
server-check union, malformed/cross-repository policies, status impersonation,
newest adverse results, changed head/base/default and reclaimed run ownership.
Host-native/platform qualification, canonical helper registration, accepted
per-repository policy publication and live installation are separate receipts.

## Explicit Windows owner installation

Corey's standing 15 November instruction can also be installed as the exact
`hermes.owner-local-review.v1` contract in the machine Hermes root's
`git-review-policy.json`. `hermes_cli.kanban_pr_local_policy.EXPECTED_POLICY`
defines its complete contents. This is outside repository worktrees and applies
to all current/future profiles. The reader uses it only when an authenticated
read confirms the accepted repository declaration is absent. Existing malformed
declarations still refuse; real server requirements remain required. The receipt
records the host policy hash and the gate checks it again before completion.

The portable official `gh` executable is resolved from the machine Hermes
`tools/github-cli` directory when it is absent from PATH. For a routed profile
without its own GitHub token or `hosts.yml`, the explicit machine policy permits
owner-repository reads using the already configured Git credential helper. The
helper runs outside the worktree, with interaction disabled. Its credential is
passed only in the child process environment and GitHub must confirm the owner
identity first. Configured profile credentials are never replaced. No broader
token, plaintext credential copy or subscription is created. Same-profile workers
and unassigned cards preserve any configured standard gh login; only a confirmed
empty gh identity configuration permits the same owner fallback.
