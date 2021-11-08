
import polyinterface
from kasa import SmartStrip,SmartDeviceException
from nodes import SmartDeviceNode

LOGGER = polyinterface.LOGGER

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
        self.nodes = []
        LOGGER.debug(f'{self.pfx} controller={controller} address={address} name={name} host={self.host}')
        # The strip is it's own parent since the plugs are it's children so
        # pass my adress as parent
        super().__init__(controller, address, address, name, dev, cfg)
        self.controller = controller

    def start(self):
        LOGGER.debug(f'{self.pfx} enter:')
        super(SmartStripNode, self).start()
        LOGGER.info(f'{self.pfx} {self.dev.alias} has {len(self.dev.children)+1} children')
        for pnum in range(len(self.dev.children)):
            naddress = "{}{:02d}".format(self.address,pnum+1)
            nname    = self.dev.children[pnum].alias
            LOGGER.info(f"{self.pfx} adding plug num={pnum} address={naddress} name={nname}")
            self.nodes.append(self.controller.add_node(parent=self, address_suffix_num=pnum+1, dev=self.dev.children[pnum]))
        self.ready = True
        LOGGER.debug(f'{self.pfx} exit')

    def query(self):
        LOGGER.debug(f'{self.pfx} enter')
        self.check_st()
        LOGGER.debug(f'{self.pfx} nodes={self.nodes}')
        for node in self.nodes:
            node.query()
        self.reportDrivers()
        LOGGER.debug(f'{self.pfx} exit')

    async def xxxset_state_a(self,set_energy=True):
        LOGGER.debug(f'{self.pfx} enter')
        await super(SmartStripNode, self).set_state_a(set_energy=set_energy)
        for nodes in self.nodes:
            await node.set_state_a()
        is_on = False
        # If any are on, then I am on.
        for pnum in range(len(self.dev.children)):
            try:
                if self.dev.children[pnum].is_on:
                    is_on = True
            except Exception as ex:
                LOGGER.error('{self.pfx} failed', exc_info=True)
        self.set_st(is_on)
        LOGGER.debug(f'{self.pfx} exit')

    def newdev(self):
        return SmartStrip(self.host)

    def set_on(self):
        self.setDriver('ST', 100)
        self.st = True

    def set_off(self):
        self.setDriver('ST', 0)
        self.st = False

    def set_st(self,st):
        if st != self.st:
            if st:
                self.set_on()
            else:
                self.set_off()

    # TODO: Should this really call the real set on like this or not?
    # TODO: It has not been tested what happens...
    def cmd_set_on(self,command):
        super().cmd_set_on(command)

    def cmd_set_off(self,command):
        super().cmd_set_off(command)

    drivers = [
        {'driver': 'ST', 'value': 0, 'uom': 78},
        {'driver': 'GV0', 'value': 0, 'uom': 2}  # Connected
    ]
    commands = {
        'DON': cmd_set_on,
        'DOF': cmd_set_off,
    }
