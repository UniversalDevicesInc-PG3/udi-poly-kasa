"""Tapo cloud device roster (wap.tplinkcloud.com).

H500 hubs often report ``child_num > 0`` while LAN ``getChildDeviceList``
returns an empty list. Cloud ``getDeviceList`` still returns paired cameras
with ``deviceId`` and MAC — same flow as python-kasa
``devtools/cloud_tapo_device_list.py``.

Login uses a caller-supplied ``terminalUUID`` (the PG3 node server id) and
reuses the returned token. A new UUID on every login is what makes the
Tapo/Kasa app send a new-login or authorization notification.
"""

from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.request
from typing import Any

DEFAULT_CLOUD = 'https://wap.tplinkcloud.com'
CAMERA_TYPE = 'SMART.IPCAMERA'
HUB_TYPE = 'SMART.TAPOHUB'
# TP-Link cloud codes that mean the saved token cannot be reused.
_TOKEN_ERROR_CODES = frozenset({-20651, -20675})


class CloudTokenError(RuntimeError):
    """``getDeviceList`` rejected the token; the caller should log in once."""


def decode_cloud_alias(value: str) -> str:
    """Cloud aliases are often base64-encoded UTF-8."""
    if not value:
        return ''
    try:
        padded = value + '=' * (-len(value) % 4)
        return base64.b64decode(padded).decode()
    except Exception:
        return value


def _post(url: str, body: dict[str, Any], *, ctx: ssl.SSLContext) -> dict[str, Any]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _error_code(resp: dict[str, Any]) -> int | None:
    try:
        return int(resp.get('error_code'))
    except (TypeError, ValueError):
        return None


def _is_token_error(resp: dict[str, Any]) -> bool:
    if _error_code(resp) in _TOKEN_ERROR_CODES:
        return True
    return 'token' in str(resp.get('msg') or '').lower()


def cloud_login(
    email: str,
    password: str,
    *,
    terminal_uuid: str,
    cloud_url: str = DEFAULT_CLOUD,
    ctx: ssl.SSLContext | None = None,
) -> tuple[str, str]:
    """Return ``(token, api_base)`` after Tapo cloud login.

    ``terminal_uuid`` must be a stable client id. Generating a new one per
    call makes TP-Link treat each login as a new device.
    """
    terminal_uuid = str(terminal_uuid or '').strip()
    if not terminal_uuid:
        raise ValueError('terminal_uuid is required for Tapo cloud login')
    if ctx is None:
        ctx = ssl.create_default_context()
    login = _post(
        cloud_url,
        {
            'method': 'login',
            'params': {
                'cloudUserName': email,
                'cloudPassword': password,
                'appType': 'TP-Link_Tapo_Android',
                'terminalUUID': terminal_uuid,
            },
        },
        ctx=ctx,
    )
    if login.get('error_code') != 0:
        raise RuntimeError(f'Tapo cloud login failed: {login!r}')
    return login['result']['token'], cloud_url


def cloud_get_device_list(
    token: str,
    api_base: str,
    *,
    ctx: ssl.SSLContext | None = None,
) -> list[dict[str, Any]]:
    """Fetch the account device list from Tapo cloud."""
    if ctx is None:
        ctx = ssl.create_default_context()
    resp = _post(
        f'{api_base}?token={token}',
        {'method': 'getDeviceList'},
        ctx=ctx,
    )
    if resp.get('error_code') != 0:
        if _is_token_error(resp):
            raise CloudTokenError(f'Tapo cloud token rejected: {resp!r}')
        raise RuntimeError(f'Tapo cloud getDeviceList failed: {resp!r}')
    device_list = resp.get('result', {}).get('deviceList', [])
    if not isinstance(device_list, list):
        return []
    return device_list


def normalize_cloud_device(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        'alias': decode_cloud_alias(str(raw.get('alias', '') or '')),
        'device_model': raw.get('deviceModel', ''),
        'device_type': raw.get('deviceType', ''),
        'device_id': raw.get('deviceId', ''),
        'mac': raw.get('deviceMac', ''),
        'app_server_url': raw.get('appServerUrl', ''),
        'parent_device_id': raw.get('parentDeviceId', ''),
    }


def _normalize_devices(raw_devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [normalize_cloud_device(d) for d in raw_devices if isinstance(d, dict)]


def fetch_cloud_device_list(
    email: str,
    password: str,
    *,
    terminal_uuid: str,
    token: str | None = None,
    cloud_url: str = DEFAULT_CLOUD,
    insecure_tls: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    """Return ``(devices, token)``.

    When ``token`` is set, call ``getDeviceList`` only. Log in when there is
    no token or the token is rejected, then retry ``getDeviceList`` once.
    """
    ctx = ssl.create_default_context()
    if insecure_tls:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    saved = str(token or '').strip()
    try:
        if saved:
            try:
                raw_devices = cloud_get_device_list(saved, cloud_url, ctx=ctx)
                return _normalize_devices(raw_devices), saved
            except CloudTokenError:
                saved = ''
        saved, api_base = cloud_login(
            email,
            password,
            terminal_uuid=terminal_uuid,
            cloud_url=cloud_url,
            ctx=ctx,
        )
        raw_devices = cloud_get_device_list(saved, api_base, ctx=ctx)
    except urllib.error.URLError as ex:
        raise RuntimeError(f'Tapo cloud request failed: {ex}') from ex
    return _normalize_devices(raw_devices), saved


def fetch_cloud_camera_roster(
    email: str,
    password: str,
    *,
    terminal_uuid: str,
    token: str | None = None,
    cloud_url: str = DEFAULT_CLOUD,
    insecure_tls: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    """Return ``(SMART.IPCAMERA entries, token)`` from Tapo cloud."""
    devices, token = fetch_cloud_device_list(
        email,
        password,
        terminal_uuid=terminal_uuid,
        token=token,
        cloud_url=cloud_url,
        insecure_tls=insecure_tls,
    )
    cameras = [d for d in devices if d.get('device_type') == CAMERA_TYPE]
    return cameras, token
