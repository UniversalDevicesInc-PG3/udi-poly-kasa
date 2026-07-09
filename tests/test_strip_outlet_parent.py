"""HS300 strip outlets must parent under the strip, not the controller."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from conftest import make_controller_stub


def test_enqueue_startup_connect_skips_strip_plugs():
    ctrl = make_controller_stub()
    ctrl.startup_connect_queue = []
    ctrl.address = 'controller'
    node = MagicMock()
    node.address = '6c5ab06d8c1601'
    node.id = 'SmartStripPlug_E'
    ctrl.enqueue_startup_connect(node)
    assert ctrl.startup_connect_queue == []


def test_strip_parent_node_for_socket_cfg():
    ctrl = make_controller_stub()
    strip = MagicMock()
    strip.id = 'SmartStrip_E'
    strip.cfg = {'type': 'DeviceType.Strip', 'model': 'HS300'}
    ctrl.poly.getNode = MagicMock(return_value=strip)
    parent = ctrl._strip_parent_node_for_socket_cfg({
        'type': 'DeviceType.StripSocket',
        'address': '6c5ab06d8c1602',
    })
    assert parent is strip
    ctrl.poly.getNode.assert_called_with('6c5ab06d8c16')


def test_queue_device_add_from_cfg_uses_strip_parent():
    ctrl = make_controller_stub()
    strip = MagicMock()
    strip.id = 'SmartStrip_E'
    strip.cfg = {'type': 'DeviceType.Strip', 'model': 'HS300'}
    ctrl.poly.getNode = MagicMock(return_value=strip)
    ctrl._upgrade_strip_cfg_if_needed = lambda cfg, **_kw: cfg
    ctrl._is_camera_cfg = lambda cfg: False
    ctrl._cfg_is_misclassified_strip = lambda cfg: False
    queued = []
    ctrl.queue_device_add = lambda **kw: queued.append(kw)
    cfg = {
        'type': 'DeviceType.StripSocket',
        'address': '6c5ab06d8c1601',
        'name': 'Outlet 1',
    }
    ctrl._queue_device_add_from_cfg(cfg)
    assert len(queued) == 1
    assert queued[0]['parent'] is strip
    assert queued[0]['cfg'] is cfg


def test_bind_strip_plug_session_node_sets_dev_and_primary():
    ctrl = make_controller_stub()
    ctrl.address = 'controller'
    existing = MagicMock()
    existing.id = 'SmartStripPlug_E'
    existing.name = 'Outlet 1'
    existing.dev = None
    existing.primary_node = ctrl
    strip = MagicMock()
    strip.name = 'SmartStrip HS300'
    strip.address = '6c5ab06d8c16'
    child_dev = MagicMock()
    ctrl._bind_strip_plug_session_node(existing, parent=strip, dev=child_dev)
    assert existing.dev is child_dev
    assert existing.primary_node is strip
    assert existing.pfx.startswith('SmartStrip HS300:')


def test_strip_plug_is_connected_safe_without_parent_method():
    ctrl = make_controller_stub()
    parent = MagicMock(spec=[])  # no is_connected
    parent.name = 'Controller'
    ctrl.poly.getNode = MagicMock(return_value=parent)
    cfg = {
        'host': '192.168.1.119',
        'emeter': True,
        'type': 'DeviceType.StripSocket',
        'address': '6c5ab06d8c1602',
        'name': 'Outlet 2',
    }
    with patch('nodes.SmartDeviceNode.Node.__init__', return_value=None):
        from nodes.SmartStripPlugNode import SmartStripPlugNode

        node = SmartStripPlugNode(
            ctrl, 'controller', '6c5ab06d8c1602', 'Outlet 2',
            dev=None, cfg=cfg,
        )
    assert node.is_connected() is False


def test_strip_plug_set_drivers_a_skips_when_dev_none():
    ctrl = make_controller_stub()
    parent = MagicMock()
    parent.name = 'SmartStrip HS300'
    parent.is_connected = MagicMock(return_value=True)
    ctrl.poly.getNode = MagicMock(return_value=parent)
    cfg = {
        'host': '192.168.1.119',
        'emeter': True,
        'type': 'DeviceType.StripSocket',
        'address': '6c5ab06d8c1602',
        'name': 'Outlet 2',
    }
    with patch('nodes.SmartDeviceNode.Node.__init__', return_value=None):
        from nodes.SmartStripPlugNode import SmartStripPlugNode

        node = SmartStripPlugNode(
            ctrl, '6c5ab06d8c16', '6c5ab06d8c1602', 'Outlet 2',
            dev=None, cfg=cfg,
        )
    node.setDriver = MagicMock()
    asyncio.run(node.set_drivers_a())
    node.setDriver.assert_not_called()
