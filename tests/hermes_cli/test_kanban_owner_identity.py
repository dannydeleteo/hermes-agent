"""Owner opt-in uses existing Git identity without crossing profile boundaries."""
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_owner_identity as identity
from hermes_cli import kanban_pr_local_policy as local


@pytest.fixture
def owner(tmp_path, monkeypatch):
    monkeypatch.setattr(local, "get_default_hermes_root", lambda: tmp_path)
    monkeypatch.setattr(identity, "get_default_hermes_root", lambda: tmp_path)
    (tmp_path / local.POLICY_FILE).write_text(json.dumps(local.EXPECTED_POLICY))
    return {"GH_CONFIG_DIR": str(tmp_path / "profile" / "gh")}


def test_existing_credential_used_only_in_child_memory(owner, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[:3] == ["git", "credential", "fill"]:
            assert kwargs["env"]["GCM_INTERACTIVE"] == "never"
            return SimpleNamespace(returncode=0, stdout="username=owner\npassword=fictional-token\n")
        assert kwargs["env"]["GH_TOKEN"] == "fictional-token"
        return SimpleNamespace(returncode=0, stdout="dannydeleteo\n")

    monkeypatch.setattr(identity.subprocess, "run", run)
    result = identity.owner_credential_env(owner, "repos/dannydeleteo/agent-bridge")
    assert result["GH_TOKEN"] == "fictional-token"
    assert "GH_TOKEN" not in owner
    assert len(calls) == 2


@pytest.mark.parametrize("reason", ["other_repo", "token", "github_token", "existing_login", "no_config", "no_policy"])
def test_no_fallback_without_explicit_route(owner, monkeypatch, tmp_path, reason):
    endpoint = "repos/dannydeleteo/agent-bridge"
    if reason == "other_repo":
        endpoint = "repos/another-owner/agent-bridge"
    if reason == "token":
        owner["GH_TOKEN"] = "profile-owned"
    if reason == "github_token":
        owner["GITHUB_TOKEN"] = "profile-owned"
    if reason == "existing_login":
        config = tmp_path / "profile" / "gh"
        config.mkdir(parents=True)
        (config / "hosts.yml").write_text("existing login")
    if reason == "no_config":
        owner.clear()
    if reason == "no_policy":
        (tmp_path / local.POLICY_FILE).unlink()

    def forbidden(*args, **kwargs):
        pytest.fail("A configured profile identity must not be replaced")

    monkeypatch.setattr(identity.subprocess, "run", forbidden)
    assert identity.owner_credential_env(owner, endpoint) is owner


@pytest.mark.parametrize("failure", ["credential_failure", "missing_token", "wrong_owner", "probe_failure"])
def test_identity_failure_is_closed_and_secret_free(owner, monkeypatch, failure):
    def run(command, **kwargs):
        if command[:3] == ["git", "credential", "fill"]:
            return SimpleNamespace(returncode=int(failure == "credential_failure"),
                stdout="" if failure == "missing_token" else "password=fictional-secret\n")
        return SimpleNamespace(returncode=int(failure == "probe_failure"), stdout="someone-else\n")

    monkeypatch.setattr(identity.subprocess, "run", run)
    with pytest.raises(ValueError) as exc:
        identity.owner_credential_env(owner, "repos/dannydeleteo/agent-bridge")
    assert "fictional-secret" not in str(exc.value)
