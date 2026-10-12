"""Private opt-in policy for the canonical runner; no import-time I/O.

The isolation root contains scratch only. It is not an OS sandbox, a substitute
for the protected test guards, or permission to run arbitrary test code. Real
interpreter/guard imports and owned-child cleanup require separate qualification.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import math
import os
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time


class IsolationError(RuntimeError):
    """Fail-closed isolation preflight or ownership failure."""


@dataclass(frozen=True)
class IsolationRoot:
    path: str
    uid: int
    device: int
    inode: int

    def revalidate(self, *, system=os) -> None:
        """Reject replaced directories; never follow/repair a changed root."""
        try:
            info = system.lstat(self.path)
            valid = (
                system.path.realpath(self.path) == self.path
                and stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700
                and info.st_uid == self.uid == system.getuid()
                and (info.st_dev, info.st_ino) == (self.device, self.inode)
            )
        except (AttributeError, OSError):
            valid = False
        if not valid:
            raise IsolationError("isolation root changed or could not be revalidated")


def validate_root(raw: str, repo_root: str, *, system=os) -> IsolationRoot:
    """Require an existing canonical, private, caller-owned scratch directory.

    Metadata/realpath reads are the only filesystem operations. No directory is
    created, repaired, chmodded or deleted by validation. The injectable system
    is an OS seam for source-only fakes, not an alternate runtime configuration.
    """
    if (not isinstance(raw, str) or not system.path.isabs(raw)
            or system.path.normpath(raw) != raw or system.path.realpath(raw) != raw):
        raise IsolationError("isolation root must be an absolute canonical non-symlink path")
    # The entry shell may have reached the checkout/home through a symlink;
    # exclusions refer to their actual locations, not just their caller spelling.
    home = system.path.realpath(system.path.expanduser("~"))
    repo_root = system.path.realpath(repo_root)
    if (raw == "/" or raw in (home, repo_root) or home.startswith(raw + "/")
            or repo_root.startswith(raw + "/") or raw.startswith(repo_root + "/")
            or any(raw == home + suffix or raw.startswith(home + suffix + "/")
                   for suffix in ("/.hermes", "/.greekfire"))):
        raise IsolationError("isolation root must be dedicated scratch outside source and private runtime roots")
    try:
        uid = system.getuid()
        info = system.lstat(raw)
    except (AttributeError, OSError):
        raise IsolationError("isolation root ownership could not be verified") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
        raise IsolationError("isolation root must be caller-owned with mode 0700")
    return IsolationRoot(raw, uid, info.st_dev, info.st_ino)


@dataclass(frozen=True)
class Attempt:
    path: str
    environment: dict[str, str]


def new_attempt(root: IsolationRoot, environment: dict[str, str], *,
                system=os, allocate=tempfile.mkdtemp) -> Attempt:
    """Allocate one fresh descendant; retain it as evidence, never sweep siblings.

    Validation is repeated around allocation. The separately required OS sandbox
    is still the write fence against path replacement by another same-user writer.
    """
    root.revalidate(system=system)
    try:
        path = allocate(prefix="r-", dir=root.path)
        root.revalidate(system=system)
        info = system.lstat(path)
    except (OSError, TypeError):
        raise IsolationError("private attempt directory could not be created") from None
    if (system.path.dirname(path) != root.path or system.path.realpath(path) != path
            or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != root.uid or info.st_dev != root.device or info.st_ino == root.inode):
        raise IsolationError("attempt directory is not a fresh private child of the admitted root")
    env = environment.copy()
    for name in ("TMPDIR", "TEMP", "TMP", "PYTEST_DEBUG_TEMPROOT"):
        env[name] = path
    env["HERMES_GATEWAY_LOCK_DIR"] = path + "/gateway-locks"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # The canonical shell already scrubs this. Also keep direct internal calls
    # from inheriting hidden pytest options; explicit arguments remain visible.
    env.pop("PYTEST_ADDOPTS", None)
    return Attempt(path, env)


def require_owned_child_support(*, system=os, signals=signal) -> None:
    """Refuse platforms/dispositions where a child cannot remain exclusively waitable."""
    required = ("waitid", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT", "getpgid", "killpg", "getuid")
    if (system.name != "posix" or any(not hasattr(system, name) for name in required)
            or not hasattr(signals, "SIGCHLD") or signals.getsignal(signals.SIGCHLD) != signals.SIG_DFL):
        raise IsolationError("isolated runner requires POSIX waitid/WNOWAIT and default SIGCHLD handling")


class OwnedChild:
    """Exclusive waiter for a Popen child launched with start_new_session=True.

    Never call Popen.poll/wait/communicate outside this owner before finish().
    WNOWAIT preserves waitability (https://docs.python.org/3.14/library/os.html#os.WNOWAIT).
    Under that exclusive-waiter contract, the unreaped session leader pins the
    numeric identity until group cleanup. A reaped/lost/foreign identity refuses
    signals; there is no fallback PID kill, global search or process-tree walk.
    Native semantics and the OS sandbox still need separate qualification.
    """
    def __init__(self, process, *, system=os, signals=signal):
        require_owned_child_support(system=system, signals=signals)
        self.process, self.system, self.signals = process, system, signals
        self.pid = process.pid
        self.closed = False

    def _observe(self):
        if (self.closed or not isinstance(self.pid, int) or isinstance(self.pid, bool)
                or self.pid <= 0 or self.process.pid != self.pid or self.process.returncode is not None):
            raise IsolationError("child identity is no longer owned and unreaped; refusing signals")
        require_owned_child_support(system=self.system, signals=self.signals)
        try:
            result = self.system.waitid(
                self.system.P_PID, self.pid,
                self.system.WEXITED | self.system.WNOHANG | self.system.WNOWAIT,
            )
            if result is not None and (result.si_pid != self.pid or result.si_uid != self.system.getuid()):
                raise IsolationError("wait observation belongs to another child")
            if self.system.getpgid(self.pid) != self.pid:
                raise IsolationError("owned child is not its own session process-group leader")
        except OSError:
            raise IsolationError("child ownership is unavailable; refusing signals") from None
        return result

    def exited(self) -> bool:
        return self._observe() is not None

    def finish(self) -> int:
        """Terminate only the still-owned group BEFORE reaping, including normal exit."""
        self._observe()
        self.closed = True
        try:
            self.system.killpg(self.pid, self.signals.SIGKILL)
        except ProcessLookupError:
            pass  # No live group remains; the exact unreaped child is still ours.
        except OSError:
            raise IsolationError("owned child cleanup was denied; no fallback signaling is allowed") from None
        try:
            return self.process.wait(timeout=10)
        except Exception:
            raise IsolationError("owned child could not be reaped; cleanup is unverified") from None


def require_timeout(timeout: float) -> None:
    try:
        valid = (isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
                 and math.isfinite(timeout) and timeout > 0)
    except OverflowError:
        valid = False
    if not valid:
        raise IsolationError("isolated child timeout must be finite and positive")


def collect_owned(process, timeout: float, *, system=os, signals=signal,
                  selector_factory=selectors.DefaultSelector, clock=time.monotonic) -> tuple[str, int]:
    """Drain pipes while waiting WITHOUT reaping; clean the owned group, then reap.

    The process must be a newly launched, exclusive, start_new_session=True child.
    No poll/communicate, process inventory, stale PID fallback or retry is used.
    """
    owner = OwnedChild(process, system=system, signals=signals)
    mux = None
    chunks = []
    eof = False

    def drain(delay):
        nonlocal eof
        for key, _ in mux.select(delay):
            chunk = system.read(key.fd, 65536)
            if chunk:
                chunks.append(chunk)
            else:
                mux.unregister(process.stdout)
                eof = True

    try:
        require_timeout(timeout)
        mux = selector_factory()
        mux.register(process.stdout, selectors.EVENT_READ)
        deadline = clock() + timeout
        timed_out = False
        while not owner.exited():
            remaining = deadline - clock()
            if remaining <= 0:
                timed_out = True
                break
            drain(min(0.05, remaining))
        status = owner.finish()
        drain_deadline = clock() + 10
        while not eof and clock() < drain_deadline:
            drain(0.05)
        if not eof:
            raise IsolationError("output pipe did not close after owned cleanup; no wider signaling is allowed")
        output = b"".join(chunks).decode("utf-8", errors="replace")
        if timed_out:
            return "(isolated test file timeout; owned group cleaned before reap)\n" + output, 124
        return output, status
    except BaseException:
        if not owner.closed:
            owner.finish()
        raise
    finally:
        if mux is not None:
            mux.close()
        process.stdout.close()


def launch_isolated(command: list[str], root: IsolationRoot, repo_root: str,
                    environment: dict[str, str], timeout: float, *, system=os, signals=signal,
                    allocate=tempfile.mkdtemp, spawn=subprocess.Popen,
                    selector_factory=selectors.DefaultSelector, clock=time.monotonic) -> tuple[str, int]:
    """One source-test attempt, using a prequalified interpreter supplied by the runner.

    The command is the runner's pytest argv, not a general command-execution API.
    All child/filesystem capabilities are injectable for disconnected policy probes.
    This function does not install dependencies or establish interpreter trust.
    """
    require_owned_child_support(system=system, signals=signals)
    require_timeout(timeout)
    if "--" in command:
        raise IsolationError("isolated pytest argv cannot end option parsing before private scratch options")
    root.revalidate(system=system)
    attempt = new_attempt(root, environment, system=system, allocate=allocate)
    root.revalidate(system=system)
    # Pytest's final explicit option wins over config/earlier options. Pin its
    # destructive basetemp initialization to a NEW child of this private attempt.
    # https://docs.pytest.org/en/stable/example/simple.html#how-to-change-command-line-options-defaults
    command = [*command, "--basetemp", attempt.path + "/pytest"]
    try:
        process = spawn(command, cwd=repo_root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        env=attempt.environment, start_new_session=True, text=False)
    except OSError:
        raise IsolationError("isolated child launch failed; scratch retained") from None
    return collect_owned(process, timeout, system=system, signals=signals,
                         selector_factory=selector_factory, clock=clock)


def preflight(raw: str, repo_root: str, expected_python: str, *, executable=sys.executable,
              version=sys.version_info[:2], system=os, signals=signal) -> IsolationRoot:
    """Verify the declared interpreter route and root without activation or allocation.

    Existence/version/route checks do not certify dependency contents or protected
    guard imports: the operator must separately qualify that existing interpreter.
    """
    # Do not compare only realpath: distinct venv routes may share the same
    # symlink target while importing different, unqualified dependencies.
    if (not isinstance(expected_python, str) or not system.path.isabs(expected_python)
            or system.path.normpath(expected_python) != expected_python
            or expected_python != executable
            or tuple(version) != (3, 14)):
        raise IsolationError("isolation requires an explicitly qualified existing Python 3.14 interpreter")
    root = validate_root(raw, repo_root, system=system)
    require_owned_child_support(system=system, signals=signals)
    return root


def main(argv=None) -> int:
    """Internal shell preflight only; never executes a test or creates scratch."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--expected-python", required=True)
    args = parser.parse_args(argv)
    try:
        preflight(args.root, args.repo_root, args.expected_python)
    except IsolationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
