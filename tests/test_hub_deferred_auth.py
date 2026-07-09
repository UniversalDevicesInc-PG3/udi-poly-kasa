"""Hub-deferred cameras should not surface misleading direct-LAN auth notices."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

from conftest import make_controller_stub
from device_errors import ERR_AUTH, ERR_NOT_READY
from kasa.exceptions import AuthenticationError, KasaException
from nodes.Controller import Controller


class _AuthDev:
    host = '192.168.1.103'
    alias = 'Kasa C675D'
    mac = 'ACA7F1DAD82B'

    async def update(self):
        raise AuthenticationError(
            'Device response did not match our challenge on ip 192.168.1.103'
        )


class _SleepingDev:
    host = '192.168.1.103'
    alias = 'Kasa C675D'
    mac = 'ACA7F1DAD82B'

    async def update(self):
        raise KasaException('getDeviceInfo not found in {}')


class _NoticesDict(dict):
    def delete(self, key):
        try:
            del self[key]
        except KeyError:
            pass


class _PolyStub:
  def __init__(self):
    self.Notices = _NoticesDict()
    self.network_interface = {'broadcast': '192.168.1.255'}

  def getNodes(self):
    return []

  def getNode(self, address):
    return None


def _controller_with_update_dev(**overrides):
    poly = _PolyStub()
    ctrl = make_controller_stub(
        _auth_fail_count={},
        _notice_last_write={},
        _host_state={},
        credential_error=False,
        Parameters={'user': 'user@example.com', 'password': 'secret'},
        change_node_names=False,
        Data={},
        poly=poly,
        **overrides,
    )
    for name in (
        'update_dev',
        'set_device_notice',
        'clear_device_notice',
        '_auth_failure_notice_message',
        '_device_display_name',
        '_notice_key_for_device',
        '_notice_dev_for_host',
        '_notice_priority_from_value',
        '_credentials_configured',
        'host_record_success',
        'host_record_failure',
        '_set_host_device_err',
        '_set_host_auth_fail_count',
        '_clear_stale_hub_deferred_auth_notices',
    ):
        setattr(ctrl, name, getattr(Controller, name).__get__(ctrl, Controller))
    ctrl.host_should_skip = MagicMock(return_value=False)
    ctrl._device_display_name = MagicMock(return_value='Kasa C675D')
    return ctrl


def test_dev_hub_deferred_context_lookup():
    ctrl = _controller_with_update_dev()
    ctrl.Data['1e9da4f1c67f2da829a6afab5171d8f9'] = json.dumps({
        'type': 'DeviceType.Camera',
        'hub_deferred': True,
        'camera_host': '192.168.1.103',
        'device_id': '1E9DA4F1C67F2DA829A6AFAB5171D8F9',
        'address': 'aca7f1dad82b',
    })
    dev = _AuthDev()
    assert ctrl._cfg_for_dev(dev) is not None
    assert ctrl._dev_hub_deferred_context(dev) is True


def test_hub_deferred_auth_error_skips_notice():
    ctrl = _controller_with_update_dev()
    ctrl.Data['1e9da4f1c67f2da829a6afab5171d8f9'] = json.dumps({
        'type': 'DeviceType.Camera',
        'hub_deferred': True,
        'camera_host': '192.168.1.103',
        'device_id': '1E9DA4F1C67F2DA829A6AFAB5171D8F9',
        'address': 'aca7f1dad82b',
    })
    dev = _AuthDev()
    key = ctrl._notice_key_for_device(dev)
    ctrl._set_host_device_err = MagicMock()

    asyncio.get_event_loop().run_until_complete(ctrl.update_dev(dev))

    assert key not in ctrl.poly.Notices
    assert '192.168.1.103' not in ctrl._auth_fail_count
    ctrl._set_host_device_err.assert_called_with('192.168.1.103', ERR_AUTH)


def test_hub_deferred_sleeping_error_clears_stale_auth_notice():
    ctrl = _controller_with_update_dev()
    ctrl.Data['1e9da4f1c67f2da829a6afab5171d8f9'] = json.dumps({
        'type': 'DeviceType.Camera',
        'hub_deferred': True,
        'camera_host': '192.168.1.103',
        'device_id': '1E9DA4F1C67F2DA829A6AFAB5171D8F9',
    })
    dev = _SleepingDev()
    key = ctrl._notice_key_for_device(dev)
    ctrl.poly.Notices[key] = (
        '[2026-07-07 14:47:16] [auth] Kasa C675D (192.168.1.103): '
        '1 consecutive auth failure — device rejected login.'
    )
    ctrl._set_host_device_err = MagicMock()

    asyncio.get_event_loop().run_until_complete(ctrl.update_dev(dev))

    assert key not in ctrl.poly.Notices
    ctrl._set_host_device_err.assert_called_with('192.168.1.103', ERR_NOT_READY)


def test_regular_device_still_gets_auth_notice():
    ctrl = _controller_with_update_dev()
    dev = _AuthDev()

    asyncio.get_event_loop().run_until_complete(ctrl.update_dev(dev))

    key = ctrl._notice_key_for_device(dev)
    assert 'auth failure' in ctrl.poly.Notices[key]
    assert ctrl._auth_fail_count['192.168.1.103'] == 1


def test_clear_stale_hub_deferred_auth_notices_on_startup():
    ctrl = _controller_with_update_dev()
    ctrl.Data['camcfg'] = json.dumps({
        'type': 'DeviceType.Camera',
        'hub_deferred': True,
        'camera_host': '192.168.1.103',
    })
    key = 'dev_192_168_1_103'
    ctrl.poly.Notices[key] = (
        '[2026-07-07 14:47:16] [auth] Kasa C675D (192.168.1.103): '
        '1 consecutive auth failure — device rejected login.'
    )

    ctrl._clear_stale_hub_deferred_auth_notices()

    assert key not in ctrl.poly.Notices


def test_clear_stale_hub_deferred_auth_notices_missing_notice_no_crash():
    """Custom Notices return None for missing keys; must not crash startup."""
    ctrl = _controller_with_update_dev()
    ctrl.Data['camcfg'] = json.dumps({
        'type': 'DeviceType.Camera',
        'hub_deferred': True,
        'camera_host': '192.168.1.103',
    })
    assert 'dev_192_168_1_103' not in ctrl.poly.Notices

    ctrl._clear_stale_hub_deferred_auth_notices()

    assert ctrl._notice_priority_from_value(None) == 0
