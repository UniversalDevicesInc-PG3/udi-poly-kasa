#
# TP Link Kasa Smart Device Node
# All Devices are one of these to share the common methods
#
#
import re,asyncio
from udi_interface import Node,LOGGER
from kasa import SmartDeviceException
from converters import myround,bri2st,st2bri

class SmartDeviceNode(Node):

    def __init__(self, controller, primary, address, name, dev=None, cfg=None):
        self.controller = controller
        self.name = name
        self.dev  = dev
        self.cfg  = cfg
        if not hasattr(self,'poll'):
            self.poll = True
        self.pfx = f"{self.name}:"
        LOGGER.debug(f'{self.pfx} dev={dev}')
        LOGGER.debug(f'{self.pfx} cfg={cfg}')
        self.ready = False
        self.ready_warn = False
        self.host = cfg['host']
        self.debug_level = 0
        self.st = None
        self.event  = None
        self.in_long_poll = False
        self.in_short_poll = False
        self.error_connect = False
        self.connected = None # So start will force setting proper status
        LOGGER.debug(f'{self.pfx} controller={controller} address={address} name={name} host={self.host} id={self.id} dev={self.dev} cfg={self.cfg}')
        if (not self.dev is None and self.dev.has_emeter) or (not self.cfg is None and 'emeter' in self.cfg and self.cfg['emeter']):
            self.drivers.append({'driver': 'CC', 'value': 0, 'uom': 1}) #amps
            self.drivers.append({'driver': 'CV', 'value': 0, 'uom': 72}) #volts
            self.drivers.append({'driver': 'CPW', 'value': 0, 'uom': 73}) #watts
            self.drivers.append({'driver': 'TPW', 'value': 0, 'uom': 33}) #kWH
        self.cfg['id'] = self.id
        super().__init__(controller.poly, primary, address, name)
        if self.poll:
            controller.poly.subscribe(controller.poly.POLL,   self.handler_poll)
        controller.poly.subscribe(controller.poly.START,  self.handler_start, address) 
        self.poly.ready()

    def handler_start(self):
        LOGGER.debug(f'enter: {self.name} dev={self.dev}')
        fut = asyncio.run_coroutine_threadsafe(self.connect_a(), self.controller.mainloop)
        res = fut.result()
        LOGGER.debug(f'result:{res} {self.name} dev={self.dev}')
        self.ready = True
        LOGGER.debug(f'exit: {self.name} dev={self.dev}')

    def handler_poll(self, polltype):
        LOGGER.debug(f'{self.pfx} poll={self.poll}')
        if not self.ready:
            LOGGER.warning(f'{self.pfx} Node not ready to poll, must be disconnected or slow to respond?')
            self.ready_warn = True
            return False
        if self.ready_warn:
            LOGGER.warning(f'{self.pfx} Node is now ready to poll')
            self.ready_warn = False
        # Set default for old node servers
        if self.getDriver('GV6') is None:
            self.setDriver('GV6',1)
        if self.getDriver('GV6') == 0:
            LOGGER.debug(f'{self.pfx} Node poll is turned off')
            return
        if polltype == 'longPoll':
            self.longPoll()
        elif polltype == 'shortPoll':
            self.shortPoll()

    def query(self):
        LOGGER.info(f'{self.pfx} enter')
        fut = asyncio.run_coroutine_threadsafe(self.set_state_a(), self.controller.mainloop)
        LOGGER.info(f'{self.pfx} waiting for set_state_a results...')
        res = fut.result()
        LOGGER.info(f'{self.pfx} res={res}')
        self.reportDrivers()
        LOGGER.info(f'{self.pfx} exit')

    async def _query_a(self):
        await self.set_state_a(set_energy=True)
        self.reportDrivers()

    def shortPoll(self):
        LOGGER.debug(f'{self.pfx} enter')
        if self.in_short_poll:
            LOGGER.warning(f'{self.pfx} Already running')
            return
        self.in_short_poll = True
        fut = asyncio.run_coroutine_threadsafe(self._shortPoll_a(), self.controller.mainloop)
        res = fut.result()
        self.in_short_poll = False
        LOGGER.debug(f'{self.pfx} res={res} exit')

    async def _shortPoll_a(self):
        LOGGER.debug(f'{self.pfx} enter: {self.name}')
        if not self.ready:
            return
        if not self.connected:
            LOGGER.debug(f'{self.pfx} Not connected, skipping')
            return
        if await self.connect_a():
            await self.set_state_a(set_energy=False)
        LOGGER.debug(f'{self.pfx} exit: {self.name}')

    def longPoll(self):
        LOGGER.debug(f'{self.pfx} enter')
        if self.in_long_poll:
            LOGGER.warning(f'{self.pfx} Already running')
            return
        self.in_long_poll = True
        fut = asyncio.run_coroutine_threadsafe(self._longPoll_a(), self.controller.mainloop)
        res = fut.result()
        self.in_long_poll = False
        LOGGER.debug(f'{self.pfx} res={res} exit')

    async def _longPoll_a(self):
        if not self.ready:
            return
        if not self.connected:
            LOGGER.info(f'{self.pfx} Not connected, will retry...')
            await self.connect_a()
        if self.connected:
            await self._set_energy_a()

    def connect(self):
        fut = asyncio.run_coroutine_threadsafe(self.connect_a(), self.controller.mainloop)
        return fut.result()

    async def connect_a(self):
        LOGGER.debug(f'{self.pfx} enter: {self.name} dev={self.dev}')
        if not self.is_connected():
            LOGGER.debug(f'{self.pfx} connected={self.is_connected()}')
            try:
                self.dev = self.newdev()
                # We can get a dev, but not really connected, so make sure we are connected.
                res = await self.update_a()
                LOGGER.debug(f'{self.pfx} update res={res}')
                if res:
                    if self.error_connect:
                        LOGGER.warning(f"{self.pfx} Device {self.host} responding again")
                        self.error_connect = False
                    self.set_connected(True)
                    LOGGER.debug(f'{self.pfx} calling reconnected')
                    self.reconnected()
                else:
                    if not self.error_connect:
                        LOGGER.error(f"{self.pfx} Unable to update device {self.host} will try again later: res={res}")
                        self.error_connect = True
            except SmartDeviceException as ex:
                if not self.error_connect:
                    LOGGER.error(f"{self.pfx} Unable to connect to device {self.host} will try again later: {ex}")
                    self.error_connect = True
            except:
                LOGGER.error(f"{self.pfx} Unknown excption connecting to device {self.host} will try again later", exc_info=True)
        LOGGER.debug(f'{self.pfx} exit:{self.connected} {self.name} dev={self.dev}')
        return self.is_connected()

    def set_on(self):
        LOGGER.debug(f'{self.pfx} enter')
        fut = asyncio.run_coroutine_threadsafe(self.set_on_a(), self.controller.mainloop)
        res = fut.result()
        LOGGER.debug(f'{self.pfx} exit result={res}')

    async def set_on_a(self):
        LOGGER.debug(f'{self.pfx} enter')
        await self.dev.turn_on()
        LOGGER.debug(f'{self.pfx} setDriver(ST,100)')
        self.setDriver('ST',100)
        await self.set_state_a(set_energy=True)
        LOGGER.debug(f'{self.pfx} exit')

    def set_off(self):
        LOGGER.debug(f'{self.pfx} enter')
        fut = asyncio.run_coroutine_threadsafe(self.set_off_a(), self.controller.mainloop)
        res = fut.result()
        LOGGER.debug(f'result={res}')
        LOGGER.debug(f'{self.pfx} exit')

    async def set_off_a(self):
        LOGGER.debug(f'{self.pfx} enter')
        await self.dev.turn_off()
        LOGGER.debug(f'{self.pfx} setDriver(ST,0)')
        self.setDriver('ST',0)
        await self.set_state_a(set_energy=True)
        LOGGER.debug(f'{self.pfx} exit')

    def update(self):
        LOGGER.debug(f'enter: {self.name} dev={self.dev}')
        #self.controller.mainloop.run_until_complete(self.update_a())
        fut = asyncio.run_coroutine_threadsafe(self.update_a(), self.controller.mainloop)
        res = fut.result()
        LOGGER.debug(f'exit:{res} {self.name} dev={self.dev}')

    async def update_a(self):
        LOGGER.debug(f'enter: {self.name} dev={self.dev}')
        if self.dev is None:
            if self.connected:
                LOGGER.debug(f"{self.pfx} No device")
                self.set_connected(False)
            LOGGER.debug(f'exit:False {self.name} dev={self.dev}')
            return False
        try:
            await self.dev.update()
            LOGGER.debug(f'exit:True {self.name} dev={self.dev}')
            return True
        except SmartDeviceException as ex:
            if self.connected:
                LOGGER.error(f'{self.pfx} failed: {ex}')
        except Exception as ex:
            if self.connected:
                LOGGER.error(f'{self.pfx} failed', exc_info=True)
        self.set_connected(False)
        LOGGER.debug(f'exit:False {self.name} dev={self.dev}')
        return False

    async def set_state_a(self,set_energy=True):
        try:
            LOGGER.debug(f'{self.pfx} enter: dev={self.dev}')
            # This doesn't call set_energy, since that is only called on long_poll's
            # We don't use self.connected here because dev might be good, but device is unplugged
            # So then when it's plugged back in the same dev will still work
            ocon = self.connected
            if await self.update_a():
                if self.dev.is_on is True:
                    if self.dev.is_dimmable:
                        self.brightness = st2bri(self.dev.brightness)
                        LOGGER.debug(f'{self.pfx} setDriver(ST,{self.dev.brightness})')
                        self.setDriver('ST',self.dev.brightness)
                        self.setDriver('GV5',int(st2bri(self.dev.brightness)))
                    else:
                        self.brightness = 100
                        LOGGER.debug(f'{self.pfx} setDriver(ST,100)')
                        self.setDriver('ST',100)
                else:
                    self.brightness = 0
                    LOGGER.debug(f'{self.pfx} setDriver(ST,0)')
                    self.setDriver('ST',0)
                if self.dev.is_color:
                    hsv = self.dev.hsv
                    self.setDriver('GV3',hsv[0])
                    self.setDriver('GV4',st2bri(hsv[1]))
                    self.setDriver('GV5',st2bri(hsv[2]))
                if self.dev.is_variable_color_temp:
                    self.setDriver('CLITEMP',self.dev.color_temp)

                # This happens when a device is alive on startup, but later disappears, then comes back.
                if not ocon and self.connected:
                    LOGGER.debug(f'{self.pfx} calling reconnected')
                    self.reconnected()
                if set_energy:
                    await self._set_energy_a()
            LOGGER.debug(f'{self.pfx} exit:  dev={self.dev}')
        except Exception as ex:
            LOGGER.error(f'Problem setting device state {self.dev.host}',exc_info=True)

    def is_on(self):
        return self.dev.is_on

    # Called by set_state when device is alive, does nothing by default, inheritance may override
    def set_all_drivers(self):
        pass

    def set_energy(self):
        fut = asyncio.run_coroutine_threadsafe(self._set_energy_au(), self.controller.mainloop)
        res = fut.result()
        LOGGER.debug(f'result={res}')

    async def _set_energy_au(self):
        if await self.update():
            await self._set_energy_a()

    async def _set_energy_a(self):
        if self.dev.has_emeter:
            try:
                energy = self.dev.emeter_realtime
                LOGGER.debug(f'{self.pfx} {energy}')
                if energy is not None:
                    # rounding the values reduces driver updating traffic for
                    # insignificant changes
                    self.setDriver('CC',myround(energy.current,3))
                    self.setDriver('CV',myround(energy.voltage,3))
                    self.setDriver('CPW',myround(energy.power,3))
                    self.setDriver('TPW',myround(energy.total,3))
            except SmartDeviceException as ex:
                LOGGER.error(f'{self.pfx} failed: {ex}')
            except:
                LOGGER.error(f'{self.pfx} failed', exc_info=True)
        else:
            LOGGER.debug(f'{self.pfx} no energy')

    # Called when connected is changed from False to True
    # On initial startup or a reconnect later
    def reconnected(self):
        try:
            self.set_all_drivers()
        except Exception as ex:
            LOGGER.error(f'{self.pfx} set_all_drivers failed: {ex}',exc_info=True)

    def set_connected(self,st):
        # Just return if setting to same status
        if st == self.connected:
            return
        LOGGER.debug(f"{self.pfx} {st}")
        self.connected = st
        self.setDriver('GV0',1 if st else 0)
        if st:
            # Make sure current cfg is saved
            LOGGER.debug(f"{self.pfx} save_cfg {st}")
            self.update()
            try:
                self.cfg['host']  = self.dev.host
                self.cfg['model'] = self.dev.model
                self.controller.save_cfg(self.cfg)
            except SmartDeviceException as ex:
                LOGGER.error(f'{self.pfx} failed: {ex}')
            except:
                LOGGER.error(f'{self.pfx} unknown failure', exc_info=True)

    def is_connected(self):
        return self.connected

    def set_mon(self,val=None):
        LOGGER.debug(f'{self.pfx} val={val}')
        if val is None:
            val = self.getDriver('GV6')
            if val is None:
                val = 1
            LOGGER.debug(f'{self.pfx} val={val}')
        self.setDriver('GV6',val)

    def cmd_set_on(self, command):
        self.set_on()

    def cmd_set_off(self, command):
        self.set_off()

    def cmd_set_mon(self, command):
        val = int(command.get('value'))
        LOGGER.debug(f'{self.pfx} val={val}')
        self.set_mon(val)
