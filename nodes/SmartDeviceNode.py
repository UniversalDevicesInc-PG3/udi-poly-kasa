#
# TP Link Kasa Smart Device Node
# All Devices are one of these to share the common methods
#
#
import re,asyncio
import polyinterface
from kasa import SmartDeviceException
from converters import bri2st,st2bri

LOGGER = polyinterface.LOGGER

class SmartDeviceNode(polyinterface.Node):

    def __init__(self, controller, parent_address, address, name, dev, cfg):
        self.controller = controller
        self.name = name
        self.dev  = dev
        self.cfg  = cfg
        self.poll = True
        self.pfx = f"{self.name}:"
        LOGGER.debug(f'{self.pfx} dev={dev}')
        LOGGER.debug(f'{self.pfx} cfg={cfg}')
        self.ready = False
        self.host = cfg['host']
        self.debug_level = 0
        self.st = None
        self.event  = None
        self.connected = None # So start will force setting proper status
        LOGGER.debug(f'{self.pfx} controller={controller} address={address} name={name} host={self.host} id={self.id}')
        if not self.dev is None and self.dev.has_emeter:
            self.drivers.append({'driver': 'CC', 'value': 0, 'uom': 1}) #amps
            self.drivers.append({'driver': 'CV', 'value': 0, 'uom': 72}) #volts
            self.drivers.append({'driver': 'CPW', 'value': 0, 'uom': 73}) #watts
            self.drivers.append({'driver': 'TPW', 'value': 0, 'uom': 33}) #kWH
        self.cfg['id'] = self.id
        super().__init__(controller, parent_address, address, name)

    def start(self):
        LOGGER.debug(f'enter: {self.name} dev={self.dev}')
        fut = asyncio.run_coroutine_threadsafe(self.connect_and_update_a(), self.controller.mainloop)
        res = fut.result()
        LOGGER.debug(f'result:{res} {self.name} dev={self.dev}')
        self.ready = True
        LOGGER.debug(f'exit: {self.name} dev={self.dev}')

    def query(self):
        fut = asyncio.run_coroutine_threadsafe(self._query_a(), self.controller.mainloop)
        return fut.result()

    async def _query_a(self):
        await self.set_state_a(set_energy=True)
        self.reportDrivers()

    async def shortPoll(self):
        LOGGER.debug(f'enter: {self.name}')
        if not self.ready:
            return
        # Keep trying to connect if possible
        if await self.connect_a():
            await self.set_state_a()
        LOGGER.debug(f'exit: {self.name}')

    async def longPoll(self):
        if not self.connected:
            LOGGER.info(f'{self.pfx} Not connected, will retry...')
            await self.connect_a()
        if self.connected:
            await self._set_energy_a()

    async def connect_and_update_a(self):
        await self.connect_a()
        await self.set_state_a()

    def connect(self):
        fut = asyncio.run_coroutine_threadsafe(self.connect_a(), self.controller.mainloop)
        return fut.result()

    async def connect_a(self):
        LOGGER.debug(f'enter: {self.name} dev={self.dev}')
        if not self.is_connected():
            LOGGER.debug(f'{self.pfx} connected={self.is_connected()}')
            try:
                self.dev = self.newdev()
                # We can get a dev, but not really connected, so make sure we are connected.
                await self._update_a()
                sys_info = self.dev.sys_info
                self.set_connected(True)
            except SmartDeviceException as ex:
                LOGGER.error(f"{self.pfx} Unable to connect to device '{self.name}' {self.host} will try again later: {ex}")
                self.set_connected(False)
            except:
                LOGGER.error(f"{self.pfx} Unknown excption connecting to device '{self.name}' {self.host} will try again later", exc_info=True)
                self.set_connected(False)
        LOGGER.debug(f'exit:{self.connected} {self.name} dev={self.dev}')
        return self.is_connected

    def set_on(self):
        LOGGER.debug(f'{self.pfx} enter')
        fut = asyncio.run_coroutine_threadsafe(self.set_on_a(), self.controller.mainloop)
        res = fut.result()
        LOGGER.debug(f'exit result={res}')

    async def set_on_a(self):
        LOGGER.debug(f'{self.pfx} enter')
        await self.dev.turn_on()
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
        await self.set_state_a(set_energy=True)
        LOGGER.debug(f'{self.pfx} exit')

    def update(self):
        LOGGER.debug(f'enter: {self.name} dev={self.dev}')
        #self.controller.mainloop.run_until_complete(self._update_a())
        fut = asyncio.run_coroutine_threadsafe(self._update_a(), self.controller.mainloop)
        res = fut.result()
        LOGGER.debug(f'exit:{res} {self.name} dev={self.dev}')

    async def _update_a(self):
        LOGGER.debug(f'enter: {self.name} dev={self.dev}')
        if self.dev is None:
            if self.connected:
                LOGGER.debug(f"{self.pfx} No device")
                self.set_connected(False)
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
        LOGGER.debug(f'enter: dev={self.dev}')
        # This doesn't call set_energy, since that is only called on long_poll's
        # We don't use self.connected here because dev might be good, but device is unplugged
        # So then when it's plugged back in the same dev will still work
        if await self._update_a():
            ocon = self.connected
            if self.dev.is_on is True:
                if self.dev.is_dimmable:
                    self.brightness = st2bri(self.dev.brightness)
                    self.setDriver('ST',self.dev.brightness)
                    self.setDriver('GV5',int(st2bri(self.dev.brightness)))
                else:
                    self.brightness = 100
                    self.setDriver('ST',100)
            else:
                self.brightness = 0
                self.setDriver('ST',0)
            if self.dev.is_color:
                hsv = self.hsv()
                self.setDriver('GV3',hsv[0])
                self.setDriver('GV4',st2bri(hsv[1]))
                self.setDriver('GV5',st2bri(hsv[2]))
            if self.dev.is_variable_color_temp:
                self.setDriver('CLITEMP',self.dev.color_temp)

            # On restore, or initial startup, set all drivers.
            if not ocon and self.connected:
                try:
                    self.set_all_drivers()
                except Exception as ex:
                    LOGGER.error(f'{self.pfx} set_all_drivers failed: {ex}',exc_info=True)
            if set_energy:
                await self._set_energy_a()
        LOGGER.debug(f'exit:  dev={self.dev}')

    # Called by set_state when device is alive, does nothing by default, enheritance may override
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
                    self.setDriver('CC',round(energy.current,3))
                    self.setDriver('CV',round(energy.voltage,3))
                    self.setDriver('CPW',round(energy.power,3))
                    self.setDriver('TPW',round(energy.total,3))
            except SmartDeviceException as ex:
                LOGGER.error(f'{self.pfx} failed: {ex}')
            except:
                LOGGER.error(f'{self.pfx} failed', exc_info=True)
        else:
            LOGGER.debug(f'{self.pfx} no energy')

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

    def cmd_set_on(self, command):
        self.set_on()

    def cmd_set_off(self, command):
        self.set_off()
