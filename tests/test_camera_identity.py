"""Tests for hub-child camera identity keys and cfg storage."""
from __future__ import annotations

from conftest import make_controller_stub
from nodes.Controller import Controller


def _controller():
    ctrl = make_controller_stub()
    ctrl._hub_child_identities = set()
    for name in (
        '_node_identity_key',
        '_cfg_storage_key',
        'smac',
        '_dev_attr',
        'register_hub_child_identity',
        'is_registered_hub_child',
    ):
        setattr(ctrl, name, getattr(Controller, name).__get__(ctrl, Controller))
    return ctrl


def test_hub_child_camera_identity_uses_device_id():
    ctrl = _controller()
    cfg = {
        'type': 'DeviceType.Camera',
        'hub_parent': 'ccbabd1606d8',
        'device_id': 'CAMERA123',
        'mac': '78:20:51:29:EF:61',
        'address': '78205129ef61',
    }
    assert ctrl._node_identity_key(cfg=cfg) == 'CAMERA123'


def test_hub_child_camera_storage_key_uses_device_id():
    ctrl = _controller()
    cfg = {
        'type': 'DeviceType.Camera',
        'hub_parent': 'ccbabd1606d8',
        'device_id': 'CAMERA123',
        'mac': '78:20:51:29:EF:61',
        'address': '78205129ef61',
    }
    assert ctrl._cfg_storage_key(cfg) == 'CAMERA123'


def test_standalone_camera_identity_uses_mac():
    ctrl = _controller()
    cfg = {
        'type': 'DeviceType.Camera',
        'mac': '78:20:51:29:EF:61',
        'address': '78205129ef61',
    }
    assert ctrl._node_identity_key(cfg=cfg) == '78205129EF61'


def test_register_hub_child_identity_by_mac():
    ctrl = _controller()
    class _Dev:
        mac = '78:20:51:29:EF:61'
        device_id = 'CHILD01'

    ctrl.register_hub_child_identity(dev=_Dev())
    assert ctrl.is_registered_hub_child(dev=_Dev())
