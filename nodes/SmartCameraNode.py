#
# TP Link Kasa / Tapo Smart Camera Node
#
from udi_interface import LOGGER
from camera_helpers import (
    battery_percent,
    camera_lan_host,
    camera_nodedef_id,
    camera_notifications_enabled,
    hub_child_control_fallback_eligible,
    is_hub_child_camera_cfg,
    is_hub_deferred_camera_cfg,
    motion_detection_enabled,
    set_camera_notifications_enabled,
)
from kasa_compat import DeviceError
from nodes import SmartDeviceNode


class SmartCameraNode(SmartDeviceNode):

    def __init__(self, controller, primary, address, name, dev=None, cfg=None):
        self.debug_level = 0
        self.name = name
        self.hub_child = bool(
            is_hub_child_camera_cfg(cfg)
            or (dev is not None and getattr(dev, 'parent', None) is not None)
        )
        self.hub_deferred = bool(is_hub_deferred_camera_cfg(cfg))
        self.primary_node = controller.poly.getNode(primary) if self.hub_child else None
        if self.hub_child and self.primary_node is not None:
            self.pfx = f"{self.primary_node.name}:{self.name}:"
        else:
            self.pfx = f"{self.name}:"
        self.poll = not self.hub_child
        self.drivers = [
            {'driver': 'ST', 'value': 0, 'uom': 78, 'name': 'Camera State'},
            {'driver': 'GV0', 'value': 0, 'uom': 2, 'name': 'Connected'},
            {'driver': 'GV2', 'value': 0, 'uom': 2, 'name': 'Motion Detection'},
            {'driver': 'GV4', 'value': 0, 'uom': 2, 'name': 'Notifications'},
            {'driver': 'GV6', 'value': 1, 'uom': 2, 'name': 'Poll Device'},
        ]
        if dev is not None:
            self.id = camera_nodedef_id(dev=dev)
            if cfg is not None:
                cfg['battery'] = self.id.endswith('_B')
        elif cfg is not None:
            self.id = camera_nodedef_id(cfg=cfg, has_battery=cfg.get('battery'))
        else:
            self.id = 'SmartCamera_N'
        if self.id == 'SmartCamera_B':
            self.drivers.insert(3, {
                'driver': 'GV3',
                'value': 0,
                'uom': 51,
                'name': 'Battery Level',
            })
        super().__init__(controller, primary, address, name, dev, cfg)

    def _quick_probe_port(self):
        return 443

    async def connect_a(self):
        if self.hub_child:
            return True
        return await super().connect_a()

    async def update_a(self):
        if self.hub_child and self.hub_deferred:
            return await self._update_hub_deferred_a()
        return await super().update_a()

    async def _update_hub_deferred_a(self):
        hub_node = self.primary_node
        hub_host = getattr(hub_node, 'host', None) if hub_node else None
        if hub_node is not None:
            if self.controller.host_should_skip(hub_host):
                self.set_connected(False)
                return False
            if await self.controller.hub_node_update_a(hub_node):
                hub_dev = getattr(hub_node, 'dev', None)
                if hub_dev is not None and getattr(hub_dev, 'children', None):
                    child = await self._resolve_hub_child_dev()
                    if (
                        child is not None
                        and getattr(child, 'parent', None) is not None
                    ):
                        ret = await self.controller.update_dev(child)
                        if ret:
                            self.set_connected(True)
                        else:
                            self.set_connected(
                                False,
                                f'{self.pfx} hub-child update failed',
                            )
                        return ret
        if hub_host and self.controller.host_hub_protocol_degraded(hub_host):
            LOGGER.debug(
                '%s hub protocol degraded; skipping direct LAN fallback',
                self.pfx,
            )
            self.set_connected(False)
            return False
        lan_host = camera_lan_host(
            cfg=self.cfg,
            dev=self.dev,
            hub_host=getattr(hub_node, 'host', None) if hub_node else None,
        )
        if not lan_host:
            self.set_connected(False)
            return False
        if self.dev is None or getattr(self.dev, 'host', None) != lan_host:
            self.dev = await self.controller.discover_single(host=lan_host)
        if self.dev is None:
            self.set_connected(False)
            return False
        ret = await self.controller.update_dev(self.dev)
        hub_node = self.primary_node
        if hub_node is not None:
            self.controller._refresh_hub_camera_naming(hub_node)
        if ret:
            self.set_connected(True)
        else:
            self.set_connected(False, f'{self.pfx} hub-deferred direct update failed')
        return ret

    async def _resolve_hub_child_dev(self):
        if not self.hub_child or self.dev is None:
            return self.dev
        hub_node = self.primary_node
        if hub_node is None:
            return self.dev
        if not await self.controller.hub_node_update_a(hub_node):
            return self.dev
        hub_dev = getattr(hub_node, 'dev', None)
        if hub_dev is None:
            return self.dev
        target_id = (self.cfg or {}).get('device_id')
        target_mac = (self.cfg or {}).get('mac')
        smac = self.controller.smac
        for child in getattr(hub_dev, 'children', None) or []:
            child_id = getattr(child, 'device_id', None)
            child_mac = getattr(child, 'mac', None)
            if target_id and child_id == target_id:
                self.dev = child
                return child
            if target_mac and child_mac and smac(child_mac) == smac(target_mac):
                self.dev = child
                return child
        return self.dev

    async def _set_camera_state_a(self, on):
        dev = self.dev
        if self.hub_child:
            dev = await self._resolve_hub_child_dev()
        if dev is None:
            return

        try:
            await dev.set_state(on)
            return
        except DeviceError as ex:
            if not self.hub_child or not hub_child_control_fallback_eligible(ex):
                raise
            hub_host = getattr(self.primary_node, 'host', None)
            lan_host = camera_lan_host(cfg=self.cfg, dev=dev, hub_host=hub_host)
            if not lan_host or lan_host == hub_host:
                raise
            LOGGER.warning(
                '%s hub child set_state failed (%s); trying direct LAN at %s',
                self.pfx,
                ex,
                lan_host,
            )
            lan_dev = await self.controller.discover_single(host=lan_host)
            if lan_dev is None:
                raise DeviceError(
                    f'Camera not reachable at {lan_host} for direct privacy control'
                ) from ex
            await self.controller.update_dev(lan_dev)
            await lan_dev.set_state(on)

    async def _set_notifications_a(self, enable):
        """Toggle Tapo push notifications (recording/detection unchanged)."""
        dev = self.dev
        if self.hub_child:
            dev = await self._resolve_hub_child_dev()
        if dev is None:
            raise DeviceError(f'{self.pfx} device not connected for notifications')

        try:
            await set_camera_notifications_enabled(dev, enable)
            return
        except DeviceError as ex:
            if not self.hub_child or not hub_child_control_fallback_eligible(ex):
                raise
            hub_host = getattr(self.primary_node, 'host', None)
            lan_host = camera_lan_host(cfg=self.cfg, dev=dev, hub_host=hub_host)
            if not lan_host or lan_host == hub_host:
                raise
            LOGGER.warning(
                '%s hub child set notifications failed (%s); trying direct LAN at %s',
                self.pfx,
                ex,
                lan_host,
            )
            lan_dev = await self.controller.discover_single(host=lan_host)
            if lan_dev is None:
                raise DeviceError(
                    f'Camera not reachable at {lan_host} for notification control'
                ) from ex
            await self.controller.update_dev(lan_dev)
            await set_camera_notifications_enabled(lan_dev, enable)

    async def set_notifications_a(self, enable):
        LOGGER.debug('%s enter enable=%s', self.pfx, enable)
        if self.dev is None and not self.hub_child:
            return
        await self._set_notifications_a(bool(enable))
        self.setDriver('GV4', 1 if enable else 0)
        await self.set_state_a(set_energy=False)
        LOGGER.debug('%s exit', self.pfx)

    def set_notifications(self, enable):
        self._run_coro(self.set_notifications_a(bool(enable)), 'set_notifications_a')

    async def set_on_a(self):
        LOGGER.debug(f'{self.pfx} enter')
        if self.dev is None and not self.hub_child:
            return
        await self._set_camera_state_a(True)
        self.setDriver('ST', 100)
        await self.set_state_a(set_energy=False)
        LOGGER.debug(f'{self.pfx} exit')

    async def set_off_a(self):
        LOGGER.debug(f'{self.pfx} enter')
        if self.dev is None and not self.hub_child:
            return
        await self._set_camera_state_a(False)
        self.setDriver('ST', 0)
        await self.set_state_a(set_energy=False)
        LOGGER.debug(f'{self.pfx} exit')

    async def set_state_a(self, set_energy=True):
        try:
            ocon = self.connected
            if self.hub_child:
                if self.hub_deferred:
                    if not await self.update_a():
                        return
                elif self.primary_node is None or not await self.controller.hub_node_update_a(self.primary_node):
                    return
            elif not await self.update_a():
                return
            try:
                is_on = self.dev.is_on
            except Exception:
                return
            if is_on:
                self.setDriver('ST', 100)
            else:
                self.setDriver('ST', 0)
            motion = motion_detection_enabled(self.dev)
            if motion is not None:
                self.setDriver('GV2', 1 if motion else 0)
            notifications = camera_notifications_enabled(self.dev)
            if notifications is not None:
                self.setDriver('GV4', 1 if notifications else 0)
            if self.id == 'SmartCamera_B':
                level = battery_percent(self.dev)
                if level is not None:
                    self.setDriver('GV3', level)
            if not ocon and self.connected:
                self.reconnected()
        except Exception as ex:
            LOGGER.error(f'{self.pfx} set_state_a failed: {ex}', exc_info=True)

    def cmd_set_on(self, command):
        super().cmd_set_on(command)

    def cmd_set_off(self, command):
        super().cmd_set_off(command)

    def cmd_set_mon(self, command):
        super().cmd_set_mon(command)

    def cmd_set_notifications(self, command):
        val = int(command.get('value'))
        LOGGER.debug('%s SET_NOTIFICATIONS val=%s', self.pfx, val)
        self.set_notifications(bool(val))

    commands = {
        'DON': cmd_set_on,
        'DOF': cmd_set_off,
        'SET_MON': cmd_set_mon,
        'SET_NOTIFICATIONS': cmd_set_notifications,
    }
