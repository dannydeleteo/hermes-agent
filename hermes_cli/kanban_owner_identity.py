"""Explicit machine-owner credential routing for the installed local gate.

Reuse the standard Git credential helper in memory. Never copy credentials into
profile files, log them, or replace an explicitly configured profile identity.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from hermes_constants import get_default_hermes_root
from hermes_cli.kanban_pr_local_policy import installed_policy


def owner_credential_env(env, endpoint, query=None):
    effective = dict(os.environ if env is None else env)
    # Restrict this installation's opt-in to its owner's API reads.
    owner_read = endpoint.startswith("repos/dannydeleteo/") or (
        endpoint == "graphql" and query and 'owner:"dannydeleteo"' in query)
    if not owner_read or installed_policy() is None:
        return env
    if effective.get("GH_TOKEN") or effective.get("GITHUB_TOKEN"):
        return env
    config = effective.get("GH_CONFIG_DIR")
    if config and (Path(config) / "hosts.yml").exists():
        return env
    # A standard user gh login also takes precedence for an unassigned card.
    if not config:
        return env
    credential_env = dict(effective, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    result = subprocess.run(
        ["git", "credential", "fill"], cwd=get_default_hermes_root(),
        input="protocol=https\nhost=github.com\n\n", env=credential_env,
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
    )
    if result.returncode:
        raise ValueError("Existing owner Git credential is unavailable")
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    token = fields.get("password")
    if not token:
        raise ValueError("Existing owner Git credential is unavailable")
    # The returned identity is checked through the GitHub API before repo access.
    from hermes_platform.resolver.github import github_cli
    command = github_cli().command or ("gh",)
    effective["GH_TOKEN"] = token
    effective["GH_PROMPT_DISABLED"] = "1"
    probe = subprocess.run([*command, "api", "user", "--hostname", "github.com", "--jq", ".login"],
                           env=effective, capture_output=True, text=True, timeout=30, check=False)
    if probe.returncode or probe.stdout.strip() != "dannydeleteo":
        raise ValueError("Existing owner Git identity could not be verified")
    return effective
