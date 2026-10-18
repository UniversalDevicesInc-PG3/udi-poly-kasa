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
from tapo_cloud import (
    CloudTokenError,
    cloud_login,
    decode_cloud_alias,
    fetch_cloud_camera_roster,
    normalize_cloud_device,
)


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
        _cloud_roster_token=None,
        _cloud_login_backoff_until=0.0,
        _cloud_login_backoff_secs=3600.0,
        _cloud_login_notice_posted=False,
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
    ctrl.poly.uuid = 'ns-uuid-1'
    return ctrl


@patch('nodes.Controller.fetch_cloud_camera_roster')
def test_refresh_tapo_cloud_roster_buffers_cameras(mock_fetch):
    mock_fetch.return_value = (
        [
            {
                'alias': 'CamOutBackSouth',
                'device_model': 'C675D',
                'device_type': 'SMART.IPCAMERA',
                'device_id': '80212A86CDD1DC98AEA18DD3E6C1D8772552D7AD',
                'mac': 'ACA7F1DAD82B',
            },
        ],
        'token-1',
    )
    ctrl = _controller()
    import asyncio

    asyncio.run(ctrl._refresh_tapo_cloud_roster_a(force=True))
    assert len(ctrl._deferred_hub_cameras) == 1
    assert ctrl._deferred_hub_cameras[0]['alias'] == 'CamOutBackSouth'
    assert ctrl._cloud_roster_token == 'token-1'
    mock_fetch.assert_called_once()
    assert mock_fetch.call_args.kwargs['terminal_uuid'] == 'ns-uuid-1'
    assert mock_fetch.call_args.kwargs['token'] is None
    ctrl._try_adopt_deferred_hub_cameras.assert_called_once()


_RAW_CAM = {
    'alias': 'CamOutBackSouth',
    'deviceModel': 'C675D',
    'deviceType': 'SMART.IPCAMERA',
    'deviceId': 'cam1',
    'deviceMac': 'ACA7F1DAD82B',
}


@patch('tapo_cloud._post')
def test_cloud_login_sends_supplied_terminal_uuid(mock_post):
    mock_post.return_value = {'error_code': 0, 'result': {'token': 'tok'}}
    token, _api = cloud_login('user@example.com', 'secret', terminal_uuid='ns-uuid-1')
    assert token == 'tok'
    body = mock_post.call_args.args[1]
    assert body['params']['terminalUUID'] == 'ns-uuid-1'


def test_cloud_login_requires_terminal_uuid():
    try:
        cloud_login('user@example.com', 'secret', terminal_uuid='')
    except ValueError as ex:
        assert 'terminal_uuid' in str(ex)
    else:
        raise AssertionError('expected ValueError')


@patch('tapo_cloud.cloud_login')
@patch('tapo_cloud.cloud_get_device_list')
def test_cached_token_skips_login(mock_list, mock_login):
    mock_list.return_value = [_RAW_CAM]
    cameras, token = fetch_cloud_camera_roster(
        'user@example.com',
        'secret',
        terminal_uuid='ns-uuid-1',
        token='existing',
    )
    mock_login.assert_not_called()
    mock_list.assert_called_once()
    assert token == 'existing'
    assert cameras[0]['alias'] == 'CamOutBackSouth'


@patch('tapo_cloud.cloud_login')
@patch('tapo_cloud.cloud_get_device_list')
def test_token_error_logs_in_once_and_retries(mock_list, mock_login):
    mock_list.side_effect = [CloudTokenError('expired'), [_RAW_CAM]]
    mock_login.return_value = ('new-token', 'https://wap.tplinkcloud.com')
    cameras, token = fetch_cloud_camera_roster(
        'user@example.com',
        'secret',
        terminal_uuid='ns-uuid-1',
        token='old-token',
    )
    assert mock_login.call_count == 1
    assert mock_login.call_args.kwargs['terminal_uuid'] == 'ns-uuid-1'
    assert mock_list.call_count == 2
    assert token == 'new-token'
    assert len(cameras) == 1


