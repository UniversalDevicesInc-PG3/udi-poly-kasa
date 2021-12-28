#
# TP Link Kasa Smart Plug Node
#
# This code is used for plugs
#
from udi_interface import Node,LOGGER
import asyncio
from kasa import SmartPlug,SmartDeviceException
from nodes import SmartDeviceNode

class SmartPlugNode(SmartDeviceNode):

    def __init__(self, controller, primary, address, name, dev=None, cfg=None):
        # All plugs have these.
        self.debug_level = 0
        self.name = name
        # All devices have these.
        self.drivers = [
            {'driver': 'ST', 'value': 0, 'uom': 78},
            {'driver': 'GV0', 'value': 0, 'uom': 2}, #connection state
        ]
        if dev is not None:
            # Figure out the id based in the device info
            self.id = 'SmartPlug_'
            if dev.is_dimmable:
                self.id += 'D'
            else:
                self.id += 'N'
            if dev.has_emeter:
                self.id += 'E'
            else:
                self.id += 'N'
        super().__init__(controller, primary, address, name, dev, cfg)

    def newdev(self):
        return SmartPlug(self.host)

    def cmd_set_on(self,command):
        super().cmd_set_on(command)

    def cmd_set_off(self,command):
        super().cmd_set_off(command)

    commands = {
        'DON': cmd_set_on,
        'DOF': cmd_set_off,
    }

