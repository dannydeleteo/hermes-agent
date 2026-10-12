"""The canonical shell must run tests and propagate a failing test's status."""
from pathlib import Path
import os
import shutil
import subprocess
import sys

import pytest


@pytest.mark.platforms("posix")
def test_shell_runner_executes_tests_and_propagates_failure(tmp_path):
    case = tmp_path / "test_runner_canary.py"
    marker = tmp_path / "executed"
    case.write_text(
        "from pathlib import Path\nimport os\n"
        "def test_canary():\n"
        f"    Path({str(marker)!r}).write_text('executed')\n"
        "    assert os.environ['PATHEXT'] == '.COM;.EXE;.BAT;.CMD'\n"
        "    assert False, 'runner failure propagation canary'\n",
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["bash", str(root / "scripts/run_tests.sh"), "-j", "1", str(case)],
        cwd=tmp_path, capture_output=True, text=True, timeout=180,
        env={**os.environ, "HERMES_PYTHON": sys.executable, "HERMES_TEST_FILE_RETRIES": "0",
             "PATHEXT": ".COM;.EXE;.BAT;.CMD"},
    )
    assert marker.read_text(encoding="utf-8") == "executed", result.stdout + result.stderr
    assert result.returncode != 0, result.stdout + result.stderr
    assert "runner failure propagation canary" in result.stdout + result.stderr


def _isolation_shell_fixture(tmp_path):
    """Copied source only; activation is a failing fixture, never the installed PM."""
    scripts = tmp_path / "runner-fixture" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[2] / "scripts"
    for name in ("run_tests.sh", "run_tests_isolation.py"):
        shutil.copy2(source / name, scripts / name)
    marker = scripts.parent / "activation-was-called"
    (scripts / "_activation.sh").write_text("hermes_activation_current() { return 1; }\n", encoding="utf-8")
    (scripts.parent / "activate").write_text(f"touch '{marker}'\nreturn 42\n", encoding="utf-8")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("HERMES_", "__HERMES_"))}
    return scripts, marker, env


@pytest.mark.platforms("posix")
def test_isolation_requires_explicit_interpreter_before_activation(tmp_path):
    scripts, marker, env = _isolation_shell_fixture(tmp_path)
    root = tmp_path / "private-scratch"
    root.mkdir(mode=0o700)
    result = subprocess.run(
        ["bash", str(scripts / "run_tests.sh"), "--isolation-root", str(root)],
        env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 2
    assert "explicit" in result.stderr
    assert not marker.exists()


@pytest.mark.platforms("posix")
@pytest.mark.skipif(sys.version_info[:2] != (3, 14), reason="isolated mode requires qualified Python 3.14")
def test_invalid_isolation_root_fails_before_activation_or_pytest(tmp_path):
    scripts, marker, env = _isolation_shell_fixture(tmp_path)
    result = subprocess.run(
        ["bash", str(scripts / "run_tests.sh"), "--isolation-root", "relative-scratch"],
        env={**env, "HERMES_PYTHON": sys.executable}, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 2
    assert "absolute canonical" in result.stderr
    assert not marker.exists()
