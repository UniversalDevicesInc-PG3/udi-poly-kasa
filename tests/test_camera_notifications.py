"""Tests for Tapo camera push-notification helpers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from camera_helpers import (
    camera_notifications_enabled,
    set_camera_notifications_enabled,
)


class _Modules(dict):
    def values(self):
        return super().values()


def test_camera_notifications_enabled_from_last_update():
    dev = SimpleNamespace(
        modules=_Modules(),
        _last_update={
            'getMsgPushConfig': {
                'msg_push': {
                    'chn1_msg_push_info': {'notification_enabled': 'on'},
                }
            }
        },
    )
    assert camera_notifications_enabled(dev) is True


def test_camera_notifications_disabled_from_last_update():
    dev = SimpleNamespace(
        modules=_Modules(),
        _last_update={
            'getMsgPushConfig': {
                'msg_push': {
                    'chn1_msg_push_info': {'notification_enabled': 'off'},
                }
            }
        },
    )
    assert camera_notifications_enabled(dev) is False


def test_camera_notifications_unavailable():
    assert camera_notifications_enabled(None) is None
    assert camera_notifications_enabled(SimpleNamespace(modules=None, _last_update={})) is None


def test_set_camera_notifications_via_query_helper():
    helper = AsyncMock(return_value={'ok': True})
    dev = SimpleNamespace(
        modules=_Modules(),
        _query_setter_helper=helper,
        protocol=None,
    )
    asyncio.run(set_camera_notifications_enabled(dev, False))
    helper.assert_awaited_once_with(
        'setMsgPushConfig',
        'msg_push',
        'chn1_msg_push_info',
        {'notification_enabled': 'off'},
    )


def test_set_camera_notifications_via_protocol():
    protocol = SimpleNamespace(query=AsyncMock(return_value={'ok': True}))
    dev = SimpleNamespace(modules=_Modules(), protocol=protocol)
    asyncio.run(set_camera_notifications_enabled(dev, True))
    protocol.query.assert_awaited_once_with({
        'setMsgPushConfig': {
            'msg_push': {'chn1_msg_push_info': {'notification_enabled': 'on'}},
        }
    })
