
from udi_interface import Node,LOGGER,Custom,LOG_HANDLER
import logging,re,json,sys,asyncio
from threading import Thread,Event
from node_funcs import get_valid_node_name
#sys.path.insert(0,"pyHS100")
#from pyHS100 import Discover
from kasa import Discover
from nodes import SmartStripPlugNode
from nodes import SmartStripNode
from nodes import SmartPlugNode
from nodes import SmartDimmerNode
from nodes import SmartBulbNode
from nodes import SmartLightStripNode

#logging.getLogger('pyHS100').setLevel(logging.DEBUG)

# We need an event loop for python-kasa since we run in a
# thread which doesn't have a loop
mainloop = asyncio.get_event_loop()

class Controller(Node):

    def __init__(self, poly, primary, address, name):
        super(Controller, self).__init__(poly, primary, address, name)
        self.poll    = False
        self.ready   = False
        self.hb = 0
        self.nodes_by_mac = {}
        self.discover_done = False
        # For the short/long poll threads, we run them in threads so the main
        # process is always available for controlling devices
        self.short_event   = False
        self.in_short_poll = False
        self.long_event    = False
        self.in_long_poll  = False
        self.Notices         = Custom(self.poly, 'notices')
        self.Parameters      = Custom(self.poly, 'customparams')
        self.poly.subscribe(self.poly.START,                  self.handler_start, address) 
        self.poly.subscribe(self.poly.POLL,                   self.handler_poll)
        self.poly.subscribe(self.poly.LOGLEVEL,               self.handler_log_level)
        self.poly.subscribe(self.poly.CONFIGDONE,             self.handler_config_done)
        self.poly.subscribe(self.poly.DISCOVER,               self.discover_new)
        self.poly.ready()
        self.poly.addNode(self)

    def handler_start(self):
        LOGGER.info(f"Started Kasa PG3 NodeServer {self.poly.serverdata['version']}")
        self.Notices.clear()
        self.mainloop = mainloop
        asyncio.set_event_loop(mainloop)
        self.connect_thread = Thread(target=mainloop.run_forever)
        self.connect_thread.start()
        self.setDriver('ST', 1)
        self.heartbeat()
        self.check_params()
        try:
            self.discover()
        except:
            LOGGER.error(f'discover failed', exc_info=True)
            return False
        self.ready = True
        LOGGER.info(f'exit {self.name}')

    # For things we only do have the configuration is loaded...
    def handler_config_done(self):
        LOGGER.debug(f'enter')
        self.poly.addLogLevel('DEBUG_MODULES',9,'Debug + Modules')
        LOGGER.debug(f'exit')

    def handler_poll(self, polltype):
        if polltype == 'longPoll':
            self.longPoll()
        elif polltype == 'shortPoll':
            self.shortPoll()

    def shortPoll(self):
        if not self.discover_done:
            LOGGER.info('waiting for discover to complete')
            return
        if self.in_short_poll:
            LOGGER.info('Already running')
            return
        self.in_short_poll = True
        if self.short_event is False:
            LOGGER.debug('Setting up Thread')
            self.short_event = Event()
            self.short_thread = Thread(name='shortPoll',target=self._shortPoll)
            self.short_thread.daemon = True
            LOGGER.debug('Starting Thread')
            st = self.short_thread.start()
            LOGGER.debug(f'Thread start st={st}')
        # Tell the thread to run
        LOGGER.debug(f'thread={self.short_thread} event={self.short_event}')
        if self.short_event is not None:
            LOGGER.debug('calling event.set')
            self.short_event.set()
        else:
            LOGGER.error(f'event is gone? thread={self.short_thread} event={self.short_event}')

    def _shortPoll(self):
        while (True):
            self.short_event.wait()
            LOGGER.debug('enter')
            asyncio.run_coroutine_threadsafe(self._shortPoll_a(), self.mainloop)
            LOGGER.debug('exit')
            self.short_event.clear()
        
    async def _shortPoll_a(self):
        LOGGER.debug('enter')
        for node_address in self.poly.getNodes():
            node = self.poly.getNode(node_address)
            LOGGER.debug(f'node.address={node.address} node.name={node.name} ')
            if node.poll:
                await node.shortPoll()
        self.in_short_poll = False
        LOGGER.debug('exit')

    def longPoll(self):
        if not self.discover_done:
            LOGGER.info('waiting for discover to complete')
            return
        if self.in_long_poll:
            LOGGER.info('Already running')
            return
        self.in_long_poll = True
        self.heartbeat()
        if not self.discover_done:
            LOGGER.info('waiting for discover to complete')
            return
        if self.long_event is False:
            LOGGER.debug('Setting up Thread')
            self.long_event = Event()
            self.long_thread = Thread(name='longPoll',target=self._longPoll)
            self.long_thread.daemon = True
            LOGGER.debug('Starting Thread')
            st = self.long_thread.start()
            LOGGER.debug('Thread start st={st}')
        # Tell the thread to run
        LOGGER.debug(f'thread={self.long_thread} event={self.long_event}')
        if self.long_event is not None:
            LOGGER.debug('calling event.set')
            self.long_event.set()
        else:
            LOGGER.error(f'event is gone? thread={self.long_thread} event={self.long_event}')

    def _longPoll(self):
        while (True):
            self.long_event.wait()
            LOGGER.debug('enter')
            asyncio.run_coroutine_threadsafe(self._longPoll_a(), self.mainloop)
            self.long_event.clear()
            LOGGER.debug('exit')

    async def _longPoll_a(self):
        LOGGER.debug('enter')
        all_connected = True
        for node_address in self.poly.getNodes():
            node = self.poly.getNode(node_address)
            if node.poll:
                try:
                    if node.is_connected():
                        await node.longPoll()
                    else:
                        LOGGER.warning(f"Known device not responding {node.address} '{node.name}'")
                        all_connected = False
                except:
                    pass # in case node doesn't have a longPoll method
        if not all_connected:
            LOGGER.warning("Not all devices are connected, running discover to check for them")
            await self._discover_new_a()
        self.in_long_poll = False
        LOGGER.debug('exit')

    def query(self):
        self.setDriver('ST', 1)
        self.reportDrivers()
        self.check_params()
        for node_address in self.poly.getNodes():
            node = self.poly.getNode(node_address)
            if node.poll:
                node.query()

    def heartbeat(self):
        LOGGER.debug('hb={self.hb}')
        if self.hb == 0:
            self.reportCmd("DON",2)
            self.hb = 1
        else:
            self.reportCmd("DOF",2)
            self.hb = 0

    def discover(self):
        self.devm = {}
        LOGGER.info(f"enter: {self.poly.network_interface['broadcast']} timout=10 discovery_packets=10 mainloop={self.mainloop}")
        future = asyncio.run_coroutine_threadsafe(self._discover(), self.mainloop)
        res = future.result()
        LOGGER.debug(f'result={res}')
        self.discover_done = True
        LOGGER.info("exit")

    async def discover_add_device(self,dev):
        LOGGER.debug(f"enter: {dev}")
        LOGGER.info(f"Got Device\n\tAlias:{dev.alias}\n\tModel:{dev.model}\n\tMac:{dev.mac}\n\tHost:{dev.host}")
        self.add_node(dev=dev)
        # Add to our list of added devices
        self.devm[self.smac(dev.mac)] = True
        LOGGER.debug(f"exit: {dev}")

    async def _discover(self):
        LOGGER.debug('enter')
        await Discover.discover(timeout=10,discovery_packets=10,target=self.poly.network_interface['broadcast'],on_discovered=self.discover_add_device)
        # make sure all we know about are added in case they didn't respond this time.
        LOGGER.info(f"Discover.discover done: checking for previously known devices")
        for mac in self.Parameters:
            LOGGER.debug(f'checking mac={mac}')
            if self.smac(mac) in self.devm:
                LOGGER.debug(f'already added mac={mac}')
            else:
                cfg = self.get_device_cfg(mac)
                LOGGER.debug(f'cfg={cfg}')
                if cfg is not None:
                    LOGGER.warning(f"Adding previously known device that didn't respond to discover: {cfg}")
                    self.add_node(cfg=cfg)
        LOGGER.debug('exit')
        return True

    async def discover_new_add_device(self,dev):
        # Known Device?
        await dev.update()
        smac = self.smac(dev.mac)
        if smac in self.nodes_by_mac:
            # Make sure the host matches
            node = self.nodes_by_mac[smac]
            if dev.host != node.host:
                LOGGER.warning(f"Updating '{node.name}' host from {node.host} to {dev.host}")
                node.host = dev.host
                node.connect()
            else:
                LOGGER.info(f"Connected:{node.is_connected()} '{node.name}'")
                if not node.is_connected():
                    # Previously connected node
                    LOGGER.warning(f"Connected:{node.is_connected()} '{node.name}' host is {node.host} same as {dev.host}")
                    await node.connect_a()
        else:
            LOGGER.info(f'found new device {dev.alias}')
            self.add_node(dev=dev)

    def discover_new(self):
        LOGGER.info('enter')
        if not self.ready:
            LOGGER.error("Node is not yet ready")
            return False
        future = asyncio.run_coroutine_threadsafe(self._discover_new_a(), self.mainloop)
        res = future.result()
        LOGGER.debug(f'result={res}')
        LOGGER.info("exit")

    async def _discover_new_a(self):
        await Discover.discover(target=self.poly.network_interface['broadcast'],on_discovered=self.discover_new_add_device)

    # Add a node based on dev returned from discover or the stored config.
    def add_node(self, parent=None, address_suffix_num=None, dev=None, cfg=None):
        LOGGER.debug(f'enter: dev={dev}')
        if parent is None:
            parent = self
        if dev is not None:
            mac  = dev.mac
            if dev.is_bulb:
                type = 'SmartBulb'
                name = dev.alias
            elif dev.is_strip:
                type = 'SmartStrip'
                # SmartStrip doesn't have an alias so use the mac
                name = 'SmartStrip {}'.format(mac)
            elif dev.is_plug:
                type = 'SmartPlug'
                name = dev.alias
            elif dev.is_strip_socket:
                type = 'SmartStripPlug'
                name = dev.alias
            elif dev.is_light_strip:
                type = 'SmartLightStrip'
                name = dev.alias
            elif dev.is_dimmable:
                type = 'SmartDimmer'
                name = dev.alias
            else:
                LOGGER.error(f"What is this? {dev}")
                return False
            if address_suffix_num is None:
                naddress = mac
            else:
                naddress = "{}{:02d}".format(mac,address_suffix_num)
            LOGGER.info(f"Got a {type}")
            cfg  = { "type": type, "name": name, "host": dev.host, "mac": mac, "model": dev.model, "address": get_valid_node_name(naddress)}
        elif cfg is None:
            LOGGER.error(f"INTERNAL ERROR: dev={dev} and cfg={cfg}")
            return False
        LOGGER.info(f"adding {cfg['type']} '{cfg['name']}' {cfg['address']}")
        #
        # Add Based on device type.  SmartStrip is a unique type, all others
        # are handled by SmartDevice
        #
