"""Tests for standalone camera discover dedup when hub child exists."""
from __future__ import annotations

from conftest import make_controller_stub
from nodes.Controller import Controller


def _controller():
    ctrl = make_controller_stub()
    ctrl._hub_child_identities = set()
    ctrl._discover_batch_has_hub = False
    ctrl._logged_hub_managed_camera_skip = set()
    ctrl.Data = {}
    for name in (
        'should_skip_standalone_camera_discover',
        'should_skip_standalone_camera_cfg',
        'register_hub_child_identity',
        'is_registered_hub_child',
        '_hub_child_identity_tokens',
        '_is_hub_cfg',
        '_is_standalone_camera_cfg',
        '_has_saved_hub_cfg',
        '_has_hub_node',
        '_tapo_hub_known',
        '_log_hub_managed_camera_skip',
        'get_device_cfg',
        'smac',
        '_dev_attr',
    ):
        setattr(ctrl, name, getattr(Controller, name).__get__(ctrl, Controller))
    return ctrl


class _CameraDev:
    device_type = 'DeviceType.Camera'
    mac = '78:20:51:29:EF:61'
    device_id = 'CHILD01'
    parent = None
    _is_hub_child = False
    host = '192.168.1.118'
    alias = 'Front Cam'
    model = 'C260'


class _HubChildCameraDev(_CameraDev):
    parent = object()
    _is_hub_child = True


def test_skip_standalone_when_registered_hub_child():
    ctrl = _controller()
    dev = _CameraDev()
    ctrl.register_hub_child_identity(dev=dev)
    assert ctrl.should_skip_standalone_camera_discover(dev) is True


def test_skip_standalone_when_python_kasa_marks_hub_child():
    ctrl = _controller()
    assert ctrl.should_skip_standalone_camera_discover(_HubChildCameraDev()) is True


def test_skip_standalone_when_tapo_hub_in_saved_cfg():
    ctrl = _controller()
    ctrl.Data = {
        'ccbabd1606d8': '{"type":"DeviceType.Hub","address":"ccbabd1606d8","name":"Tapo Hub","mac":"CC:BA:BD:16:06:D8","host":"192.168.1.150"}',
    }
    assert ctrl.should_skip_standalone_camera_discover(_CameraDev()) is True


def test_skip_standalone_when_hub_in_discover_batch():
    ctrl = _controller()
    ctrl._discover_batch_has_hub = True
    assert ctrl.should_skip_standalone_camera_discover(_CameraDev()) is True


def test_skip_standalone_camera_cfg_when_hub_present():
    ctrl = _controller()
    ctrl.Data = {
        'ccbabd1606d8': '{"type":"DeviceType.Hub","address":"ccbabd1606d8","name":"Tapo Hub","mac":"CC:BA:BD:16:06:D8","host":"192.168.1.150"}',
    }
    cfg = {
        'type': 'DeviceType.Camera',
        'address': '78205129ef61',
        'name': 'Front Cam',
        'mac': '78:20:51:29:EF:61',
        'host': '192.168.1.118',
    }
    assert ctrl.should_skip_standalone_camera_cfg(cfg) is True


def test_do_not_skip_unregistered_standalone_camera_without_hub():
    ctrl = _controller()
    assert ctrl.should_skip_standalone_camera_discover(_CameraDev()) is False
