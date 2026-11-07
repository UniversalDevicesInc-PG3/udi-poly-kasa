#
# TP Link Kasa Smart Dimmer Node
#
# This code is used for dimmers (HS220) and switches (HS200)
#
from udi_interface import Node,LOGGER
import asyncio
from kasa import SmartPlug, SmartDimmer
from kasa_compat import SmartDeviceException
from converters import bri2st, st2bri, don_level_to_bri
from nodes import SmartDeviceNode

class SmartDimmerNode(SmartDeviceNode):

    def __init__(self, controller, primary, address, name, dev=None, cfg=None):
        # All plugs have these.
        self.debug_level = 0
        self.name = name
        # All devices have these.
        self.drivers = [
            {'driver': 'ST', 'value': 0, 'uom': 51, 'name': 'State'},
            {'driver': 'GV0', 'value': 0, 'uom': 2, 'name': 'Connected'},
            {'driver': 'GV6', 'value': 1, 'uom': 2, 'name': 'Poll Device'},
        ]
        if dev is not None:
            # Figure out the id based in the device info
            self.id = 'SmartDimmer_'
            if self.is_dimmable(dev):
                self.id += 'D'
                self.drivers.append({'driver': 'GV5', 'value': 0, 'uom': 100, 'name': 'Brightness'})
            else:
                self.id += 'N'
            if dev.has_emeter:
                self.id += 'E'
            else:
                self.id += 'N'
            cfg['emeter'] = dev.has_emeter
        else:
            self.id = cfg['id']
            if len(self.id) > 12 and self.id[12] == 'D':
                self.drivers.append({'driver': 'GV5', 'value': 0, 'uom': 100, 'name': 'Brightness'})
        if cfg.get('emeter'):
            self.drivers.append({'driver': 'CC', 'value': 0, 'uom': 1, 'name': 'Current Current'})
            self.drivers.append({'driver': 'CV', 'value': 0, 'uom': 72, 'name': 'Current Voltage'})
            self.drivers.append({'driver': 'CPW', 'value': 0, 'uom': 73, 'name': 'Current Power Watts'})
            self.drivers.append({'driver': 'TPW', 'value': 0, 'uom': 33, 'name': 'Total Energy kWh'})
        super().__init__(controller, primary, address, name, dev, cfg)

    def set_bri(self,val):
        LOGGER.debug(f'{self.pfx} connected={self.connected} val={val}')
        if self.is_connected():
            self.brightness = int(val)
            LOGGER.debug(f'{val}')
            self.setDriver('GV5',self.brightness)
            # This won't actually change unless the device is on
            asyncio.run(self.dev.set_brightness(int(bri2st(self.brightness))))
            asyncio.run(self.dev.update())
            self.setDriver('ST',self.dev.brightness)

    def brt(self):
        LOGGER.debug(f'{self.pfx} connected={self.connected}')
        asyncio.run(self.dev.update())
        self.brightness = st2bri(self.dev.brightness)
        if self.is_connected() and self.brightness <= 100:
            self.set_bri(self.brightness + 7)

    def dim(self):
        LOGGER.debug(f'{self.pfx} connected={self.connected}')
        asyncio.run(self.dev.update())
        self.brightness = st2bri(self.dev.brightness)
        if self.is_connected() and self.brightness > 0:
            self.set_bri(self.brightness - 7)

    def newdev(self):
        return SmartDimmer(self.host)

    def _accepts_don_level(self):
        # SmartDimmer_NN is a switch. Only the D node defs take a DON level.
        node_id = getattr(self, 'id', '') or ''
        if not (len(node_id) > 12 and node_id[12] == 'D'):
            return False
        return super()._accepts_don_level()

    async def _set_bri_a(self, val):
        if not self.is_connected():
            return
        self.brightness = int(val)
        self.setDriver('GV5', self.brightness)
        await self.dev.set_brightness(int(bri2st(self.brightness)))
        await self.dev.update()
        self.setDriver('ST', self.dev.brightness)

    async def _don_level_a(self, level):
        if level == 0:
            await self.set_off_a()
            return
        await self.dev.turn_on()
        await self._set_bri_a(level)

    def cmd_set_on(self,command):
        level = don_level_to_bri(command)
        if level is None or not self._accepts_don_level():
            super().cmd_set_on(command)
            return
        LOGGER.info(f'{self.pfx} DON level={level}')
        self._schedule(self._don_level_a(level))

    def cmd_set_off(self,command):
        super().cmd_set_off(command)

    def cmd_set_bri(self,command):
        if not self._cmd_requires_dimmable('SET_BRI'):
            return False
        val = int(command.get('value'))
        LOGGER.info(f'{self.pfx} val={val}')
        self._schedule(self._don_level_a(val))

    def cmd_brt(self,command):
        if not self._cmd_requires_dimmable('BRT'):
            return False
        self._schedule(self._nudge_bri_a(7))

    def cmd_dim(self,command):
        if not self._cmd_requires_dimmable('DIM'):
            return False
        self._schedule(self._nudge_bri_a(-7))

    async def _nudge_bri_a(self, delta):
        if delta > 0:
            await self.dev.turn_on()
        await self.dev.update()
        level = st2bri(self.dev.brightness)
        if delta > 0:
            if not self.is_connected() or level > 100:
                return
        elif not self.is_connected() or level <= 0:
            return
        await self._set_bri_a(level + delta)

    def cmd_set_mon(self,command):
        super().cmd_set_mon(command)

    commands = {
        'DON': cmd_set_on,
        'DOF': cmd_set_off,
        'SET_BRI': cmd_set_bri,
        'BRT': cmd_brt,
        'DIM': cmd_dim,
        'SET_MON': cmd_set_mon,
    }

