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


def test_hub_deferred_cameras_missing_lan_host():
    ctrl = make_controller_stub()
    ctrl.Data = {}
    ctrl.get_device_cfg = MagicMock(return_value=None)
    ctrl._deferred_hub_cameras = []

    missing = MagicMock()
    missing.id = 'SmartCamera_B'
    missing.name = 'CamOutFrontEntry'
    missing.host = None
    missing.cfg = {
        'hub_deferred': True,
        'hub_parent': 'ccbabd1606d8',
        'mac': '78:20:51:CD:41:38',
        'host': None,
    }
    missing.primary_node = MagicMock(host='192.168.1.150')

    known = MagicMock()
    known.id = 'SmartCamera_B'
    known.name = 'CamOutBackSouth'
    known.host = '192.168.1.103'
    known.cfg = {
        'hub_deferred': True,
        'hub_parent': 'ccbabd1606d8',
        'mac': 'AC:A7:F1:DA:D8:2B',
        'camera_host': '192.168.1.103',
        'host': '192.168.1.103',
    }
    known.primary_node = MagicMock(host='192.168.1.150')

    non_deferred = MagicMock()
    non_deferred.id = 'SmartCamera_N'
    non_deferred.cfg = {'host': '192.168.1.48'}

    nodes = {
        'front': missing,
        'south': known,
        'other': non_deferred,
    }
    ctrl.poly.getNodes.return_value = list(nodes.keys())
    ctrl.poly.getNode.side_effect = lambda a: nodes.get(a)

    found = Controller.hub_deferred_cameras_missing_lan_host(ctrl)
    assert found == [missing]


def test_rediscover_hub_deferred_missing_lan_skips_when_none_or_rate_limited():
    ctrl = make_controller_stub()
    ctrl.hub_deferred_cameras_missing_lan_host = MagicMock(return_value=[])
    ctrl._discover_targets = MagicMock(return_value=['192.168.1.255'])

    assert (
        asyncio.get_event_loop().run_until_complete(
            Controller.rediscover_hub_deferred_missing_lan_a(ctrl)
        )
        == 0
    )
    ctrl._discover_targets.assert_not_called()

    node = MagicMock()
    node.name = 'CamOutFrontEntry'
    ctrl.hub_deferred_cameras_missing_lan_host = MagicMock(return_value=[node])
    ctrl._hub_deferred_lan_rediscover_next = 1e12  # far future
    assert (
        asyncio.get_event_loop().run_until_complete(
            Controller.rediscover_hub_deferred_missing_lan_a(ctrl)
        )
        == 0
    )


def test_rediscover_hub_deferred_missing_lan_runs_discover():
    ctrl = make_controller_stub()
    node = MagicMock()
    node.name = 'CamOutFrontEntry'
    ctrl.hub_deferred_cameras_missing_lan_host = MagicMock(return_value=[node])
    ctrl._discover_targets = MagicMock(return_value=['192.168.1.255'])
    ctrl._kasa_credentials = MagicMock(return_value=None)
    ctrl._hub_deferred_lan_rediscover_next = 0.0

    async def fake_discover(**kwargs):
        # Simulate finding nothing; still prove Discover was invoked.
        assert kwargs.get('discovery_timeout') == 5
        assert kwargs.get('target') == '192.168.1.255'
        assert kwargs.get('on_discovered') is ctrl._on_hub_deferred_lan_rediscover

    with patch('nodes.Controller.kasa.Discover.discover', new=AsyncMock(side_effect=fake_discover)) as disc:
        touched = asyncio.get_event_loop().run_until_complete(
            Controller.rediscover_hub_deferred_missing_lan_a(ctrl, force=True)
        )
    assert touched == 0
    disc.assert_awaited_once()
    assert ctrl._hub_deferred_lan_rediscover_inflight is False
    assert ctrl._hub_deferred_lan_rediscover_next > 0


def test_camera_query_forces_lan_rediscover_when_hub_deferred():
    ctrl = make_controller_stub()
    ctrl.rediscover_hub_deferred_missing_lan_a = AsyncMock(return_value=1)

    node = SmartCameraNode.__new__(SmartCameraNode)
    node.pfx = 'Tapo:Cam:'
    node.controller = ctrl
    node.hub_deferred = True
    node.set_state_a = AsyncMock()
    node.reportDrivers = MagicMock()

    asyncio.get_event_loop().run_until_complete(node._query_a())
    ctrl.rediscover_hub_deferred_missing_lan_a.assert_awaited_once_with(
        force=True
    )
    node.set_state_a.assert_awaited_once()
    node.reportDrivers.assert_called_once()


def test_camera_query_skips_rediscover_when_not_hub_deferred():
    ctrl = make_controller_stub()
    ctrl.rediscover_hub_deferred_missing_lan_a = AsyncMock(return_value=0)

    node = SmartCameraNode.__new__(SmartCameraNode)
    node.pfx = 'Cam:'
    node.controller = ctrl
    node.hub_deferred = False
    node.set_state_a = AsyncMock()
    node.reportDrivers = MagicMock()

    asyncio.get_event_loop().run_until_complete(node._query_a())
    ctrl.rediscover_hub_deferred_missing_lan_a.assert_not_called()
    node.set_state_a.assert_awaited_once()


def test_on_hub_deferred_lan_rediscover_closes_non_match():
    ctrl = make_controller_stub()
    ctrl._touch_hub_deferred_camera_from_lan = AsyncMock(return_value=False)
    ctrl._close_device_quietly = AsyncMock()
    ctrl._buffer_hub_cameras_for_adoption = MagicMock()
    ctrl._hub_deferred_lan_rediscover_touched = 0

    cam = MagicMock()
    cam.device_type = 'DeviceType.Camera'
    cam.host = '192.168.1.107'
    asyncio.get_event_loop().run_until_complete(
        Controller._on_hub_deferred_lan_rediscover(ctrl, cam)
    )
    ctrl._close_device_quietly.assert_awaited_once_with(cam)
    assert ctrl._hub_deferred_lan_rediscover_touched == 0

    ctrl._touch_hub_deferred_camera_from_lan = AsyncMock(return_value=True)
    ctrl._close_device_quietly.reset_mock()
    asyncio.get_event_loop().run_until_complete(
        Controller._on_hub_deferred_lan_rediscover(ctrl, cam)
    )
    ctrl._close_device_quietly.assert_not_called()
    assert ctrl._hub_deferred_lan_rediscover_touched == 1
    ctrl._buffer_hub_cameras_for_adoption.assert_called()

