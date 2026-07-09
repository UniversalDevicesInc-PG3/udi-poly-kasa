#
# TP Link Kasa / Tapo Smart Hub Node (e.g. H500)
#
import asyncio

from udi_interface import LOGGER
from camera_helpers import camera_model_has_battery
from node_funcs import get_valid_node_address, get_valid_node_name
from nodes import SmartDeviceNode


class SmartHubNode(SmartDeviceNode):

    def __init__(self, controller, address, name, dev=None, cfg=None):
        self.ready = False
        self.name = name
        self.drivers = [
            {'driver': 'GV0', 'value': 0, 'uom': 2, 'name': 'Connected'},
            {'driver': 'GV6', 'value': 1, 'uom': 2, 'name': 'Poll Device'},
        ]
        self.address = address
        self.connected = None
        if dev is not None:
            self.host = dev.host
        else:
            self.host = cfg['host']
        self.id = 'SmartHub_N'
        self.debug_level = 0
        self.pfx = f"{self.name}:"
        self.child_nodes = []
        LOGGER.debug(
            f'{self.pfx} controller={controller} address={address} '
            f'name={name} host={self.host}'
        )
        controller.poly.subscribe(controller.poly.ADDNODEDONE, self.add_node_done)
        super().__init__(controller, address, address, name, dev, cfg)
        controller.poly.subscribe(controller.poly.START, self.handler_start, address)

    def handler_start(self):
        LOGGER.debug(f'{self.pfx} enter')
        super(SmartHubNode, self).handler_start()
        self.ready = True
        LOGGER.debug(f'{self.pfx} exit')

    def _quick_probe_port(self):
        return 443

    async def update_a(self):
        return await self.controller.hub_node_update_a(self)

    async def update_device_a(self):
        return await super().update_a()

    async def _shortPoll_a(self):
        if not self.ready:
            LOGGER.warning(f'{self.pfx} Not ready, skipping')
            return
        if self.controller.host_should_skip(self.host):
            LOGGER.debug(
                f'{self.pfx} skipping shortPoll; hub {self.host} circuit-broken'
            )
            return
        await super()._shortPoll_a()

    def add_node_done(self, data):
        if data['address'] != self.address:
            return
        LOGGER.debug(f'{self.pfx} add_node_done data={data}')
        if self.controller.startup_in_progress:
            LOGGER.debug(f'{self.pfx} add_node_done deferred during startup')
            return
        if self.is_connected():
            self.update()
            self.add_children()

    def reconnected(self):
        LOGGER.debug(f'{self.pfx} enter')

        async def _hub_reconnected_a():
            try:
                await self.update_a()
                if self.dev is not None:
                    self.controller._remember_hub_child_aliases(self.dev)
                self.controller._schedule_tapo_cloud_roster_refresh(force=True)
                self.add_children()
                self.controller._try_adopt_deferred_hub_cameras()
                self.controller._upgrade_generic_camera_cfg_names()
                self.controller._fix_stale_auto_generated_camera_names()
            except Exception:
                LOGGER.error('%s hub reconnected follow-up failed', self.pfx, exc_info=True)
            LOGGER.debug(f'{self.pfx} exit')

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.controller.mainloop:
            asyncio.create_task(_hub_reconnected_a())
        else:
            asyncio.run_coroutine_threadsafe(
                _hub_reconnected_a(),
                self.controller.mainloop,
            )

    async def adopt_deferred_cameras_a(self, snapshots):
        """Discover LAN cameras and return add_node kwargs for the main thread."""
        if self.dev is None or not self._is_hub_parent_device():
            return []
        native = getattr(self.dev, 'children', None) or []
        if native:
            return []
        if not snapshots:
            return []
        LOGGER.warning(
            '%s hub API reports 0 paired cameras; adopting %s LAN-discovered camera(s)',
            self.pfx,
            len(snapshots),
        )
        pending_adds = []
        for snap in snapshots:
            mac = snap.get('mac')
            naddress = get_valid_node_address(mac) if mac else None
            if naddress:
                existing = self.controller.poly.getNode(naddress)
                if (
                    existing is not None
                    and self._is_hub_camera_child(existing)
                    and self.controller._session_has_device(cfg={'address': naddress})
                ):
                    continue
            nname = snap.get('alias') or snap.get('model') or 'Camera'
            dev = await self.controller.discover_single(host=snap.get('host'))
            if dev is not None:
                self.controller.migrate_standalone_camera_to_hub_child(dev)
                pending_adds.append({
                    'parent': self,
                    'dev': dev,
                    'hub_deferred': True,
                    'camera_snapshot': snap,
                })
            else:
                LOGGER.warning(
                    '%s adding offline hub-child placeholder for %s (%s); '
                    'solar/battery cameras may appear when they wake on LAN',
                    self.pfx,
                    nname,
                    snap.get('host') or snap.get('mac'),
                )
                placeholder_cfg = {
                    'type': 'DeviceType.Camera',
                    'host': snap.get('host'),
                    'mac': snap.get('mac'),
                    'model': snap.get('model'),
                    'address': naddress,
                    'hub_parent': self.address,
                    'hub_deferred': True,
                    'device_id': snap.get('device_id'),
                }
                if snap.get('battery') or camera_model_has_battery(snap.get('model')):
                    placeholder_cfg['battery'] = True
                placeholder_cfg['name'] = self.controller._preferred_hub_camera_name(
                    cfg=placeholder_cfg,
                    address=naddress,
                    snapshot=snap,
                    model=snap.get('model'),
                )
                pending_adds.append({
                    'parent': self,
                    'cfg': placeholder_cfg,
                    'camera_snapshot': snap,
                })
        return pending_adds

    def refresh_child_nodes(self):
        """Rebuild child_nodes from hub-child camera cfg under this hub."""
        if self.dev is None or not self._is_hub_parent_device():
            self.child_nodes = []
            return
        native = getattr(self.dev, 'children', None) or []
        if native:
            self.add_children()
            return
        self.child_nodes = self.controller.hub_child_camera_nodes(self.address)

    def _is_hub_parent_device(self):
        return str(getattr(self.dev, 'device_type', None)) == 'DeviceType.Hub'

    def _is_hub_camera_child(self, node):
        return (
            node is not self
            and getattr(node, 'id', '').startswith('SmartCamera_')
        )

    def add_children(self):
        if self.dev is None or not self._is_hub_parent_device():
            self.child_nodes = []
            return
        children = getattr(self.dev, 'children', None) or []
        LOGGER.info(f'{self.pfx} {self.dev.alias} has {len(children)} child camera(s)')
        child_nodes = []
        for pnum in range(len(children)):
            child_dev = children[pnum]
            naddress = get_valid_node_address(child_dev.mac)
            nname = getattr(child_dev, 'alias', None) or f'Camera {pnum + 1}'
            LOGGER.info(
                f"{self.pfx} adding camera num={pnum} address={naddress} name={nname}"
            )
            self.controller.migrate_standalone_camera_to_hub_child(child_dev)
            node = self.controller.add_device_node(
                parent=self,
                dev=child_dev,
            )
            if node in (False, None):
                LOGGER.error(
                    f'{self.pfx} Failed to add child num={pnum} '
                    f'address={naddress} name={nname}'
                )
            elif node is self or not self._is_hub_camera_child(node):
                LOGGER.error(
                    "%s Ignoring unexpected child node for %s: address=%s "
                    "type=%s id=%s",
                    self.pfx,
                    nname,
                    getattr(node, 'address', None),
                    type(node).__name__,
                    getattr(node, 'id', None),
                )
                if self.controller.remove_device_node(naddress, wait_for_pg3=True):
                    node = self.controller.add_device_node(
                        parent=self,
                        dev=child_dev,
                    )
            if node not in (False, None, self) and self._is_hub_camera_child(node):
                child_nodes.append(node)
        self.child_nodes = child_nodes

    async def set_state_a(self, set_energy=True):
        LOGGER.debug(f'{self.pfx} enter')
        if await self.update_a():
            await self.set_children_drivers_a()
        LOGGER.debug(f'{self.pfx} exit')

    async def set_children_drivers_a(self):
        for node in self.child_nodes:
            if not self._is_hub_camera_child(node):
                continue
            await node.set_state_a(set_energy=False)

    def cmd_set_on(self, command):
        LOGGER.debug(f'{self.pfx} hub has no on/off; ignoring DON')

    def cmd_set_off(self, command):
        LOGGER.debug(f'{self.pfx} hub has no on/off; ignoring DOF')

    def cmd_set_mon(self, command):
        super().cmd_set_mon(command)

    commands = {
        'SET_MON': cmd_set_mon,
    }
