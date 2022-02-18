
from udi_interface import Node,LOGGER
import asyncio
from kasa import SmartStrip,SmartDeviceException
from nodes import SmartDeviceNode

class SmartStripNode(SmartDeviceNode):

    def __init__(self, controller, address, name, dev=None, cfg=None):
        self.ready = False
        self.name = name
        if dev is not None:
            self.host = dev.host
            cfg['emeter'] = dev.has_emeter
        else:
            self.host = cfg['host']
        self.id = 'SmartStrip_'
        if cfg['emeter']:
            self.id += 'E'
        else:
            self.id += 'N'
        self.debug_level = 0
        self.st = None
        self.pfx = f"{self.name}:"
        self.child_nodes = []
        LOGGER.debug(f'{self.pfx} controller={controller} address={address} name={name} host={self.host}')
        # The strip is it's own parent since the plugs are it's children so
        # pass my adress as parent
        super().__init__(controller, address, address, name, dev, cfg)
        controller.poly.subscribe(controller.poly.START,  self.handler_start, address) 

    def handler_start(self):
        LOGGER.debug(f'{self.pfx} enter:')
        super(SmartStripNode, self).handler_start()
        LOGGER.info(f'{self.pfx} {self.dev.alias} has {len(self.dev.children)+1} children')
        for pnum in range(len(self.dev.children)):
            naddress = "{}{:02d}".format(self.address,pnum+1)
            nname    = self.dev.children[pnum].alias
            LOGGER.info(f"{self.pfx} adding plug num={pnum} address={naddress} name={nname}")
            node = self.controller.add_node(parent=self, address_suffix_num=pnum+1, dev=self.dev.children[pnum])
            if node is False:
                LOGGER.error(f'{self.pfx} Failed to add node num={pnum} address={naddress} name={nname}')
            else:
                self.child_nodes.append(node)
        self.ready = True
        LOGGER.debug(f'{self.pfx} exit')

    def query(self):
        super().query()

    async def set_state_a(self,set_energy=True):
        LOGGER.debug(f'enter: dev={self.dev}')
        # This doesn't call set_energy, since that is only called on long_poll's
        # We don't use self.connected here because dev might be good, but device is unplugged
        # So then when it's plugged back in the same dev will still work
        if await self.update_a():
            ocon = self.connected

            # We dont update children since that forces an update on myself each time
            self.set_st_from_children()

            # On restore, or initial startup, set all drivers.
            if not ocon and self.connected:
                try:
                    self.set_all_drivers()
                except Exception as ex:
                    LOGGER.error(f'{self.pfx} set_all_drivers failed: {ex}',exc_info=True)
            if set_energy:
                await self._set_energy_a()
        LOGGER.debug(f'exit:  dev={self.dev}')

    # Set my ST based on the children's current ST
    # This is called by the child when their ST changes.
    def set_st_from_children(self):
        LOGGER.debug(f'enter: {self.dev}')
        # Check if any node is on, update their status
        for node in self.child_nodes:
            if int(node.getDriver('ST')) > 0:
                LOGGER.debug(f'{self.pfx} node is on {node.name}')
                self.set_on()
                return
        self.set_off()
        LOGGER.debug(f'exit: {self.dev}')

    def newdev(self):
        return SmartStrip(self.host)

    def set_on(self):
        LOGGER.debug(f'enter: {self.dev}')
        self.setDriver('ST', 100)
        self.st = True
        LOGGER.debug(f'exit: {self.dev}')

    def set_off(self):
        LOGGER.debug(f'enter: {self.dev}')
        self.setDriver('ST', 0)
        self.st = False
        LOGGER.debug(f'exit: {self.dev}')

    def set_st(self,st):
        if st != self.st:
            if st:
                self.set_on()
            else:
                self.set_off()

    def cmd_set_on(self,command):
        for node in self.child_nodes:
            if not node.is_on():
                node.q_set_on()
        self.set_on()

    def cmd_set_off(self,command):
        for node in self.child_nodes:
            if node.is_on():
                node.q_set_off()
        self.set_off()

    # TODO: Querying the child nodes calls update on myself each time, really don't need that.
    def cmd_query_all(self,command):
        LOGGER.debug(f'{self.pfx} enter')
        self.query()
        for node in self.child_nodes:
            node.query()
        LOGGER.debug(f'{self.pfx} exit')

    drivers = [
        {'driver': 'ST', 'value': 0, 'uom': 78},
        {'driver': 'GV0', 'value': 0, 'uom': 2}  # Connected
    ]
    commands = {
        'DON': cmd_set_on,
        'DOF': cmd_set_off,
        'QUERY': query,
        'QUERY_ALL': cmd_query_all,
    }
