#
# TP Link Kasa Smart Device Node
# All Devices are one of these to share the common methods
#
#
import re,asyncio,time
from concurrent.futures import TimeoutError as FutureTimeoutError
from udi_interface import Node,LOGGER
from kasa import SmartDeviceException
from converters import myround,bri2st,st2bri

class SmartDeviceNode(Node):

    def __init__(self, controller, primary, address, name, dev=None, cfg=None):
        self.controller = controller
        self.name = name
        self.dev  = dev
        self.cfg  = cfg
        # All devices call poll by default
        if not hasattr(self,'poll'):
            self.poll = True
        if not hasattr(self,'pfx'):
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
        self._short_poll_busy_logged = False
        self._long_poll_busy_logged = False
        self._poll_disabled_logged = False
        self._ready_wait_logged = False
        self.error_connect = False
        self.connected = None # So start will force setting proper status
        LOGGER.debug(f'{self.pfx} controller={controller} address={address} name={name} host={self.host} id={self.id} dev={self.dev} cfg={self.cfg}')
        if (not self.dev is None and self.dev.has_emeter) or (not self.cfg is None and 'emeter' in self.cfg and self.cfg['emeter']):
            self.drivers.append({'driver': 'CC', 'value': 0, 'uom': 1, 'name': 'Current Current'})
            self.drivers.append({'driver': 'CV', 'value': 0, 'uom': 72, 'name': 'Current Voltage'})
            self.drivers.append({'driver': 'CPW', 'value': 0, 'uom': 73, 'name': 'Current Power Watts'})
            self.drivers.append({'driver': 'TPW', 'value': 0, 'uom': 33, 'name': 'Total Energy kWh'})
        self.cfg['id'] = self.id
        super().__init__(controller.poly, primary, address, name)
        if self.poll:
            controller.poly.subscribe(controller.poly.POLL,   self.handler_poll)
        controller.poly.subscribe(controller.poly.START,  self.handler_start, address) 
        controller.poly.subscribe(controller.poly.DELETE,  self.handler_delete) 
        self.poly.ready()

    def handler_start(self):
        LOGGER.debug(f'enter: {self.name} dev={self.dev}')
        res = self.connect()
        LOGGER.debug(f'result:{res} {self.name} dev={self.dev}')
        self.ready = True
        LOGGER.debug(f'exit: {self.name} dev={self.dev}')

    def handler_poll(self, polltype):
        if self.get_mon == 0:
            if not self._poll_disabled_logged:
                LOGGER.info(f'{self.pfx} Node poll is turned off')
                self._poll_disabled_logged = True
            return
        self._poll_disabled_logged = False
        if not self.ready:
            if not self._ready_wait_logged:
                LOGGER.warning(f'{self.pfx} Node not ready to poll, waiting for initialization')
                self._ready_wait_logged = True
            self.ready_warn = True
            return False
        self._ready_wait_logged = False
        if self.ready_warn:
            if self.is_connected():
                LOGGER.warning(f'{self.pfx} Node is now ready to poll')
            else:
                LOGGER.warning(f'{self.pfx} Node is ready to poll, but Kasa device is not responding')
            self.ready_warn = False
        if polltype == 'longPoll':
            self.longPoll()
        elif polltype == 'shortPoll':
            self.shortPoll()

    def handler_delete(self):
        LOGGER.warning(f'{self.pfx} address={self.adress}')

    def _run_coro(self, coro, label, timeout=None, default=None):
        """Run an asyncio coroutine on the controller mainloop, time it, and
        bound it. Logs a warning when the call takes longer than the
        controller's slow-future threshold so we can spot mainloop pressure
        before it escalates to a watchdog kill.

        Any exception raised by the coroutine is caught and logged here.
        Re-raising into the caller is what handler_poll runs into, and an
        un-caught exception there terminates the polling thread and dumps
        a multi-line annotated traceback to stderr; udi_interface's stderr
        capture then writes one ERROR line per character (~27k lines for a
        single Python 3.11 traceback) which buries the actual signal in
        the log. Returning `default` keeps the polling thread alive.
        """
        if timeout is None:
            timeout = self.controller.async_future_timeout
        threshold = getattr(self.controller, 'slow_future_warn_threshold', 5)
        fut = asyncio.run_coroutine_threadsafe(coro, self.controller.mainloop)
        start = time.monotonic()
        try:
            res = fut.result(timeout=timeout)
        except FutureTimeoutError:
            elapsed = time.monotonic() - start
            LOGGER.error(
                '%s %s timed out after %ss (waited %.1fs)',
                self.pfx,
                label,
                timeout,
                elapsed,
                exc_info=True,
            )
            return default
        except Exception as ex:  # noqa: BLE001
            elapsed = time.monotonic() - start
            LOGGER.error(
                '%s %s raised %s after %.1fs; returning default to keep '
                'polling thread alive: %s',
                self.pfx,
                label,
                type(ex).__name__,
                elapsed,
                ex,
                exc_info=True,
            )
            return default
        elapsed = time.monotonic() - start
        if elapsed >= threshold:
            LOGGER.warning(
                '%s %s took %.1fs (>= %ss); mainloop is under pressure',
                self.pfx,
                label,
                elapsed,
                threshold,
            )
        return res

    def query(self):
        LOGGER.info(f'{self.pfx} enter')
        LOGGER.info(f'{self.pfx} waiting for set_state_a results...')
        res = self._run_coro(self.set_state_a(), 'query/set_state_a')
        LOGGER.info(f'{self.pfx} res={res}')
        self.reportDrivers()
        LOGGER.info(f'{self.pfx} exit')

    async def _query_a(self):
        await self.set_state_a(set_energy=True)
        self.reportDrivers()

    def shortPoll(self):
        if self.in_short_poll:
            if not self._short_poll_busy_logged:
                LOGGER.warning(f'{self.pfx} shortPoll already running, skipping this cycle')
                self._short_poll_busy_logged = True
            return
        self._short_poll_busy_logged = False
        self.in_short_poll = True
        try:
            res = self._run_coro(self._shortPoll_a(), '_shortPoll_a')
        finally:
            self.in_short_poll = False
        LOGGER.debug(f'{self.pfx} shortPoll result={res}')

    async def _shortPoll_a(self):
        LOGGER.debug(f'{self.pfx} enter: {self.name}')
        if not self.ready:
            LOGGER.warning(f'{self.pfx} Not ready, skipping')
            return
        if not self.connected:
            # When the host is circuit-broken, opportunistically TCP
            # probe to detect it coming back online without paying the
            # full 5-12s kasa protocol timeout. host_should_quick_probe
            # gates this to once per host_quick_probe_interval (default
            # 30s) so a wall of offline hosts can't dominate the
            # mainloop. On alive, host_quick_probe resets the breaker
            # and we fall through to connect_a; on dead, return silently
            # (no per-poll spam).
            if self.controller.host_should_quick_probe(self.host):
                if not await self.controller.host_quick_probe(self.host):
                    LOGGER.debug(f'{self.pfx} quick-probe says down, skipping')
                    return
                LOGGER.debug(
                    f'{self.pfx} quick-probe says alive; falling through to '
                    f'connect_a (breaker-reset INFO will follow)'
                )
            else:
                LOGGER.debug(f'{self.pfx} Not connected, skipping')
                return
        if await self.connect_a():
            await self.set_state_a(set_energy=False)
        LOGGER.debug(f'{self.pfx} exit: {self.name}')

    def longPoll(self):
        if self.in_long_poll:
            if not self._long_poll_busy_logged:
                LOGGER.warning(f'{self.pfx} longPoll already running, skipping this cycle')
                self._long_poll_busy_logged = True
            return
        self._long_poll_busy_logged = False
        self.in_long_poll = True
        try:
            res = self._run_coro(self._longPoll_a(), '_longPoll_a')
        finally:
            self.in_long_poll = False
        LOGGER.debug(f'{self.pfx} longPoll result={res}')

    async def _longPoll_a(self):
        if not self.ready:
            LOGGER.warning(f'{self.pfx} Not ready, skipping')
            return
        # When a host is circuit-broken, skip the longPoll entirely.
        # Without this gate, an offline host pays a ~12s kasa-protocol
        # TCP timeout per long-poll (every 4 minutes), generating the
        # `Failed to update ... Host is down` ERROR spam and the
        # `mainloop is under pressure` warnings. shortPoll's TCP probe
        # is responsible for detecting the host coming back online.
        if self.controller.host_should_skip(self.host):
            LOGGER.debug(
                f'{self.pfx} skipping longPoll; host {self.host} circuit-broken'
            )
            return
        if not self.connected:
            LOGGER.info(f'{self.pfx} Not connected, will retry...')
            await self.connect_a()
        if self.connected:
            await self._set_energy_a()

    def connect(self):
        if self.is_connected():
            return True
        return bool(self._run_coro(self.connect_a(), 'connect_a', default=False))

    async def connect_a(self):
        LOGGER.debug(f'{self.pfx} enter: {self.name} dev={self.dev}')
        if not self.is_connected():
            LOGGER.debug(f'{self.pfx} connected={self.is_connected()}')
            # When the controller's circuit breaker has marked this host as
            # repeatedly unreachable, don't pay the discovery/update timeout
            # cost again until next_try elapses, the longPoll re-test wins,
            # or shortPoll's TCP probe resets the breaker. Apply this
            # regardless of whether self.dev is set; an existing Device
            # whose host is offline still pays a 5-12s timeout in
            # update_a -> dev.update() and is the dominant source of
            # `Failed to update ... Host is down` log spam.
            if self.controller.host_should_skip(self.host):
                LOGGER.debug(f'{self.pfx} skipping connect; host circuit-broken')
                self.set_connected(False)
                return False
            try:
                # Try to discover if we don't have it
                if self.dev is None:
                    # If found, discover will connect it.
                    self.dev = await self.controller.discover_single(host=self.cfg['host'])
                if self.dev is None:
                    self.set_connected(False,f"{self.pfx} Unable to discover {self.host}")
                else:
                    res = await self.update_a()
                    LOGGER.debug(f'{self.pfx} update res={res}')
                    if res:
                        self.set_connected(True)
                        LOGGER.debug(f'{self.pfx} calling reconnected')
                        self.reconnected()
                    else:
                        self.set_connected(
                            False,
                            f"{self.pfx} Unable to update device {self.host} will try again later: res={res}"
                        )
            except SmartDeviceException as ex:
                self.controller.host_record_failure(self.host)
                self.set_connected(
                    False,
                    f"{self.pfx} Unable to connect to device {self.host} will try again later: {ex}"
                )
            except Exception as ex:
                self.controller.host_record_failure(self.host)
                self.set_connected(
                    False,
                    f"{self.pfx} Unknown exception {ex} connecting to device {self.host} will try again later",
                    exc_info=True
                )
        LOGGER.debug(f'{self.pfx} exit:{self.connected} {self.name} dev={self.dev}')
        return self.is_connected()

    def update(self):
        LOGGER.debug(f'enter: {self.name} dev={self.dev}')
        res = self._run_coro(self.update_a(), 'update_a', default=False)
        LOGGER.debug(f'exit:{res} {self.name} dev={self.dev}')
        return res

    async def update_a(self):
        LOGGER.debug(f'enter: {self.name} dev={self.dev}')
        ret = False
        if self.dev is None:
            ret = await self.connect_a()
        else:
            ret = await self.controller.update_dev(self.dev)
            if not ret:
                self.set_connected(
                    False,
                    f'{self.pfx} failed updating, see log'
                )
        LOGGER.debug(f'exit:{ret} {self.name} dev={self.dev}')
        return ret

    def set_on(self):
        LOGGER.debug(f'{self.pfx} enter')
        res = self._run_coro(self.set_on_a(), 'set_on_a')
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
        res = self._run_coro(self.set_off_a(), 'set_off_a')
        LOGGER.debug(f'result={res}')
        LOGGER.debug(f'{self.pfx} exit')

    async def set_off_a(self):
        LOGGER.debug(f'{self.pfx} enter')
        await self.dev.turn_off()
        LOGGER.debug(f'{self.pfx} setDriver(ST,0)')
        self.setDriver('ST',0)
        await self.set_state_a(set_energy=True)
        LOGGER.debug(f'{self.pfx} exit')

    # python-kasa don't have these on all devices anymore :(
    def is_dimmable(self,dev):
        return True if dev.features.get("brightness") else False
    def is_color(self,dev):
        return True if dev.features.get("hsv") else False
    def is_variable_color_temp(self,dev):
        return True if dev.features.get('color_temperature') else False

    async def set_state_a(self,set_energy=True):
        try:
            LOGGER.debug(f'{self.pfx} enter: dev={self.dev}')
            # This doesn't call set_energy, since that is only called on long_poll's
            # We don't use self.connected here because dev might be good, but device is unplugged
            # So then when it's plugged back in the same dev will still work
            ocon = self.connected
            if await self.update_a():
                if self.dev.is_on is True:
                    if self.is_dimmable(self.dev):
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
                if self.is_color(self.dev):
                    hsv = self.dev.hsv
                    self.setDriver('GV3',hsv[0])
                    self.setDriver('GV4',st2bri(hsv[1]))
                    self.setDriver('GV5',st2bri(hsv[2]))
                if self.is_variable_color_temp(self.dev):
                    self.setDriver('CLITEMP',self.dev.color_temp)

                # This happens when a device is alive on startup, but later disappears, then comes back.
                if not ocon and self.connected:
                    LOGGER.debug(f'{self.pfx} calling reconnected')
                    self.reconnected()
                if set_energy:
                    await self._set_energy_a()
            LOGGER.debug(f'{self.pfx} exit:  dev={self.dev}')
        except Exception as ex:
            LOGGER.error(f'Problem {ex} setting device state {self.dev.host}',exc_info=True)

    def is_on(self):
        return self.dev.is_on

    # Called by set_state when device is alive, does nothing by default, inheritance may override
    def set_all_drivers(self):
        pass

    def set_energy(self):
        res = self._run_coro(self._set_energy_au(), '_set_energy_au')
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
            except Exception as ex:
                LOGGER.error(f'{self.pfx} failed {ex}', exc_info=True)
        else:
            LOGGER.debug(f'{self.pfx} no energy')

    # Called when connected is changed from False to True
    # On initial startup or a reconnect later
    def reconnected(self):
        try:
            self.set_all_drivers()
        except Exception as ex:
            LOGGER.error(f'{self.pfx} set_all_drivers failed: {ex}',exc_info=True)

    def set_connected(self,st,msg=None,exc_info=False):
        if self.error_connect and st:
            LOGGER.warning(f"{self.pfx} Device {self.host} responding again")
            if self.dev is not None:
                self.controller.clear_device_notice(self.dev)
            self.error_connect = False
        if not st:
            self.error_connect = True
            if msg is not None:
                LOGGER.error(msg,exc_info=exc_info)
                if self.dev is not None:
                    # Lower priority than update_dev/auth so we don't replace
                    # the more specific exception with a generic echo like
                    # "failed updating, see log" or "res=False".
                    self.controller.set_device_notice(
                        self.dev,
                        msg.replace(f"{self.pfx} ", "", 1),
                        source='connect',
                    )
        # Just return if setting to same status
        if st == self.connected:
            return
        LOGGER.debug(f"{self.pfx} st={st}")
        self.connected = st
        self.setDriver('GV0',1 if st else 0)
        if st:
            # Make sure current cfg is saved
            LOGGER.debug(f"{self.pfx} save_cfg {st}")
            try:
                self.cfg['host']  = self.dev.host
                # Can't update host if not connected.
                if st:
                    self.cfg['model'] = self.dev.model
                self.controller.save_cfg(self.cfg)
            except SmartDeviceException as ex:
                LOGGER.error(f'{self.pfx} failed: {ex}')
            except Exception as ex:
                LOGGER.error(f'{self.pfx} unknown failure {ex}', exc_info=True)

    def is_connected(self):
        return self.connected

    def get_mon(self):
        LOGGER.debug(f'{self.pfx}')
        val = self.getDriver('GV6')
        if val is None:
            val = 1
        LOGGER.debug(f'{self.pfx} val={val}')
        return val

    def set_mon(self,val=None):
        LOGGER.debug(f'{self.pfx} val={val}')
        if val is None:
            val = self.get_mon()
        self.setDriver('GV6',val)

    def cmd_set_on(self, command):
        self.set_on()

    def cmd_set_off(self, command):
        self.set_off()

    def cmd_set_mon(self, command):
        val = int(command.get('value'))
        LOGGER.debug(f'{self.pfx} val={val}')
        self.set_mon(val)
