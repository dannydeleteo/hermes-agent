"""Plan restrictions are not auth failures; accepted policy still requires exact CI."""
import base64
import hashlib
import json
import subprocess

import pytest

from hermes_cli import kanban_pr_acceptance as gate
from hermes_cli.kanban_pr_requirements import FeatureUnavailable, POLICY_PATH


def test_only_exact_rules_plan_403_is_feature_unavailable(monkeypatch):
    monkeypatch.setattr(gate, "_gh_env", lambda _: None)
    message = "Upgrade to GitHub Pro or make this repository public to enable this feature."
    rules = "repos/acme/repo/rules/branches/main?per_page=100"
    for status, endpoint, text, feature in [
        (403, rules, message, True), (401, rules, message, False),
        (404, rules, message, False), (403, rules, "Resource not accessible", False),
        (403, "repos/acme/repo/pulls/7", message, False),
    ]:
        def fail(command, **kwargs):
            raise subprocess.CalledProcessError(1, command,
                output=json.dumps({"message": text, "status": str(status), "private": "DO_NOT_PERSIST"}),
                stderr=f"gh: private-context (HTTP {status})")
        monkeypatch.setattr(gate.subprocess, "run", fail)
        with pytest.raises(FeatureUnavailable if feature else gate._GateAuthError) as error:
            gate._api(endpoint)
        assert "DO_NOT_PERSIST" not in str(error.value) and "private-context" not in str(error.value)
    for count in [1, 2]:
        def slurped(command, **kwargs):
            raise subprocess.CalledProcessError(1, command,
                output=json.dumps([{"message": message, "status": "403"}] * count), stderr="HTTP 403")
        monkeypatch.setattr(gate.subprocess, "run", slurped)
        with pytest.raises(FeatureUnavailable if count == 1 else gate._GateAuthError):
            gate._api(rules)
    def malformed(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, output=message, stderr="HTTP 403")
    monkeypatch.setattr(gate.subprocess, "run", malformed)
    with pytest.raises(gate._GateAuthError):
        gate._api(rules)


def test_base_owned_policy_keeps_required_ci_and_head_guards(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sha, base = "a" * 40, "b" * 40
    for fault in [None, "missing_policy", "empty_policy", "bad_blob", "missing_check", "pending", "failure",
                  "wrong_app", "partial_pages", "head_change", "base_change", "auth", "no_rules", "nondefault_base",
                  "default_move", "bad_ref"]:
        state = {"checked": False, "requests": []}
        branch = "author-branch" if fault == "nondefault_base" else "main"
        def api(endpoint, **kwargs):
            state["requests"].append(endpoint)
            if endpoint == "repos/acme/repo":
                return {"default_branch": "main"}
            if "/git/ref/heads/" in endpoint:
                return {"ref": "refs/heads/main", "object": {"type": "blob" if fault == "bad_ref" else "commit",
                        "sha": "e" * 40 if state["checked"] and fault == "default_move" else base}}
            if endpoint == "graphql":
                return {"data": {"repository": {"pullRequest": {"headRefOid": sha, "baseRefName": branch,
                    "state": "OPEN", "baseRef": {"branchProtectionRule": None}}}}}
            if "/rules/branches/" in endpoint:
                if fault == "no_rules":
                    return [[]]
                raise FeatureUnavailable("Known plan restriction")
            if "/contents/" in endpoint:
                assert endpoint.endswith("?ref=" + base)
                if fault == "missing_policy":
                    raise gate._GateAuthError("policy missing", http_status=404)
                checks = [] if fault == "empty_policy" else [{"context": c, "app_id": 15368} for c in ["python", "node"]]
                raw = json.dumps({"schema": "hermes.kanban-required-checks.v1", "base_branch": "main", "required_checks": checks}).encode()
                blob = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
                return {"type": "file", "encoding": "base64", "path": POLICY_PATH, "size": len(raw),
                        "sha": "0" * 40 if fault == "bad_blob" else blob, "content": base64.b64encode(raw).decode()}
            if "/check-runs?" in endpoint:
                if fault == "auth":
                    raise gate._GateAuthError("HTTP 403 on checks", http_status=403)
                state["checked"] = True
                runs = [{"id": i, "name": name, "app": {"id": 9 if fault == "wrong_app" else 15368},
                         "head_sha": sha, "status": "in_progress" if fault == "pending" else "completed",
                         "conclusion": "failure" if fault == "failure" else "success"}
                        for i, name in enumerate(["python"] if fault == "missing_check" else ["python", "node"])]
                return [{"total_count": 3 if fault == "partial_pages" else len(runs), "check_runs": runs}]
            if "/statuses?" in endpoint:
                return [[]]
            if "/pulls/" in endpoint:
                return {"head": {"sha": "c" * 40 if state["checked"] and fault == "head_change" else sha},
                        "base": {"sha": "d" * 40 if state["checked"] and fault == "base_change" else "f" * 40,
                                 "ref": branch}, "state": "open"}
            raise AssertionError(endpoint)
        monkeypatch.setattr(gate, "_api", api)
        result = gate.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
        assert result["ok"] is (fault is None), (fault, result)
        if fault is None:
            assert {c["name"] for c in result["checks"]} == {"python", "node"}
            assert result["required_policy"]["base_sha"] == base
        if fault in {"missing_policy", "empty_policy", "bad_blob", "nondefault_base"}:
            assert result["classification"] == "feature_unavailable"
        if fault in {"head_change", "base_change", "default_move"}:
            assert result["classification"] == "stale"
        if fault == "auth":
            assert result["classification"] == "auth"
