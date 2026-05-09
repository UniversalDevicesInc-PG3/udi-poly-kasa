
from udi_interface import Node,LOGGER,Custom,LOG_HANDLER
import logging,re,json,asyncio,signal,sys,threading,time,os,markdown2
from datetime import datetime
from threading import Thread,Event
from concurrent.futures import TimeoutError as FutureTimeoutError
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
        # Max seconds to wait on asyncio futures scheduled on mainloop.
        # Short ops (per-device update/query/poll) get a tight bound so a
        # single hung host can't hold a worker thread for minutes.
        # Network-broadcast discovery keeps a longer bound.
        self.async_future_timeout = 30
        self.discover_future_timeout = 180
        # Warn when any single sync->async hop exceeds this; helps localize
        # mainloop pressure before it becomes a watchdog kill.
        self.slow_future_warn_threshold = 5
        self._discover_wait_logged = False
        self._long_poll_busy_logged = False
        self._unsupported_device_types_logged = set()
        self._heartbeat_thread = None
        self._heartbeat_stop = Event()
        self._start_monotonic = time.monotonic()
        # (notice_key, source) -> monotonic timestamp of last write.
        self._notice_last_write = {}
        # Per-host circuit breaker. Hosts that have failed N times in a row
        # stop being probed by per-node connect_a/discover_single calls until
        # their next_try monotonic time. The longPoll discover sweep still
        # re-tests them at its 4-minute cadence.
        self._host_state = {}  # host -> {"fail": int, "next_try": float}
        self._host_breaker_threshold = 3
        # Per-host discover_single bound; one slow host shouldn't dominate a
        # poll cycle. python-kasa's default is 5s; we bound a bit tighter so
        # multiple unreachable hosts add up to less mainloop blocking before
        # the breaker trips.
        self.discover_single_timeout = 3
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
        self._install_signal_handlers()
        self._install_exception_hooks()
        self._start_heartbeat_log()
        self.update_profile()
        self.Notices.clear()
        self.mainloop = mainloop
        asyncio.set_event_loop(mainloop)
        # Route asyncio task/callback exceptions through LOGGER so they
        # don't fall back to Python's default handler, which writes
        # multi-line annotated tracebacks to stderr that udi_interface
        # captures one character at a time.
        try:
            mainloop.set_exception_handler(self._asyncio_exception_handler)
        except Exception as ex:  # noqa: BLE001
            LOGGER.debug(f'unable to install asyncio exception handler: {ex}')
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
        except Exception:
            LOGGER.error('discover failed', exc_info=True)
        try:
            self.add_manual_devices()
        except Exception:
            LOGGER.error('add_manual_devices failed', exc_info=True)
        self.ready = True
        LOGGER.info(f'exit {self.name}')

    # For things we only do when have the configuration is loaded...
    def handler_config_done(self):
        LOGGER.debug(f'enter')
        self.poly.addLogLevel('DEBUG_MODULES',9,'Debug + Modules')
        LOGGER.debug(f'exit')

    def _install_signal_handlers(self):
        # Without these a SIGTERM from the watchdog leaves no log evidence,
        # which is exactly the failure mode we hit on 2026-05-07.
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                signal.signal(sig, self._handle_signal)
            except (ValueError, OSError) as ex:
                # Non-main thread or unsupported platform; not fatal.
                LOGGER.debug(f'unable to install handler for {sig!r}: {ex}')

    def _handle_signal(self, signum, frame):
        try:
            name = signal.Signals(signum).name
        except (ValueError, AttributeError):
            name = str(signum)
        LOGGER.warning(f'received signal {name}; shutting down')
        self._heartbeat_stop.set()
        try:
            self.poly.stop()
        except Exception as ex:
            LOGGER.error(f'poly.stop() raised during signal handling: {ex}', exc_info=True)

    def _install_exception_hooks(self):
        """Route any otherwise-uncaught exception through LOGGER as a
        single record. Without this, Python's default thread/asyncio
        handlers dump multi-line annotated tracebacks to stderr; the
        udi_interface stderr capture writes one ERROR line per
        character (each '^' caret of a Python 3.11 annotated traceback
        arrives as its own write), which has produced ~27k log lines
        from a single uncaught exception in the field.
        """
        def _log_exc(prefix, exc_type, exc_value, exc_traceback):
            try:
                LOGGER.error(
                    '%s: %s: %s',
                    prefix,
                    getattr(exc_type, '__name__', exc_type),
                    exc_value,
                    exc_info=(exc_type, exc_value, exc_traceback),
                )
            except Exception:  # noqa: BLE001
                # Logger itself failed; fall back to a single-line
                # write so we don't trigger per-character capture.
                try:
                    sys.__stderr__.write(
                        f'{prefix}: {exc_type}: {exc_value}\n'
                    )
                except Exception:
                    pass

        def _thread_excepthook(args):
            if args.exc_type is SystemExit:
                return
            tname = args.thread.name if args.thread is not None else '<unknown>'
            _log_exc(
                f'unhandled exception in thread {tname}',
                args.exc_type, args.exc_value, args.exc_traceback,
            )

        def _sys_excepthook(exc_type, exc_value, exc_traceback):
            if exc_type is SystemExit:
                return
            _log_exc(
                'unhandled exception (main thread)',
                exc_type, exc_value, exc_traceback,
            )

        try:
            threading.excepthook = _thread_excepthook
        except Exception as ex:  # noqa: BLE001
            LOGGER.debug(f'unable to install threading.excepthook: {ex}')
        try:
            sys.excepthook = _sys_excepthook
        except Exception as ex:  # noqa: BLE001
            LOGGER.debug(f'unable to install sys.excepthook: {ex}')

    def _asyncio_exception_handler(self, loop, context):
        msg = context.get('message', 'asyncio exception')
        exc = context.get('exception')
        if exc is not None:
            LOGGER.error(
                'asyncio: %s: %s: %s',
                msg, type(exc).__name__, exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        else:
            LOGGER.error('asyncio: %s context=%r', msg, context)

    def _start_heartbeat_log(self):
        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_thread = Thread(
            target=self._heartbeat_log_loop,
            name='kasa-heartbeat-log',
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _heartbeat_log_loop(self):
        # One info line per minute. Gaps in this stream make it trivial to
        # spot mainloop freezes and watchdog kills in the log.
        while not self._heartbeat_stop.is_set():
            try:
                uptime = int(time.monotonic() - self._start_monotonic)
                short, long_ = self._poll_inflight_counts()
                LOGGER.info(
                    f'alive uptime={uptime}s discover_done={self.discover_done} '
                    f'inflight short={short} long={long_}'
                )
            except Exception as ex:
                LOGGER.debug(f'heartbeat log error: {ex}')
            self._heartbeat_stop.wait(60)

    def _poll_inflight_counts(self):
        short = 0
        long_ = 0
        try:
            for node_address in list(self.poly.getNodes()):
                node = self.poly.getNode(node_address)
                if node is None:
                    continue
                if getattr(node, 'in_short_poll', False):
                    short += 1
                if getattr(node, 'in_long_poll', False):
                    long_ += 1
        except Exception:
            pass
        return short, long_

    # Controller only needs longPoll
    def handler_poll(self, polltype):
        LOGGER.debug('enter')
        if polltype == 'longPoll':
            self.longPoll()
        LOGGER.debug('exit')

    def longPoll(self):
        if not self.discover_done:
            # Avoid repeating this every long poll until discover finishes.
            if not self._discover_wait_logged:
                LOGGER.info('waiting for discover to complete')
                self._discover_wait_logged = True
            return
        self._discover_wait_logged = False
        if self.in_long_poll:
            # Re-entrancy can happen when device updates are slow; log once until recovered.
            if not self._long_poll_busy_logged:
                LOGGER.warning('longPoll already running, skipping this cycle')
                self._long_poll_busy_logged = True
            return
        self._long_poll_busy_logged = False
        self.in_long_poll = True
        try:
            # Heartbeat is not sent if stuck in discover or long_poll?
            self.heartbeat()
            if self.auto_discover:
                self.discover_new()
            else:
                LOGGER.debug(f'auto_discover disabled {self.auto_discover}')
        finally:
            self.in_long_poll = False

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
        try:
            res = future.result(timeout=self.discover_future_timeout)
        except FutureTimeoutError:
            LOGGER.error(
                '_add_manual_devices timed out after %ss', self.discover_future_timeout, exc_info=True
            )
            res = None
        LOGGER.debug(f'result={res}')
        self.discover_done = True
        LOGGER.info("exit")

    async def _add_manual_devices(self):
        for mdev in self.manual_devices:
            LOGGER.info(f"Adding manual device {mdev['address']}")
            try:
                dev = await self.discover_single(host=mdev['address'])
                if dev is None:
                    LOGGER.warning(
                        f"discover_single returned no device for {mdev['address']}; "
                        f"skipping (host likely unreachable or circuit-broken)"
                    )
                    continue
                self.add_device_node(dev=dev)
            except Exception as ex:
                LOGGER.error(f"{ex} trying to connect to {mdev['address']}",exc_info=False)
        
    def discover(self):
        self.devm = {}
        LOGGER.info(f"enter: {self.poly.network_interface['broadcast']}")
        future = asyncio.run_coroutine_threadsafe(self._discover(target=self.poly.network_interface['broadcast']), self.mainloop)
        try:
            res = future.result(timeout=self.discover_future_timeout)
        except FutureTimeoutError:
            LOGGER.error(
                '_discover timed out after %ss', self.discover_future_timeout, exc_info=True
            )
            res = None
        LOGGER.debug(f'result={res}')
        if self.manual_networks is None or len(self.manual_networks) == 0:
            LOGGER.info("No manual networks configured")
        else:
            for network in self.manual_networks:
                LOGGER.info(f"calling: _discover(target={network['address']})")
                future = asyncio.run_coroutine_threadsafe(self._discover(target=network['address']), self.mainloop)
                try:
                    res = future.result(timeout=self.discover_future_timeout)
                except FutureTimeoutError:
                    LOGGER.error(
                        '_discover(%s) timed out after %ss',
                        network['address'],
                        self.discover_future_timeout,
                        exc_info=True,
                    )
                    res = None
                LOGGER.debug(f'result={res}')
        self.discover_done = True
        LOGGER.info("exit")

    # We have this in controller so all error handling is in one
    # place and we need ability to update device before the node
    # is created.  The SmartDeviceNode calls this update.
    async def update_dev(self,dev):
        ret = False
        host = getattr(dev, 'host', None)
        try:
            await dev.update()
            ret = True
        except AuthenticationError as msg:
            self.set_device_notice(
                dev,
                f'Authentication failed: {msg}',
                source='auth',
            )
            LOGGER.error(f'Failed to authenticate {dev}: {msg}')
        except KasaException as ex:
            # KasaException already encodes the actionable bit (e.g. "Host
            # is down", "Timed out") in its message. The traceback is the
            # same vendor stack every time and just bloats the log.
            self.host_record_failure(host)
            self.set_device_notice(
                dev,
                f'Update failed: {type(ex).__name__}: {ex}',
                source='update',
            )
            LOGGER.error(f"Failed to update {ex}: {dev}")
        except Exception as ex:
            self.host_record_failure(host)
            self.set_device_notice(
                dev,
                f'Update failed: {type(ex).__name__}: {ex}',
                source='update',
            )
            LOGGER.error(f"Failed to update {ex}: {dev}", exc_info=True)
        if ret:
            self.host_record_success(host)
            self.clear_device_notice(dev)
        return ret

    def _notice_key_for_device(self, dev):
        host = getattr(dev, 'host', 'unknown')
        return f"dev_{host}".replace('.', '_')

    # Sources ranked by how specific/useful the message is. Higher wins.
    _NOTICE_SOURCE_PRIORITY = {
        'state': 1,
        'connect': 2,
        'update': 3,
        'auth': 4,
    }

    # Minimum seconds between Notices writes for the same (host, source).
    # Without this, slightly-varying exception strings (e.g. different
    # transient socket errors) round-trip through the udi_interface MQTT
    # Notices channel each poll, generating tens of DEBUG lines per write.
    _NOTICE_WRITE_COOLDOWN_SECS = 60

    def set_device_notice(self, dev, message, source='state'):
        """Set a single, timestamped notice per device.

        The timestamp reflects first-seen for the current failure body so the
        UI shows when the problem started, not the latest poll. A more
        specific source (update/auth) wins over a generic state echo so we
        don't churn between two near-identical messages for the same failure.
        Repeated writes for the same (host, source) inside the cooldown
        window are coalesced even if the body wobbles, so transient error
        text differences don't cause MQTT/log churn.
        """
        key = self._notice_key_for_device(dev)
        body = f"{getattr(dev, 'alias', 'Device')} ({getattr(dev, 'host', 'unknown')}): {message}"
        new_priority = self._NOTICE_SOURCE_PRIORITY.get(source, 0)
        try:
            existing = self.poly.Notices[key]
        except KeyError:
            existing = None
        if existing is not None:
            # Same failure body already shown; preserve original timestamp.
            if existing.endswith(body):
                return
            existing_priority = self._notice_priority_from_value(existing)
            if existing_priority > new_priority:
                # A more specific failure is already on the UI; don't downgrade it.
                return
        # Cooldown: same (host, source) recently written -> skip.
        cooldown_key = (key, source)
        last = self._notice_last_write.get(cooldown_key, 0.0)
        now = time.monotonic()
        if existing is not None and now - last < self._NOTICE_WRITE_COOLDOWN_SECS:
            return
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # Embed source so future calls can compare priority without extra state.
        self.poly.Notices[key] = f"[{timestamp}] [{source}] {body}"
        self._notice_last_write[cooldown_key] = now

    def clear_device_notice(self, dev):
        key = self._notice_key_for_device(dev)
        try:
            self.poly.Notices.delete(key)
        except KeyError:
            pass
        # Drop any cooldown entries so the next failure isn't suppressed.
        for ck in [k for k in self._notice_last_write if k[0] == key]:
            self._notice_last_write.pop(ck, None)

    def _notice_priority_from_value(self, value):
        # Notice format is "[timestamp] [source] body". Be tolerant of legacy
        # values written without a source tag.
        try:
            after_ts = value.split('] ', 1)[1]
            tag = after_ts.split(']', 1)[0].lstrip('[')
        except (IndexError, ValueError):
            return 0
        return self._NOTICE_SOURCE_PRIORITY.get(tag, 0)

    def host_should_skip(self, host):
        """True when a host has failed too recently to be worth probing again.

        Used by per-node connect paths so a wall of unreachable hosts can't
        keep the asyncio mainloop blocked on serial 5 s discovery timeouts.
        """
        if host is None:
            return False
        s = self._host_state.get(host)
        if s is None:
            return False
        if s.get('fail', 0) < self._host_breaker_threshold:
            return False
        return time.monotonic() < s.get('next_try', 0.0)

    def host_record_failure(self, host):
        if host is None:
            return
        s = self._host_state.setdefault(host, {'fail': 0, 'next_try': 0.0})
        s['fail'] += 1
        # 60s, 120s, 240s, 480s, 960s, capped at 15 minutes.
        backoff = min(15 * 60, 30 * (2 ** min(s['fail'], 5)))
        s['next_try'] = time.monotonic() + backoff
        if s['fail'] == self._host_breaker_threshold:
            LOGGER.warning(
                'host %s circuit-broken after %s failures; will skip per-node '
                'probes for %ss',
                host,
                s['fail'],
                int(backoff),
            )

    def host_record_success(self, host):
        if host is None:
            return
        prev = self._host_state.get(host)
        self._host_state[host] = {'fail': 0, 'next_try': 0.0}
        if prev is not None and prev.get('fail', 0) >= self._host_breaker_threshold:
            LOGGER.info('host %s circuit reset after success', host)

    def is_unsupported_discovered_type(self, dev):
        """Skip device classes this plugin cannot yet represent as nodes."""
        return str(dev.device_type) in ('DeviceType.Camera', 'DeviceType.Hub')

    def log_unsupported_discovered_type(self, dev):
        key = f"{dev.mac}:{dev.device_type}"
        if key in self._unsupported_device_types_logged:
            return
        self._unsupported_device_types_logged.add(key)
        LOGGER.warning(
            "Ignoring unsupported discovered device type=%s mac=%s host=%s model=%s",
            dev.device_type,
            dev.mac,
            dev.host,
            getattr(dev, 'model', None),
        )

    async def discover_add_device(self,dev):
        LOGGER.debug(f"enter: {dev}")
        if self.is_unsupported_discovered_type(dev):
            self.log_unsupported_discovered_type(dev)
            return False
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
            if self.is_unsupported_discovered_type(dev) and smac not in self.nodes_by_mac:
                self.log_unsupported_discovered_type(dev)
                return False
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
        if self.host_should_skip(host):
            s = self._host_state.get(host, {})
            remaining = max(0, int(s.get('next_try', 0.0) - time.monotonic()))
            LOGGER.debug(
                f'host {host} circuit-broken ({s.get("fail", 0)} failures); '
                f'skipping discover_single, retry in {remaining}s'
            )
            return None
        try:
            dev = await kasa.Discover.discover_single(
                host,
                credentials=self.credentials,
                discovery_timeout=self.discover_single_timeout,
            )
        except Exception:
            self.host_record_failure(host)
            raise
        if dev is None:
            self.host_record_failure(host)
        else:
            self.host_record_success(host)
        LOGGER.debug(f'exit: dev={dev}')
        return dev


    def discover_new(self):
        LOGGER.info('enter')
        if not self.ready:
            LOGGER.error("Node is not yet ready")
            return False
        future = asyncio.run_coroutine_threadsafe(self._discover_new_a(), self.mainloop)
        try:
            res = future.result(timeout=self.discover_future_timeout)
        except FutureTimeoutError:
            LOGGER.error(
                '_discover_new_a timed out after %ss', self.discover_future_timeout, exc_info=True
            )
            res = None
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
        # Idempotency guard (issue #25). handler_typed_data() re-fires every
        # time PG3 saves customdata, so each addNode triggers another
        # add_manual_devices() pass on the same hosts. Without this guard,
        # add_device_node would re-add the same node on every cycle, flooding
        # the log and causing the IoX UI to lock up. Look up by mac first
        # (canonical identity) and fall back to address (in case the node was
        # added before nodes_by_mac was populated, e.g. from getNodesFromDb on
        # restart).
        mac_key = self.smac(cfg['mac']) if cfg.get('mac') else None
        existing = self.nodes_by_mac.get(mac_key) if mac_key else None
        if existing is None:
            existing = self.poly.getNode(cfg['address'])
        if existing is not None:
            if mac_key is not None:
                self.nodes_by_mac[mac_key] = existing
            LOGGER.debug(
                f"Device already added type={cfg['type']} "
                f"address={cfg['address']} name='{cfg['name']}'"
            )
            return existing
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
        except Exception:
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
                        except Exception:
                            LOGGER.error(
                                'renameNode error, which is a known issue with PG3x Version <= 3.2.7',
                                exc_info=True,
                            )
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
        except (json.JSONDecodeError, TypeError) as err:
            LOGGER.error('failed to parse cfg=%r Error: %s', cfg, err)
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
        # PG3 Custom Parameters always come back as strings, but kasa.Discover.discover
        # passes timeout straight to asyncio.sleep(), which raises
        # `TypeError: '<=' not supported between instances of 'str' and 'int'`
        # on newer python-kasa (issue #21). Coerce to int and fall back to the
        # default if the operator typed something non-numeric.
        raw_timeout = self.Parameters['discover_timeout']
        try:
            self.discover_timeout = int(raw_timeout)
        except (TypeError, ValueError):
            LOGGER.warning(
                "Invalid discover_timeout=%r in Custom Parameters; using default %d",
                raw_timeout, defaults['discover_timeout'],
            )
            self.discover_timeout = defaults['discover_timeout']
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
