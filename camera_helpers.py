"""Helpers for Tapo smartcam (Camera/Hub) device support."""

from __future__ import annotations

import base64

from strip_models import normalize_model

HUB_CHILD_CAMERA_TYPES = frozenset({'SmartCamera', 'DeviceType.Camera'})
TAPO_CLOUD_CAMERA_TYPE = 'SMART.IPCAMERA'

# Tapo solar/battery cameras that expose Battery only after a full update().
SOLAR_BATTERY_CAMERA_MODELS = frozenset({
    'C400',
    'C420',
    'C425',
    'C460',
    'C465',
    'C475',
    'C500',
    'C510',
    'C520',
    'C660',
    'C675',
    'C675D',
})


def module_of_type(dev, module_cls):
    """Return the first loaded smartcam module instance of module_cls."""
    modules = getattr(dev, 'modules', None)
    if not modules:
        return None
    try:
        values = modules.values()
    except Exception:
        return None
    for mod in values:
        if isinstance(mod, module_cls):
            return mod
    return None


def camera_model_has_battery(model):
    """True for known solar/battery Tapo models before update() completes."""
    return normalize_model(model) in SOLAR_BATTERY_CAMERA_MODELS


def dev_has_battery(dev):
    """True when the device exposes a smartcam Battery module."""
    if dev is None:
        return False
    try:
        from kasa.smartcam.modules import Battery
    except ImportError:
        return camera_model_has_battery(getattr(dev, 'model', None))
    if module_of_type(dev, Battery) is not None:
        return True
    return camera_model_has_battery(getattr(dev, 'model', None))


def motion_detection_enabled(dev):
    """Return motion-detection armed state, or None if unavailable."""
    if dev is None:
        return None
    try:
        from kasa.smartcam.modules import MotionDetection
    except ImportError:
        return None
    mod = module_of_type(dev, MotionDetection)
    if mod is None:
        return None
    try:
        return bool(mod.enabled)
    except Exception:
        return None


def _msg_push_info_from_last_update(dev):
    """Return chn1_msg_push_info from the last device update, if present."""
    last = getattr(dev, '_last_update', None) or {}
    cfg = last.get('getMsgPushConfig') or {}
    if not isinstance(cfg, dict):
        return None
    msg_push = cfg.get('msg_push') or cfg
    info = msg_push.get('chn1_msg_push_info')
    return info if isinstance(info, dict) else None


def camera_notifications_enabled(dev):
    """Return Tapo push-notification enabled state, or None if unavailable.

    This is msg_push.notification_enabled — app alerts only. Motion detection
    and recording continue when notifications are off.
    """
    if dev is None:
        return None
    try:
        from kasa.smartcam.modules import MsgPush
    except ImportError:
        MsgPush = None  # type: ignore[misc, assignment]
    if MsgPush is not None:
        mod = module_of_type(dev, MsgPush)
        if mod is not None:
            try:
                return bool(mod.enabled)
            except Exception:
                pass
    info = _msg_push_info_from_last_update(dev)
    if not info:
        return None
    return str(info.get('notification_enabled', 'off')).lower() == 'on'


async def set_camera_notifications_enabled(dev, enable):
    """Enable or disable Tapo push notifications on a camera device."""
    if dev is None:
        raise ValueError('device is required')
    try:
        from kasa.smartcam.modules import MsgPush
    except ImportError:
        MsgPush = None  # type: ignore[misc, assignment]
    if MsgPush is not None:
        mod = module_of_type(dev, MsgPush)
        if mod is not None:
            return await mod.set_enabled(bool(enable))
    params = {'notification_enabled': 'on' if enable else 'off'}
    helper = getattr(dev, '_query_setter_helper', None)
    if callable(helper):
        return await helper(
            'setMsgPushConfig',
            'msg_push',
            'chn1_msg_push_info',
            params,
        )
    protocol = getattr(dev, 'protocol', None)
    if protocol is None:
        raise RuntimeError('device does not support msg_push notification control')
    return await protocol.query({
        'setMsgPushConfig': {
            'msg_push': {'chn1_msg_push_info': params},
        }
    })


