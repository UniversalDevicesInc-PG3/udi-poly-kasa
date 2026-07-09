"""Cfg-only SmartStripPlugNode must set a non-empty IoX nodeDefId."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from conftest import make_controller_stub


def test_strip_plug_node_sets_id_from_cfg_without_dev():
    ctrl = make_controller_stub()
    parent = MagicMock()
    parent.name = 'SmartStrip HS300'
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
    assert node.id == 'SmartStripPlug_E'
    assert cfg['id'] == 'SmartStripPlug_E'


def test_strip_plug_node_defaults_id_when_cfg_missing_emeter():
    ctrl = make_controller_stub()
    parent = MagicMock()
    parent.name = 'SmartStrip HS300'
    ctrl.poly.getNode = MagicMock(return_value=parent)
    cfg = {
        'host': '192.168.1.119',
        'type': 'DeviceType.StripSocket',
        'address': '6c5ab06d8c1603',
        'name': 'Outlet 3',
    }
    with patch('nodes.SmartDeviceNode.Node.__init__', return_value=None):
        from nodes.SmartStripPlugNode import SmartStripPlugNode

        node = SmartStripPlugNode(
            ctrl, '6c5ab06d8c16', '6c5ab06d8c1603', 'Outlet 3',
            dev=None, cfg=cfg,
        )
    assert node.id == 'SmartStripPlug_N'
