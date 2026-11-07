"""Tests for SmartBulb command capability guards and DON brightness."""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

from conftest import make_controller_stub
from converters import don_level_to_bri, st2bri
from nodes.SmartBulbNode import SmartBulbNode
from nodes.SmartDimmerNode import SmartDimmerNode

_bulb_mod = importlib.import_module('nodes.SmartBulbNode')
_dimmer_mod = importlib.import_module('nodes.SmartDimmerNode')


def _make_bulb(dev=None):
    bulb = object.__new__(SmartBulbNode)
    bulb.dev = dev
    bulb.pfx = 'TestBulb:'
    bulb.controller = make_controller_stub()
    return bulb


def _mock_dev(dimmable=True):
    dev = MagicMock()
    dev.features = {'brightness': True} if dimmable else {}
    return dev


def test_cmd_brt_rejects_when_not_connected():
    bulb = _make_bulb(dev=None)
    assert bulb.cmd_brt({}) is False


def test_cmd_dim_rejects_non_dimmable_device():
    bulb = _make_bulb(_mock_dev(dimmable=False))
    assert bulb.cmd_dim({}) is False


def test_cmd_set_bri_rejects_non_dimmable_device():
    bulb = _make_bulb(_mock_dev(dimmable=False))
    assert bulb.cmd_set_bri({'value': 50}) is False


def test_cmd_set_color_xy_explicitly_rejected():
    bulb = _make_bulb(_mock_dev())
    assert bulb.cmd_set_color_xy({}) is False


def _patch_bulb_on(monkeypatch, bulb):
    bulb.controller.mainloop = object()
    bulb.set_on = MagicMock()
    bulb.set_off = MagicMock()
    bulb.set_bri_a = MagicMock(return_value='coro')
    bulb._future_result = MagicMock(return_value=True)
    monkeypatch.setattr(
        _bulb_mod.asyncio, 'run_coroutine_threadsafe', lambda coro, loop: 'fut'
    )
    return bulb


def test_don_level_percent_and_byte_scales():
    assert don_level_to_bri({}) is None
    assert don_level_to_bri({'value': ''}) is None
    assert don_level_to_bri({'value': '50'}) == st2bri(50)
    assert don_level_to_bri({'value': '50', 'uom': 51}) == st2bri(50)
    assert don_level_to_bri({'value': '50', 'uom': '51'}) == st2bri(50)
    assert don_level_to_bri({'value': '0'}) == 0
    assert don_level_to_bri({'value': '180', 'uom': 100}) == 180
    assert don_level_to_bri({'value': '255'}) == 254


def test_cmd_set_on_without_value_does_not_set_brightness(monkeypatch):
    bulb = _patch_bulb_on(monkeypatch, _make_bulb(_mock_dev()))
    bulb.cmd_set_on({})
    bulb.set_on.assert_called_once_with()
    bulb.set_bri_a.assert_not_called()
    bulb.set_off.assert_not_called()


def test_cmd_set_on_percent_sets_brightness(monkeypatch):
    bulb = _patch_bulb_on(monkeypatch, _make_bulb(_mock_dev()))
    assert bulb.cmd_set_on({'value': '50'}) is True
    bulb.set_bri_a.assert_called_once_with(st2bri(50))
    bulb.set_on.assert_not_called()
    bulb.set_bri_a.reset_mock()
    bulb.cmd_set_on({'value': '50', 'uom': 51})
    bulb.set_bri_a.assert_called_once_with(st2bri(50))


def test_cmd_set_on_uom_100_passes_byte_through(monkeypatch):
    bulb = _patch_bulb_on(monkeypatch, _make_bulb(_mock_dev()))
    bulb.cmd_set_on({'value': '180', 'uom': 100})
    bulb.set_bri_a.assert_called_once_with(180)
    bulb.set_on.assert_not_called()


def test_cmd_set_on_zero_turns_off(monkeypatch):
    bulb = _patch_bulb_on(monkeypatch, _make_bulb(_mock_dev()))
    bulb.cmd_set_on({'value': '0'})
    bulb.set_off.assert_called_once_with()
    bulb.set_on.assert_not_called()
    bulb.set_bri_a.assert_not_called()


def test_cmd_set_on_non_dimmable_ignores_level(monkeypatch):
    bulb = _patch_bulb_on(monkeypatch, _make_bulb(_mock_dev(dimmable=False)))
    bulb.cmd_set_on({'value': '50'})
    bulb.set_on.assert_called_once_with()
    bulb.set_bri_a.assert_not_called()


def _make_dimmer(node_id, dimmable=True):
    node = object.__new__(SmartDimmerNode)
    node.id = node_id
    node.pfx = 'TestDimmer:'
    node.dev = _mock_dev(dimmable=dimmable)
    node.controller = make_controller_stub()
    node.set_on = MagicMock()
    node.set_bri = MagicMock()
    node.cmd_set_off = MagicMock()
    return node


def test_dimmer_don_percent_sets_brightness(monkeypatch):
    node = _make_dimmer('SmartDimmer_DN')
    monkeypatch.setattr(_dimmer_mod.asyncio, 'run', lambda coro: None)
    node.cmd_set_on({'value': '50', 'uom': 51})
    node.set_bri.assert_called_once_with(st2bri(50))
    node.set_on.assert_not_called()
    node.cmd_set_off.assert_not_called()


def test_dimmer_don_without_value_is_plain_on(monkeypatch):
    node = _make_dimmer('SmartDimmer_DN')
    monkeypatch.setattr(_dimmer_mod.asyncio, 'run', lambda coro: None)
    node.cmd_set_on({})
    node.set_on.assert_called_once_with()
    node.set_bri.assert_not_called()


def test_switch_don_ignores_level(monkeypatch):
    node = _make_dimmer('SmartDimmer_NN', dimmable=False)
    monkeypatch.setattr(_dimmer_mod.asyncio, 'run', lambda coro: None)
    node.cmd_set_on({'value': '50'})
    node.set_on.assert_called_once_with()
    node.set_bri.assert_not_called()