def battery_percent(dev):
    """Return battery percentage, or None if unavailable."""
    if dev is None:
        return None
    try:
        from kasa.smartcam.modules import Battery
    except ImportError:
        return None
    mod = module_of_type(dev, Battery)
    if mod is None:
        return None
    try:
        return int(mod.battery_percent)
    except Exception:
        return None


def is_hub_child_dev(dev):
    """True when python-kasa marks this device as a hub child."""
    if dev is None:
        return False
    if getattr(dev, '_is_hub_child', False):
        return True
    parent = getattr(dev, 'parent', None)
    return parent is not None


def is_hub_child_camera_cfg(cfg):
    """True when saved cfg represents a camera nested under a hub."""
    if not cfg:
        return False
    if cfg.get('type') not in HUB_CHILD_CAMERA_TYPES:
        return False
    return bool(cfg.get('hub_parent'))


def is_hub_deferred_camera_cfg(cfg):
    """True when a hub-child camera is adopted from LAN discover, not hub API."""
    return bool(cfg and cfg.get('hub_deferred'))


def camera_lan_host(cfg=None, dev=None, hub_host=None):
    """Return the camera's LAN IP when known (hub-child privacy control fallback)."""
    cfg = cfg or {}
    host = str(cfg.get('camera_host') or '').strip()
    if host:
        return host
    if is_hub_deferred_camera_cfg(cfg):
        saved = str(cfg.get('host') or '').strip()
        hub = str(hub_host or '').strip()
        if saved and saved != hub:
            return saved
    if dev is not None:
        info = getattr(dev, '_child_info_from_parent', None)
        if isinstance(info, dict):
            ipaddr = str(info.get('ipaddr') or '').strip()
            if ipaddr:
                return ipaddr
    return None


def hub_child_control_fallback_eligible(ex):
    """True when a hub-child write may succeed via direct LAN instead."""
    code = getattr(ex, 'error_code', None)
    if code is not None:
        name = getattr(code, 'name', None) or str(code)
        if name == 'PROTOCOL_FORMAT_ERROR':
            return True
    text = str(ex).upper()
    return 'PROTOCOL_FORMAT_ERROR' in text or '-40210' in text


def is_auto_generated_camera_name(name, model=None):
    """True for plugin fallbacks like ``Kasa C200`` or ``Camera 1``."""
    name = str(name or '').strip()
    if not name:
        return True
    if model and normalize_model(name) == normalize_model(model):
        return True
    lower = name.lower()
    if lower.startswith('camera '):
        return True
    if not lower.startswith('kasa '):
        return False
    if not model:
        return True
    tail = name.split(None, 1)[1] if len(name.split(None, 1)) > 1 else ''
    return normalize_model(tail) == normalize_model(model)


def camera_snapshot_alias(dev):
    """Return a user alias from a discover snapshot, not the model fallback."""
    alias = str(getattr(dev, 'alias', None) or '').strip()
    model = str(getattr(dev, 'model', None) or '').strip()
    if alias and not is_auto_generated_camera_name(alias, model):
        return alias
    return None


def normalize_mac_for_match(mac):
    """Normalize MAC/device id strings for identity comparisons."""
    if mac is None:
        return None
    text = str(mac).strip().lower()
    if not text:
        return None
    return text.replace(':', '').replace('-', '')


def decode_tapo_nickname(nickname):
    """Decode a base64 Tapo nickname from hub child list payloads."""
    if not nickname:
        return None
    try:
        decoded = base64.b64decode(nickname).decode()
    except Exception:
        return None
    decoded = str(decoded or '').strip()
    return decoded or None


