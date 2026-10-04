"""Opt-in fixture for provider regressions in worktrees of an installed checkout.

Load with ``-p tests.agent.local_model_test_support``. Provider tests do not
exercise updater recovery; redirect its lookup to an empty temporary repository
instead of weakening the suite's real-home I/O guard. Production code is unchanged.
"""
import pytest


@pytest.fixture(autouse=True)
def isolated_updater_lookup(tmp_path, monkeypatch):
    from hermes_cli import _early_recovery
    root = tmp_path / "isolated-updater"
    (root / ".git").mkdir(parents=True)
    monkeypatch.setattr(_early_recovery, "_project_root", lambda: root)
