#
# TP Link Kasa Smart LightStrip Node
#
# This code is used for light strips
#
from udi_interface import Node,LOGGER
import asyncio
from kasa import SmartLightStrip,SmartDeviceException
from nodes import SmartBulbNode

# LightSTrip is the same as bulb
# TODO: Add length Driver for info?

class SmartLightStripNode(SmartBulbNode):

    def __init__(self, controller, primary, address, name, dev=None, cfg=None):
        LOGGER.debug(f'enter:xxx address={address} name={name}')
        if dev is not None:
            # Figure out the id based in the device info
            id = 'SmartLightStrip_'
            if dev.is_dimmable:
                id += 'D'
            else:
                id += 'N'
            if self.is_variable_color_temp(dev):
                id += 'T'
            else:
                id += 'N'
            if self.is_color(dev):
                id += 'C'
            else:
                id += 'N'
            if dev.has_emeter:
                id += 'E'
            else:
                id += 'N'
            cfg['emeter'] = dev.has_emeter
            cfg['color']  = self.is_color(dev)
            cfg['color_temp'] = self.is_variable_color_temp(dev)
        else:
            id = cfg['id']
        LOGGER.debug(f'enter:xxx address={address} name={name}')
        if cfg['color_temp']:
            self.drivers.append({'driver': 'CLITEMP', 'value': 0, 'uom': 26, 'name': 'Color Temperature'})
        if cfg['color']:
            self.drivers.append({'driver': 'GV3', 'value': 0, 'uom': 100, 'name': 'Hue'})
            self.drivers.append({'driver': 'GV4', 'value': 0, 'uom': 100, 'name': 'Saturation'})
        super().__init__(controller, primary, address, name, dev, cfg)
        self.id = id
