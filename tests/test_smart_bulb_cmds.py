"""Tests for SmartBulb command capability guards."""

from __future__ import annotations

from unittest.mock import MagicMock

from conftest import make_controller_stub
from nodes.SmartBulbNode import SmartBulbNode


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
