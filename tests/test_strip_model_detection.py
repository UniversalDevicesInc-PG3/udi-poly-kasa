"""Tests for strip vs plug detection via kasa children (HS103 fix)."""
from __future__ import annotations
import json
from unittest.mock import MagicMock
from conftest import make_controller_stub
from kasa_compat import KasaException
from nodes.Controller import Controller
from strip_models import (
    cfg_is_misclassified_plug,
    cfg_is_misclassified_strip,
    cfg_is_misclassified_strip_socket,
    dev_has_strip_children,
    dev_is_strip_parent,
    is_auto_misclassified_strip_name,
    is_strip_child_address,
    normalize_model,
    strip_plug_nodedef_id,
    upgrade_misclassified_plug_cfg,
)


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
        '_migrate_misclassified_strip_plugs', '_node_matches_cfg_type',
        '_node_is_strip_parent', '_dev_default_name', '_iter_unique_nodes',
        '_fix_stale_misclassified_strip_names', '_apply_node_name',
        '_upgrade_strip_cfg_if_needed', '_migrate_misclassified_strip_socket_cfg',
        '_is_strip_plug_node', '_normalize_dev_type', 'add_device_node', 'add_node',
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


def test_sys_info_kasa_exception_returns_false():
    class _RaisingDev:
        @property
        def sys_info(self):
            raise KasaException('You need to await update() to access the data')

    assert not dev_has_strip_children(_RaisingDev())


def test_node_matches_cfg_type_strip_plug():
    ctrl = _controller()
    node = MagicMock()
    node.id = 'SmartStrip_E'
    assert not ctrl._node_matches_cfg_type(
        node, {'type': 'DeviceType.StripSocket'},
    )
    node.id = 'SmartStripPlug_E'
    assert ctrl._node_matches_cfg_type(
        node, {'type': 'DeviceType.StripSocket'},
    )


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


def test_dev_is_strip_parent_hs300_with_children():
    assert _controller()._dev_is_strip_parent(
        _Dev('DeviceType.Plug', 'HS300', children=[object(), object()]),
    )


def test_dev_is_strip_parent_hs300_model_only():
    assert dev_is_strip_parent(_Dev('DeviceType.Plug', 'HS300'))


def test_dev_is_strip_parent_hs300_strip_socket_not_parent():
    assert not dev_is_strip_parent(_Dev('DeviceType.StripSocket', 'HS300'))


def test_is_strip_child_address():
    assert is_strip_child_address('6c5ab06d8c1601')
    assert is_strip_child_address('6c5ab06d8c1606')
    assert not is_strip_child_address('6c5ab06d8c16')
    assert not is_strip_child_address('6c5ab06d8c1607')


def test_cfg_is_misclassified_strip_socket():
    assert cfg_is_misclassified_strip_socket({
        'type': 'SmartPlug',
        'model': 'HS300',
        'address': '6c5ab06d8c1602',
        'id': 'SmartStrip_E',
    })
    assert not cfg_is_misclassified_strip_socket({
        'type': 'DeviceType.StripSocket',
        'address': '6c5ab06d8c1602',
        'id': 'SmartStripPlug_E',
    })


def test_cfg_is_misclassified_plug_skips_strip_child_address():
    assert not cfg_is_misclassified_plug({
        'type': 'SmartPlug',
        'model': 'HS300',
        'address': '6c5ab06d8c1601',
    })


def test_normalize_dev_type_strip_child_under_parent():
    ctrl = _controller()
    parent = MagicMock()
    parent.address = '6c5ab06d8c16'
    dev = _Dev('DeviceType.StripSocket', 'HS300')
    assert ctrl._normalize_dev_type(
        dev, parent=parent, address_suffix_num=1,
    ) == 'DeviceType.StripSocket'


def test_is_auto_misclassified_strip_name_skips_socket_label():
    assert not is_auto_misclassified_strip_name(
        'SmartStrip Socket for HS300US', 'HS300',
    )


def test_dev_is_strip_parent_hs103_strip_no_children():
    assert not dev_is_strip_parent(_Dev('DeviceType.Strip', 'HS103', is_strip=True))


def test_cfg_is_misclassified_plug_hs300():
    assert cfg_is_misclassified_plug(
        {'type': 'SmartPlug', 'model': 'HS300', 'address': 'aabbccddeeff'},
    )


def test_upgrade_misclassified_plug_cfg_hs300():
    cfg = {
        'type': 'SmartPlug',
        'model': 'HS300(US)',
        'name': 'Kasa HS300',
        'address': '6c5ab06d8c16',
        'host': '192.168.1.119',
        'id': '',
    }
    upgraded = upgrade_misclassified_plug_cfg(
        cfg,
        default_name='SmartStrip HS300',
    )
    assert upgraded['type'] == 'DeviceType.Strip'
    assert upgraded['model'] == 'HS300'
    assert upgraded['name'] == 'SmartStrip HS300'
    assert upgraded['emeter'] is True
    assert 'id' not in upgraded


