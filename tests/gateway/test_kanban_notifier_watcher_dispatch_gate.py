"""Notifier polling has an independent gateway config gate."""

import asyncio
import os
import time
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

from gateway.config import Platform
from gateway.run import GatewayRunner


def _make_runner(with_adapter=False):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: MagicMock()} if with_adapter else {}
    runner._kanban_sub_fail_counts = {}
    return runner


def test_notifier_watcher_skips_when_notifications_disabled():
    runner = _make_runner(with_adapter=True)

    with patch(
        "hermes_cli.config.load_config",
        return_value={"kanban": {"notify_in_gateway": False}},
    ):
        with patch("hermes_cli.kanban_db.list_boards") as list_boards:
            asyncio.run(runner._kanban_notifier_watcher())

    list_boards.assert_not_called()


def test_notifier_watcher_polls_without_dispatch_ownership():
    """A profile gateway still polls its profile-owned subscriptions."""
    runner = _make_runner(with_adapter=True)
    past_gate = []
    sleep_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)
        # Stop after the initial delay + first per-interval sleep so the loop
        # body runs exactly once.
        if len(sleep_calls) >= 2:
            runner._running = False

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    import hermes_cli.kanban_db as _kb

    with patch(
        "hermes_cli.config.load_config",
        return_value={
            "kanban": {
                "dispatch_in_gateway": False,
                "notify_in_gateway": True,
            }
        },
    ):
        with patch.object(
            _kb, "list_boards",
            side_effect=lambda *a, **kw: past_gate.append(True) or [],
        ):
            with patch("asyncio.sleep", side_effect=fake_sleep):
                with patch("asyncio.to_thread", side_effect=fake_to_thread):
                    asyncio.run(runner._kanban_notifier_watcher())

    assert past_gate, (
        "gateways without the dispatch lock must still poll owned subscriptions"
    )


def test_dispatch_rotates_single_slot_between_boards_at_host_cap(tmp_path, monkeypatch):
    from gateway.kanban_watchers_dispatcher import _KanbanDispatcher, _resolve_dispatcher_settings
    from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch, profiles
    from hermes_cli.kanban_db_connect import connect

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.setattr(profiles, 'profile_exists', lambda _: True)
    monkeypatch.setattr(dispatch, '_memory_pressure_level', lambda: 'normal')
    monkeypatch.setattr(dispatch, '_default_spawn', lambda *a, **kw: os.getpid())
    settings = _resolve_dispatcher_settings({'max_in_progress': 10}, kb)
    watcher = _KanbanDispatcher(kb, settings)

    def seed(slug, count):
        if slug != 'default':
            kb.create_board(slug)
        with closing(connect(board=slug)) as conn:
            for n in range(count):
                kb.create_task(conn, title=f'{slug} ready {n}', assignee='default')

    def tick(expected, active):
        results = watcher.tick_once()
        assert {slug for slug, _ in results} == set(active)
        assert [slug for slug, result in results for _ in getattr(result, 'spawned', ())] == expected
        running = 0
        for slug in active:
            with closing(connect(board=slug)) as conn:
                running += dispatch.count_running_tasks(conn)
        assert running == 10

    def release():
        with closing(connect(board='default')) as conn:
            task_id, run_id = conn.execute(
                "SELECT id, current_run_id FROM tasks WHERE status='running' LIMIT 1").fetchone()
            assert kb.complete_task(conn, task_id, summary='fixture slot released', expected_run_id=run_id)

    active = ('default', 'middle', 'youtube')
    for slug in active:
        seed(slug, 20)
    tick(['default'] * 10, active)
    for turn in ('middle', 'youtube', 'default', 'middle'):
        tick([], active)  # Full ticks do not consume the next board's turn.
        release()
        tick([turn], active)

    # A newly listed empty board and an existing quarantined board cannot strand
    # the slot. Keep the real per-board quarantine handling in this fixture.
    seed('parked', 0)
    active += ('parked',)
    watcher.disabled_corrupt_boards['youtube'] = (watcher.board_db_fingerprint('youtube'), time.monotonic())
    release()
    tick(['default'], active)
    watcher.disabled_corrupt_boards.clear()
    release()
    tick(['middle'], active)