#         LOGGER.error(f"alb:controller.py:{cfg['type']}")
        if cfg['type'] == 'SmartPlug':
            node = self.poly.addNode(SmartPlugNode(self, parent.address, cfg['address'], cfg['name'], dev=dev, cfg=cfg))
        elif cfg['type'] == 'SmartStrip':
            node = self.poly.addNode(SmartStripNode(self, cfg['address'], cfg['name'],  dev=dev, cfg=cfg))
        elif cfg['type'] == 'SmartStripPlug':
            node = self.poly.addNode(SmartStripPlugNode(self, parent.address, cfg['address'], cfg['name'],  dev=dev, cfg=cfg))
        elif cfg['type'] == 'SmartDimmer':
            node = self.poly.addNode(SmartDimmerNode(self, parent.address, cfg['address'], cfg['name'], dev=dev, cfg=cfg))
        elif cfg['type'] == 'SmartBulb':
            node = self.poly.addNode(SmartBulbNode(self, parent.address, cfg['address'], cfg['name'], dev=dev, cfg=cfg))
        elif cfg['type'] == 'SmartLightStrip':
            node = self.poly.addNode(SmartLightStripNode(self, parent.address, cfg['address'], cfg['name'], dev=dev, cfg=cfg))
        else:
            LOGGER.error(f"Device type not yet supported: {cfg['type']}")
            return False
        # We always add it to update the host if necessary
        self.nodes_by_mac[self.smac(cfg['mac'])] = node
        LOGGER.debug(f'exit: dev={dev}')
        return True

    def smac(self,mac):
        return re.sub(r'[:]+', '', mac)

    def exist_device_param(self,mac):
        cparams = self.polyConfig['customParams']
        return True if self.smac(mac) in cparams else False

    def save_cfg(self,cfg):
        LOGGER.debug(f'Saving config: {cfg}')
        js = json.dumps(cfg)
        self.Parameters[self.smac(cfg['mac'])] = js

    def get_device_cfg(self,mac):
        cfg = self.polyConfig['customParams'][self.smac(mac)]
        try:
            cfgd = json.loads(cfg)
        except:
            err = sys.exc_info()[0]
            LOGGER.error(f'failed to parse cfg={cfg} Error: {err}')
            return None
        return cfgd


    def handler_log_level(self,level):
        LOGGER.info(f'enter: level={level}')
        if level['level'] < 10:
            LOGGER.info("Setting basic config to DEBUG...")
            LOG_HANDLER.set_basic_config(True,logging.DEBUG)
        else:
            LOGGER.info("Setting basic config to WARNING...")
            LOG_HANDLER.set_basic_config(True,logging.WARNING)
        LOGGER.info(f'exit: level={level}')

    def delete(self):
        LOGGER.info('Oh No I\'m being deleted. Nooooooooooooooooooooooooooooooooooooooooo.')

    def stop(self):
        LOGGER.debug('NodeServer stopped.')

    def check_params(self):
        pass

    def update_profile(self):
        LOGGER.info('start')
        st = self.poly.installprofile()
        return st

    def _cmd_update_profile(self,command):
        self.update_profile()

    def _cmd_discover(self,cmd):
        self.discover_new()

    id = 'KasaController'
    commands = {
      'QUERY': query,
      'DISCOVER': _cmd_discover,
      'UPDATE_PROFILE': _cmd_update_profile,
    }
    drivers = [
        {'driver': 'ST',  'value':  1, 'uom':  2} ,
    ]