def test_hs300_strip_cfg_not_misclassified_without_child_nodes():
    assert not cfg_is_misclassified_strip(
        {'type': 'DeviceType.Strip', 'model': 'HS300'},
        has_child_nodes=False,
    )


def test_upgrade_strip_cfg_if_needed_saves():
    ctrl = _controller()
    addr = '6c5ab06d8c16'
    ctrl.Data[addr] = json.dumps({
        'type': 'SmartPlug',
        'model': 'HS300',
        'address': addr,
        'name': 'Kasa HS300',
        'host': '192.168.1.119',
    })
    upgraded = ctrl._upgrade_strip_cfg_if_needed(ctrl.get_device_cfg(addr))
    saved = json.loads(ctrl.Data[addr])
    assert saved['type'] == 'DeviceType.Strip'
    assert upgraded['type'] == 'DeviceType.Strip'


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


def test_is_auto_misclassified_strip_name():
    assert is_auto_misclassified_strip_name('SmartStrip HS200', 'HS200')
    assert is_auto_misclassified_strip_name('SmartStrip HS200(US)', 'HS200')
    assert not is_auto_misclassified_strip_name('Kitchen HS200', 'HS200')
    assert not is_auto_misclassified_strip_name('SmartStrip HS300', 'HS200')


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
    assert plug['name'] == 'Kasa HS103'


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
    assert saved['name'] == 'Kasa HS103'


def test_fix_stale_misclassified_strip_names_hs200():
    ctrl = _controller()
    node = MagicMock()
    node.address = 'aabbccddeeff'
    node.name = 'SmartStrip HS200'
    node.id = 'SmartDimmer_'
    node.cfg = {'model': 'HS200', 'name': 'SmartStrip HS200', 'address': 'aabbccddeeff'}
    node.dev = _Dev('DeviceType.WallSwitch', 'HS200')
    node.dev.alias = 'Hall Dimmer'
    ctrl.nodes_by_mac = {'aabbccddeeff': node}
    ctrl.poly.getNodeNameFromDb = MagicMock(return_value='SmartStrip HS200')
    ctrl.poly.renameNode = MagicMock()
    ctrl.save_cfg = MagicMock()
    ctrl._fix_stale_misclassified_strip_names()
    ctrl.poly.renameNode.assert_called_once_with('aabbccddeeff', 'Hall Dimmer')
    assert node.name == 'Hall Dimmer'


def test_strip_plug_nodedef_id_from_emeter_and_cfg():
    assert strip_plug_nodedef_id(has_emeter=True) == 'SmartStripPlug_E'
    assert strip_plug_nodedef_id(has_emeter=False) == 'SmartStripPlug_N'
    assert strip_plug_nodedef_id(cfg={'emeter': True}) == 'SmartStripPlug_E'
    assert strip_plug_nodedef_id(cfg={'id': 'SmartStrip_E'}) == 'SmartStripPlug_E'
    assert strip_plug_nodedef_id(cfg={'id': 'SmartStripPlug_N'}) == 'SmartStripPlug_N'
    assert strip_plug_nodedef_id(cfg={}) == 'SmartStripPlug_N'


def test_migrate_misclassified_strip_socket_cfg():
    ctrl = _controller()
    addr = '6c5ab06d8c1602'
    ctrl.Data[addr] = json.dumps({
        'type': 'SmartPlug',
        'model': 'HS300',
        'address': addr,
        'name': 'SmartStrip Socket for HS300US',
        'host': '192.168.1.119',
        'id': 'SmartStrip_E',
        'emeter': True,
    })
    ctrl.remove_device_node = MagicMock(return_value=True)
    ctrl._migrate_misclassified_strip_socket_cfg()
    ctrl.remove_device_node.assert_called_once_with(addr, wait_for_pg3=True)
    saved = json.loads(ctrl.Data[addr])
    assert saved['type'] == 'DeviceType.StripSocket'
    assert saved['id'] == 'SmartStripPlug_E'
    assert saved['emeter'] is True


def test_add_node_refuses_empty_nodedef_id():
    ctrl = _controller()
    ctrl.add_node_gap = 0
    ctrl.wait_for_node_done = MagicMock()
    node = MagicMock()
    node.name = 'Broken Outlet'
    node.id = ''
    assert ctrl.add_node('6c5ab06d8c1602', node) is None
    ctrl.poly.addNode.assert_not_called()


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


def test_migrate_skips_hs300_strip_without_child_nodes():
    ctrl = _controller()
    addr = '6c5ab06d8c16'
    ctrl.Data[addr] = json.dumps({
        'type': 'DeviceType.Strip',
        'model': 'HS300',
        'address': addr,
        'name': 'SmartStrip HS300',
        'host': '192.168.1.119',
    })
    ctrl.poly.getNode = MagicMock(return_value=None)
    ctrl.remove_device_node = MagicMock()
    ctrl._migrate_misclassified_strip_plugs()
    ctrl.remove_device_node.assert_not_called()
    saved = json.loads(ctrl.Data[addr])
    assert saved['type'] == 'DeviceType.Strip'
