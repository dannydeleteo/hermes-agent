"""Source-only policy probes; filesystem and child operations are injected fakes.

Standalone stdlib execution is preliminary evidence, not canonical-runner acceptance.
No child process, signal, native profile or real test runner is executed here.
"""
from pathlib import Path
from types import SimpleNamespace
import posixpath
import stat
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.run_tests_isolation import IsolationError, validate_root


class FakeSystem:
    """Supplied filesystem metadata, not a temporary directory on the actual host."""
    def __init__(self):
        self.metadata = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=501, st_dev=2, st_ino=100)
        self.rows = {}
        self.path = SimpleNamespace(
            isabs=posixpath.isabs, normpath=posixpath.normpath,
            dirname=posixpath.dirname,
            realpath=lambda path: path, expanduser=lambda path: "/Users/owner",
        )
        self.getuid = lambda: 501
        self.lstat = lambda path: self.rows.get(path, self.metadata)


class IsolationPolicyTests(unittest.TestCase):
    def test_existing_private_owned_root_is_bound_to_its_identity(self):
        root = validate_root("/private/var/tmp/isolated-tests-abc", "/project", system=FakeSystem())
        self.assertEqual(root.path, "/private/var/tmp/isolated-tests-abc")
        self.assertEqual((root.uid, root.device, root.inode), (501, 2, 100))

    def test_unsafe_or_unverifiable_roots_fail_without_repair(self):
        for raw in ("relative", "", "/", "/Users", "/Users/owner", "/project", "/project/scratch",
                    "/Users/owner/.hermes/scratch", "/Users/owner/.greekfire/scratch",
                    "/private/var/tmp/scratch/", "/private/var/tmp/a/../scratch"):
            with self.subTest(raw=raw), self.assertRaises(IsolationError):
                validate_root(raw, "/project", system=FakeSystem())
        for field, value in (("st_uid", 999), ("st_mode", stat.S_IFDIR | 0o755),
                             ("st_mode", stat.S_IFREG | 0o700), ("st_mode", stat.S_IFLNK | 0o700)):
            fs = FakeSystem()
            setattr(fs.metadata, field, value)
            with self.subTest(field=field, value=value), self.assertRaises(IsolationError):
                validate_root("/private/var/tmp/scratch", "/project", system=fs)
        fs = FakeSystem()
        fs.path.realpath = lambda path: "/other/place"
        with self.assertRaises(IsolationError):
            validate_root("/private/var/tmp/scratch", "/project", system=fs)
        fs = FakeSystem()
        def denied(path):
            raise PermissionError("metadata denied")
        fs.lstat = denied
        with self.assertRaises(IsolationError):
            validate_root("/private/var/tmp/scratch", "/project", system=fs)

    def test_changed_directory_identity_is_rejected_before_attempt_creation(self):
        fs = FakeSystem()
        root = validate_root("/private/var/tmp/scratch", "/project", system=fs)
        fs.metadata.st_ino = 101
        with self.assertRaises(IsolationError):
            root.revalidate(system=fs)

    def test_source_alias_cannot_hide_a_scratch_root_inside_the_checkout(self):
        fs = FakeSystem()
        fs.path.realpath = lambda path: "/project" if path == "/source-alias" else path
        with self.assertRaises(IsolationError):
            validate_root("/project/scratch", "/source-alias", system=fs)

    def test_attempt_uses_only_its_private_descendant_and_preserves_home(self):
        from scripts.run_tests_isolation import new_attempt
        fs = FakeSystem()
        root = validate_root("/private/var/tmp/scratch", "/project", system=fs)
        received = []
        def allocate(**kwargs):
            received.append(kwargs)
            fs.rows["/private/var/tmp/scratch/r-fake"] = SimpleNamespace(**{**vars(fs.metadata), "st_ino": 101})
            return "/private/var/tmp/scratch/r-fake"
        original = {"HOME": "/Users/owner", "TMPDIR": "/old", "HERMES_GATEWAY_LOCK_DIR": "/shared"}
        attempt = new_attempt(root, original, system=fs, allocate=allocate)
        self.assertEqual(received, [{"prefix": "r-", "dir": root.path}])
        self.assertEqual(attempt.path, "/private/var/tmp/scratch/r-fake")
        self.assertEqual(attempt.environment["HOME"], "/Users/owner")
        for key in ("TMPDIR", "TEMP", "TMP", "PYTEST_DEBUG_TEMPROOT"):
            self.assertEqual(attempt.environment[key], attempt.path)
        self.assertEqual(attempt.environment["HERMES_GATEWAY_LOCK_DIR"], attempt.path + "/gateway-locks")
        self.assertEqual(attempt.environment["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(original["HERMES_GATEWAY_LOCK_DIR"], "/shared")

    def test_root_replacement_prevents_any_scratch_allocation(self):
        from scripts.run_tests_isolation import new_attempt
        fs = FakeSystem()
        root = validate_root("/private/var/tmp/scratch", "/project", system=fs)
        fs.metadata.st_ino = 999
        allocated = []
        with self.assertRaises(IsolationError):
            new_attempt(root, {}, system=fs, allocate=lambda **kwargs: allocated.append(kwargs))
        self.assertEqual(allocated, [])


class FakeChildSystem:
    name = "posix"
    P_PID, WEXITED, WNOHANG, WNOWAIT = 1, 4, 1, 0x1000000

    def __init__(self):
        self.events = []
        self.child = SimpleNamespace(pid=4242, returncode=None)
        self.child.stdout = SimpleNamespace(fileno=lambda: 8, close=lambda: self.events.append(("close",)))
        self.child.wait = self.reap
        self.observation = SimpleNamespace(si_pid=4242, si_uid=501)
        self.chunks = [b"one test passed\n", b""]
        self.signals = SimpleNamespace(SIGCHLD=17, SIGKILL=9, SIG_DFL=0, getsignal=lambda _: 0)

    def getuid(self):
        return 501

    def waitid(self, kind, pid, flags):
        self.events.append(("waitid", kind, pid, flags))
        return self.observation

    def getpgid(self, pid):
        self.events.append(("getpgid", pid))
        return 4242

    def killpg(self, pid, signum):
        self.events.append(("killpg", pid, signum))

    def reap(self, timeout):
        self.events.append(("reap", timeout))
        self.child.returncode = 0
        return 0

    def read(self, fd, size):
        self.events.append(("read", fd))
        return self.chunks.pop(0) if self.chunks else b""


class FakeMultiplexer:
    def __init__(self):
        self.registered = False
        self.now = 0.0

    def register(self, stream, mask):
        self.registered = True

    def unregister(self, stream):
        self.registered = False

    def select(self, timeout):
        self.now += max(timeout, 0.001)
        return [(SimpleNamespace(fd=8), 1)] if self.registered else []

    def close(self):
        pass


class OwnedChildPolicyTests(unittest.TestCase):
    def test_successful_child_stays_unreaped_until_owned_group_cleanup(self):
        from scripts.run_tests_isolation import OwnedChild
        fs = FakeChildSystem()
        owner = OwnedChild(fs.child, system=fs, signals=fs.signals)
        self.assertTrue(owner.exited())
        self.assertIsNone(fs.child.returncode)
        self.assertEqual(owner.finish(), 0)
        kinds = [event[0] for event in fs.events]
        self.assertLess(kinds.index("killpg"), kinds.index("reap"))
        self.assertEqual([event for event in fs.events if event[0] == "killpg"], [("killpg", 4242, 9)])
        self.assertTrue(all(event[3] & fs.WNOWAIT for event in fs.events if event[0] == "waitid"))
        events = list(fs.events)
        with self.assertRaises(IsolationError):
            owner.finish()
        self.assertEqual(events, fs.events)

    def test_lost_foreign_or_changed_child_identity_never_grants_signal_authority(self):
        from scripts.run_tests_isolation import OwnedChild
        for failure in ("lost", "foreign-pid", "foreign-uid", "different-group", "replaced-handle"):
            fs = FakeChildSystem()
            owner = OwnedChild(fs.child, system=fs, signals=fs.signals)
            if failure == "lost":
                def lost(*_):
                    raise ChildProcessError("child already reaped by another waiter")
                fs.waitid = lost
            elif failure == "foreign-pid":
                fs.observation.si_pid = 9999
            elif failure == "foreign-uid":
                fs.observation.si_uid = 999
            elif failure == "different-group":
                fs.getpgid = lambda _: 9999
            else:
                fs.child.pid = 9999
            with self.subTest(failure=failure), self.assertRaises(IsolationError):
                owner.finish()
            self.assertFalse(any(event[0] in ("killpg", "reap") for event in fs.events))

    def test_denied_cleanup_has_no_fallback_or_second_signal(self):
        from scripts.run_tests_isolation import OwnedChild
        fs = FakeChildSystem()
        def denied(pid, signum):
            fs.events.append(("denied-signal", pid, signum))
            raise PermissionError("sandbox refused owned group cleanup")
        fs.killpg = denied
        owner = OwnedChild(fs.child, system=fs, signals=fs.signals)
        with self.assertRaisesRegex(IsolationError, "no fallback"):
            owner.finish()
        with self.assertRaises(IsolationError):
            owner.finish()
        self.assertEqual([e for e in fs.events if e[0] == "denied-signal"], [("denied-signal", 4242, 9)])
        self.assertFalse(any(e[0] == "reap" for e in fs.events))


class IsolatedExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fs, self.child_system, self.mux = FakeSystem(), FakeChildSystem(), FakeMultiplexer()
        for name in ("name", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT", "waitid", "getpgid", "killpg", "read"):
            setattr(self.fs, name, getattr(self.child_system, name))
        self.root = validate_root("/private/var/tmp/scratch", "/project", system=self.fs)
        self.allocations, self.launches = [], []

    def allocate(self, **kwargs):
        self.allocations.append(kwargs)
        path = self.root.path + "/r-owned"
        self.fs.rows[path] = SimpleNamespace(**{**vars(self.fs.metadata), "st_ino": 101})
        return path

    def spawn(self, command, **kwargs):
        self.launches.append((command, kwargs))
        return self.child_system.child

    def execute(self, timeout=1):
        from scripts.run_tests_isolation import launch_isolated
        return launch_isolated(
            ["/qualified/python", "-B", "-m", "pytest", "test_contract.py"], self.root,
            "/project", {"HOME": "/Users/owner"}, timeout, system=self.fs,
            signals=self.child_system.signals, allocate=self.allocate, spawn=self.spawn,
            selector_factory=lambda: self.mux, clock=lambda: self.mux.now,
        )

    def test_fake_complete_attempt_uses_scoped_environment_and_owned_lifetime(self):
        output, status = self.execute()
        self.assertEqual((output, status), ("one test passed\n", 0))
        command, options = self.launches[0]
        self.assertEqual(command[0], "/qualified/python")
        self.assertTrue(options["start_new_session"])
        self.assertEqual(options["env"]["HERMES_GATEWAY_LOCK_DIR"], self.root.path + "/r-owned/gateway-locks")
        self.assertEqual(options["env"]["HOME"], "/Users/owner")

    def test_pytest_basetemp_is_bound_to_the_fresh_attempt_even_with_an_earlier_override(self):
        from scripts.run_tests_isolation import launch_isolated
        launch_isolated(
            ["/qualified/python", "-m", "pytest", "test_contract.py", "--basetemp", "/shared"],
            self.root, "/project", {"PYTEST_ADDOPTS": "--basetemp=/shared"}, 1,
            system=self.fs, signals=self.child_system.signals, allocate=self.allocate, spawn=self.spawn,
            selector_factory=lambda: self.mux, clock=lambda: self.mux.now,
        )
        command, options = self.launches[0]
        self.assertEqual(command[-2:], ["--basetemp", self.root.path + "/r-owned/pytest"])
        self.assertNotIn("PYTEST_ADDOPTS", options["env"])

    def test_child_option_terminator_cannot_hide_the_private_basetemp_option(self):
        from scripts.run_tests_isolation import launch_isolated
        with self.assertRaises(IsolationError):
            launch_isolated(
                ["/qualified/python", "-m", "pytest", "--", "test_contract.py"], self.root,
                "/project", {}, 1, system=self.fs, signals=self.child_system.signals,
                allocate=self.allocate, spawn=self.spawn, selector_factory=lambda: self.mux,
                clock=lambda: self.mux.now,
            )
        self.assertEqual(self.allocations, [])
        self.assertEqual(self.launches, [])

    def test_unsupported_host_and_invalid_timeout_fail_before_allocation_or_launch(self):
        self.fs.name = "nt"
        with self.assertRaises(IsolationError):
            self.execute()
        self.fs.name = "posix"
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(IsolationError):
                self.execute(timeout)
        self.assertEqual(self.allocations, [])
        self.assertEqual(self.launches, [])
        self.assertEqual(self.child_system.events, [])

    def test_nondefault_child_handler_and_missing_wait_capability_fail_before_launch(self):
        self.child_system.signals.getsignal = lambda _: "unqualified-handler"
        with self.assertRaises(IsolationError):
            self.execute()
        self.child_system.signals.getsignal = lambda _: 0
        del self.fs.WNOWAIT
        with self.assertRaises(IsolationError):
            self.execute()
        self.assertEqual(self.allocations, [])
        self.assertEqual(self.launches, [])
        self.assertEqual(self.child_system.events, [])

    def test_root_replacement_during_allocation_leaves_evidence_without_launch(self):
        original = self.allocate
        def replaced(**kwargs):
            path = original(**kwargs)
            self.fs.metadata.st_ino = 999
            return path
        self.allocate = replaced
        with self.assertRaises(IsolationError):
            self.execute()
        self.assertEqual(len(self.allocations), 1)
        self.assertEqual(self.launches, [])
        self.assertEqual(self.child_system.events, [])

    def test_unowned_or_outside_attempt_never_launches_a_child(self):
        original = self.allocate
        for failure in ("outside", "foreign-owner", "different-device", "not-directory"):
            def invalid(**kwargs):
                path = original(**kwargs)
                if failure == "outside":
                    return "/private/var/tmp/someone-elses-attempt"
                info = self.fs.rows[path]
                if failure == "foreign-owner":
                    info.st_uid = 999
                elif failure == "different-device":
                    info.st_dev = 99
                else:
                    info.st_mode = stat.S_IFREG | 0o700
                return path
            self.allocate = invalid
            with self.subTest(failure=failure), self.assertRaises(IsolationError):
                self.execute()
        self.assertEqual(self.launches, [])
        self.assertEqual(self.child_system.events, [])

    def test_preflight_requires_the_explicit_supported_interpreter(self):
        from scripts.run_tests_isolation import preflight
        for declared, actual, version in (
            ("", "/qualified/python", (3, 14)), ("python", "/qualified/python", (3, 14)),
            ("/other/python", "/qualified/python", (3, 14)),
            ("/qualified/python", "/qualified/python", (3, 13)),
        ):
            with self.subTest(declared=declared, actual=actual, version=version), self.assertRaises(IsolationError):
                preflight(self.root.path, "/project", declared, executable=actual, version=version,
                          system=self.fs, signals=self.child_system.signals)
        self.assertEqual(self.allocations, [])
        self.assertEqual(self.launches, [])
        self.assertEqual(self.child_system.events, [])

    def test_preflight_validates_without_launching_or_allocating(self):
        from scripts.run_tests_isolation import preflight
        root = preflight(self.root.path, "/project", "/qualified/python", executable="/qualified/python",
                         version=(3, 14), system=self.fs, signals=self.child_system.signals)
        self.assertEqual(root, self.root)
        self.assertEqual(self.allocations, [])
        self.assertEqual(self.launches, [])
        self.assertEqual(self.child_system.events, [])

    def test_other_virtual_environment_is_not_qualified_by_sharing_the_python_binary(self):
        from scripts.run_tests_isolation import preflight
        # Two environments can symlink to the same base executable while loading
        # different dependencies/site hooks. Qualification belongs to the route.
        self.fs.path.realpath = lambda path: "/base/python" if path.endswith("/python") else path
        with self.assertRaises(IsolationError):
            preflight(self.root.path, "/project", "/other-venv/python", executable="/qualified/python",
                      version=(3, 14), system=self.fs, signals=self.child_system.signals)
        self.assertEqual(self.allocations, [])
        self.assertEqual(self.launches, [])


class OwnedCollectionTests(unittest.TestCase):
    def test_already_reaped_child_is_never_probed_or_signaled(self):
        from scripts.run_tests_isolation import OwnedChild
        fs = FakeChildSystem()
        owner = OwnedChild(fs.child, system=fs, signals=fs.signals)
        fs.child.returncode = 0
        with self.assertRaises(IsolationError):
            owner.finish()
        self.assertEqual(fs.events, [])

    def test_output_is_drained_without_early_reaping(self):
        from scripts.run_tests_isolation import collect_owned
        fs, mux = FakeChildSystem(), FakeMultiplexer()
        output, status = collect_owned(fs.child, 1, system=fs, signals=fs.signals,
                                       selector_factory=lambda: mux, clock=lambda: mux.now)
        self.assertEqual(output, "one test passed\n")
        self.assertEqual(status, 0)
        kinds = [event[0] for event in fs.events]
        self.assertLess(kinds.index("killpg"), kinds.index("reap"))

    def test_timeout_cleans_only_the_unreaped_owned_child_and_returns_failure(self):
        from scripts.run_tests_isolation import collect_owned
        fs, mux = FakeChildSystem(), FakeMultiplexer()
        fs.observation = None
        output, status = collect_owned(fs.child, 0.1, system=fs, signals=fs.signals,
                                       selector_factory=lambda: mux, clock=lambda: mux.now)
        self.assertEqual(status, 124)
        self.assertIn("one test passed", output)
        self.assertIn("timeout", output)
        self.assertEqual([event for event in fs.events if event[0] == "killpg"], [("killpg", 4242, 9)])


if __name__ == "__main__":
    unittest.main()
