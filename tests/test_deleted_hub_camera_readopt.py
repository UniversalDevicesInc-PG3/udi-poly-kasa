"""Re-adopt hub-child cameras after IoX delete + startup discover."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from conftest import make_controller_stub
from nodes.Controller import Controller


def _hub_controller():
    ctrl = make_controller_stub()
    ctrl.address = 'tplkasactl'
    ctrl.Data = {
        'ccbabd1606d8': (
            '{"type":"DeviceType.Hub","address":"ccbabd1606d8",'
            '"name":"Tapo Hub","mac":"CC:BA:BD:16:06:D8","host":"192.168.1.150"}'
        ),
        '870AD1B43FFBB3474B6801D0B35A03DD': (
            '{"type":"DeviceType.Camera","name":"CamInLivingRoom",'
            '"host":"192.168.1.59","mac":"78:20:51:29:F4:D4","model":"C260",'
            '"device_id":"870AD1B43FFBB3474B6801D0B35A03DD",'
            '"hub_parent":"ccbabd1606d8","hub_deferred":true,"id":"SmartCamera_N"}'
        ),
    }
    ctrl._deferred_hub_cameras = []
    ctrl._hub_child_identities = set()
    for name in (
        'get_device_cfg',
        '_is_hub_cfg',
        '_has_saved_hub_cfg',
        '_has_hub_node',
        '_tapo_hub_known',
        '_hub_child_cfg_snapshot',
        '_cfg_iox_address',
        '_seed_deferred_hub_cameras_from_saved_cfg',
        'smac',
    ):
        setattr(ctrl, name, getattr(Controller, name).__get__(ctrl, Controller))
    return ctrl


def test_cfg_iox_address_falls_back_to_mac():
    ctrl = _hub_controller()
    assert Controller._cfg_iox_address(
        ctrl,
        {'mac': '78:20:51:29:F4:D4'},
    ) == '78205129f4d4'


def test_seed_deferred_includes_camera_deleted_from_pg3():
    ctrl = _hub_controller()
    ctrl.poly.getNodeNameFromDb.return_value = None
    ctrl.poly.getNode.return_value = None
    ctrl._session_has_device = MagicMock(return_value=False)

    Controller._seed_deferred_hub_cameras_from_saved_cfg(ctrl)

    assert len(ctrl._deferred_hub_cameras) == 1
    assert ctrl._deferred_hub_cameras[0]['alias'] == 'CamInLivingRoom'


def test_discover_skips_saved_hub_child_without_address_key():
    ctrl = _hub_controller()
    ctrl.Data = {
        '870AD1B43FFBB3474B6801D0B35A03DD': (
            '{"type":"DeviceType.Camera","name":"CamInLivingRoom",'
            '"host":"192.168.1.59","mac":"78:20:51:29:F4:D4","model":"C260",'
            '"device_id":"870AD1B43FFBB3474B6801D0B35A03DD",'
            '"hub_parent":"ccbabd1606d8","hub_deferred":true,"id":"SmartCamera_N"}'
        ),
    }
    ctrl.discover_timeout = 5
    ctrl._discover_batch = []
    ctrl._discover_batch_has_hub = False
    ctrl.devm = {}
    ctrl._pending_device_adds = []
    ctrl.poly.getNodeNameFromDb.return_value = None
    ctrl.should_skip_standalone_camera_cfg = MagicMock(return_value=False)
    ctrl._node_identity_key = MagicMock(return_value=None)
    ctrl._session_has_device = MagicMock(return_value=False)
    ctrl._kasa_credentials = MagicMock(return_value=MagicMock())
    ctrl._process_discover_batch = AsyncMock()

    async def _fake_discover(**_kwargs):
        return None

    with patch('nodes.Controller.kasa.Discover.discover', new=_fake_discover):
        asyncio.run(Controller._discover(ctrl, target='192.168.1.255'))

    ctrl.poly.getNodeNameFromDb.assert_not_called()
