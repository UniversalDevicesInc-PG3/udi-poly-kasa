"""Tests for restoring orphan hub-child cameras from IoX."""
from __future__ import annotations

from unittest.mock import MagicMock

from conftest import make_controller_stub
from nodes.Controller import Controller


def _controller():
    ctrl = make_controller_stub()
    ctrl.address = 'tplkasactl'
    ctrl.Data = {
        'ccbabd1606d8': (
            '{"type":"DeviceType.Hub","address":"ccbabd1606d8",'
            '"name":"Tapo Hub","mac":"CC:BA:BD:16:06:D8","host":"192.168.1.150"}'
        ),
    }
    ctrl.devm = {}
    ctrl.manual_devices = [
        {'address': '192.168.1.103', 'name': 'CamOutBackSouth'},
    ]
    ctrl._manual_host_identity = {}
    ctrl._manual_host_names = {}
    ctrl._pending_device_adds = []
    for name in (
        'get_device_cfg',
        '_is_hub_cfg',
        '_hub_address',
        '_cfg_for_address',
        '_orphan_hub_camera_cfg_from_meta',
        '_session_has_device',
        '_node_identity_key',
        '_queue_device_add_from_cfg',
        'drain_pending_device_adds',
        '_find_manual_device_name',
        '_manual_device_host',
        '_manual_device_name_from_row',
        'smac',
    ):
        setattr(ctrl, name, getattr(Controller, name).__get__(ctrl, Controller))
    return ctrl


def test_restore_orphan_hub_camera_queues_cfg():
    ctrl = _controller()
    hub = MagicMock()
    hub.id = 'SmartHub_N'
    ctrl.poly.getNode.side_effect = lambda addr: hub if addr == 'ccbabd1606d8' else None
    ctrl.poly.getNodes.return_value = ['ccbabd1606d8']
    ctrl.poly.getNodesFromDb.return_value = [
        {
            'address': 'aca7f1dad82b',
            'name': 'Kasa C675D',
            'nodeDefId': 'SmartCamera_N',
            'primaryNode': 'ccbabd1606d8',
        },
    ]
    ctrl._session_has_device = MagicMock(return_value=False)
    ctrl._queue_device_add_from_cfg = MagicMock()
    ctrl._node_identity_key = MagicMock(return_value='aca7f1dad82b')
    ctrl.drain_pending_device_adds = MagicMock()

    Controller._restore_orphan_hub_cameras_from_pg3(ctrl)

    ctrl._queue_device_add_from_cfg.assert_called_once()
    cfg = ctrl._queue_device_add_from_cfg.call_args[0][0]
    assert cfg['address'] == 'aca7f1dad82b'
    assert cfg['hub_parent'] == 'ccbabd1606d8'
    assert cfg['hub_deferred'] is True
    assert cfg['host'] == '192.168.1.103'
    assert cfg['battery'] is True
    assert cfg['name'] == 'CamOutBackSouth'
