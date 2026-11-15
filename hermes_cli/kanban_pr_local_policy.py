"""Machine-owner policy for repositories without an accepted local declaration.

Installed by the owner outside all repository worktrees. This requires genuine
local-gate evidence; it never emits a status or relaxes a server requirement.
"""
from __future__ import annotations

import hashlib
import json

from hermes_constants import get_default_hermes_root

POLICY_ID = "corey-github-local-review-all-projects-20261115"
POLICY_FILE = "git-review-policy.json"
EXPECTED_POLICY = {
    "schema": "hermes.owner-local-review.v1",
    "policy_id": POLICY_ID,
    "repository_owner": "dannydeleteo",
    "scope": "all-owned-repositories",
    "required_context": "local-gate",
    "status_creator": "dannydeleteo",
    "independent_review": True,
    "merge_result_tests": True,
}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate owner-policy key")
        result[key] = value
    return result


def installed_policy():
    """Only an explicit exact owner installation enables the fallback."""
    path = get_default_hermes_root() / POLICY_FILE
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > 16384:
        raise ValueError("Invalid installed owner policy")
    raw = path.read_bytes()
    policy = json.loads(raw, object_pairs_hook=_unique_object)
    if (policy != EXPECTED_POLICY or type(policy.get("independent_review")) is not bool
            or type(policy.get("merge_result_tests")) is not bool):
        raise ValueError("Installed owner policy does not match the approved contract")
    return path, hashlib.sha256(raw).hexdigest()


def missing_repository_policy(repo, base_sha, pr_base_sha):
    """The caller has already authenticated the repo and pinned its default ref."""
    if repo.split("/", 1)[0] != EXPECTED_POLICY["repository_owner"]:
        return None
    installed = installed_policy()
    if installed is None:
        return None
    path, digest = installed
    return {("local-gate", None)}, {
        "path": str(path), "schema": EXPECTED_POLICY["schema"],
        "owner_policy_id": POLICY_ID, "host_policy_sha256": digest,
        "base_sha": base_sha, "pr_base_sha": pr_base_sha,
        "commit_statuses": {"local-gate": EXPECTED_POLICY["status_creator"]},
        "source": "explicit-machine-owner-policy",
    }


def policy_is_current(metadata):
    digest = metadata.get("host_policy_sha256")
    if digest is None:
        return True
    installed = installed_policy()
    return installed is not None and installed[1] == digest
