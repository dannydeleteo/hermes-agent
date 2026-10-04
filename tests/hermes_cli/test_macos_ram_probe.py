"""A failed or empty Mac memory reading must never grant invented launch capacity."""

import subprocess

import pytest

from hermes_cli.local_runtime import hardware
from hermes_cli.local_runtime.context_policy import initial_window
from hermes_cli.local_runtime.estimator import ModelProfile, PhysicsRefusal


GIB = 1 << 30
TOTAL = 48 * GIB


def _snapshot(*, page=16384, free=0, inactive=0, speculative=0, purgeable=0):
    return (
        f"Mach Virtual Memory Statistics: (page size of {page} bytes)\n"
        f"Pages free: {free}.\n"
        f"Pages inactive: {inactive}.\n"
        f"Pages speculative: {speculative}.\n"
        f"Pages purgeable: {purgeable}.\n"
    )


def _mac_probes(monkeypatch, output, *, returncode=0):
    def run(argv, **kwargs):
        if argv == ["/usr/sbin/sysctl", "-n", "hw.memsize"]:
            return subprocess.CompletedProcess(argv, 0, stdout=str(TOTAL))
        assert argv == ["/usr/bin/vm_stat"]
        if isinstance(output, Exception):
            raise output
        result = subprocess.CompletedProcess(argv, returncode, stdout=output)
        if kwargs.get("check"):
            result.check_returncode()
        return result

    monkeypatch.setattr(hardware.subprocess, "run", run)
    # The Mac shared-memory path, without querying or starting any GPU runtime.
    monkeypatch.setattr(hardware, "_nvidia_vram", lambda: None)
    monkeypatch.setattr(hardware, "_device_pool_view", lambda: None)


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("output, returncode", [
    pytest.param(_snapshot(), 0, id="real-zero"),
    pytest.param("", 0, id="empty"),
    pytest.param("not a memory reading", 0, id="unreadable"),
    pytest.param(_snapshot(free=100).split("\n", 1)[1], 0, id="missing-page-size"),
    pytest.param(_snapshot(page=0, free=100), 0, id="zero-page-size"),
    pytest.param(_snapshot(page=3000, free=100), 0, id="invalid-page-size"),
    pytest.param(_snapshot(page=16 * GIB, free=1), 0, id="impossible-power-of-two-page"),
    pytest.param(_snapshot(free=100).replace("Pages inactive: 0.\n", ""), 0,
                 id="incomplete"),
    pytest.param(_snapshot(free=100) + "Pages free: 200.\n", 0, id="duplicate"),
    pytest.param(_snapshot(free=100, inactive=-1), 0, id="negative-count"),
    pytest.param(_snapshot(free=100).replace("inactive: 0.", "inactive: unknown."), 0,
                 id="invalid-count"),
    pytest.param(_snapshot(free=TOTAL // 16384 + 1), 0, id="impossible-total"),
    pytest.param(_snapshot(free=100), 1, id="nonzero-command-exit"),
    pytest.param(OSError("probe unavailable"), 0, id="command-missing"),
    pytest.param(subprocess.TimeoutExpired("vm_stat", 5), 0, id="command-timeout"),
])
def test_unusable_reading_yields_zero_budget_and_physics_refusal(monkeypatch, output, returncode):
    _mac_probes(monkeypatch, output, returncode=returncode)
    budget = hardware.probe_budget()
    assert budget.total_device_bytes == TOTAL
    assert budget.usable_vram_bytes + budget.ram_available_bytes == 0
    profile = ModelProfile("fixture", GIB, 0, 65536, [])
    assert isinstance(initial_window(profile, budget), PhysicsRefusal)
    # Catalog capacity remains distinct from a launch-time grant.
    assert hardware.probe_budget(planning=True).usable_vram_bytes > 0


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("page", [4096, 16384])
def test_reclaimable_counts_do_not_add_purgeable_pages_twice(monkeypatch, page):
    free, inactive, speculative = 120000, 80000, 20000
    available = (free + inactive + speculative) * page
    for purgeable in (0, inactive // 2):
        _mac_probes(monkeypatch, _snapshot(page=page, free=free, inactive=inactive,
                                         speculative=speculative, purgeable=purgeable))
        assert hardware._ram_bytes() == (TOTAL, available)
        budget = hardware.probe_budget()
        assert 0 < budget.usable_vram_bytes < available
        assert budget.ram_available_bytes == 0  # UMA has one physical pool, not two.
