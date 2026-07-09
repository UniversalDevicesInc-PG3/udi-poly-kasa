"""Tapo cloud device roster (wap.tplinkcloud.com).

H500 hubs often report ``child_num > 0`` while LAN ``getChildDeviceList``
returns an empty list. Cloud ``getDeviceList`` still returns paired cameras
with ``deviceId`` and MAC — same flow as python-kasa
``devtools/cloud_tapo_device_list.py``.
"""

from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.request
import uuid
from typing import Any

DEFAULT_CLOUD = 'https://wap.tplinkcloud.com'
CAMERA_TYPE = 'SMART.IPCAMERA'
HUB_TYPE = 'SMART.TAPOHUB'


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


def cloud_login(
    email: str,
    password: str,
    *,
    cloud_url: str = DEFAULT_CLOUD,
    ctx: ssl.SSLContext | None = None,
) -> tuple[str, str]:
    """Return ``(token, api_base)`` after Tapo cloud login."""
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
                'terminalUUID': str(uuid.uuid4()),
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


def fetch_cloud_device_list(
    email: str,
    password: str,
    *,
    cloud_url: str = DEFAULT_CLOUD,
    insecure_tls: bool = False,
) -> list[dict[str, Any]]:
    """Login and return normalized cloud devices (hubs, cameras, etc.)."""
    ctx = ssl.create_default_context()
    if insecure_tls:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        token, api_base = cloud_login(
            email, password, cloud_url=cloud_url, ctx=ctx
        )
        raw_devices = cloud_get_device_list(token, api_base, ctx=ctx)
    except urllib.error.URLError as ex:
        raise RuntimeError(f'Tapo cloud request failed: {ex}') from ex
    return [normalize_cloud_device(d) for d in raw_devices if isinstance(d, dict)]


def fetch_cloud_camera_roster(
    email: str,
    password: str,
    *,
    cloud_url: str = DEFAULT_CLOUD,
    insecure_tls: bool = False,
) -> list[dict[str, Any]]:
    """Return normalized SMART.IPCAMERA entries from Tapo cloud."""
    devices = fetch_cloud_device_list(
        email,
        password,
        cloud_url=cloud_url,
        insecure_tls=insecure_tls,
    )
    return [d for d in devices if d.get('device_type') == CAMERA_TYPE]
