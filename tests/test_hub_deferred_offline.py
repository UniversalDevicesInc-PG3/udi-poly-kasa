"""Hub-deferred camera offline / LAN-touch behavior."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from conftest import make_controller_stub
from device_errors import ERR_NOT_READY, ERR_OK, err_code_for_connect_message
from nodes.Controller import Controller
from nodes.SmartCameraNode import SmartCameraNode


def test_err_code_for_connect_message_hub_deferred():
    assert (
        err_code_for_connect_message(
            'Tapo:Cam: hub-deferred direct update failed'
        )
        == ERR_NOT_READY
    )
    assert err_code_for_connect_message('camera asleep or not ready') == ERR_NOT_READY


def test_nodes_for_host_matches_camera_host():
    ctrl = make_controller_stub()
    node = MagicMock()
    node.host = '192.168.1.150'  # hub IP (legacy)
    node.cfg = {
        'host': '192.168.1.150',
        'camera_host': '192.168.1.107',
        'hub_deferred': True,
    }
    ctrl.nodes_by_mac = {'cam': node}
    ctrl.poly.getNodes.return_value = []

    found = Controller._nodes_for_host(ctrl, '192.168.1.107')
    assert found == [node]
    assert Controller._nodes_for_host(ctrl, '192.168.1.150') == [node]


def test_refresh_hub_deferred_camera_lan_host():
    ctrl = make_controller_stub()
    ctrl.save_cfg = MagicMock()
    node = MagicMock()
    node.name = 'CamOutFrontEntry'
    node.host = '192.168.1.150'
    node.cfg = {
        'host': '192.168.1.150',
        'hub_deferred': True,
        'hub_parent': 'ccbabd1606d8',
    }

    changed = Controller._refresh_hub_deferred_camera_lan_host(
        ctrl, node, '192.168.1.107'
    )
    assert changed is True
    assert node.cfg['camera_host'] == '192.168.1.107'
    assert node.cfg['host'] == '192.168.1.107'
    assert node.host == '192.168.1.107'
    ctrl.save_cfg.assert_called_once()


def test_touch_hub_deferred_camera_from_lan_updates_drivers():
    ctrl = make_controller_stub()
    ctrl.save_cfg = MagicMock()
    ctrl.update_dev = AsyncMock(return_value=True)
    ctrl._set_host_device_err = MagicMock()

    node = MagicMock()
    node.name = 'CamOutFrontEntry'
    node.address = '782051cd4138'
    node.id = 'SmartCamera_B'
    node.host = '192.168.1.150'
    node.cfg = {
        'host': '192.168.1.150',
        'hub_deferred': True,
        'hub_parent': 'ccbabd1606d8',
        'mac': '78:20:51:CD:41:38',
    }
    node.setDriver = MagicMock()
    ctrl._existing_node_for_dev = MagicMock(return_value=node)

    dev = MagicMock()
    dev.device_type = 'DeviceType.Camera'
    dev.host = '192.168.1.107'
    dev.is_on = True

    with patch(
        'nodes.Controller.motion_detection_enabled', return_value=True
    ), patch(
        'nodes.Controller.camera_notifications_enabled', return_value=False
    ), patch(
        'nodes.Controller.battery_percent', return_value=87
    ):
        ok = asyncio.get_event_loop().run_until_complete(
            Controller._touch_hub_deferred_camera_from_lan(ctrl, dev)
        )

    assert ok is True
    assert node.dev is dev
    assert node.cfg['camera_host'] == '192.168.1.107'
    node.set_connected.assert_called_with(True)
    ctrl._set_host_device_err.assert_called_with('192.168.1.107', ERR_OK)
    node.setDriver.assert_any_call('ST', 100)
    node.setDriver.assert_any_call('GV3', 87)


def test_lan_host_for_hub_camera_uses_discover_buffer():
    ctrl = make_controller_stub()
    ctrl.Data = {}
    ctrl.get_device_cfg = MagicMock(return_value=None)
    ctrl._deferred_hub_cameras = [
        {'mac': '78:20:51:CD:41:38', 'host': '192.168.1.107', 'model': 'C460'},
    ]
    cfg = {
        'host': '192.168.1.150',  # hub IP only — no camera_host
        'mac': '78:20:51:CD:41:38',
        'hub_deferred': True,
        'hub_parent': 'ccbabd1606d8',
    }
    assert (
        Controller.lan_host_for_hub_camera(
            ctrl,
            mac=cfg['mac'],
            cfg=cfg,
            hub_host='192.168.1.150',
            node_host='192.168.1.150',
        )
        == '192.168.1.107'
    )


def test_lan_host_for_hub_camera_uses_node_host_when_not_hub():
    ctrl = make_controller_stub()
    ctrl.Data = {}
    ctrl.get_device_cfg = MagicMock(return_value=None)
    ctrl._deferred_hub_cameras = []
    cfg = {'hub_deferred': True, 'hub_parent': 'ccbabd1606d8', 'mac': 'aa'}
    assert (
        Controller.lan_host_for_hub_camera(
            ctrl,
            mac='aa',
            cfg=cfg,
            hub_host='192.168.1.150',
            node_host='192.168.1.107',
        )
        == '192.168.1.107'
    )


def test_mark_hub_deferred_offline_sets_not_ready():
    ctrl = make_controller_stub()
    ctrl._set_host_device_err = MagicMock()

    node = SmartCameraNode.__new__(SmartCameraNode)
    node.pfx = 'Tapo:Cam:'
    node.controller = ctrl
    node.primary_node = MagicMock(host='192.168.1.150')
    node.cfg = {
        'camera_host': '192.168.1.107',
        'hub_deferred': True,
        'hub_parent': 'ccbabd1606d8',
    }
    node.dev = None
    node.host = '192.168.1.107'
    node.set_connected = MagicMock()

    node._mark_hub_deferred_offline('no camera LAN host for hub-deferred update')
    node.set_connected.assert_called_once_with(False)
    ctrl._set_host_device_err.assert_called_once_with(
        '192.168.1.107', ERR_NOT_READY
    )
