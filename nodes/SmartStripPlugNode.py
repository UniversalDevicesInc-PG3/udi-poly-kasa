#
# TP Link Kasa Smart StripPlug Node
#
# This code is used for StripPlugs
#
from udi_interface import Node,LOGGER
import asyncio
from kasa import SmartStrip,SmartDeviceException
from nodes import SmartDeviceNode

class SmartStripPlugNode(SmartDeviceNode):

    def __init__(self, controller, primary, address, name, dev=None, cfg=None):
        # All StripPlugs have these.
        self.debug_level = 0
        self.name = name
        self.primary_node = controller.poly.getNode(primary)
        # All devices have these.
        self.drivers = [
            {'driver': 'ST', 'value': 0, 'uom': 78},
            {'driver': 'GV0', 'value': 0, 'uom': 2}, #connection state
        ]
        if dev is not None:
            # Figure out the id based in the device info
            self.id = 'SmartStripPlug_'
            if dev.has_emeter:
                self.id += 'E'
            else:
                self.id += 'N'
        super().__init__(controller, primary, address, name, dev, cfg)
        self.poll = False

    def start(self):
        LOGGER.debug(f'enter: {self.dev}')
        super().start()
        LOGGER.debug(f'exit: {self.dev}')

    def query(self):
        super().query()

    async def connect_a(self):
        # TODO: Confirm parent is connected?
        pass

    async def set_state_a(self,set_energy=True):
        LOGGER.debug(f'enter: dev={self.dev}')
        # This doesn't call set_energy, since that is only called on long_poll's
        # We don't use self.connected here because dev might be good, but device is unplugged
        # So then when it's plugged back in the same dev will still work
        if await self.primary_node.update_a():
            if self.dev.is_on is True:
                self.brightness = 100
                self.setDriver('ST',100)
            else:
                self.brightness = 0
                self.setDriver('ST',0)

            if set_energy:
                await self._set_energy_a()
        LOGGER.debug(f'exit:  dev={self.dev}')

    # The q versions are called by the parent
    def q_set_on(self):
        LOGGER.debug(f'enter: {self.dev}')
        super().cmd_set_on(False)
        LOGGER.debug(f'exit: {self.dev}')

    def q_set_off(self):
        LOGGER.debug(f'enter: {self.dev}')
        super().cmd_set_off(False)
        LOGGER.debug(f'exit: {self.dev}')

    def cmd_set_on(self,command):
        LOGGER.debug(f'enter: {self.dev}')
        super().cmd_set_on(command)
        self.primary_node.set_on()
        LOGGER.debug(f'exit: {self.dev}')

    def cmd_set_off(self,command):
        LOGGER.debug(f'enter: {self.dev}')
        super().cmd_set_off(command)
        self.primary_node.set_st_from_children()
        LOGGER.debug(f'exit: {self.dev}')

    def is_connected(self):
        return self.primary_node.is_connected()
        
    commands = {
        'DON': cmd_set_on,
        'DOF': cmd_set_off,
        'QUERY': query,
    }
