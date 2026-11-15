"""Explicit machine policy enables all owned projects without weakening evidence."""
import json

import pytest

from hermes_cli import kanban_pr_acceptance as gate
from hermes_cli import kanban_pr_local_policy as local
from tests.hermes_cli.test_kanban_owner_local_gate import ProjectGitHub


@pytest.fixture
def installed(tmp_path, monkeypatch):
    monkeypatch.setattr(local, "get_default_hermes_root", lambda: tmp_path)
    path = tmp_path / local.POLICY_FILE
    path.write_text(json.dumps(local.EXPECTED_POLICY), encoding="utf-8")
    return path


def absent_policy_api(monkeypatch, repo="dannydeleteo/agent-bridge", **kwargs):
    api = ProjectGitHub(repo, **kwargs)

    def request(endpoint, **options):
        if "/contents/" in endpoint:
            raise gate._GateAuthError("Synthetic missing declaration", http_status=404)
        return api(endpoint, **options)

    monkeypatch.setattr(gate, "_api", request)
    return api


PROJECTS = ["greekfire-pm-desk", "agent-bridge", "night-watch", "greekfire-brain",
            "youtube-creator", "greekfire-investments", "kdp-publishing", "greekfire-factset",
            "woodshed", "hermes-agent", "powerpermit", "frontier-discovery", "greekfire-quant",
            "13fagents", "Greekfire-bug-mitigation", "greekfire-finance", "greekfire-real-estate",
            "future-owned-project"]


@pytest.mark.parametrize("project", PROJECTS)
@pytest.mark.parametrize("readable_rules", [False, True])
def test_installed_policy_accepts_only_genuine_status_for_each_project(installed, monkeypatch, project, readable_rules):
    repo = "dannydeleteo/" + project
    api = absent_policy_api(monkeypatch, repo, readable_rules=readable_rules)
    result = gate.collect_acceptance(repo, f"https://github.com/{repo}/pull/7")
    assert result["ok"], result
    assert result["required_policy"]["source"] == "explicit-machine-owner-policy"
    assert result["required_policy"]["owner_policy_id"] == local.POLICY_ID
    assert result["checks"][0]["kind"] == "commit_status"
    assert not any("/check-runs?" in e for e in api.requests)


@pytest.mark.parametrize("fault", ["missing", "pending", "failure", "error", "wrong_creator",
                                  "missing_creator", "wrong_head", "wrong_url", "forged_check_run",
                                  "latest_failure", "head_change", "base_change", "default_move"])
def test_machine_policy_does_not_pass_bad_evidence(installed, monkeypatch, fault):
    repo = "dannydeleteo/agent-bridge"
    absent_policy_api(monkeypatch, repo, fault=fault)
    assert not gate.collect_acceptance(repo, f"https://github.com/{repo}/pull/7")["ok"]


@pytest.mark.parametrize("fault,passes", [("server_check", True), ("server_wrong_app", False)])
def test_machine_policy_preserves_server_requirement(installed, monkeypatch, fault, passes):
    repo = "dannydeleteo/agent-bridge"
    absent_policy_api(monkeypatch, repo, fault=fault, readable_rules=True)
    result = gate.collect_acceptance(repo, f"https://github.com/{repo}/pull/7")
    assert result["ok"] is passes
    assert {c["context"] for c in result["required"]} == {"python", "local-gate"}


@pytest.mark.parametrize("fault", ["bad_blob", "wrong_repo", "wrong_policy_creator"])
def test_installed_policy_never_masks_invalid_existing_declaration(installed, monkeypatch, fault):
    repo = "dannydeleteo/agent-bridge"
    monkeypatch.setattr(gate, "_api", ProjectGitHub(repo, fault=fault))
    assert not gate.collect_acceptance(repo, f"https://github.com/{repo}/pull/7")["ok"]


def test_machine_policy_is_not_authority_for_other_owners(installed, monkeypatch):
    repo = "third-party/project"
    absent_policy_api(monkeypatch, repo)
    assert not gate.collect_acceptance(repo, f"https://github.com/{repo}/pull/7")["ok"]


@pytest.mark.parametrize("body", ["{}", "{", '{"policy_id":"a","policy_id":"b"}',
                                  json.dumps({**local.EXPECTED_POLICY, "independent_review": 1}),
                                  json.dumps({**local.EXPECTED_POLICY, "status_creator": "other"})])
def test_invalid_installed_policy_refuses(installed, monkeypatch, body):
    installed.write_text(body, encoding="utf-8")
    absent_policy_api(monkeypatch)
    result = gate.collect_acceptance("dannydeleteo/agent-bridge", "https://github.com/dannydeleteo/agent-bridge/pull/7")
    assert not result["ok"] and result["classification"] == "feature_unavailable"


def test_installed_policy_change_during_read_refuses(installed, monkeypatch):
    api = absent_policy_api(monkeypatch)
    api.race = lambda: installed.write_text(json.dumps(local.EXPECTED_POLICY, indent=2), encoding="utf-8")
    result = gate.collect_acceptance("dannydeleteo/agent-bridge", "https://github.com/dannydeleteo/agent-bridge/pull/7")
    assert not result["ok"] and result["classification"] == "stale"


def test_old_collector_cannot_enable_machine_policy(installed, monkeypatch):
    from hermes_cli.kanban_pr_requirements import repository_policy, FeatureUnavailable
    api = absent_policy_api(monkeypatch)
    with pytest.raises(FeatureUnavailable):
        repository_policy(gate._api, api.repo, 7, "a" * 40, "main", None)


def test_missing_machine_installation_is_not_implicit_permission(tmp_path, monkeypatch):
    monkeypatch.setattr(local, "get_default_hermes_root", lambda: tmp_path)
    absent_policy_api(monkeypatch)
    result = gate.collect_acceptance("dannydeleteo/agent-bridge", "https://github.com/dannydeleteo/agent-bridge/pull/7")
    assert not result["ok"] and result["classification"] == "feature_unavailable"
