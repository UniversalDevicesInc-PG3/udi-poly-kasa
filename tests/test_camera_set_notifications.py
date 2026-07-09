"""Hub-child Set Notifications when camera.dev is unbound."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from conftest import make_controller_stub


def _make_hub_camera(*, cfg, hub_children=None):
    ctrl = make_controller_stub()
    hub = MagicMock()
    hub.name = 'Tapo_H500_06D8'
    hub.host = '192.168.1.48'
    hub.dev = MagicMock()
    hub.dev.children = hub_children or []
    ctrl.poly.getNode = MagicMock(return_value=hub)
    ctrl.hub_node_update_a = AsyncMock(return_value=True)
    ctrl.smac = lambda mac: str(mac or '').replace(':', '').lower()
    ctrl.discover_single = AsyncMock(return_value=None)
    ctrl.update_dev = AsyncMock(return_value=True)

    with patch('nodes.SmartDeviceNode.Node.__init__', return_value=None):
        from nodes.SmartCameraNode import SmartCameraNode

        node = SmartCameraNode(
            ctrl, 'ccbabd1606d8', 'aabbccddeeff', 'CamOutBackNorth',
            dev=None, cfg=cfg,
        )
    node.dev = None
    node.cfg = cfg
    node.controller = ctrl
    node.primary_node = hub
    node.hub_child = True
    node.hub_deferred = bool(cfg.get('hub_deferred'))
    node.pfx = f'{hub.name}:{node.name}:'
    node.setDriver = MagicMock()
    return node, ctrl, hub


def test_resolve_hub_child_dev_finds_child_when_dev_none():
    child = MagicMock()
    child.device_id = 'child-1'
    child.mac = 'AA:BB:CC:DD:EE:FF'
    cfg = {
        'type': 'DeviceType.Camera',
        'hub_parent': 'ccbabd1606d8',
        'hub_deferred': True,
        'device_id': 'child-1',
        'mac': 'AA:BB:CC:DD:EE:FF',
        'camera_host': '192.168.1.90',
        'host': '192.168.1.48',
    }
    node, _ctrl, _hub = _make_hub_camera(cfg=cfg, hub_children=[child])
    resolved = asyncio.run(node._resolve_hub_child_dev())
    assert resolved is child
    assert node.dev is child


def test_set_notifications_falls_back_to_lan_when_unbound():
    cfg = {
        'type': 'DeviceType.Camera',
        'hub_parent': 'ccbabd1606d8',
        'hub_deferred': True,
        'device_id': 'missing',
        'mac': 'AA:BB:CC:DD:EE:FF',
        'camera_host': '192.168.1.90',
        'host': '192.168.1.48',
    }
    node, ctrl, _hub = _make_hub_camera(cfg=cfg, hub_children=[])
    lan_dev = MagicMock()
    ctrl.discover_single = AsyncMock(return_value=lan_dev)
    with patch(
        'nodes.SmartCameraNode.set_camera_notifications_enabled',
        new_callable=AsyncMock,
    ) as set_notif:
        asyncio.run(node._set_notifications_a(False))
        ctrl.discover_single.assert_awaited_once_with(host='192.168.1.90')
        set_notif.assert_awaited_once_with(lan_dev, False)


def test_set_notifications_uses_resolved_hub_child():
    child = MagicMock()
    child.device_id = 'child-1'
    child.mac = 'AA:BB:CC:DD:EE:FF'
    cfg = {
        'type': 'DeviceType.Camera',
        'hub_parent': 'ccbabd1606d8',
        'device_id': 'child-1',
        'mac': 'AA:BB:CC:DD:EE:FF',
        'camera_host': '192.168.1.90',
        'host': '192.168.1.48',
    }
    node, ctrl, _hub = _make_hub_camera(cfg=cfg, hub_children=[child])
    with patch(
        'nodes.SmartCameraNode.set_camera_notifications_enabled',
        new_callable=AsyncMock,
    ) as set_notif:
        asyncio.run(node._set_notifications_a(True))
        set_notif.assert_awaited_once_with(child, True)
        ctrl.discover_single.assert_not_called()


def test_set_on_does_not_set_st_when_write_fails():
    from kasa.exceptions import DeviceError

    cfg = {
        'type': 'DeviceType.Camera',
        'hub_parent': 'ccbabd1606d8',
        'hub_deferred': True,
        'device_id': 'missing',
        'mac': 'AA:BB:CC:DD:EE:FF',
        'host': '192.168.1.48',
        # no camera_host → LAN fallback cannot run
    }
    node, _ctrl, _hub = _make_hub_camera(cfg=cfg, hub_children=[])
    try:
        asyncio.run(node.set_on_a())
        assert False, 'expected DeviceError'
    except DeviceError as ex:
        assert 'privacy control' in str(ex) or 'LAN host' in str(ex)
    node.setDriver.assert_not_called()


def test_set_on_sets_st_only_after_success():
    child = MagicMock()
    child.device_id = 'child-1'
    child.mac = 'AA:BB:CC:DD:EE:FF'
    child.set_state = AsyncMock()
    cfg = {
        'type': 'DeviceType.Camera',
        'hub_parent': 'ccbabd1606d8',
        'device_id': 'child-1',
        'mac': 'AA:BB:CC:DD:EE:FF',
        'camera_host': '192.168.1.90',
        'host': '192.168.1.48',
    }
    node, _ctrl, _hub = _make_hub_camera(cfg=cfg, hub_children=[child])
    node.set_state_a = AsyncMock()
    asyncio.run(node.set_on_a())
    child.set_state.assert_awaited_once_with(True)
    node.setDriver.assert_called_once_with('ST', 100)
