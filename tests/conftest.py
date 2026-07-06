"""Pytest configuration: plugin root on sys.path and kasa stubs."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Stub kasa exception types with real Exception subclasses (MagicMock breaks except).
_kasa_exc = types.ModuleType('kasa.exceptions')
for _name in ('AuthenticationError', 'KasaException', 'DeviceError'):
    setattr(_kasa_exc, _name, type(_name, (Exception,), {}))
_kasa_exc.SmartDeviceException = _kasa_exc.DeviceError
sys.modules['kasa.exceptions'] = _kasa_exc

if 'kasa' not in sys.modules:
    _kasa = MagicMock()
    _kasa.exceptions = _kasa_exc
    sys.modules['kasa'] = _kasa


def make_controller_stub(**overrides):
    """Minimal Controller instance without running __init__."""
    from nodes.Controller import Controller

    ctrl = object.__new__(Controller)
    ctrl.n_queue = []
    ctrl.add_node_timeout = 1.0
    ctrl._host_state = {}
    ctrl._host_breaker_threshold = 3
    ctrl.manual_networks = []
    ctrl.manual_devices = []
    ctrl.poly = MagicMock()
    ctrl.poly.network_interface = {'broadcast': '192.168.1.255'}
    ctrl.poly.getNodes.return_value = []
    ctrl.poly.getNode.return_value = None
    ctrl.nodes_by_mac = {}
    ctrl.Data = {}
    ctrl._auth_fail_count = {}
    ctrl.set_device_notice = MagicMock()
    ctrl.clear_device_notice = MagicMock()
    for key, value in overrides.items():
        setattr(ctrl, key, value)
    return ctrl