@patch('tapo_cloud.cloud_login', side_effect=RuntimeError('Tapo cloud login failed'))
@patch('tapo_cloud.cloud_get_device_list')
def test_login_failure_does_not_retry_inside_fetch(mock_list, mock_login):
    try:
        fetch_cloud_camera_roster(
            'user@example.com',
            'secret',
            terminal_uuid='ns-uuid-1',
        )
    except RuntimeError as ex:
        assert 'login failed' in str(ex)
    else:
        raise AssertionError('expected RuntimeError')
    assert mock_login.call_count == 1
    mock_list.assert_not_called()


@patch('nodes.Controller.fetch_cloud_camera_roster')
def test_login_failure_does_not_retry_inside_backoff(mock_fetch):
    import asyncio

    mock_fetch.side_effect = RuntimeError('Tapo cloud login failed: bad password')
    ctrl = _controller()
    asyncio.run(ctrl._refresh_tapo_cloud_roster_a(force=True))
    assert mock_fetch.call_count == 1
    assert ctrl._cloud_login_backoff_until > 0
    assert ctrl._cloud_login_notice_posted is True
    asyncio.run(ctrl._refresh_tapo_cloud_roster_a(force=True))
    assert mock_fetch.call_count == 1


def test_long_poll_discover_does_not_schedule_cloud_refresh():
    ctrl = _controller()
    ctrl._pending_device_adds = []
    ctrl._discover_targets = MagicMock(return_value=[])
    ctrl.drain_pending_device_adds = MagicMock()
    ctrl._after_inventory_sync = MagicMock()
    ctrl._schedule_tapo_cloud_roster_refresh = MagicMock()
    Controller.discover_new(ctrl)
    ctrl._schedule_tapo_cloud_roster_refresh.assert_not_called()
    Controller.discover_new(ctrl, refresh_cloud=True)
    ctrl._schedule_tapo_cloud_roster_refresh.assert_called_once_with(force=True)


def test_password_change_retries_authorization():
    ctrl = _controller()
    ctrl._cloud_cred_key = ('user@example.com', 'old')
    ctrl._cloud_login_backoff_until = 99
    ctrl._cloud_roster_token = 'stale'
    ctrl._schedule_auth_retry = MagicMock()
    Controller._remember_cloud_credentials(ctrl, 'user@example.com', 'new')
    assert ctrl._cloud_login_backoff_until == 0.0
    assert ctrl._cloud_roster_token is None
    ctrl._schedule_auth_retry.assert_called_once()


def test_unchanged_password_does_not_retry():
    ctrl = _controller()
    ctrl._cloud_cred_key = ('user@example.com', 'same')
    ctrl._schedule_auth_retry = MagicMock()
    Controller._remember_cloud_credentials(ctrl, 'user@example.com', 'same')
    ctrl._schedule_auth_retry.assert_not_called()


def test_schedule_auth_retry_clears_host_backoff_and_submits():
    ctrl = _controller()
    ctrl._auth_fail_count = {'192.168.1.150': 3}
    ctrl._host_state = {
        '192.168.1.150': {
            'fail': 4,
            'next_try': 50.0,
            'next_probe': 50.0,
            'protocol_fail': True,
        },
    }
    ctrl.mainloop = object()
    with patch(
        'nodes.Controller.asyncio.get_running_loop',
        side_effect=RuntimeError,
    ), patch('nodes.Controller.asyncio.run_coroutine_threadsafe') as submit:
        Controller._schedule_auth_retry(ctrl)
    assert ctrl._auth_fail_count == {}
    assert ctrl._host_state['192.168.1.150']['fail'] == 0
    assert ctrl._host_state['192.168.1.150']['next_try'] == 0.0
    assert ctrl._host_state['192.168.1.150']['protocol_fail'] is False
    submit.assert_called_once()
    assert submit.call_args.args[1] is ctrl.mainloop
    submit.call_args.args[0].close()
