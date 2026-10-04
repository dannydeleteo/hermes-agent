"""Job-specific script budgets must stay inside the owning profile and script."""

from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("overrides, expected", [
    ({"daily-research": value}, expected) for value, expected in [
    (1500, 1500), ("1500", 1500),
    (True, 660), (False, 660), (0, 660), (-1, 660),
    (float("inf"), 660), (float("nan"), 660), ("invalid", 660),
    (None, 660), ({}, 660), (0.5, 660),
    ]
] + [({}, 660), (None, 660), ([], 660)])
def test_script_budget_is_job_and_profile_local(tmp_path, monkeypatch, overrides, expected):
    from agent.secret_scope import reset_multiplex_context, set_multiplex_context
    from cron import monitor, scheduler, scheduler_prompt, scheduler_script
    from cron.scheduler_provider import _profile_cron_scope
    from hermes_cli.config import atomic_config_replace, atomic_config_write

    root = tmp_path / ".hermes"
    profiles = (root, root / "profiles" / "other")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_CRON_SCRIPT_TIMEOUT", "99")  # launch-profile residue
    monkeypatch.setattr(scheduler, "_SCRIPT_TIMEOUT", scheduler._DEFAULT_SCRIPT_TIMEOUT)
    for home, mapping in zip(profiles, (overrides, {"daily-research": 900})):
        (home / "scripts").mkdir(parents=True)
        (home / "scripts" / "probe.sh").write_text("exit 0\n", encoding="utf-8")
        atomic_config_write(home / "config.yaml", {"cron": {
            "script_timeout_seconds": 660,
            "script_timeout_seconds_by_job": mapping,
        }})

    # Exercise the real execution/deadline and prompt paths. Only the OS process
    # and clock boundaries are simulated; tree termination has real-process coverage.
    class FinishedProcess:
        def poll(self):
            return 0

        def communicate(self, timeout):
            return "", ""

    monkeypatch.setattr(scheduler_script.subprocess, "Popen", lambda *a, **k: FinishedProcess())

    def execute(call):
        clock = iter((0, 10000))
        monkeypatch.setattr(scheduler_script, "time", SimpleNamespace(monotonic=lambda: next(clock)))
        return call()

    token = set_multiplex_context(True)
    try:
        for home, seconds in ((profiles[0], expected), (profiles[1], 900), (profiles[0], expected)):
            with _profile_cron_scope(home):
                job = {"id": "daily-research", "script": "probe.sh", "prompt": "Report."}
                ok, output = execute(lambda: scheduler_script._run_job_script_with_claim_heartbeat(job, "probe.sh"))
                assert not ok and output.startswith(f"Script timed out after {seconds}s:")
                prompt = execute(lambda: scheduler_prompt._build_job_prompt(job))
                assert f"Script timed out after {seconds}s:" in prompt
                ok, output = execute(lambda: monitor._run_monitor_source({
                    "id": "daily-research", "monitor_script": "probe.sh"}))
                assert not ok and output.startswith("Script timed out after 660s:")
                for job_id in ("unrelated", None):
                    ok, output = execute(lambda: scheduler_script._run_job_script("probe.sh", job_id=job_id))
                    assert not ok and output.startswith("Script timed out after 660s:")

        # A matching valid entry wins even over a legacy patched module or env
        # value, while every absent-job read keeps the old precedence intact.
        with _profile_cron_scope(profiles[0]):
            env_file = profiles[0] / ".env"
            env_file.write_text("HERMES_CRON_SCRIPT_TIMEOUT=720\n", encoding="utf-8")
            assert scheduler_script._get_script_timeout("unrelated") == 720
            assert scheduler_script._get_script_timeout("daily-research") == (1500 if expected == 1500 else 720)
            monkeypatch.setattr(scheduler, "_SCRIPT_TIMEOUT", 800)
            assert scheduler_script._get_script_timeout("daily-research") == (1500 if expected == 1500 else 800)
            assert scheduler_script._get_script_timeout("unrelated") == 800
            assert scheduler_script._get_script_timeout() == 800
            monkeypatch.setattr(scheduler, "_SCRIPT_TIMEOUT", scheduler._DEFAULT_SCRIPT_TIMEOUT)
            env_file.unlink()
            assert scheduler_script._get_script_timeout("daily-research") == expected
            atomic_config_replace(profiles[0] / "config.yaml", {
                "cron": {"script_timeout_seconds_by_job": overrides}})
            assert scheduler_script._get_script_timeout("daily-research") == (1500 if expected == 1500 else 3600)
            atomic_config_replace(profiles[0] / "config.yaml", {})
            assert scheduler_script._get_script_timeout("daily-research") == 3600
    finally:
        reset_multiplex_context(token)
