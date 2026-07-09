"""Discover status notice: start, finish, clear on next longPoll."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from conftest import make_controller_stub
from nodes.Controller import Controller


class _NoticesDict(dict):
    def delete(self, key):
        try:
            del self[key]
        except KeyError:
            pass


def _controller():
    notices = _NoticesDict()
    ctrl = make_controller_stub(
        Notices=notices,
        poly=MagicMock(),
        ready=True,
        discover_done=True,
        in_long_poll=False,
        auto_discover=False,
        _discover_wait_logged=False,
        _long_poll_busy_logged=False,
        _discover_notice_clear_on_longpoll=False,
        hb=0,
    )
    for name in (
        '_set_discover_notice',
        '_clear_discover_notice',
        '_clear_discover_notice_if_due',
        '_finish_discover_notice',
        'longPoll',
        'discover_new',
    ):
        setattr(ctrl, name, getattr(Controller, name).__get__(ctrl, Controller))
    ctrl.heartbeat = MagicMock()
    return ctrl, notices


def test_set_discover_notice_posts_started_message():
    ctrl, notices = _controller()
    ctrl._set_discover_notice('Discovery started (1 network)')
    assert 'discover' in notices
    assert 'Discovery started (1 network)' in notices['discover']
    assert '[discover]' in notices['discover']


def test_finish_discover_notice_marks_clear_on_next_longpoll():
    ctrl, notices = _controller()
    ctrl._finish_discover_notice('Discovery finished')
    assert 'Discovery finished' in notices['discover']
    assert ctrl._discover_notice_clear_on_longpoll is True


def test_longpoll_clears_finished_discover_notice():
    ctrl, notices = _controller()
    ctrl._finish_discover_notice('Discovery finished')
    assert 'discover' in notices

    ctrl.longPoll()

    assert 'discover' not in notices
    assert ctrl._discover_notice_clear_on_longpoll is False


def test_longpoll_does_not_clear_active_discover_notice():
    """While discover is in progress, clear flag is false — keep the notice."""
    ctrl, notices = _controller()
    ctrl._set_discover_notice('Discovery started')
    ctrl._discover_notice_clear_on_longpoll = False

    ctrl.longPoll()

    assert 'discover' in notices
    assert 'Discovery started' in notices['discover']


def test_discover_new_posts_start_and_finish_notices():
    ctrl, notices = _controller()
    ctrl._discover_targets = MagicMock(return_value=['192.168.1.255'])
    ctrl.discover_future_timeout = 1
    ctrl.mainloop = MagicMock()
    ctrl.drain_pending_device_adds = MagicMock()
    ctrl._schedule_tapo_cloud_roster_refresh = MagicMock()
    ctrl._try_adopt_deferred_hub_cameras = MagicMock()
    ctrl._after_inventory_sync = MagicMock()

    async def _noop_discover_new_a(target=None):
        return None

    ctrl._discover_new_a = _noop_discover_new_a
    future = MagicMock()
    future.result.return_value = None
    with patch.object(asyncio, 'run_coroutine_threadsafe', return_value=future) as run_coro:
        ctrl.discover_new()
        # Consume the coroutine so pytest does not warn about it.
        coro = run_coro.call_args[0][0]
        if asyncio.iscoroutine(coro):
            coro.close()

    assert 'discover' in notices
    assert 'Discovery finished' in notices['discover']
    assert ctrl._discover_notice_clear_on_longpoll is True
