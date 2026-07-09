"""Tests for hub-child camera privacy control helpers."""

from __future__ import annotations

from camera_helpers import camera_lan_host, hub_child_control_fallback_eligible


class _ChildDev:
  def __init__(self, ipaddr='192.168.1.77'):
    self._child_info_from_parent = {'ipaddr': ipaddr}


def test_camera_lan_host_prefers_saved_cfg():
    cfg = {'camera_host': '192.168.1.55', 'host': '192.168.1.48'}
    assert camera_lan_host(cfg=cfg, hub_host='192.168.1.48') == '192.168.1.55'


def test_camera_lan_host_from_child_info():
    cfg = {'host': '192.168.1.48'}
    assert camera_lan_host(
        cfg=cfg,
        dev=_ChildDev('192.168.1.77'),
        hub_host='192.168.1.48',
    ) == '192.168.1.77'


def test_camera_lan_host_deferred_uses_saved_host():
    cfg = {'host': '192.168.1.88', 'hub_deferred': True}
    assert camera_lan_host(cfg=cfg, hub_host='192.168.1.48') == '192.168.1.88'


def test_hub_child_control_fallback_eligible():
    class _Ex:
        error_code = type('Code', (), {'name': 'PROTOCOL_FORMAT_ERROR'})()

    assert hub_child_control_fallback_eligible(_Ex())
    assert hub_child_control_fallback_eligible(Exception('PROTOCOL_FORMAT_ERROR(-40210)'))