def cloud_device_to_snapshot(entry):
    """Build a hub-deferred adoption snapshot from a normalized cloud row."""
    if not isinstance(entry, dict):
        return None
    model = str(entry.get('device_model') or '').strip() or None
    alias = str(entry.get('alias') or '').strip() or None
    if alias and is_auto_generated_camera_name(alias, model):
        alias = None
    mac = entry.get('mac')
    device_id = entry.get('device_id')
    if not mac and not device_id:
        return None
    return {
        'mac': mac,
        'host': None,
        'model': model,
        'alias': alias,
        'device_id': device_id,
        'battery': camera_model_has_battery(model),
        'cloud': True,
    }


def cloud_devices_to_snapshots(devices):
    """Convert normalized Tapo cloud camera rows to adoption snapshots."""
    snapshots = []
    for entry in devices or []:
        if str(entry.get('device_type') or '') != TAPO_CLOUD_CAMERA_TYPE:
            continue
        snap = cloud_device_to_snapshot(entry)
        if snap:
            snapshots.append(snap)
    return snapshots


def merge_camera_snapshots(existing, new):
    """Merge discovery/cloud snapshots, keeping LAN host when known."""
    merged = dict(existing or {})
    for key, value in (new or {}).items():
        if value in (None, ''):
            continue
        if key == 'host' and merged.get('host'):
            continue
        merged[key] = value
    return merged


def hub_child_list_alias(child_list, mac=None, device_id=None, model=None):
    """Return a Tapo alias from hub ``child_device_list`` entries."""
    mac_key = normalize_mac_for_match(mac)
    did_key = str(device_id or '').strip().lower() or None
    for info in child_list or []:
        if not isinstance(info, dict):
            continue
        entry_mac = normalize_mac_for_match(info.get('mac') or info.get('hw_id'))
        entry_did = str(info.get('device_id') or '').strip().lower()
        if mac_key and entry_mac and entry_mac == mac_key:
            pass
        elif did_key and entry_did and entry_did == did_key:
            pass
        else:
            continue
        alias = decode_tapo_nickname(info.get('nickname'))
        entry_model = info.get('model') or info.get('device_model') or model
        if alias and not is_auto_generated_camera_name(alias, entry_model):
            return alias
    return None


def hub_child_alias_from_hub_dev(hub_dev, mac=None, device_id=None, model=None):
    """Find a child camera alias from a hub device object."""
    if hub_dev is None:
        return None
    mac_key = normalize_mac_for_match(mac)
    did_key = str(device_id or '').strip().lower() or None
    for child in getattr(hub_dev, 'children', None) or []:
        child_mac = normalize_mac_for_match(getattr(child, 'mac', None))
        child_did = str(getattr(child, 'device_id', '') or '').strip().lower()
        if mac_key and child_mac == mac_key:
            pass
        elif did_key and child_did and child_did == did_key:
            pass
        else:
            continue
        alias = camera_snapshot_alias(child) or getattr(child, 'alias', None)
        if alias and not is_auto_generated_camera_name(alias, model):
            return str(alias).strip()
    last_update = getattr(hub_dev, '_last_update', None) or {}
    try_get = getattr(hub_dev, '_try_get_response', None)
    if not callable(try_get):
        return None
    for method in ('getChildDeviceList', 'get_child_device_list'):
        child_info = try_get(last_update, method, {}) or {}
        if not isinstance(child_info, dict):
            continue
        alias = hub_child_list_alias(
            child_info.get('child_device_list'),
            mac=mac,
            device_id=device_id,
            model=model,
        )
        if alias:
            return alias
    return None


def camera_nodedef_id(dev=None, cfg=None, has_battery=None):
    """Select SmartCamera_N vs SmartCamera_B nodedef id."""
    if has_battery is None:
        if dev is not None:
            has_battery = dev_has_battery(dev)
        elif cfg is not None:
            has_battery = bool(cfg.get('battery'))
            if not has_battery:
                has_battery = camera_model_has_battery(cfg.get('model'))
        else:
            has_battery = False
    return 'SmartCamera_B' if has_battery else 'SmartCamera_N'
