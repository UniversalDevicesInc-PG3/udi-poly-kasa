"""Tests for add_device_node dispatch to camera and hub node classes."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from conftest import make_controller_stub
from nodes.Controller import Controller


def _controller():
    ctrl = make_controller_stub()
    ctrl.address = 'kasacontroller'
    ctrl._hub_child_identities = set()
    ctrl._pending_device_adds = []
    ctrl.devm = {}
    ctrl.add_node = MagicMock()
    ctrl.poly.getNode.return_value = MagicMock(name='added_node')
    for name in (
        '_normalize_dev_type',
        '_dev_default_name',
        '_session_has_device',
        '_remember_session_node',
        '_node_identity_key',
        '_cfg_storage_key',
        'register_hub_child_identity',
        'smac',
        '_dev_attr',
        '_dev_model',
        '_dev_is_strip_parent',
        '_is_strip_parent_cfg',
    ):
        setattr(ctrl, name, getattr(Controller, name).__get__(ctrl, Controller))
    return ctrl


class _CameraDev:
    device_type = 'DeviceType.Camera'
    mac = '78205129ef61'
    host = '192.168.1.118'
    model = 'C260'
    alias = 'Front Cam'
    device_id = None
    parent = None


class _HubDev:
    device_type = 'DeviceType.Hub'
    mac = 'ccbabd1606d8'
    host = '192.168.1.150'
    model = 'H500'
    alias = 'Tapo Hub'


@patch('nodes.Controller.SmartCameraNode')
def test_add_device_node_camera(MockCameraNode):
    ctrl = _controller()
    dev = _CameraDev()
    ctrl.add_device_node(dev=dev)
    MockCameraNode.assert_called_once()
    args = MockCameraNode.call_args[0]
    assert args[2] == dev.mac  # IoX address from device MAC
    ctrl.add_node.assert_called_once()


@patch('nodes.Controller.SmartHubNode')
def test_add_device_node_hub(MockHubNode):
    ctrl = _controller()
    dev = _HubDev()
    ctrl.add_device_node(dev=dev)
    MockHubNode.assert_called_once()
    ctrl.add_node.assert_called_once()


@patch('nodes.Controller.SmartCameraNode')
def test_add_hub_child_camera_sets_hub_parent(MockCameraNode):
    ctrl = _controller()
    parent = MagicMock()
    parent.address = 'ccbabd1606d8'
    parent.host = '192.168.1.150'
    dev = _CameraDev()
    dev.device_id = 'CHILD01'
    dev.parent = parent
    ctrl.add_device_node(parent=parent, dev=dev)
    cfg_passed = MockCameraNode.call_args[1]['cfg']
    assert cfg_passed['hub_parent'] == 'ccbabd1606d8'
    assert cfg_passed['device_id'] == 'CHILD01'
    assert cfg_passed['host'] == '192.168.1.150'
    assert cfg_passed['address'] == '78205129ef61'
    args = MockCameraNode.call_args[0]
    assert args[2] == '78205129ef61'


@patch('nodes.Controller.SmartCameraNode')
def test_add_hub_deferred_child_keeps_lan_host(MockCameraNode):
    ctrl = _controller()
    parent = MagicMock()
    parent.address = 'ccbabd1606d8'
    parent.host = '192.168.1.150'
    dev = _CameraDev()
    ctrl.add_device_node(
        parent=parent, dev=dev, hub_deferred=True,
    )
    cfg_passed = MockCameraNode.call_args[1]['cfg']
    assert cfg_passed['hub_parent'] == 'ccbabd1606d8'
    assert cfg_passed['host'] == '192.168.1.118'
    assert cfg_passed.get('hub_deferred') is True
    assert cfg_passed['address'] == '78205129ef61'
