"""Tests for strip vs plug detection via kasa children (HS103 fix)."""
from __future__ import annotations
import json
from unittest.mock import MagicMock
from conftest import make_controller_stub
from nodes.Controller import Controller
from strip_models import cfg_is_misclassified_strip, dev_has_strip_children, normalize_model


class _Dev:
    def __init__(self, device_type, model, is_strip=False, children=None, sys_info=None):
        self.device_type = device_type
        self.model = model
        self.is_strip = is_strip
        self._children = list(children or [])
        self.sys_info = sys_info

    @property
    def children(self):
        return self._children


def _controller():
    ctrl = make_controller_stub()
    for name in (
        '_dev_model', '_dev_is_strip_parent', '_cfg_has_strip_child_nodes',
        '_cfg_is_misclassified_strip', '_cfg_as_plug', '_is_strip_parent_cfg',
        '_strip_parent_address_from_cfg', '_strip_child_address_pattern',
        '_pg3_node_meta', '_cfg_storage_key', 'smac', 'get_device_cfg', 'save_cfg',
        '_migrate_misclassified_strip_plugs',
    ):
        setattr(ctrl, name, getattr(Controller, name).__get__(ctrl, Controller))
    ctrl._pg3_name_for_address = MagicMock(return_value=None)
    return ctrl


def test_normalize_model_strips_region():
    assert normalize_model('HS300(US)') == 'HS300'


def test_hs103_strip_type_without_children():
    dev = _Dev('DeviceType.Strip', 'HS103', is_strip=True)
    assert not dev_has_strip_children(dev)


def test_hs300_with_children():
    dev = _Dev('DeviceType.Strip', 'HS300', children=[object(), object(), object()])
    assert dev_has_strip_children(dev)


def test_plug_with_sysinfo_children():
    dev = _Dev(
        'DeviceType.Plug',
        'HS300',
        sys_info={'children': [{'id': '1'}, {'id': '2'}, {'id': '3'}]},
    )
    assert dev_has_strip_children(dev)


def test_misclassified_hs103_cfg():
    assert cfg_is_misclassified_strip(
        {'type': 'DeviceType.Strip', 'model': 'HS103'},
        has_child_nodes=False,
    )


def test_hs300_cfg_not_misclassified_when_children_exist():
    assert not cfg_is_misclassified_strip(
        {'type': 'DeviceType.Strip', 'model': 'HS300'},
        has_child_nodes=True,
    )


def test_dev_is_strip_parent_hs103_strip_no_children():
    assert not _controller()._dev_is_strip_parent(
        _Dev('DeviceType.Strip', 'HS103', is_strip=True),
    )


def test_dev_is_strip_parent_hs300_with_children():
    assert _controller()._dev_is_strip_parent(
        _Dev('DeviceType.Plug', 'HS300', children=[object(), object()]),
    )


def test_is_strip_parent_cfg_rejects_hs103_without_child_nodes():
    ctrl = _controller()
    assert not ctrl._is_strip_parent_cfg({'type': 'DeviceType.Strip', 'model': 'HS103'})


def test_is_strip_parent_cfg_accepts_hs300_with_child_nodes():
    ctrl = _controller()
    addr = '112233445566'
    child_addr = f'{addr}01'
    ctrl.poly.getNode = MagicMock(side_effect=lambda a: object() if a == child_addr else None)
    assert ctrl._is_strip_parent_cfg({
        'type': 'DeviceType.Strip',
        'model': 'HS300',
        'address': addr,
    })


def test_cfg_as_plug_rewrites_type_and_model():
    ctrl = _controller()
    cfg = {
        'type': 'DeviceType.Strip',
        'model': 'HS103(US)',
        'address': 'aabbccddeeff',
        'name': 'SmartStrip HS103',
    }
    plug = ctrl._cfg_as_plug(cfg)
    assert plug['type'] == 'SmartPlug'
    assert plug['model'] == 'HS103'
    assert plug['name'] == 'SmartStrip HS103'


def test_migrate_misclassified_strip_plugs():
    ctrl = _controller()
    addr = 'aabbccddeeff'
    mac = 'aa:bb:cc:dd:ee:ff'
    ctrl.Data[addr] = json.dumps({
        'type': 'SmartStrip',
        'model': 'HS103',
        'address': addr,
        'mac': mac,
        'name': 'SmartStrip HS103',
        'host': '192.168.1.50',
    })
    ctrl.remove_device_node = MagicMock(return_value=True)
    ctrl._migrate_misclassified_strip_plugs()
    ctrl.remove_device_node.assert_called_once_with(addr, wait_for_pg3=True)
    saved = json.loads(ctrl.Data[addr])
    assert saved['type'] == 'SmartPlug'
    assert saved['model'] == 'HS103'


def test_migrate_skips_real_strip():
    ctrl = _controller()
    addr = '112233445566'
    child_addr = f'{addr}01'
    ctrl.Data[addr] = json.dumps({
        'type': 'DeviceType.Strip',
        'model': 'HS300',
        'address': addr,
        'mac': '11:22:33:44:55:66',
        'name': 'SmartStrip HS300',
        'host': '192.168.1.51',
    })
    ctrl.poly.getNode = MagicMock(side_effect=lambda a: object() if a == child_addr else None)
    ctrl.remove_device_node = MagicMock()
    ctrl._migrate_misclassified_strip_plugs()
    ctrl.remove_device_node.assert_not_called()
    saved = json.loads(ctrl.Data[addr])
    assert saved['type'] == 'DeviceType.Strip'
