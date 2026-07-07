"""Strip detection for udi-poly-kasa."""
STRIP_PARENT_TYPES = ('SmartStrip', 'DeviceType.Strip')


def normalize_model(model):
    model = str(model or '').upper().strip()
    if not model:
        return ''
    if '(' in model:
        model = model.split('(', 1)[0]
    return model


def dev_has_strip_children(dev):
    """True when the live kasa device exposes at least one strip outlet child."""
    if dev is None:
        return False
    try:
        children = getattr(dev, 'children', None)
        if children is not None and len(children) > 0:
            return True
    except (TypeError, KeyError):
        pass
    sys_info = getattr(dev, 'sys_info', None)
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
    return not has_child_nodes
