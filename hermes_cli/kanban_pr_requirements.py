"""Required CI policy when GitHub explicitly withholds private-plan branch rules.

GitHub contents API supports a commit ref; a proposed PR cannot weaken the
accepted base policy. https://docs.github.com/en/rest/repos/contents#get-repository-content
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from urllib.parse import quote

POLICY_PATH = ".github/kanban-required-checks.json"
PM_LOCAL_STATUS_REPO = "dannydeleteo/greekfire-pm-desk"
_PLAN_MESSAGE = "Upgrade to GitHub Pro or make this repository public to enable this feature."
_RULES = re.compile(r"repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/rules/branches/[^?]+(?:\?.*)?")
_SHA = re.compile(r"[0-9a-f]{40}")


class FeatureUnavailable(RuntimeError):
    """A known plan restriction, or unavailable explicit accepted CI policy."""


def is_plan_restriction(endpoint: str, stdout: str | None) -> bool:
    if not _RULES.fullmatch(endpoint):
        return False
    try:
        body = json.loads(stdout or "")
    except ValueError:
        return False
    # gh --paginate --slurp wraps even a failed first response in one page.
    if isinstance(body, list) and len(body) == 1:
        body = body[0]
    return (isinstance(body, dict) and body.get("message") == _PLAN_MESSAGE
            and str(body.get("status")) == "403")


def from_rules(protection: dict, pages: list) -> set:
    required = {(r["context"], (r.get("app") or {}).get("databaseId"))
                for r in protection.get("requiredStatusChecks", [])}
    for page in pages:
        for rule in page:
            if rule["type"] == "required_status_checks":
                required.update((r["context"], r.get("integration_id"))
                                for r in rule["parameters"]["required_status_checks"])
    return required


def accepted_branch_sha(api, repo: str, branch: str, profile_home) -> str:
    # A PR can be retargeted to an author-controlled feature branch. Only the
    # repository's default branch supplies accepted fallback requirements.
    metadata = api(f"repos/{repo}", profile_home=profile_home)
    if not isinstance(metadata.get("default_branch"), str) or branch != metadata["default_branch"]:
        raise FeatureUnavailable("Fallback CI policy requires the repository's accepted default branch.")
    ref = api(f"repos/{repo}/git/ref/heads/{quote(branch, safe='')}", profile_home=profile_home)
    if ref["ref"] != f"refs/heads/{branch}" or ref["object"]["type"] != "commit" or not _SHA.fullmatch(ref["object"]["sha"]):
        raise ValueError("Accepted default branch commit is unavailable")
    return ref["object"]["sha"]


def _pm_local_status_policy(policy: dict, repo: str, branch: str) -> tuple[set, dict]:
    """Owner-declared PM exception; v1 and every other repository stay app-pinned."""
    expected = [{"context": "local-gate", "kind": "commit_status", "creator_login": "dannydeleteo"}]
    if (repo != PM_LOCAL_STATUS_REPO or branch != "main"
            or set(policy) != {"schema", "repository", "base_branch", "required_checks", "advisory_checks"}
            or policy["repository"] != repo or policy["base_branch"] != branch
            or policy["required_checks"] != expected):
        raise ValueError("Unsupported or ambiguous local-status policy")
    advisory = policy["advisory_checks"]
    if not isinstance(advisory, list) or len(advisory) > 50:
        raise ValueError("Invalid advisory checks")
    seen = set()
    for check in advisory:
        if (not isinstance(check, dict) or set(check) != {"context", "app_id"}
                or not isinstance(check["context"], str) or not check["context"].strip()
                or len(check["context"]) > 200 or check["context"] in seen
                or check["context"] == "local-gate" or type(check["app_id"]) is not int or check["app_id"] <= 0):
            raise ValueError("Invalid advisory provenance")
        seen.add(check["context"])
    return {("local-gate", None)}, {"schema": policy["schema"],
        "commit_statuses": {"local-gate": "dannydeleteo"}, "advisory_checks": advisory}


def repository_policy(api, repo: str, number: int, sha: str, branch: str, profile_home, *, allow_commit_status=False) -> tuple[set, dict]:
    # PR base.sha can remain the creation-time snapshot after main advances.
    base_sha = accepted_branch_sha(api, repo, branch, profile_home)
    current = api(f"repos/{repo}/pulls/{number}", profile_home=profile_home)
    pr_base_sha = current["base"]["sha"]
    if current["head"]["sha"] != sha or current["base"]["ref"] != branch or not _SHA.fullmatch(pr_base_sha):
        raise ValueError("PR changed before reading accepted policy")
    endpoint = f"repos/{repo}/contents/{POLICY_PATH}?ref={base_sha}"
    try:
        item = api(endpoint, profile_home=profile_home)
    except RuntimeError as exc:
        if getattr(exc, "http_status", None) == 404:
            raise FeatureUnavailable("Accepted repository CI policy is missing.") from None
        raise
    try:
        if (item["type"] != "file" or item["encoding"] != "base64" or item["path"] != POLICY_PATH
                or type(item["size"]) is not int or not 0 < item["size"] <= 16384):
            raise ValueError
        raw = base64.b64decode("".join(item["content"].split()), validate=True)
        blob = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
        if len(raw) != item["size"] or item["sha"] != blob:
            raise ValueError
        policy = json.loads(raw)
        policy_metadata = {}
        if policy.get("schema") == "hermes.kanban-required-checks.v2":
            # Old installed collectors do not verify typed status provenance.
            # A parser-first promotion must refuse until that reader is updated.
            if not allow_commit_status:
                raise ValueError("Reader does not support commit-status provenance")
            required, policy_metadata = _pm_local_status_policy(policy, repo, branch)
        else:
            checks = policy["required_checks"]
            if (policy["schema"] != "hermes.kanban-required-checks.v1" or policy["base_branch"] != branch
                    or not isinstance(checks, list) or not 0 < len(checks) <= 50):
                raise ValueError
            required = set()
            for check in checks:
                context, app_id = check["context"], check["app_id"]
                if (not isinstance(context, str) or not context.strip() or len(context) > 200
                        or type(app_id) is not int or app_id <= 0 or any(c == context for c, _ in required)):
                    raise ValueError
                required.add((context, app_id))
    except (ValueError, KeyError, TypeError, AttributeError):
        raise FeatureUnavailable("Accepted repository CI policy is invalid or incomplete.") from None
    return required, {"path": POLICY_PATH, "base_sha": base_sha, "pr_base_sha": pr_base_sha, "blob_sha": blob, **policy_metadata}
