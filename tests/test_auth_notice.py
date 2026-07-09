"""Tests for consecutive auth-failure notice counting."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from conftest import make_controller_stub
from kasa.exceptions import AuthenticationError
from nodes.Controller import Controller


class _AuthDev:
    host = '192.168.1.50'
    alias = 'Test Plug'
    device_type = 'DeviceType.Plug'

    def __init__(self, fail_times=1):
        self._fail_times = fail_times
        self._calls = 0

    async def update(self):
        self._calls += 1
        if self._calls <= self._fail_times:
            raise AuthenticationError('Failed to authenticate')
        return None


class _NoticesDict(dict):
    def delete(self, key):
        try:
            del self[key]
        except KeyError:
            pass


def _controller_with_notices(**overrides):
    ctrl = make_controller_stub(
        _auth_fail_count={},
        _notice_last_write={},
        credential_error=False,
        Parameters={'user': 'user@example.com', 'password': 'secret'},
        change_node_names=False,
        **overrides,
    )
    ctrl.poly.Notices = _NoticesDict()
    for name in (
        'set_device_notice',
        'clear_device_notice',
        '_auth_failure_notice_message',
        '_device_display_name',
        '_notice_key_for_device',
        '_credentials_configured',
        'host_record_success',
        'host_should_skip',
    ):
        setattr(ctrl, name, getattr(Controller, name).__get__(ctrl, Controller))
    ctrl.host_record_success = MagicMock(wraps=ctrl.host_record_success)
    ctrl.host_should_skip = MagicMock(return_value=False)
    return ctrl


def test_auth_failure_notice_includes_consecutive_count():
    ctrl = _controller_with_notices()
    assert '1 consecutive auth failure —' in ctrl._auth_failure_notice_message(
        AuthenticationError('x'), 1
    )
    assert '3 consecutive auth failures —' in ctrl._auth_failure_notice_message(
        AuthenticationError('x'), 3
    )


def test_auth_fail_count_increments_and_resets_on_success():
    ctrl = _controller_with_notices()
    dev = _AuthDev(fail_times=2)
    key = ctrl._notice_key_for_device(dev)

    asyncio.get_event_loop().run_until_complete(ctrl.update_dev(dev))
    assert ctrl._auth_fail_count['192.168.1.50'] == 1
    assert '1 consecutive auth failure' in ctrl.poly.Notices[key]

    asyncio.get_event_loop().run_until_complete(ctrl.update_dev(dev))
    assert ctrl._auth_fail_count['192.168.1.50'] == 2
    assert '2 consecutive auth failures' in ctrl.poly.Notices[key]

    asyncio.get_event_loop().run_until_complete(ctrl.update_dev(dev))
    assert '192.168.1.50' not in ctrl._auth_fail_count
    assert key not in ctrl.poly.Notices


def test_set_host_auth_fail_count_updates_nodes_for_host():
    ctrl = make_controller_stub(_auth_fail_count={})
    host = '192.168.1.50'

    node_a = MagicMock()
    node_a.host = host
    node_b = MagicMock()
    node_b.host = host
    ctrl.nodes_by_mac = {'a': node_a, 'b': node_b}
    ctrl.poly.getNodes.return_value = []

    Controller._set_host_auth_fail_count(ctrl, host, 3)

    node_a.setDriver.assert_called_once_with('GV1', 3)
    node_b.setDriver.assert_called_once_with('GV1', 3)

    node_a.setDriver.reset_mock()
    node_b.setDriver.reset_mock()
    Controller._set_host_auth_fail_count(ctrl, host, 0)
    node_a.setDriver.assert_called_once_with('GV1', 0)
    node_b.setDriver.assert_called_once_with('GV1', 0)
