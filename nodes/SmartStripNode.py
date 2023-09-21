
from udi_interface import Node,LOGGER
import asyncio
from kasa import SmartStrip,SmartDeviceException
from nodes import SmartDeviceNode

class SmartStripNode(SmartDeviceNode):

    def __init__(self, controller, address, name, dev=None, cfg=None):
        self.ready = False
        self.name = name
        self.drivers = [
            {'driver': 'ST', 'value': 0, 'uom': 51, 'name': 'State'},
            {'driver': 'GV0', 'value': 0, 'uom': 2, 'name': 'Connected'},
            {'driver': 'GV6', 'value': 1, 'uom': 2, 'name': 'Poll Device'},
        ]
        self.address = address
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
        controller.poly.subscribe(controller.poly.ADDNODEDONE,       self.add_node_done)
        # The strip is it's own parent since the plugs are it's children so
        # pass my adress as parent
        super().__init__(controller, address, address, name, dev, cfg)
        controller.poly.subscribe(controller.poly.START,  self.handler_start, address) 

    def handler_start(self):
        LOGGER.debug(f'{self.pfx} enter:')
        super(SmartStripNode, self).handler_start()
        self.ready = True
        LOGGER.debug(f'{self.pfx} exit')

    def add_node_done(self, data):
        if (data['address'] != self.address):
            return
        LOGGER.debug(f'{self.pfx} enter: data={data} address={self.address}')
        if self.is_connected():
            self.update()
            self.add_children()

    def query(self):
        super().query()

    async def set_state_a(self,set_energy=True):
        LOGGER.debug(f'{self.pfx} enter: dev={self.dev}')
        if await self.update_a():
            if set_energy:
                await self._set_energy_a()
            # We dont update children since that forces an update on myself each time
            await self.set_children_drivers_a(set_energy=set_energy)
            self.set_st_from_children()
        LOGGER.debug(f'{self.pfx} exit:  dev={self.dev}')

    # Called when connected is changed from False to True
    # On initial startup or a reconnect later
    def reconnected(self):
        LOGGER.debug(f'{self.pfx} enter: dev={self.dev}')
        #try:
        #    self.set_all_drivers()
        #except Exception as ex:
        #    LOGGER.error(f'{self.pfx} set_all_drivers failed: {ex}',exc_info=True)
        self.add_children()
        LOGGER.debug(f'{self.pfx} exit: dev={self.dev}')

    def add_children(self):
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

    async def set_children_drivers_a(self,set_energy=True):
        LOGGER.debug(f'{self.pfx} enter: {self.dev}')
        for node in self.child_nodes:
            await node.set_drivers_a(set_energy=set_energy)
        LOGGER.debug(f'{self.pfx} exit: {self.dev}')

    # Set my ST based on the children's current ST
    # This is called by the child when their ST changes
    # and by set_state_a above
    def set_st_from_children(self):
        LOGGER.debug(f'{self.pfx} enter: {self.dev}')
        # Check if any node is on, update their status
        for node in self.child_nodes:
            if int(node.getDriver('ST')) > 0:
                LOGGER.debug(f'{self.pfx} node is on {node.name}')
                self.set_on()
                return
        self.set_off()
        LOGGER.debug(f'{self.pfx} exit: {self.dev}')

    def newdev(self):
        return SmartStrip(self.host)

    def set_on(self):
        LOGGER.debug(f'enter: {self.dev}')
        LOGGER.debug(f'{self.pfx} setDriver(ST,100)')
        self.setDriver('ST', 100)
        self.st = True
        LOGGER.debug(f'exit: {self.dev}')

    def set_off(self):
        LOGGER.debug(f'enter: {self.dev}')
        LOGGER.debug(f'{self.pfx} setDriver(ST,0)')
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

    def cmd_set_mon(self,command):
        super().cmd_set_mon(command)

    commands = {
        'DON': cmd_set_on,
        'DOF': cmd_set_off,
        'QUERY': query,
        'QUERY_ALL': cmd_query_all,
        'SET_MON': cmd_set_mon,
    }
