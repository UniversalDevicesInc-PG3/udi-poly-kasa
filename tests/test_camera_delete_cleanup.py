"""Deleting a camera must clear all related customdata."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from conftest import make_controller_stub
from nodes.Controller import Controller
from nodes.SmartDeviceNode import SmartDeviceNode


def _bind(ctrl, *names):
    for name in names:
        setattr(ctrl, name, getattr(Controller, name).__get__(ctrl, Controller))


def _camera_controller():
    ctrl = make_controller_stub()
    ctrl.poly.Notices = MagicMock()
    ctrl.poly.Notices.delete = MagicMock()
    ctrl._camera_alias_cache = {}
    ctrl._hub_child_identities = set()
    ctrl.nodes_by_mac = {}
    _bind(
        ctrl,
        'smac',
        'get_device_cfg',
        'cfg_to_dict',
        '_is_camera_cfg',
        '_cfg_entries_for_camera',
        '_cfg_storage_key',
        '_delete_cfg_key',
        '_norm_camera_mac',
        '_norm_camera_device_id',
        '_persist_camera_alias_cache',
        '_forget_camera_alias_tokens',
        '_clear_host_notice',
        'delete_cfg',
        'on_node_deleted',
        '_forget_node_identity',
        'unregister_hub_child_identity',
        'register_hub_child_identity',
        '_hub_child_identity_tokens',
        '_node_identity_key',
    )
    return ctrl


def test_delete_cfg_removes_sibling_camera_keys_and_aliases():
    ctrl = _camera_controller()
    stale = '1E9DA4F1C67F2DA829A6AFAB5171D8F9'
    cloud = '80212A86CDD1DC98AEA18DD3E6C1D8772552D7AD'
    ctrl.Data = {
        stale: json.dumps({
            'type': 'DeviceType.Camera',
            'name': 'Kasa C675D',
            'address': 'aca7f1dad82b',
            'mac': 'aca7f1dad82b',
            'device_id': stale,
            'hub_parent': 'ccbabd1606d8',
            'hub_deferred': True,
            'host': '192.168.1.103',
            'camera_host': '192.168.1.103',
            'model': 'C675D',
        }),
        cloud: json.dumps({
            'type': 'DeviceType.Camera',
            'name': 'CamOutBackSouth',
            'address': 'aca7f1dad82b',
            'mac': 'ACA7F1DAD82B',
            'device_id': cloud,
            'hub_parent': 'ccbabd1606d8',
            'hub_deferred': True,
            'host': '192.168.1.103',
            'camera_host': '192.168.1.103',
            'model': 'C675D',
        }),
        '_camera_aliases': json.dumps({
            'aca7f1dad82b': 'CamOutBackSouth',
            stale: 'CamOutBackSouth',
            cloud: 'CamOutBackSouth',
            'othercam': 'KeepMe',
        }),
    }
    ctrl._camera_alias_cache = json.loads(ctrl.Data['_camera_aliases'])
    ctrl.register_hub_child_identity(
        cfg={'mac': 'ACA7F1DAD82B', 'device_id': cloud}
    )

    ctrl.delete_cfg(json.loads(ctrl.Data[cloud]))

    assert stale not in ctrl.Data
    assert cloud not in ctrl.Data
    assert 'aca7f1dad82b' not in ctrl.Data
    aliases = json.loads(ctrl.Data['_camera_aliases'])
    assert 'aca7f1dad82b' not in aliases
    assert stale not in aliases
    assert cloud not in aliases
    assert aliases['othercam'] == 'KeepMe'
    assert ctrl.smac(cloud) not in ctrl._hub_child_identities
    ctrl.poly.Notices.delete.assert_any_call('dev_192_168_1_103')


def test_handler_delete_clears_customdata_via_controller():
    ctrl = _camera_controller()
    cloud = '80212A86CDD1DC98AEA18DD3E6C1D8772552D7AD'
    cfg = {
        'type': 'DeviceType.Camera',
        'name': 'CamOutBackSouth',
        'address': 'aca7f1dad82b',
        'mac': 'ACA7F1DAD82B',
        'device_id': cloud,
        'hub_parent': 'ccbabd1606d8',
        'hub_deferred': True,
        'host': '192.168.1.103',
        'camera_host': '192.168.1.103',
        'model': 'C675D',
        'id': 'SmartCamera_B',
    }
    ctrl.Data = {
        cloud: json.dumps(cfg),
        '_camera_aliases': json.dumps({
            'aca7f1dad82b': 'CamOutBackSouth',
            cloud: 'CamOutBackSouth',
        }),
    }
    ctrl._camera_alias_cache = json.loads(ctrl.Data['_camera_aliases'])
    node = SimpleNamespace(
        pfx='Tapo_H500:CamOutBackSouth:',
        address='aca7f1dad82b',
        name='CamOutBackSouth',
        host='192.168.1.103',
        cfg=cfg,
        controller=ctrl,
        id='SmartCamera_B',
    )
    SmartDeviceNode.handler_delete(node)
    assert cloud not in ctrl.Data
    aliases = json.loads(ctrl.Data.get('_camera_aliases', '{}'))
    assert 'aca7f1dad82b' not in aliases
    assert cloud not in aliases
