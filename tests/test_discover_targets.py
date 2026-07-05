"""Tests for discovery broadcast target normalization and ordering."""

from __future__ import annotations

from conftest import make_controller_stub


def test_normalize_broadcast_address_maps_host_to_subnet_broadcast():
    ctrl = make_controller_stub()
    assert ctrl._normalize_broadcast_address('192.168.222.1') == '192.168.222.255'
    assert ctrl._normalize_broadcast_address('192.168.222.255') == '192.168.222.255'


def test_normalize_broadcast_address_passthrough_non_ipv4():
    ctrl = make_controller_stub()
    assert ctrl._normalize_broadcast_address('fe80::1') == 'fe80::1'
    assert ctrl._normalize_broadcast_address('') is None


def test_discover_targets_dedupes_interface_manual_and_cfg_hosts():
    ctrl = make_controller_stub(
        manual_networks=[{'address': '192.168.222.1'}],
        manual_devices=[{'address': '192.168.222.42'}],
    )
    ctrl.Data = ['plug1', 'plug2']
    ctrl.get_device_cfg = lambda key: {
        'plug1': {'host': '10.0.0.5'},
        'plug2': {'host': '10.0.0.5'},
    }.get(key)

    targets = ctrl._discover_targets()

    assert targets[0] == '192.168.1.255'
    assert '192.168.222.255' in targets
    assert '10.0.0.255' in targets
    assert targets.count('192.168.222.255') == 1
    assert targets.count('10.0.0.255') == 1
