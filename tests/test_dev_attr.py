"""Tests for safe kasa device attribute access on failed update."""

from __future__ import annotations

from conftest import make_controller_stub
from kasa.exceptions import KasaException


class _DevWithoutMac:
    host = '192.168.1.23'

    @property
    def mac(self):
        raise KasaException('You need to await update() to access the data')


def test_dev_attr_returns_none_when_property_requires_update():
    ctrl = make_controller_stub()
    assert ctrl._dev_attr(_DevWithoutMac(), 'mac') is None


def test_cfg_for_dev_falls_back_to_host_without_mac():
    ctrl = make_controller_stub()
    ctrl.Data = ['strip1']
    ctrl.get_device_cfg = lambda key: {
        'strip1': {'host': '192.168.1.23', 'name': 'HS200'},
    }.get(key)

    cfg = ctrl._cfg_for_dev(_DevWithoutMac())
    assert cfg['name'] == 'HS200'
