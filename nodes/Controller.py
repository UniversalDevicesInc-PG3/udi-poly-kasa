
from udi_interface import Node,LOGGER,Custom,LOG_HANDLER
import logging,re,json,sys,asyncio,time,os,markdown2
from threading import Thread,Event
from node_funcs import get_valid_node_name,get_valid_node_address
import kasa
from kasa.exceptions import *
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
        self.ready   = False
        self.hb = 0
        self.nodes_by_mac = {}
        self.discover_done = False
        self.in_long_poll  = False
        self.credential_error = False
        self.Notices         = Custom(self.poly, 'notices')
        self.Parameters      = Custom(self.poly, 'customparams')
        self.handler_params_st = None
        self.Data            = Custom(self.poly, 'customdata')
        self.handler_data_st = None
        self.TypedParameters = Custom(poly, 'customtypedparams')
        self.handler_typedparams_st = None
        self.TypedData       = Custom(poly, 'customtypeddata')
        self.handler_typeddata_st = None
        self.poly.subscribe(self.poly.START,                  self.handler_start, address) 
        self.poly.subscribe(self.poly.POLL,                   self.handler_poll)
        self.poly.subscribe(self.poly.LOGLEVEL,               self.handler_log_level)
        self.poly.subscribe(self.poly.CONFIGDONE,             self.handler_config_done)
        self.poly.subscribe(self.poly.CUSTOMPARAMS,           self.handler_params)
        self.poly.subscribe(self.poly.CUSTOMDATA,             self.handler_data)
        self.poly.subscribe(self.poly.DISCOVER,               self.discover_new)
        self.poly.subscribe(poly.CUSTOMTYPEDPARAMS,           self.handler_typed_params)
        self.poly.subscribe(poly.CUSTOMTYPEDDATA,             self.handler_typed_data)
        self.poly.ready()
        self.poly.addNode(self, conn_status='ST')

    def handler_start(self):
        LOGGER.info(f"Started Kasa PG3 NodeServer {self.poly.serverdata['version']}")
        LOGGER.info(f"Kasa Library Version {kasa.__version__}")
        self.update_profile()
        self.Notices.clear()
        self.mainloop = mainloop
        asyncio.set_event_loop(mainloop)
        self.connect_thread = Thread(target=mainloop.run_forever)
        self.connect_thread.start()
        self.setDriver('ST', 1)
        self.heartbeat()
        self.set_params()
        self.check_params()
        configurationHelp = './CONFIG.md'
        if os.path.isfile(configurationHelp):
            cfgdoc = markdown2.markdown_path(configurationHelp)
            self.poly.setCustomParamsDoc(cfgdoc)
        #
        # Wait for all handlers to finish
        #
        cnt = 600
        while ((self.handler_params_st is None or self.handler_data_st is None or self.handler_typedparams_st is None or self.handler_typeddata_st is None) and cnt > 0):
            LOGGER.warning(f'Waiting for all to be loaded params={self.handler_params_st} data={self.handler_data_st}... cnt={cnt}')
            time.sleep(1)
            cnt -= 1
        if cnt == 0:
            LOGGER.error("Timed out waiting for handlers to startup")
            #self.exit()
        # Discover
        try:
            self.discover()
        except:
            LOGGER.error(f'discover failed', exc_info=True)
        try:
            self.add_manual_devices()
        except:
            LOGGER.error(f'add_manual_devices failed', exc_info=True)
        self.ready = True
        LOGGER.info(f'exit {self.name}')

    # For things we only do when have the configuration is loaded...
    def handler_config_done(self):
        LOGGER.debug(f'enter')
        self.poly.addLogLevel('DEBUG_MODULES',9,'Debug + Modules')
        LOGGER.debug(f'exit')

    # Controller only needs longPoll
    def handler_poll(self, polltype):
        LOGGER.debug('enter')
        if polltype == 'longPoll':
            self.longPoll()
        LOGGER.debug('exit')

    def longPoll(self):
        LOGGER.debug('enter')
        if not self.discover_done:
            LOGGER.info('waiting for discover to complete')
            return
        if self.in_long_poll:
            LOGGER.info('Already running')
            return
        self.in_long_poll = True
        # Heartbeat is not sent if stuck in discover or long_poll?
        self.heartbeat()
        if self.auto_discover:
            self.discover_new()
        else:
            LOGGER.debug(f'auto_discover disabled {self.auto_discover}')
        self.in_long_poll = False
        LOGGER.debug('exit')

    def query(self):
        self.setDriver('ST', 1)
        self.reportDrivers()
        self.check_params()

    def heartbeat(self):
        LOGGER.debug(f'hb={self.hb}')
        if self.hb == 0:
            self.reportCmd("DON",2)
            self.hb = 1
        else:
            self.reportCmd("DOF",2)
            self.hb = 0

    def add_manual_devices(self):
        if self.manual_devices is None or len(self.manual_devices) == 0:
            LOGGER.info("No manual devices configured")
            return
        future = asyncio.run_coroutine_threadsafe(self._add_manual_devices(), self.mainloop)
        res = future.result()
        LOGGER.debug(f'result={res}')
        self.discover_done = True
        LOGGER.info("exit")

    async def _add_manual_devices(self):
        for mdev in self.manual_devices:
            LOGGER.info(f"Adding manual device {mdev['address']}")
            try:
                dev = await self.discover_single(address=mdev['address'])
                self.add_device_node(dev=dev)
            except Exception as ex:
                LOGGER.error(f"{ex} trying to connect to {mdev['address']}",exc_info=False) 
        
    def discover(self):
        self.devm = {}
        LOGGER.info(f"enter: {self.poly.network_interface['broadcast']}")
        future = asyncio.run_coroutine_threadsafe(self._discover(target=self.poly.network_interface['broadcast']), self.mainloop)
        res = future.result()
        LOGGER.debug(f'result={res}')
        if self.manual_networks is None or len(self.manual_networks) == 0:
            LOGGER.info("No manual networks configured")
        else:
            for network in self.manual_networks:
                LOGGER.info(f"calling: _discover(target={network['address']})")
                future = asyncio.run_coroutine_threadsafe(self._discover(target=network['address']), self.mainloop)
                res = future.result()
                LOGGER.debug(f'result={res}')
        self.discover_done = True
        LOGGER.info("exit")

    # We have this in controller so all error handling is in one
    # place and we need ability to update device before the node
    # is created.  The SmartDeviceNode calls this update.
    async def update_dev(self,dev):
        LOGGER.debug(f"enter: {dev}")
        ret = False
        try:
            await dev.update()
            ret = True
        except AuthenticationError as msg:
            LOGGER.error(f'Failed to authenticate {dev}: {msg}')
        except Exception as ex:
            LOGGER.error(f"Failed to update {ex}: {dev}", exc_info=True)
        LOGGER.debug(f'update_dev:exit:{ret} {dev}')
        LOGGER.debug(f"exit:{ret} {dev}")
        return ret

    async def discover_add_device(self,dev):
        LOGGER.debug(f"enter: {dev}")
        LOGGER.info(f"Got Device\n\tAlias:{dev.alias}\n\tModel:{dev.model}\n\tMac:{dev.mac}\n\tHost:{dev.host}")
        if not await self.update_dev(dev):
            return False
        self.add_device_node(dev=dev)
        # Add to our list of added devices
        self.devm[self.smac(dev.mac)] = True
        LOGGER.debug(f"exit: {dev}")
        return True

    async def _discover(self,target):
        LOGGER.debug(f'enter: target={target}')
        await kasa.Discover.discover(
            credentials=self.credentials,
            timeout=self.discover_timeout,
            discovery_packets=10,
            target=target,
            on_discovered=self.discover_add_device
            )
        # make sure all we know about are added in case they didn't respond this time.
        LOGGER.info(f"kasa.Discover.discover({target}) done: checking for previously known devices")
        for mac in self.Data:
            LOGGER.debug(f'checking mac={mac}')
            if self.smac(mac) in self.devm:
                LOGGER.debug(f'already added mac={mac}')
            else:
                cfg = self.get_device_cfg(mac)
                LOGGER.debug(f'cfg={cfg}')
                if cfg is not None:
                    # If it's not not in the DB, then user deleted it, so don't add it back.
                    cname = self.poly.getNodeNameFromDb(cfg['address'])
                    if cname is None:                    
                        LOGGER.warning(f"NOT adding previously known device that didn't respond to discover because it was deleted from PG3: {cfg}")
                    else:
                        LOGGER.warning(f"Adding previously known device that didn't respond to discover: {cfg}")
                        self.add_device_node(cfg=cfg)
        LOGGER.debug('exit')
        #return True

    async def discover_new_add_device(self,dev):
        try:
            LOGGER.debug(f'enter: host={dev.host}')
            smac = self.smac(dev.mac)
            LOGGER.debug(f'enter: mac={smac} dev={dev}')
            # Known Device?
            if not await self.update_dev(dev):
                return False
            LOGGER.debug(f'mac={smac} dev={dev}')
            if smac in self.nodes_by_mac:
                node = self.nodes_by_mac[smac]
                # Make sure the host matches
                if dev.host != node.host:
                    LOGGER.warning(f"Updating '{node.name}' host from {node.host} to {dev.host}")
                    node.host = dev.host
                    await node.connect_a()
                else:
                    LOGGER.info(f"Connected:{node.is_connected()} '{node.name}'")
                    if not node.is_connected():
                        # Previously connected node
                        LOGGER.warning(f"Connected:{node.is_connected()} '{node.name}' host is {node.host} same as {dev.host}")
                        await node.connect_a()
            else:
                LOGGER.warning(f'Found a new device {dev.mac}, adding {dev.alias}')
                self.add_device_node(dev=dev)
        except Exception as ex:
            LOGGER.error(f'{ex} {dev}',exc_info=True)
            
    async def discover_single(self, host=None):
        LOGGER.debug(f'enter: host={host}')
        dev = await kasa.Discover.discover_single(
            host,
            credentials=self.credentials,
            )
        LOGGER.debug(f'exit: dev={dev}')
        return dev


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
        await kasa.Discover.discover(
            credentials=self.credentials,
            target=self.poly.network_interface['broadcast'],
            on_discovered=self.discover_new_add_device
            )

    # Add a node based on dev returned from discover or the stored config.
    def add_device_node(self, parent=None, address_suffix_num=None, dev=None, cfg=None):
        LOGGER.debug(f'enter: dev={dev}')
        if parent is None:
            parent = self
        if dev is not None:
            mac  = dev.mac
            type = str(dev.device_type)
            if hasattr(dev,'alias'):
                name = dev.alias 
            elif dev.is_strip:
                # SmartStrip doesn't have an alias so use the mac
                name = get_valid_node_name(f'SmartStrip {mac}')
            else:
                LOGGER.error(f"What is this device with no alias? {dev}")
                return False
            LOGGER.info(f"Got a {type}: {dev}")
            if address_suffix_num is None:
                address = get_valid_node_address(mac)
            else:
                address = get_valid_node_address("{}{:02d}".format(mac,address_suffix_num))
            cfg  = { "type": type, "name": get_valid_node_name(name), "host": dev.host, "mac": mac, "model": dev.model, "address": address}
        elif cfg is not None:
            name = cfg['name']
        else:
            LOGGER.error(f"INTERNAL ERROR: dev={dev} and cfg={cfg}")
            return False
        LOGGER.info(f"adding type={cfg['type']} address={cfg['address']} name='{cfg['name']}' ")
        #
        # Add Based on device type.  SmartStrip is a unique type, all others
        # are handled by SmartDevice
        #
        if cfg['name'] is None:
            LOGGER.error(f'Refusing to add node with name None!')
            return False
        try:
            if cfg['type'] == 'SmartPlug' or cfg['type'] == 'DeviceType.Plug':
                self.add_node(cfg['address'],SmartPlugNode(self, parent.address, cfg['address'], cfg['name'], dev=dev, cfg=cfg))
            elif cfg['type'] == 'SmartStrip' or cfg['type'] == 'DeviceType.Strip':
                self.add_node(cfg['address'],SmartStripNode(self, cfg['address'], cfg['name'],  dev=dev, cfg=cfg))
            elif cfg['type'] == 'SmartStripPlug' or cfg['type'] == 'DeviceType.StripSocket':
                self.add_node(cfg['address'],SmartStripPlugNode(self, parent.address, cfg['address'], cfg['name'],  dev=dev, cfg=cfg))
            elif cfg['type'] == 'SmartDimmer' or cfg['type'] == 'DeviceType.WallSwitch':
                self.add_node(cfg['address'],SmartDimmerNode(self, parent.address, cfg['address'], cfg['name'], dev=dev, cfg=cfg))
            elif cfg['type'] == 'SmartBulb' or cfg['type'] == 'DeviceType.Bulb':
                self.add_node(cfg['address'],SmartBulbNode(self, parent.address, cfg['address'], cfg['name'], dev=dev, cfg=cfg))
            elif cfg['type'] == 'SmartLightStrip' or cfg['type'] == 'DeviceType.LightStrip':
                self.add_node(cfg['address'],SmartLightStripNode(self, parent.address, cfg['address'], cfg['name'], dev=dev, cfg=cfg))
            else:
                LOGGER.error(f"Device type not yet supported: {cfg['type']}")
                return False
        except:
                LOGGER.error(f'Failed adding dev={dev}', exc_info=True)
        node = self.poly.getNode(cfg['address'])
        if node is None:
            LOGGER.error(f"Unable to retrieve node address {cfg['address']} for {type} returned {node}")
        else:
            self.nodes_by_mac[self.smac(cfg['mac'])] = node
        LOGGER.debug(f'exit: dev={dev}')
        return node

    def add_node(self,address,node):
        LOGGER.debug(f"Adding: {node.name}")
        self.poly.addNode(node)
        #self.wait_for_node_done()
        gnode = self.poly.getNode(address)
        if gnode is None:
            msg = f'Failed to add node address {address}'
            LOGGER.error(msg)
            #self.inc_error(msg)
        else:
            # See if we need to check for node name changes where Kasa is the source
            cname = self.poly.getNodeNameFromDb(address)
            if cname is not None:
                LOGGER.debug(f"node {address} Requested: '{node.name}' Current: '{cname}'")
                # Check that the name matches
                if node.name != cname:
                    if self.change_node_names:
                        LOGGER.warning(f"Existing node name '{cname}' for {address} does not match requested name '{node.name}', changing to match")
                        try:
                            self.poly.renameNode(address,node.name)
                        except:
                            LOGGER.error(f'renameNode error, which is a known issue with PG3x Version <= 3.2.7', exc_info=True)
                    else:
                        LOGGER.warning(f"Existing node name '{cname}' for {address} does not match requested name '{node.name}', NOT changing to match, set change_node_names=true to enable")
                        # Change it to existing name to avoid addNode error
                        node.name = cname
        return gnode

    def smac(self,mac):
        return re.sub(r'[:]+', '', mac)

    def exist_device_param(self,mac):
        return True if self.smac(mac) in self.Data else False

    def save_cfg(self,cfg):
        mac = self.smac(cfg['mac'])
        LOGGER.debug(f'Saving config for mac: {mac}: {cfg}')
        self.Data[mac] = json.dumps(cfg)

    def get_device_cfg(self,mac):
        return(self.cfg_to_dict(self.Data[self.smac(mac)]))
 
    def cfg_to_dict(self,cfg):
        try:
            cfgd = json.loads(cfg)
        except:
            err = sys.exc_info()[0]
            LOGGER.error(f'failed to parse cfg={cfg} Error: {err}')
            return None
        return cfgd

    def handler_data(self,data):
        LOGGER.debug(f'enter: Loading data {data}')
        if data is None:
            LOGGER.warning("No custom data")
        else:
            self.Data.load(data)
        self.handler_data_st = True

    def handler_params(self,params):
        LOGGER.debug(f'enter: Loading typed data now {params}')
        self.Parameters.load(params)
        #
        # Make sure params exist
        #
        defaults = {
            "change_node_names": "false",
            "discover_timeout": 10,
            "auto_discover": "true",
            'user': '',
            'password': "",
        }
        for param in defaults:
            if params is None or not param in params:
                self.Parameters[param] = defaults[param]
                return
        #
        # Move Old Params with just the mac to Data
        # Wait for data to be loaded.
        #
        cnt = 300
        while ((self.handler_data_st is None) and cnt > 0):
            LOGGER.warning(f'Waiting for Data to be loaded data={self.handler_data_st}... cnt={cnt}')
            time.sleep(1)
            cnt -= 1
        if cnt == 0:
            LOGGER.error("Timed out waiting for data to be loaded")
            #self.exit()

        for param in self.Parameters:
            if not (param in defaults):
                data = self.Parameters[param]
                LOGGER.debug(f'Transfering from parms to data: {data}')
                self.save_cfg(self.cfg_to_dict(data))
                self.Parameters.delete(param)
                return

        self.change_node_names = True if self.Parameters['change_node_names'] == 'true' else False
        self.auto_discover     = True if self.Parameters['auto_discover']     == 'true' else False
        self.discover_timeout  = self.Parameters['discover_timeout']
        #
        # Build our credentials
        #
        if ( self.Parameters['user'] == "" or self.Parameters['password'] == ""):
            if not self.credential_error:
                msg = f"Must Enter Kasa user and password if using newer devices, use none/none if you don't have any"
                self.poly.Notices['credentials'] = msg
                LOGGER.error(msg)
            self.credential_error = True
            self.credentials = kasa.Credentials('none','none')
        else:
            if self.credential_error:
                self.poly.Notices.delete('credentials')
            self.credential_error = False
            self.credentials = kasa.Credentials(self.Parameters['user'],self.Parameters['password'])
        #self.check_params()
        self.handler_params_st = True

    def set_params(self):
        self.TypedParameters.load( 
            [
                {
                    'name': 'devices',
                    'title': 'Kasa Devices',
                    'desc': 'Allow adding Kasa Devices manually',
                    'isList': True,
                    'params': [
                        {
                            'name': 'address',
                            'title': "Device host or IP",
                            'isRequired': True,
                        },
                    ]
                },
                {
                    'name': 'networks',
                    'title': 'Extra Discovery Networks',
                    'desc': 'Allow specifying other networks to run discovery',
                    'isList': True,
                    'params': [
                        {
                            'name': 'address',
                            'title': "Broadcast Address",
                            'isRequired': True,
                        },
                    ]
                },
            ], True)

    def handler_typed_params(self,params):
        LOGGER.debug(f'Loading typed params now {params}')
        self.handler_typedparams_st = True
        return

    def handler_typed_data(self,params):
        LOGGER.debug(f'Loading typed data now {params}')
        self.TypedData.load(params)
        LOGGER.debug(params)

        self.manual_devices  = self.TypedData['devices']
        self.manual_networks = self.TypedData['networks']
        # We don't add on initial startup, wait for all startup to finish
        if (self.ready):
            # devices were changed after node server was restarted, so add them.
            self.add_manual_devices()
        self.handler_typeddata_st = True

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

    def check_params(self):
        pass

    def update_profile(self):
        LOGGER.info('start')
        return self.poly.updateProfile()

    def _cmd_query_all(self,command):
        self.query()
        for node_address in self.poly.getNodes():
            node = self.poly.getNode(node_address)
            if node.poll:
                node.query()

    def _cmd_update_profile(self,command):
        self.update_profile()

    def _cmd_discover(self,cmd):
        self.discover_new()

    id = 'KasaController'
    commands = {
      'QUERY': query,
      'QUERY_ALL': _cmd_query_all,
      'DISCOVER': _cmd_discover,
      'UPDATE_PROFILE': _cmd_update_profile,
    }
    drivers = [
        {'driver': 'ST',  'value':  1, 'uom':  25, 'name': 'NodeServer Online'} ,
    ]
