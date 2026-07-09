"""Tests for hub-child camera name resolution."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from camera_helpers import (
    camera_snapshot_alias,
    hub_child_list_alias,
    is_auto_generated_camera_name,
)
from conftest import make_controller_stub
from nodes.Controller import Controller


class _CameraDev:
    device_type = 'DeviceType.Camera'
    mac = '8C:90:2D:29:90:21'
    host = '192.168.1.48'
    model = 'C200'
    alias = None
    device_id = 'F449FD6BD80002138128C5D1EA39518A'


def _controller():
    ctrl = make_controller_stub()
    ctrl.Data = {}
    for name in (
        '_dev_model',
        '_dev_attr',
        '_dev_default_name',
        'smac',
        'get_device_cfg',
        'save_cfg',
        '_find_manual_device_name',
        '_find_hub_child_alias',
        '_apply_node_name',
        '_sync_stale_camera_name',
        '_fix_stale_auto_generated_camera_names',
    ):
        setattr(ctrl, name, getattr(Controller, name).__get__(ctrl, Controller))
    ctrl.poly.getNodes = MagicMock(return_value=[])
    ctrl.poly.getNodeNameFromDb = MagicMock(return_value=None)
    ctrl.poly.getNode = MagicMock(return_value=None)
    ctrl.poly.renameNode = MagicMock()
    ctrl.manual_devices = []
    ctrl._manual_host_names = {}
    ctrl._manual_host_identity = {}
    ctrl._camera_alias_cache = {}
    return ctrl


def test_is_auto_generated_camera_name():
    assert is_auto_generated_camera_name('Kasa C200', 'C200')
    assert is_auto_generated_camera_name('Camera 1', 'C200')
    assert not is_auto_generated_camera_name('CamInMasterBedroom', 'C200')


def test_camera_snapshot_alias_ignores_model_fallback():
    dev = _CameraDev()
    dev.alias = 'C200'
    assert camera_snapshot_alias(dev) is None
    dev.alias = 'CamInMasterBedroom'
    assert camera_snapshot_alias(dev) == 'CamInMasterBedroom'


def test_preferred_hub_camera_name_uses_saved_cfg():
    ctrl = _controller()
    addr = '8c902d299021'
    ctrl.Data[addr] = json.dumps({
        'type': 'DeviceType.Camera',
        'name': 'CamInMasterBedroom',
        'mac': '8C:90:2D:29:90:21',
        'model': 'C200',
        'address': 'ccbabd1606d803',
        'hub_parent': 'ccbabd1606d8',
    })
    cfg = {
        'type': 'DeviceType.Camera',
        'name': 'Kasa C200',
        'mac': '8C:90:2D:29:90:21',
        'model': 'C200',
        'address': addr,
        'hub_parent': 'ccbabd1606d8',
    }
    name = ctrl._preferred_hub_camera_name(cfg=cfg, address=addr, model='C200')
    assert name == 'CamInMasterBedroom'


def test_preferred_hub_camera_name_uses_snapshot():
    ctrl = _controller()
    dev = _CameraDev()
    snap = {'alias': 'CamInMasterBedroom', 'model': 'C200'}
    name = ctrl._preferred_hub_camera_name(
        dev=dev,
        snapshot=snap,
        model='C200',
    )
    assert name == 'CamInMasterBedroom'


def test_preferred_hub_camera_name_uses_iox_db():
    ctrl = _controller()
    addr = '8c902d299021'
    ctrl.poly.getNodeNameFromDb = MagicMock(return_value='CamInMasterBedroom')
    cfg = {
        'type': 'DeviceType.Camera',
        'name': 'Kasa C200',
        'mac': '8C:90:2D:29:90:21',
        'model': 'C200',
        'address': addr,
        'hub_parent': 'ccbabd1606d8',
    }
    name = ctrl._preferred_hub_camera_name(cfg=cfg, address=addr, model='C200')
    assert name == 'CamInMasterBedroom'


def test_hub_child_list_alias_decodes_nickname():
    alias = hub_child_list_alias(
        [{
            'mac': 'ACA7F1DAD82B',
            'device_id': '8021abc',
            'nickname': 'Q2FtT3V0QmFja1NvdXRo',
            'model': 'C675D',
        }],
        mac='aca7f1dad82b',
        model='C675D',
    )
    assert alias == 'CamOutBackSouth'


def test_preferred_hub_camera_name_uses_manual_device_row():
    ctrl = _controller()
    ctrl.manual_devices = [{
        'address': '192.168.1.103',
        'name': 'CamOutBackSouth',
    }]
    cfg = {
        'type': 'DeviceType.Camera',
        'name': 'Kasa C675D',
        'host': '192.168.1.103',
        'mac': 'ACA7:F1:DA:D8:2B',
        'model': 'C675D',
        'address': 'aca7f1dad82b',
        'hub_parent': 'ccbabd1606d8',
    }
    name = ctrl._preferred_hub_camera_name(cfg=cfg, model='C675D')
    assert name == 'CamOutBackSouth'


def test_preferred_hub_camera_name_uses_hub_child_list():
    ctrl = _controller()
    hub_dev = MagicMock()
    hub_dev.children = []
    hub_dev._try_get_response = MagicMock(return_value={
        'child_device_list': [{
            'mac': 'ACA7F1DAD82B',
            'nickname': 'Q2FtT3V0QmFja1NvdXRo',
            'model': 'C675D',
        }],
    })
    hub_node = MagicMock(dev=hub_dev)
    ctrl.poly.getNode = MagicMock(return_value=hub_node)
    cfg = {
        'type': 'DeviceType.Camera',
        'name': 'Kasa C675D',
        'host': '192.168.1.103',
        'mac': 'ACA7F1DAD82B',
        'model': 'C675D',
        'address': 'aca7f1dad82b',
        'hub_parent': 'ccbabd1606d8',
    }
    name = ctrl._preferred_hub_camera_name(cfg=cfg, model='C675D')
    assert name == 'CamOutBackSouth'


def test_sync_stale_camera_name_renames_auto_generated_node():
    ctrl = _controller()
    node = MagicMock()
    node.id = 'SmartCamera_N'
    node.address = 'aca7f1dad82b'
    node.name = 'Kasa C675D'
    node.cfg = {
        'type': 'DeviceType.Camera',
        'name': 'Kasa C675D',
        'host': '192.168.1.103',
        'mac': 'ACA7F1DAD82B',
        'model': 'C675D',
        'hub_parent': 'ccbabd1606d8',
    }
    node.dev = None
    ctrl.manual_devices = [{'address': '192.168.1.103', 'name': 'CamOutBackSouth'}]
    assert ctrl._sync_stale_camera_name(node) is True
    ctrl.poly.renameNode.assert_called_once_with('aca7f1dad82b', 'CamOutBackSouth')
    assert node.name == 'CamOutBackSouth'


def test_dedupe_camera_cfg_merges_duplicate_keys():
    ctrl = _controller()
    ctrl.Data = {
        'aca7f1dad82b': json.dumps({
            'type': 'DeviceType.Camera',
            'name': 'Kasa C675D',
            'address': 'aca7f1dad82b',
            'mac': 'aca7f1dad82b',
            'hub_parent': 'ccbabd1606d8',
            'hub_deferred': True,
            'model': 'C675D',
        }),
        '1E9DA4F1C67F2DA829A6AFAB5171D8F9': json.dumps({
            'type': 'DeviceType.Camera',
            'name': 'CamOutBackSouth',
            'host': '192.168.1.103',
            'mac': 'ACA7F1DAD82B',
            'model': 'C675D',
            'address': 'aca7f1dad82b',
            'device_id': '1E9DA4F1C67F2DA829A6AFAB5171D8F9',
            'hub_parent': 'ccbabd1606d8',
            'hub_deferred': True,
            'camera_host': '192.168.1.103',
        }),
    }
    for name in (
        '_is_camera_cfg',
        '_cfg_entries_for_camera',
        '_pick_camera_cfg_name',
        '_preferred_camera_device_id',
        '_merge_camera_cfg_dict',
        '_purge_stale_camera_cfg_keys',
        '_prune_stale_camera_alias_keys',
        '_dedupe_camera_cfg',
        '_cfg_storage_key',
        '_remember_camera_alias',
        '_alias_from_cache',
        '_persist_camera_alias_cache',
        '_delete_cfg_key',
        '_norm_camera_mac',
        '_norm_camera_device_id',
    ):
        setattr(ctrl, name, getattr(Controller, name).__get__(ctrl, Controller))
    ctrl._dedupe_camera_cfg()
    assert 'aca7f1dad82b' not in ctrl.Data
    merged = json.loads(ctrl.Data['1E9DA4F1C67F2DA829A6AFAB5171D8F9'])
    assert merged['name'] == 'CamOutBackSouth'
    assert merged['camera_host'] == '192.168.1.103'


def test_dedupe_prefers_cloud_device_id_and_drops_stale_short_id():
    """CamOutBackSouth-style: short legacy device_id + newer cloud device_id."""
    ctrl = _controller()
    stale = '1E9DA4F1C67F2DA829A6AFAB5171D8F9'
    cloud = '80212A86CDD1DC98AEA18DD3E6C1D8772552D7AD'
    ctrl.Data = {
        stale: json.dumps({
            'type': 'DeviceType.Camera',
            'name': 'Kasa C675D',
            'address': 'aca7f1dad82b',
            'mac': 'aca7f1dad82b',
            'hub_parent': 'ccbabd1606d8',
            'hub_deferred': True,
            'battery': True,
            'model': 'C675D',
            'host': '192.168.1.103',
            'device_id': stale,
            'camera_host': '192.168.1.103',
        }),
        cloud: json.dumps({
            'type': 'DeviceType.Camera',
            'mac': 'ACA7F1DAD82B',
            'model': 'C675D',
            'address': 'aca7f1dad82b',
            'hub_parent': 'ccbabd1606d8',
            'hub_deferred': True,
            'device_id': cloud,
            'battery': True,
            'name': 'CamOutBackSouth',
            'id': 'SmartCamera_B',
            'host': '192.168.1.103',
            'camera_host': '192.168.1.103',
        }),
        '_camera_aliases': json.dumps({
            'aca7f1dad82b': 'CamOutBackSouth',
            stale: 'CamOutBackSouth',
            cloud: 'CamOutBackSouth',
        }),
    }
    for name in (
        '_is_camera_cfg',
        '_cfg_entries_for_camera',
        '_pick_camera_cfg_name',
        '_preferred_camera_device_id',
        '_merge_camera_cfg_dict',
        '_purge_stale_camera_cfg_keys',
        '_prune_stale_camera_alias_keys',
        '_dedupe_camera_cfg',
        '_cfg_storage_key',
        '_remember_camera_alias',
        '_alias_from_cache',
        '_persist_camera_alias_cache',
        '_load_camera_alias_cache',
        '_delete_cfg_key',
        '_norm_camera_mac',
        '_norm_camera_device_id',
        '_preferred_hub_camera_name',
        'get_device_cfg',
        'smac',
    ):
        setattr(ctrl, name, getattr(Controller, name).__get__(ctrl, Controller))
    ctrl._load_camera_alias_cache()
    ctrl._dedupe_camera_cfg()
    assert stale not in ctrl.Data
    assert cloud in ctrl.Data
    merged = json.loads(ctrl.Data[cloud])
    assert merged['device_id'] == cloud
    assert merged['name'] == 'CamOutBackSouth'
    aliases = json.loads(ctrl.Data['_camera_aliases'])
    assert stale not in aliases
    assert aliases.get('aca7f1dad82b') == 'CamOutBackSouth'
    assert aliases.get(cloud) == 'CamOutBackSouth'


def test_save_cfg_does_not_resurrect_stale_short_device_id():
    ctrl = _controller()
    stale = '1E9DA4F1C67F2DA829A6AFAB5171D8F9'
    cloud = '80212A86CDD1DC98AEA18DD3E6C1D8772552D7AD'
    ctrl.Data = {
        cloud: json.dumps({
            'type': 'DeviceType.Camera',
            'name': 'CamOutBackSouth',
            'host': '192.168.1.103',
            'mac': 'ACA7F1DAD82B',
            'model': 'C675D',
            'address': 'aca7f1dad82b',
            'device_id': cloud,
            'hub_parent': 'ccbabd1606d8',
            'hub_deferred': True,
            'camera_host': '192.168.1.103',
        }),
    }
    for name in (
        'save_cfg',
        '_is_camera_cfg',
        '_cfg_entries_for_camera',
        '_merge_camera_cfg_dict',
        '_preferred_camera_device_id',
        '_purge_stale_camera_cfg_keys',
        '_prune_stale_camera_alias_keys',
        '_cfg_storage_key',
        '_remember_camera_alias',
        '_alias_from_cache',
        '_persist_camera_alias_cache',
        '_find_saved_camera_name',
        '_preferred_hub_camera_name',
        '_delete_cfg_key',
        '_norm_camera_mac',
        '_norm_camera_device_id',
        'get_device_cfg',
        'smac',
    ):
        setattr(ctrl, name, getattr(Controller, name).__get__(ctrl, Controller))
    # Incoming save still carries the old short device_id (discover/offline path).
    ctrl.save_cfg({
        'type': 'DeviceType.Camera',
        'name': 'Kasa C675D',
        'address': 'aca7f1dad82b',
        'mac': 'aca7f1dad82b',
        'hub_parent': 'ccbabd1606d8',
        'hub_deferred': True,
        'model': 'C675D',
        'host': '192.168.1.103',
        'device_id': stale,
        'camera_host': '192.168.1.103',
    })
    assert stale not in ctrl.Data
    saved = json.loads(ctrl.Data[cloud])
    assert saved['device_id'] == cloud
    assert saved['name'] == 'CamOutBackSouth'


def test_save_cfg_keeps_better_name_over_generic_duplicate():
    ctrl = _controller()
    ctrl.Data = {
        '1E9DA4F1C67F2DA829A6AFAB5171D8F9': json.dumps({
            'type': 'DeviceType.Camera',
            'name': 'CamOutBackSouth',
            'host': '192.168.1.103',
            'mac': 'ACA7F1DAD82B',
            'model': 'C675D',
            'address': 'aca7f1dad82b',
            'device_id': '1E9DA4F1C67F2DA829A6AFAB5171D8F9',
            'hub_parent': 'ccbabd1606d8',
            'hub_deferred': True,
            'camera_host': '192.168.1.103',
        }),
    }
    for name in (
        'save_cfg',
        '_is_camera_cfg',
        '_cfg_entries_for_camera',
        '_merge_camera_cfg_dict',
        '_preferred_camera_device_id',
        '_purge_stale_camera_cfg_keys',
        '_prune_stale_camera_alias_keys',
        '_cfg_storage_key',
        '_remember_camera_alias',
        '_alias_from_cache',
        '_persist_camera_alias_cache',
        '_find_saved_camera_name',
        '_preferred_hub_camera_name',
        '_delete_cfg_key',
        '_norm_camera_mac',
        '_norm_camera_device_id',
        'get_device_cfg',
        'smac',
    ):
        setattr(ctrl, name, getattr(Controller, name).__get__(ctrl, Controller))
    ctrl.save_cfg({
        'type': 'DeviceType.Camera',
        'name': 'Kasa C675D',
        'address': 'aca7f1dad82b',
        'mac': 'aca7f1dad82b',
        'hub_parent': 'ccbabd1606d8',
        'hub_deferred': True,
        'model': 'C675D',
        'host': '',
    })
    saved = json.loads(ctrl.Data['1E9DA4F1C67F2DA829A6AFAB5171D8F9'])
    assert saved['name'] == 'CamOutBackSouth'
    assert 'aca7f1dad82b' not in ctrl.Data


def test_persisted_camera_aliases_survive_restart():
    ctrl = _controller()
    for name in (
        '_persist_camera_alias_cache',
        '_load_camera_alias_cache',
        '_upgrade_generic_camera_cfg_names',
        '_preferred_hub_camera_name',
    ):
        setattr(ctrl, name, getattr(Controller, name).__get__(ctrl, Controller))
    ctrl._remember_camera_alias(
        mac='ACA7F1DAD82B',
        device_id='1E9DA4F1C67F2DA829A6AFAB5171D8F9',
        alias='CamOutBackSouth',
        model='C675D',
    )
    assert '_camera_aliases' in ctrl.Data

    restarted = _controller()
    restarted.Data = dict(ctrl.Data)
    for name in (
        '_load_camera_alias_cache',
        '_upgrade_generic_camera_cfg_names',
        '_preferred_hub_camera_name',
        '_is_camera_cfg',
        'get_device_cfg',
        'save_cfg',
        '_cfg_storage_key',
        '_cfg_entries_for_camera',
        '_merge_camera_cfg_dict',
        '_purge_stale_camera_cfg_keys',
        '_remember_camera_alias',
        '_alias_from_cache',
        '_find_manual_device_name',
        '_find_hub_child_alias',
        '_find_saved_camera_name',
        'smac',
        '_dev_model',
        '_dev_attr',
        '_dev_default_name',
    ):
        setattr(restarted, name, getattr(Controller, name).__get__(restarted, Controller))
    restarted.Data['1E9DA4F1C67F2DA829A6AFAB5171D8F9'] = json.dumps({
        'type': 'DeviceType.Camera',
        'name': 'Kasa C675D',
        'address': 'aca7f1dad82b',
        'mac': 'aca7f1dad82b',
        'device_id': '1E9DA4F1C67F2DA829A6AFAB5171D8F9',
        'hub_parent': 'ccbabd1606d8',
        'hub_deferred': True,
        'camera_host': '192.168.1.103',
        'model': 'C675D',
    })
    restarted._load_camera_alias_cache()
    restarted._upgrade_generic_camera_cfg_names()
    saved = json.loads(restarted.Data['1E9DA4F1C67F2DA829A6AFAB5171D8F9'])
    assert saved['name'] == 'CamOutBackSouth'


def test_handler_data_preserves_alias_cache_on_stale_reload():
    ctrl = _controller()
    for name in (
        '_persist_camera_alias_cache',
        '_load_camera_alias_cache',
        '_snapshot_camera_cfg_names',
        '_restore_camera_cfg_names_after_load',
        '_merge_camera_alias_cache',
        '_dedupe_camera_cfg',
        '_seed_camera_aliases_from_cfg',
        '_seed_camera_aliases_from_manual_devices',
        '_upgrade_generic_camera_cfg_names',
        '_fix_stale_auto_generated_camera_names',
        '_is_camera_cfg',
        'get_device_cfg',
        'save_cfg',
        '_remember_camera_alias',
        '_alias_from_cache',
        '_preferred_hub_camera_name',
        '_norm_camera_mac',
        '_norm_camera_device_id',
        'smac',
        'handler_data',
    ):
        setattr(ctrl, name, getattr(Controller, name).__get__(ctrl, Controller))
    ctrl.Data = {
        '_camera_aliases': json.dumps({'aca7f1dad82b': 'CamOutBackSouth'}),
        '1E9DA4F1C67F2DA829A6AFAB5171D8F9': json.dumps({
            'type': 'DeviceType.Camera',
            'name': 'CamOutBackSouth',
            'address': 'aca7f1dad82b',
            'mac': 'aca7f1dad82b',
            'device_id': '1E9DA4F1C67F2DA829A6AFAB5171D8F9',
            'hub_parent': 'ccbabd1606d8',
            'hub_deferred': True,
            'camera_host': '192.168.1.103',
            'model': 'C675D',
        }),
    }
    ctrl._load_camera_alias_cache()
    stale_reload = {
        '_camera_aliases': json.dumps({
            '782051cd4138': 'CamOutFrontEntry',
        }),
        '1E9DA4F1C67F2DA829A6AFAB5171D8F9': json.dumps({
            'type': 'DeviceType.Camera',
            'name': 'Kasa C675D',
            'address': 'aca7f1dad82b',
            'mac': 'aca7f1dad82b',
            'device_id': '1E9DA4F1C67F2DA829A6AFAB5171D8F9',
            'hub_parent': 'ccbabd1606d8',
            'hub_deferred': True,
            'camera_host': '192.168.1.103',
            'model': 'C675D',
        }),
    }

    class _DataStub:
        def __init__(self, store):
            self._store = dict(store)

        def load(self, new_data):
            self._store = dict(new_data)

        def get(self, key, default=None):
            return self._store.get(key, default)

        def delete(self, key):
            self._store.pop(key, None)

        def __setitem__(self, key, value):
            self._store[key] = value

        def __delitem__(self, key):
            del self._store[key]

        def __iter__(self):
            return iter(self._store)

    ctrl.Data = _DataStub(ctrl.Data)
    ctrl._clear_stale_hub_deferred_auth_notices = MagicMock()
    ctrl._seed_hub_child_identities = MagicMock()
    ctrl._find_manual_device_name = MagicMock(return_value=None)
    ctrl._find_hub_child_alias = MagicMock(return_value=None)
    ctrl._find_saved_camera_name = MagicMock(return_value=None)
    ctrl.manual_devices = []
    ctrl.handler_data(stale_reload)

    aliases = json.loads(ctrl.Data.get('_camera_aliases'))
    assert aliases['aca7f1dad82b'] == 'CamOutBackSouth'
    saved = json.loads(ctrl.Data.get('1E9DA4F1C67F2DA829A6AFAB5171D8F9'))
    assert saved['name'] == 'CamOutBackSouth'


def test_manual_device_row_seeds_camera_alias():
    ctrl = _controller()
    for name in (
        '_seed_camera_aliases_from_manual_devices',
        '_remember_camera_alias',
        '_persist_camera_alias_cache',
        '_alias_from_cache',
        '_manual_device_host',
        '_manual_device_name_from_row',
        '_is_camera_cfg',
        'get_device_cfg',
        'smac',
        '_norm_camera_mac',
        '_norm_camera_device_id',
    ):
        setattr(ctrl, name, getattr(Controller, name).__get__(ctrl, Controller))
    ctrl.manual_devices = [{'address': '192.168.1.103', 'name': 'CamOutBackSouth'}]
    ctrl.Data = {
        '1E9DA4F1C67F2DA829A6AFAB5171D8F9': json.dumps({
            'type': 'DeviceType.Camera',
            'name': 'Kasa C675D',
            'address': 'aca7f1dad82b',
            'mac': 'aca7f1dad82b',
            'device_id': '1E9DA4F1C67F2DA829A6AFAB5171D8F9',
            'host': '192.168.1.103',
            'camera_host': '192.168.1.103',
            'hub_parent': 'ccbabd1606d8',
            'hub_deferred': True,
            'model': 'C675D',
        }),
    }
    ctrl._seed_camera_aliases_from_manual_devices()
    assert ctrl._alias_from_cache(mac='aca7f1dad82b') == 'CamOutBackSouth'
