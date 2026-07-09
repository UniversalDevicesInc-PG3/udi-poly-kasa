"""Tests for Tapo cloud getDeviceList roster integration."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from camera_helpers import (
    cloud_device_to_snapshot,
    cloud_devices_to_snapshots,
    merge_camera_snapshots,
)
from conftest import make_controller_stub
from nodes.Controller import Controller
from tapo_cloud import decode_cloud_alias, normalize_cloud_device


def test_decode_cloud_alias_plain_text():
    assert decode_cloud_alias('CamOutBackSouth') == 'CamOutBackSouth'


def test_decode_cloud_alias_base64():
    import base64

    encoded = base64.b64encode(b'CamOutBackSouth').decode()
    assert decode_cloud_alias(encoded) == 'CamOutBackSouth'


def test_normalize_cloud_device():
    raw = {
        'alias': 'Q2FtT3V0QmFja1NvdXRo',
        'deviceModel': 'C675D',
        'deviceType': 'SMART.IPCAMERA',
        'deviceId': '80212A86CDD1DC98AEA18DD3E6C1D8772552D7AD',
        'deviceMac': 'ACA7F1DAD82B',
    }
    norm = normalize_cloud_device(raw)
    assert norm['alias'] == 'CamOutBackSouth'
    assert norm['device_model'] == 'C675D'
    assert norm['mac'] == 'ACA7F1DAD82B'


def test_cloud_device_to_snapshot():
    snap = cloud_device_to_snapshot({
        'alias': 'CamOutBackSouth',
        'device_model': 'C675D',
        'device_type': 'SMART.IPCAMERA',
        'device_id': '80212A86CDD1DC98AEA18DD3E6C1D8772552D7AD',
        'mac': 'ACA7F1DAD82B',
    })
    assert snap['alias'] == 'CamOutBackSouth'
    assert snap['battery'] is True
    assert snap['cloud'] is True
    assert snap['host'] is None


def test_cloud_devices_to_snapshots_filters_type():
    devices = [
        {
            'alias': 'Tapo Hub',
            'device_model': 'H500',
            'device_type': 'SMART.TAPOHUB',
            'device_id': 'hub1',
            'mac': 'CCBABD1606D8',
        },
        {
            'alias': 'CamOutBackSouth',
            'device_model': 'C675D',
            'device_type': 'SMART.IPCAMERA',
            'device_id': 'cam1',
            'mac': 'ACA7F1DAD82B',
        },
    ]
    snaps = cloud_devices_to_snapshots(devices)
    assert len(snaps) == 1
    assert snaps[0]['alias'] == 'CamOutBackSouth'


def test_merge_camera_snapshots_keeps_lan_host():
    merged = merge_camera_snapshots(
        {'mac': 'ACA7F1DAD82B', 'host': '192.168.1.103', 'alias': None},
        {'mac': 'ACA7F1DAD82B', 'host': None, 'alias': 'CamOutBackSouth'},
    )
    assert merged['host'] == '192.168.1.103'
    assert merged['alias'] == 'CamOutBackSouth'


def _controller():
    ctrl = make_controller_stub(
        Parameters={'user': 'user@example.com', 'password': 'secret'},
        _deferred_hub_cameras=[],
        _camera_alias_cache={},
        _cloud_roster_last_fetch=0.0,
        _cloud_roster_refresh_secs=300.0,
        ready=True,
    )
    for name in (
        '_tapo_hub_known',
        '_credentials_configured',
        '_buffer_camera_snapshots_for_adoption',
        '_remember_cloud_camera_aliases',
        '_apply_cloud_camera_roster',
        '_try_adopt_deferred_hub_cameras',
        '_upgrade_generic_camera_cfg_names',
        '_fix_stale_auto_generated_camera_names',
        '_remember_camera_alias',
        '_norm_camera_mac',
        '_norm_camera_device_id',
        '_persist_camera_alias_cache',
        'smac',
    ):
        setattr(ctrl, name, getattr(Controller, name).__get__(ctrl, Controller))
    ctrl._tapo_hub_known = MagicMock(return_value=True)
    ctrl._credentials_configured = MagicMock(return_value=True)
    ctrl._try_adopt_deferred_hub_cameras = MagicMock()
    ctrl._upgrade_generic_camera_cfg_names = MagicMock()
    ctrl._fix_stale_auto_generated_camera_names = MagicMock()
    return ctrl


@patch('nodes.Controller.fetch_cloud_camera_roster')
def test_refresh_tapo_cloud_roster_buffers_cameras(mock_fetch):
    mock_fetch.return_value = [
        {
            'alias': 'CamOutBackSouth',
            'device_model': 'C675D',
            'device_type': 'SMART.IPCAMERA',
            'device_id': '80212A86CDD1DC98AEA18DD3E6C1D8772552D7AD',
            'mac': 'ACA7F1DAD82B',
        },
    ]
    ctrl = _controller()
    import asyncio

    asyncio.run(ctrl._refresh_tapo_cloud_roster_a(force=True))
    assert len(ctrl._deferred_hub_cameras) == 1
    assert ctrl._deferred_hub_cameras[0]['alias'] == 'CamOutBackSouth'
    ctrl._try_adopt_deferred_hub_cameras.assert_called_once()
