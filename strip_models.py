"""Strip detection for udi-poly-kasa."""
from kasa_compat import KasaException

STRIP_PARENT_TYPES = ('SmartStrip', 'DeviceType.Strip')
STRIP_SOCKET_TYPES = ('SmartStripPlug', 'DeviceType.StripSocket')
PLUG_TYPES = ('SmartPlug', 'DeviceType.Plug')

# Multi-outlet power strips (not HS103-style single-outlet devices misreported as Strip).
MULTI_OUTLET_STRIP_MODELS = frozenset({
    'EP40',
    'EP40M',
    'HS107',
    'HS300',
    'KP200',
    'KP303',
    'KP400',
    'P210M',
    'P300',
    'P304M',
    'P306',
    'P316M',
    'TP25',
})

MULTI_OUTLET_STRIP_MODELS_WITH_EMETER = frozenset({
    'EP40',
    'HS300',
    'KP303',
})


def normalize_model(model):
    model = str(model or '').upper().strip()
    if not model:
        return ''
    if '(' in model:
        model = model.split('(', 1)[0]
    return model


def is_strip_child_address(address):
    """True for HS300-style outlet IoX addresses ``{parent_mac}01``..``06``."""
    address = str(address or '').lower()
    if len(address) != 14 or not address.isalnum():
        return False
    suffix = address[-2:]
    if not suffix.isdigit():
        return False
    return 1 <= int(suffix) <= 6


def is_multi_outlet_strip_model(model):
    return normalize_model(model) in MULTI_OUTLET_STRIP_MODELS


def dev_is_strip_parent(dev):
    """True when the live kasa device is a multi-outlet strip parent."""
    if dev is None:
        return False
    if str(getattr(dev, 'device_type', None)) == 'DeviceType.StripSocket':
        return False
    if dev_has_strip_children(dev):
        return True
    if is_multi_outlet_strip_model(getattr(dev, 'model', None)):
        return True
    return False


def dev_has_strip_children(dev):
    """True when the live kasa device exposes at least one strip outlet child."""
    if dev is None:
        return False
    try:
        children = getattr(dev, 'children', None)
        if children is not None and len(children) > 0:
            return True
    except (TypeError, KeyError, KasaException):
        pass
    try:
        sys_info = getattr(dev, 'sys_info', None)
    except KasaException:
        return False
    if isinstance(sys_info, dict):
        si_children = sys_info.get('children')
        if isinstance(si_children, list) and len(si_children) > 0:
            return True
    return False


def cfg_is_misclassified_strip(cfg, strip_types=STRIP_PARENT_TYPES, *, has_child_nodes=False):
    """Saved strip cfg with no outlet children is likely a misclassified plug."""
    if not isinstance(cfg, dict):
        return False
    if cfg.get('type') not in strip_types:
        return False
    if is_multi_outlet_strip_model(cfg.get('model')):
        return False
    return not has_child_nodes


def cfg_is_misclassified_plug(cfg, *, dev=None, dev_is_strip_parent=None):
    """Saved plug cfg that should be a multi-outlet strip parent."""
    if not isinstance(cfg, dict):
        return False
    if is_strip_child_address(cfg.get('address')):
        return False
    if cfg.get('type') not in PLUG_TYPES:
        return False
    if dev_is_strip_parent is not None and dev is not None and dev_is_strip_parent(dev):
        return True
    return is_multi_outlet_strip_model(cfg.get('model'))


def upgrade_misclassified_plug_cfg(cfg, *, dev=None, default_name=None):
    """Rewrite a misclassified SmartPlug cfg as a strip parent cfg."""
    if not isinstance(cfg, dict):
        return cfg
    strip_cfg = dict(cfg)
    strip_cfg['type'] = 'DeviceType.Strip'
    model = normalize_model(strip_cfg.get('model') or getattr(dev, 'model', None))
    if model:
        strip_cfg['model'] = model
    if dev is not None:
        alias = (getattr(dev, 'alias', None) or '').strip()
        if alias:
            strip_cfg['name'] = alias
        elif default_name:
            strip_cfg['name'] = default_name
        elif model:
            strip_cfg['name'] = f'SmartStrip {model}'
        try:
            strip_cfg['emeter'] = bool(dev.has_emeter)
        except Exception:
            pass
    else:
        if default_name:
            strip_cfg['name'] = default_name
        elif model:
            strip_cfg['name'] = f'SmartStrip {model}'
        if model in MULTI_OUTLET_STRIP_MODELS_WITH_EMETER:
            strip_cfg.setdefault('emeter', True)
    strip_cfg.pop('id', None)
    return strip_cfg


def is_auto_misclassified_strip_name(name, model=None):
    """True for plugin auto-generated ``SmartStrip {model}`` names (not user labels)."""
    name = str(name or '').strip()
    if not name.lower().startswith('smartstrip '):
        return False
    if not model:
        return True
    tail = name.split(None, 1)[1] if len(name.split(None, 1)) > 1 else ''
    if tail.lower().startswith('socket '):
        return False
    return normalize_model(tail) == normalize_model(model)


def strip_plug_nodedef_id(*, cfg=None, has_emeter=None):
    """IoX nodedef id for an HS300-style strip outlet (``SmartStripPlug_E`` / ``_N``)."""
    if isinstance(cfg, dict):
        existing = str(cfg.get('id') or '')
        if existing.startswith('SmartStripPlug_'):
            return existing
        if has_emeter is None:
            if 'emeter' in cfg:
                has_emeter = bool(cfg.get('emeter'))
            elif existing.endswith('_E'):
                has_emeter = True
            elif existing.endswith('_N'):
                has_emeter = False
    if has_emeter is None:
        has_emeter = False
    return f"SmartStripPlug_{'E' if has_emeter else 'N'}"


def cfg_is_misclassified_strip_socket(cfg):
    """Saved cfg for an HS300 outlet using strip-parent type or nodedef."""
    if not isinstance(cfg, dict):
        return False
    if not is_strip_child_address(cfg.get('address')):
        return False
    cfg_type = cfg.get('type')
    if cfg_type in STRIP_SOCKET_TYPES:
        node_id = str(cfg.get('id') or '')
        return node_id.startswith('SmartStrip_') and not node_id.startswith('SmartStripPlug_')
    if cfg_type in STRIP_PARENT_TYPES + PLUG_TYPES:
        return True
    return False
