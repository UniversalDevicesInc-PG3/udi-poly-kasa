"""Tests for Tapo camera continuous 24/7 SD capture helpers."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from camera_helpers import (
    camera_continuous_recording_enabled,
    fetch_camera_record_plan_channel,
    set_camera_continuous_recording_enabled,
)

_DAYS = (
    'sunday',
    'monday',
    'tuesday',
    'wednesday',
    'thursday',
    'friday',
    'saturday',
)


def _run(coro):
    """Run a coroutine without leaving the default loop closed for later imports."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def _channel(*, enabled='on', slot='0000-2400:1'):
    day = json.dumps([slot])
    ch = {'enabled': enabled}
    for name in _DAYS:
        ch[name] = day
    return ch


def _dev_with_plan(channel):
    return SimpleNamespace(
        modules={},
        _components={'record': 1},
        _last_update={
            'getRecordPlan': {'record_plan': {'chn1_channel': channel}},
        },
        protocol=None,
    )


def test_continuous_recording_true_for_full_day_type_1():
    assert camera_continuous_recording_enabled(
        _dev_with_plan(_channel(slot='0000-2400:1'))
    ) is True


def test_continuous_recording_false_for_motion_type_2():
    assert camera_continuous_recording_enabled(
        _dev_with_plan(_channel(slot='0000-2400:2'))
    ) is False


def test_continuous_recording_false_when_plan_disabled():
    assert camera_continuous_recording_enabled(
        _dev_with_plan(_channel(enabled='off', slot='0000-2400:1'))
    ) is False


def test_continuous_recording_unavailable():
    assert camera_continuous_recording_enabled(None) is None
    assert camera_continuous_recording_enabled(
        SimpleNamespace(_last_update={})
    ) is None


def test_fetch_uses_cache_without_query():
    dev = _dev_with_plan(_channel())
    helper = AsyncMock()
    dev._query_helper = helper
    channel = _run(fetch_camera_record_plan_channel(dev))
    assert channel['enabled'] == 'on'
    helper.assert_not_awaited()


def test_fetch_queries_when_missing():
    channel = _channel(slot='0000-2400:2')
    helper = AsyncMock(
        return_value={
            'getRecordPlan': {'record_plan': {'chn1_channel': channel}},
        }
    )
    dev = SimpleNamespace(
        _components={'record': 1},
        _last_update={},
        _query_helper=helper,
        protocol=None,
    )
    got = _run(fetch_camera_record_plan_channel(dev))
    assert got == channel
    helper.assert_awaited_once_with(
        'getRecordPlan',
        {'record_plan': {'name': ['chn1_channel']}},
    )
    assert camera_continuous_recording_enabled(dev) is False


def test_set_continuous_recording_on_via_query_helper():
    helper = AsyncMock(return_value={'ok': True})
    dev = SimpleNamespace(
        _last_update={},
        _query_setter_helper=helper,
        protocol=None,
    )
    _run(set_camera_continuous_recording_enabled(dev, True))
    args = helper.await_args.args
    assert args[0] == 'setRecordPlan'
    assert args[1] == 'record_plan'
    assert args[2] == 'chn1_channel'
    params = args[3]
    assert params['enabled'] == 'on'
    assert params['monday'] == json.dumps(['0000-2400:1'])
    assert camera_continuous_recording_enabled(dev) is True


def test_set_continuous_recording_off_uses_motion_slots():
    helper = AsyncMock(return_value={'ok': True})
    dev = SimpleNamespace(
        _last_update={},
        _query_setter_helper=helper,
        protocol=None,
    )
    _run(set_camera_continuous_recording_enabled(dev, False))
    params = helper.await_args.args[3]
    assert params['friday'] == json.dumps(['0000-2400:2'])
    assert camera_continuous_recording_enabled(dev) is False
