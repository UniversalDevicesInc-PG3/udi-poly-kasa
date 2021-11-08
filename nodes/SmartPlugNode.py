#
# TP Link Kasa Smart Plug Node
#
# This code is used for plugs
#
import polyinterface
from kasa import SmartPlug,SmartDeviceException

from nodes import SmartDeviceNode

LOGGER = polyinterface.LOGGER

class SmartPlugNode(SmartDeviceNode):

    def __init__(self, controller, parent, address, name, cfg={}, dev=None):
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
        super().__init__(controller, parent.address, address, name, dev, cfg)

    def start(self):
        LOGGER.debug(f'enter: {self.dev}')
        super().start()
        LOGGER.debug(f'exit: {self.dev}')

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

