
from udi_interface import Node,LOGGER,Custom,LOG_HANDLER
import logging,re,json,asyncio,signal,sys,threading,time,os,markdown2,html
from datetime import datetime
from threading import Thread,Event
from concurrent.futures import TimeoutError as FutureTimeoutError
from node_funcs import get_valid_node_name,get_valid_node_address
from safe_custom import SafeCustom, redact_sensitive_params
import kasa
from kasa.exceptions import AuthenticationError, KasaException, SmartDeviceException
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

_MANUAL_HOST_RE = re.compile(r'^(\d+\.\d+\.\d+\.\d+)\s*-\s*(.+)$')

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
        self.Parameters      = SafeCustom(self.poly, 'customparams')
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
        # host -> True after the first auth ERROR for that host; further
        # failures log at DEBUG until a successful update clears the flag.
        self._auth_fail_logged = {}
        # Per-host circuit breaker. Hosts that have failed N times in a row
        # stop being probed by per-node connect_a/discover_single calls until
        # their next_try monotonic time. The longPoll discover sweep still
        # re-tests them at its 4-minute cadence.
        # State shape (per host): {"fail": int, "next_try": float, "next_probe": float}.
        # next_probe gates the cheap TCP-level reconnect probe driven by
        # SmartDeviceNode._shortPoll_a; it lives independently of next_try
        # so we can re-test offline hosts much more cheaply (and more
        # often) than the full kasa protocol probe.
        self._host_state = {}
        self._host_breaker_threshold = 3
        # Cheap TCP-connect probe used inside shortPoll to detect a
        # circuit-broken host coming back online without paying a 5-12s
        # kasa protocol timeout. 9999 is the legacy Kasa control port; on
        # Tapo SMART devices the port may be closed but the kernel sends
        # a RST which we treat as "host alive" (see host_quick_probe).
        self.host_quick_probe_timeout = 1.0
        self.host_quick_probe_interval = 30.0
        self.host_quick_probe_port = 9999
        # Per-host discover_single bound; one slow host shouldn't dominate a
        # poll cycle. python-kasa's default is 5s; we bound a bit tighter so
        # multiple unreachable hosts add up to less mainloop blocking before
        # the breaker trips.
        self.discover_single_timeout = 3
        # Serialize async addNode calls (Ecobee pattern); wait_for_node_done
        # blocks until ADDNODEDONE fires for the pending add.
        self.n_queue = []
        # Defer per-node startup connects until after discover completes.
        self.startup_connect_queue = []
        self.startup_in_progress = False
        self.startup_connect_gap = 0.05
        # Minimum pause after each addNode ACK so PG3 is not flooded on restart.
        self.add_node_gap = 0.2
        self.add_node_timeout = 30.0
        self._strip_cleanup_notified = set()
        # Devices discovered on the asyncio thread; drained serially on the
        # caller thread so wait_for_node_done does not block the mainloop.
        self._pending_device_adds = []
        # Safe default until handler_params loads real credentials.
        self.credentials = kasa.Credentials('none', 'none')
        self.manual_devices = None
        self.manual_networks = None
        # TypedData plugin writes: suppress add_manual_devices feedback loop (#25).
        self._syncing_device_inventory = False
        self._manual_device_hosts = set()
        self._manual_failed_hosts = set()
        self._manual_host_names = {}
        self._manual_host_identity = {}
        self._config_doc_table_sig = None
        self.poly.subscribe(self.poly.START,                  self.handler_start, address) 
        self.poly.subscribe(self.poly.POLL,                   self.handler_poll)
        self.poly.subscribe(self.poly.LOGLEVEL,               self.handler_log_level)
        self.poly.subscribe(self.poly.CONFIGDONE,             self.handler_config_done)
        self.poly.subscribe(self.poly.CUSTOMPARAMS,           self.handler_params)
        self.poly.subscribe(self.poly.CUSTOMDATA,             self.handler_data)
        self.poly.subscribe(self.poly.DISCOVER,               self.discover_new)
        self.poly.subscribe(self.poly.ADDNODEDONE,            self.handler_add_node_done)
        self.poly.subscribe(poly.CUSTOMTYPEDPARAMS,           self.handler_typed_params)
        self.poly.subscribe(poly.CUSTOMTYPEDDATA,             self.handler_typed_data)
        self.poly.ready()
        self.poly.addNode(self, conn_status='ST')

    def node_queue(self, data):
        addr = data.get('address') if isinstance(data, dict) else data
        if addr is not None:
            self.n_queue.append(str(addr).lower())

    def wait_for_node_done(self, address=None, timeout_sec=None):
        """Block until ADDNODEDONE for *address* (or any address if None)."""
        address = str(address or '').lower()
        if timeout_sec is None:
            timeout_sec = self.add_node_timeout
        deadline = time.time() + max(0.1, float(timeout_sec))
        while time.time() < deadline:
            if address:
                for i, queued in enumerate(self.n_queue):
                    if queued == address:
                        self.n_queue.pop(i)
                        return True
            elif self.n_queue:
                self.n_queue.pop(0)
                return True
            time.sleep(0.05)
        LOGGER.warning(
            'wait_for_node_done timed out after %ss for %s',
            timeout_sec,
            address or '<any>',
        )
        return False

    def handler_add_node_done(self, node):
        if isinstance(node, dict):
            addr = node.get('address')
        else:
            addr = getattr(node, 'address', None)
        if addr is not None:
            self.node_queue({'address': addr})

    def enqueue_startup_connect(self, node):
        address = getattr(node, 'address', None)
        if address is None or address == self.address:
            return
        if address not in self.startup_connect_queue:
            self.startup_connect_queue.append(address)

    def drain_startup_connects(self):
        LOGGER.info('Startup connect queue: %s nodes', len(self.startup_connect_queue))
        for address in list(self.startup_connect_queue):
            node = self.poly.getNode(address)
            if node is None:
                LOGGER.warning('Startup connect: node not found for %s', address)
                continue
            name = getattr(node, 'name', address)
            LOGGER.debug('Startup connect: %s', name)
            try:
                if hasattr(node, 'connect'):
                    node.connect()
            except Exception:
                LOGGER.error('Startup connect failed for %s', address, exc_info=True)
            if self.startup_connect_gap:
                time.sleep(self.startup_connect_gap)
        self.startup_connect_queue.clear()

    def handler_start(self):
        self.startup_in_progress = True
        try:
            self._handler_start_body()
        finally:
            self.startup_in_progress = False

    def _handler_start_body(self):
        # Drop any ADDNODEDONE from the controller addNode so the first
        # device wait is not satisfied by a stale queue entry.
        self.n_queue.clear()
        self._pending_device_adds = []
        LOGGER.info(f"Started Kasa PG3 NodeServer {self.poly.serverdata['version']}")
        LOGGER.info(f"Kasa Library Version {kasa.__version__}")
        self._install_signal_handlers()
        self._install_exception_hooks()
        self._start_heartbeat_log()
        self.update_profile()
        self.Notices.clear()
        try:
            self.Data.delete('_strip_cleanup')
        except KeyError:
            pass
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
        #
        # Wait for all handlers to finish (credentials must be loaded
        # before discover / discover_single).
        #
        def _handlers_ready():
            return (
                self.handler_params_st is not None
                and self.handler_data_st is not None
                and self.handler_typedparams_st is not None
                and self.handler_typeddata_st is not None
            )

        cnt = 600
        wait_warned = False
        last_wait_summary = time.monotonic()
        while not _handlers_ready() and cnt > 0:
            if not wait_warned:
                LOGGER.warning(
                    'Waiting for handlers to load (params/data/typedparams/typeddata)...'
                )
                wait_warned = True
            else:
                LOGGER.debug(
                    'Waiting for handlers params=%s data=%s typedparams=%s '
                    'typeddata=%s... cnt=%s',
                    self.handler_params_st,
                    self.handler_data_st,
                    self.handler_typedparams_st,
                    self.handler_typeddata_st,
                    cnt,
                )
                now = time.monotonic()
                if now - last_wait_summary >= 30:
                    LOGGER.warning(
                        'Still waiting for handlers to load... cnt=%s', cnt
                    )
                    last_wait_summary = now
            time.sleep(1)
            cnt -= 1
        if not _handlers_ready():
            LOGGER.error(
                'Timed out waiting for handlers to startup; skipping discover '
                'and manual device add'
            )
            self.poly.Notices['startup'] = (
                'Configuration handlers did not finish loading in time; '
                'discover was skipped. Restart the node server or check PG3 logs.'
            )
        else:
            self._ensure_credentials()
            try:
                self._purge_stale_misnamed_strip_cfg()
            except Exception:
                LOGGER.error('_purge_stale_misnamed_strip_cfg failed', exc_info=True)
            try:
                self.discover()
            except Exception:
                LOGGER.error('discover failed', exc_info=True)
            try:
                self.add_manual_devices()
            except Exception:
                LOGGER.error('add_manual_devices failed', exc_info=True)
            try:
                self.drain_startup_connects()
            except Exception:
                LOGGER.error('drain_startup_connects failed', exc_info=True)
            try:
                self._after_inventory_sync()
            except Exception:
                LOGGER.error('_after_inventory_sync after startup failed', exc_info=True)
            # Outlet child nodes may not exist until IoX finishes hydrating and
            # startup connects run; delete with PG3 ack waits (homekit-hub pattern).
            try:
                self.cleanup_corrupt_strip_nodes()
            except Exception:
                LOGGER.error('cleanup_corrupt_strip_nodes failed', exc_info=True)
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
        # Close per-node aiohttp ClientSessions while the mainloop is
        # still running. If we let poly.stop() tear things down first,
        # the sessions are reaped in interpreter shutdown order and
        # asyncio prints a burst of "Unclosed client session" warnings.
        try:
            self._close_known_devices_on_shutdown()
        except Exception as ex:  # noqa: BLE001
            LOGGER.debug(f'_close_known_devices_on_shutdown error: {ex}')
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
        # aiohttp emits "Unclosed client session" / "Unclosed connector"
        # via the loop exception handler from ClientSession.__del__ when
        # GC reaps a session that wasn't explicitly closed. The plugin
        # closes freshly-discovered devices that aren't retained on a
        # node (see _close_device_quietly), but a residual race during
        # shutdown / GC ordering can still surface these. They are
        # cleanup noise, not real failures, so log at DEBUG to avoid
        # confusing operators reading the IoX UI.
        if (
            exc is None
            and isinstance(msg, str)
            and (
                msg.startswith('Unclosed client session')
                or msg.startswith('Unclosed connector')
            )
        ):
            LOGGER.debug('asyncio: %s', msg)
            return
        if exc is not None:
            LOGGER.error(
                'asyncio: %s: %s: %s',
                msg, type(exc).__name__, exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        else:
            LOGGER.error('asyncio: %s context=%r', msg, context)

    async def _close_device_quietly(self, dev):
        """Disconnect a python-kasa Device, swallowing all errors.

        Fresh Device objects produced by Discover that we don't intend
        to retain on a SmartDeviceNode must have their underlying
        aiohttp ClientSession closed; otherwise GC fires "Unclosed
        client session" / "Unclosed connector" warnings every
        long-poll cycle (see Controller.discover_new_add_device).
        """
        if dev is None:
            return
        disconnect = getattr(dev, 'disconnect', None)
        if disconnect is None:
            return
        try:
            await disconnect()
        except Exception as ex:  # noqa: BLE001
            LOGGER.debug(
                f'disconnect() on {getattr(dev, "host", "?")} raised '
                f'{type(ex).__name__}: {ex}'
            )

    def _close_known_devices_on_shutdown(self):
        """Close aiohttp sessions held by long-lived node Devices.

        Without this, a SIGTERM/reload leaves the per-device HttpClient
        ClientSessions to be reaped during interpreter shutdown, which
        produces a burst of "Unclosed client session" warnings via the
        asyncio loop exception handler that the operator then sees in
        the next plugin start's tail of the previous log.
        """
        devs = []
        try:
            for node in list(self.nodes_by_mac.values()):
                d = getattr(node, 'dev', None)
                if d is not None:
                    devs.append(d)
        except Exception as ex:  # noqa: BLE001
            LOGGER.debug(f'collecting node devices for shutdown failed: {ex}')
            return
        if not devs:
            return
        loop = getattr(self, 'mainloop', None)
        if loop is None or not loop.is_running():
            return

        async def _close_all():
            await asyncio.gather(
                *(self._close_device_quietly(d) for d in devs),
                return_exceptions=True,
            )

        try:
            fut = asyncio.run_coroutine_threadsafe(_close_all(), loop)
            fut.result(timeout=5)
        except Exception as ex:  # noqa: BLE001
            LOGGER.debug(f'shutdown device cleanup error: {ex}')

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
            self._after_inventory_sync()
            return
        self._manual_failed_hosts = set()
        self._manual_host_names = {}
        self._manual_host_identity = {}
        future = asyncio.run_coroutine_threadsafe(self._add_manual_devices(), self.mainloop)
        try:
            res = future.result(timeout=self.discover_future_timeout)
        except FutureTimeoutError:
            LOGGER.error(
                '_add_manual_devices timed out after %ss', self.discover_future_timeout, exc_info=True
            )
            res = None
        LOGGER.debug(f'result={res}')
        # Register nodes on this thread (not asyncio) with paced addNode.
        self.drain_pending_device_adds()
        self.discover_done = True
        self._after_inventory_sync()
        LOGGER.info("exit")

    async def _add_manual_devices(self):
        for mdev in self.manual_devices or []:
            host = self._manual_device_host(mdev)
            if not host:
                continue
            LOGGER.info(f"Adding manual device {host}")
            try:
                dev = await self.discover_single(host=host)
                if dev is None:
                    self._manual_failed_hosts.add(host)
                    LOGGER.warning(
                        f"discover_single returned no device for {host}; "
                        f"skipping (host likely unreachable or circuit-broken)"
                    )
                    continue
                self._manual_failed_hosts.discard(host)
                if not await self.update_dev(dev):
                    self._manual_failed_hosts.add(host)
                    LOGGER.warning(
                        'Manual device %s reachable but update failed; skipping add',
                        host,
                    )
                    continue
                self._remember_manual_host_lookup(host, dev)
                self.queue_device_add(dev=dev)
            except Exception as ex:
                self._manual_failed_hosts.add(host)
                LOGGER.error(f"{ex} trying to connect to {host}", exc_info=False)

    def _normalize_broadcast_address(self, address):
        """Return a UDP broadcast address for kasa discover.

        Extra Discovery Networks must be broadcast (e.g. 192.168.222.255).
        Operators often enter a gateway (.1) or host IP; map those to .255.
        """
        address = str(address or '').strip()
        if not address:
            return None
        parts = address.split('.')
        if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            if parts[3] != '255':
                broadcast = '.'.join(parts[:3] + ['255'])
                LOGGER.warning(
                    'Discovery target %s is not a broadcast address; using %s',
                    address,
                    broadcast,
                )
                return broadcast
            return address
        return address

    def _broadcast_for_host(self, host):
        host = str(host or '').strip()
        parts = host.split('.')
        if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            return '.'.join(parts[:3] + ['255'])
        return None

    def _discover_targets(self):
        """Ordered unique broadcast targets: interface, manual networks, cfg/manual hosts."""
        targets = []
        seen = set()

        def add(addr):
            broadcast = self._normalize_broadcast_address(addr)
            if not broadcast or broadcast in seen:
                return
            seen.add(broadcast)
            targets.append(broadcast)

        try:
            add(self.poly.network_interface.get('broadcast'))
        except Exception:
            LOGGER.debug('network_interface broadcast unavailable', exc_info=True)

        for network in self.manual_networks or []:
            if isinstance(network, dict):
                add(network.get('address'))

        for mdev in self.manual_devices or []:
            if isinstance(mdev, dict):
                add(self._manual_device_host(mdev))

        for key in list(self.Data):
            cfg = self.get_device_cfg(key)
            if cfg and cfg.get('host'):
                add(self._broadcast_for_host(cfg.get('host')))

        return targets

    def discover(self):
        self.devm = {}
        self._pending_device_adds = []
        targets = self._discover_targets()
        LOGGER.info('enter: targets=%s', targets)
        for target in targets:
            LOGGER.info('calling: _discover(target=%s)', target)
            future = asyncio.run_coroutine_threadsafe(
                self._discover(target=target), self.mainloop
            )
            try:
                res = future.result(timeout=self.discover_future_timeout)
            except FutureTimeoutError:
                LOGGER.error(
                    '_discover(%s) timed out after %ss',
                    target,
                    self.discover_future_timeout,
                    exc_info=True,
                )
                res = None
            LOGGER.debug('result=%s', res)
        # addNode on this thread with pacing; always register even if in PG3 DB.
        self.drain_pending_device_adds()
        self.discover_done = True
        self._after_inventory_sync()
        LOGGER.info("exit")

    # We have this in controller so all error handling is in one
    # place and we need ability to update device before the node
    # is created.  The SmartDeviceNode calls this update.
    async def update_dev(self,dev):
        ret = False
        host = getattr(dev, 'host', None)
        # Snapshot whether the host is already circuit-broken at the
        # start of this attempt. If yes, demote the failure log to
        # DEBUG so an offline host doesn't generate a steady-state
        # ERROR per poll cycle. The breaker-threshold WARNING in
        # host_record_failure still covers the down transition, and
        # host_record_success still covers the up transition.
        was_broken = self.host_should_skip(host)
        try:
            await dev.update()
            ret = True
        except AuthenticationError as msg:
            notice = self._auth_failure_notice_message(msg)
            self.set_device_notice(dev, notice, source='auth')
            auth_key = host or 'unknown'
            log = LOGGER.debug if self._auth_fail_logged.get(auth_key) else LOGGER.error
            log(
                'Failed to authenticate %s (%s): %s',
                self._device_display_name(dev),
                host,
                msg,
            )
            self._auth_fail_logged[auth_key] = True
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
            log = LOGGER.debug if was_broken else LOGGER.error
            log(f"Failed to update {ex}: {dev}")
        except Exception as ex:
            self.host_record_failure(host)
            self.set_device_notice(
                dev,
                f'Update failed: {type(ex).__name__}: {ex}',
                source='update',
            )
            if was_broken:
                LOGGER.debug(f"Failed to update {ex}: {dev}", exc_info=True)
            else:
                LOGGER.error(f"Failed to update {ex}: {dev}", exc_info=True)
        if ret:
            self.host_record_success(host)
            if host and host in self._auth_fail_logged:
                self._auth_fail_logged.pop(host, None)
                LOGGER.info('host %s authenticated successfully after prior failure', host)
            self.clear_device_notice(dev)
            if self.change_node_names:
                identity = self._node_identity_key(dev=dev)
                node = self.nodes_by_mac.get(identity) if identity else None
                alias = getattr(dev, 'alias', None)
                # HS300 parent alias can mirror an outlet name; only sync
                # outlet nodes, not the strip parent itself.
                if (
                    node is not None
                    and alias
                    and not isinstance(node, SmartStripNode)
                ):
                    self.sync_node_name(node, alias)
                if (
                    node is not None
                    and str(getattr(dev, 'device_type', None)) == 'DeviceType.Strip'
                ):
                    self.sync_strip_child_names(node)
        return ret

    def sync_node_name(self, node, alias, on_add=False):
        """Sync an IoX node name from the live Kasa alias when enabled."""
        if not alias:
            return
        requested = get_valid_node_name(alias)
        address = getattr(node, 'address', None)
        if address is None and getattr(node, 'cfg', None):
            address = node.cfg.get('address')
        if address is None:
            return

        if self.change_node_names:
            if not on_add and requested == node.name:
                return
            cname = self.poly.getNodeNameFromDb(address)
            current = cname if cname is not None else node.name
            if requested == current:
                if node.name != requested:
                    node.name = requested
                return
            LOGGER.warning(
                "Node name '%s' for %s does not match Kasa alias '%s', changing to match",
                current,
                address,
                requested,
            )
            try:
                self.poly.renameNode(address, requested)
            except Exception:
                LOGGER.error(
                    'renameNode error, which is a known issue with PG3x Version <= 3.2.7',
                    exc_info=True,
                )
                return
            node.name = requested
            if getattr(node, 'cfg', None) is not None:
                node.cfg['name'] = requested
                self.save_cfg(node.cfg)
            return

        if on_add:
            cname = self.poly.getNodeNameFromDb(address)
            if cname is not None and node.name != cname:
                LOGGER.warning(
                    "Existing node name '%s' for %s does not match requested name '%s', "
                    "NOT changing to match, set change_node_names=true to enable",
                    cname,
                    address,
                    node.name,
                )
                node.name = cname

    def sync_strip_child_names(self, strip_node):
        """Sync HS300 outlet node names from the parent strip's child devices."""
        if not self.change_node_names:
            return
        dev = getattr(strip_node, 'dev', None)
        if dev is None or str(getattr(dev, 'device_type', None)) != 'DeviceType.Strip':
            return
        children = getattr(dev, 'children', None)
        if not children:
            return
        child_nodes = getattr(strip_node, 'child_nodes', None) or []
        for pnum, child_dev in enumerate(children):
            identity = self._node_identity_key(dev=child_dev)
            plug_node = self.nodes_by_mac.get(identity) if identity else None
            if plug_node is None and pnum < len(child_nodes):
                plug_node = child_nodes[pnum]
            alias = getattr(child_dev, 'alias', None)
            if plug_node is not None and alias:
                self.sync_node_name(plug_node, alias)

    def _notice_key_for_device(self, dev):
        host = getattr(dev, 'host', 'unknown')
        return f"dev_{host}".replace('.', '_')

    def _notice_dev_for_host(self, host, dev=None):
        """Return a dev-like object for per-host notices (real dev or host stub)."""
        if dev is not None:
            return dev
        if host is None:
            return None
        for node in self._nodes_for_host(host):
            node_dev = getattr(node, 'dev', None)
            if node_dev is not None:
                return node_dev
        class _HostNoticeStub:
            pass
        stub = _HostNoticeStub()
        stub.host = host
        return stub

    def _node_label(self, node):
        """Best-effort friendly label from an IoX node."""
        if node is None:
            return None
        name = getattr(node, 'name', None)
        if name:
            return name
        cfg = getattr(node, 'cfg', None)
        if cfg and cfg.get('name'):
            return cfg['name']
        return None

    def _nodes_for_host(self, host):
        """Return IoX nodes that manage a host IP (session map + poly registry)."""
        if not host:
            return []
        host = str(host)
        found = []
        seen = set()
        for node in self.nodes_by_mac.values():
            node_id = id(node)
            if node_id in seen:
                continue
            if str(getattr(node, 'host', None) or '') == host:
                found.append(node)
                seen.add(node_id)
        for node_address in self.poly.getNodes():
            node = self.poly.getNode(node_address)
            if node is None:
                continue
            node_id = id(node)
            if node_id in seen:
                continue
            node_host = getattr(node, 'host', None)
            if node_host is None:
                cfg = getattr(node, 'cfg', None)
                if cfg:
                    node_host = cfg.get('host')
            if str(node_host or '') == host:
                found.append(node)
                seen.add(node_id)
        return found

    def _cfg_for_host(self, host):
        if not host:
            return None
        host = str(host)
        for key in self.Data:
            cfg = self.get_device_cfg(key)
            if cfg and str(cfg.get('host') or '') == host:
                return cfg
        return None

    @staticmethod
    def _dev_attr(dev, name, default=None):
        """Read a kasa device attribute without raising when update() failed."""
        if dev is None:
            return default
        try:
            return getattr(dev, name)
        except KasaException:
            return default
        except Exception:
            return default

    def _cfg_for_dev(self, dev):
        """Return saved device cfg for a kasa dev, keyed by MAC/device_id/host."""
        if dev is None:
            return None
        mac = self._dev_attr(dev, 'mac')
        if mac:
            cfg = self.get_device_cfg(self.smac(mac))
            if cfg:
                return cfg
            cfg = self.get_device_cfg(get_valid_node_address(mac))
            if cfg:
                return cfg
        device_id = self._dev_attr(dev, 'device_id')
        if device_id:
            cfg = self.get_device_cfg(self.smac(device_id))
            if cfg:
                return cfg
        host = self._dev_attr(dev, 'host')
        if host:
            return self._cfg_for_host(host)
        return None

    def _pg3_name_for_address(self, address):
        if not address:
            return None
        address = str(address).strip()
        if hasattr(self.poly, 'getNodeNameFromDb'):
            try:
                name = self.poly.getNodeNameFromDb(address)
                if name:
                    return name
            except Exception:
                LOGGER.debug(
                    'getNodeNameFromDb failed for %s', address, exc_info=True
                )
        meta = self._pg3_node_meta(address)
        if meta:
            name = meta.get('name')
            if name:
                return name
        return None

    def _db_name_for_dev(self, dev):
        """IoX/PG3 node name for a kasa dev before Python nodes are registered."""
        if dev is None:
            return None
        cfg = self._cfg_for_dev(dev)
        if cfg:
            name = cfg.get('name')
            if name:
                return name
            address = cfg.get('address')
            if address:
                name = self._pg3_name_for_address(address)
                if name:
                    return name
        mac = self._dev_attr(dev, 'mac')
        if mac:
            name = self._pg3_name_for_address(get_valid_node_address(mac))
            if name:
                return name
        identity = self._node_identity_key(dev=dev)
        if identity:
            for node_address in self.poly.getNodes():
                if str(node_address).lower().startswith(identity.lower()):
                    name = self._pg3_name_for_address(node_address)
                    if name:
                        return name
        if hasattr(self.poly, 'getNodesFromDb'):
            try:
                for meta in self.poly.getNodesFromDb() or []:
                    if not isinstance(meta, dict):
                        continue
                    addr = str(meta.get('address') or '').strip()
                    if not addr or addr == self.address:
                        continue
                    if mac and addr == get_valid_node_address(mac):
                        name = meta.get('name')
                        if name:
                            return name
            except Exception:
                LOGGER.debug('getNodesFromDb scan failed', exc_info=True)
        return None

    def _device_display_name(self, dev):
        """Friendly device label for notices/logs when dev.alias is missing."""
        alias = None
        try:
            alias = getattr(dev, 'alias', None)
            if alias:
                alias = str(alias).strip()
        except Exception:
            alias = None
        if alias:
            return alias
        db_name = self._db_name_for_dev(dev)
        if db_name:
            return db_name
        identity = self._node_identity_key(dev=dev)
        if identity:
            node = self.nodes_by_mac.get(identity)
            label = self._node_label(node)
            if label:
                return label
        host = getattr(dev, 'host', None)
        if host:
            host = str(host)
            # Prefer the strip parent node name over an arbitrary outlet
            # when several nodes share the same host IP.
            for node in self._nodes_for_host(host):
                if isinstance(node, SmartStripNode):
                    label = self._node_label(node)
                    if label:
                        return label
            for node in self._nodes_for_host(host):
                label = self._node_label(node)
                if label:
                    return label
        try:
            return self._dev_default_name(dev)
        except Exception:
            pass
        return 'Device'

    def _credentials_configured(self):
        """True when real Kasa credentials are set (not blank or none/none)."""
        if self.credential_error:
            return False
        user = str(self.Parameters.get('user') or '').strip()
        password = str(self.Parameters.get('password') or '').strip()
        if not user or not password:
            return False
        return not (user.lower() == 'none' and password.lower() == 'none')

    def _auth_failure_notice_message(self, _detail):
        """User-facing auth notice; differs for missing vs rejected credentials."""
        if not self._credentials_configured():
            return (
                'Kasa credentials not configured — enter your TP-Link email and '
                'password in Custom Parameters (both case-sensitive). Use none/none '
                'only for older local-only devices.'
            )
        return (
            'Authentication failed — device rejected login. Verify the Kasa '
            'email and password in Custom Parameters (case-sensitive) and '
            'confirm this device is on the same TP-Link account in the Kasa app.'
        )

    def _existing_node_for_dev(self, dev):
        """Return an IoX node already managing this discovered device, if any."""
        identity = self._node_identity_key(dev=dev)
        if identity:
            node = self.nodes_by_mac.get(identity)
            if node is not None:
                return node
        mac = getattr(dev, 'mac', None)
        if mac is not None:
            node = self.poly.getNode(get_valid_node_address(mac))
            if node is not None:
                return node
        return None

    _STRIP_PARENT_TYPES = ('SmartStrip', 'DeviceType.Strip')

    def _dev_model(self, dev):
        return str(getattr(dev, 'model', None) or '')

    def _model_looks_like_strip(self, model):
        model = str(model or '').upper()
        if not model:
            return False
        return (
            model.startswith('HS')
            or model.startswith('KP3')
            or 'STRIP' in model
        )

    def _dev_is_strip_parent(self, dev):
        if dev is None:
            return False
        if str(dev.device_type) == 'DeviceType.Strip' or getattr(dev, 'is_strip', False):
            return True
        return self._model_looks_like_strip(self._dev_model(dev))

    def _normalize_dev_type(self, dev):
        dev_type = str(dev.device_type)
        if self._dev_is_strip_parent(dev):
            return 'DeviceType.Strip'
        if dev_type == 'DeviceType.Unknown':
            model = self._dev_model(dev).upper()
            if model.startswith('P1') or model.startswith('KP115') or model.startswith('EP25'):
                return 'DeviceType.Plug'
        return dev_type

    def _dev_default_name(self, dev):
        if self._dev_is_strip_parent(dev):
            model = self._dev_model(dev) or 'Strip'
            return get_valid_node_name(f'SmartStrip {model}')
        alias = (getattr(dev, 'alias', None) or '').strip()
        if alias:
            return get_valid_node_name(alias)
        model = self._dev_model(dev)
        if model:
            return get_valid_node_name(f'Kasa {model}')
        mac = self._dev_attr(dev, 'mac')
        if mac:
            return get_valid_node_name(f'Kasa {self.smac(mac)}')
        host = getattr(dev, 'host', None)
        return get_valid_node_name(host or 'Kasa device')

    def _is_strip_plug_node(self, node):
        return getattr(node, 'id', '').startswith('SmartStripPlug_')

    def _strip_parent_address_from_cfg(self, cfg):
        addr = cfg.get('address')
        if addr:
            return addr.lower()
        mac = cfg.get('mac')
        if mac:
            return self.smac(mac).lower()
        return None

    def _strip_child_address_pattern(self, parent_address):
        """Child IoX addresses by HS300 suffix pattern (``{parent}01``..)."""
        parent_address = (parent_address or '').lower()
        child_addrs = []
        for addr in self.poly.getNodes():
            addr_lower = addr.lower()
            if addr_lower == parent_address:
                continue
            if not addr_lower.startswith(parent_address):
                continue
            suffix = addr_lower[len(parent_address):]
            if len(suffix) == 2 and suffix.isdigit():
                child_addrs.append(addr)
        return sorted(child_addrs)

    def _strip_child_addresses(self, parent_address):
        """IoX addresses of SmartStripPlug children for a strip parent."""
        child_addrs = self._strip_child_address_pattern(parent_address)
        plug_addrs = []
        for addr in child_addrs:
            node = self.poly.getNode(addr)
            if node is None or self._is_strip_plug_node(node):
                plug_addrs.append(addr)
        return plug_addrs if plug_addrs else child_addrs

    def _is_strip_parent_cfg(self, cfg):
        if not isinstance(cfg, dict):
            return False
        if cfg.get('type') in self._STRIP_PARENT_TYPES:
            return True
        return self._model_looks_like_strip(cfg.get('model'))

    @staticmethod
    def _is_misnamed_strip_parent_name(name):
        return not (name or '').strip().lower().startswith('smartstrip')

    def _node_is_strip_parent(self, node, cfg=None):
        if isinstance(node, SmartStripNode):
            return True
        if cfg is None:
            cfg = getattr(node, 'cfg', None)
        if cfg and self._is_strip_parent_cfg(cfg):
            return True
        node_id = getattr(node, 'id', '') or ''
        return str(node_id).startswith('SmartStrip_')

    def _outlet_cfg_names_for_host(self, host):
        names = []
        for key in self.Data:
            cfg = self.get_device_cfg(key)
            if not isinstance(cfg, dict) or cfg.get('host') != host:
                continue
            if cfg.get('type') not in ('SmartStripPlug', 'DeviceType.StripSocket'):
                continue
            name = (cfg.get('name') or '').strip()
            if name:
                names.append(name)
        return names

    @staticmethod
    def _node_names_collide(parent_name, other_name):
        parent = (parent_name or '').strip().lower()
        other = (other_name or '').strip().lower()
        if not parent or not other:
            return False
        return parent == other or parent.startswith(other) or other.startswith(parent)

    def _strip_parent_name_collides_with_outlet(self, host, parent_name):
        if not host:
            return False
        for outlet_name in self._outlet_cfg_names_for_host(host):
            if self._node_names_collide(parent_name, outlet_name):
                return True
        return False

    def _strip_parent_name_collides_with_live_children(self, parent_address, parent_name):
        for addr in self._strip_child_address_pattern(parent_address):
            node = self.poly.getNode(addr)
            if node is None:
                continue
            child_name = (getattr(node, 'name', '') or '').strip()
            if self._node_names_collide(parent_name, child_name):
                return True
        return False

    def _strip_parent_has_outlet_alias(self, parent_address, host, parent_name):
        """True when parent name matches an outlet (3.3.11 corruption signature)."""
        return (
            self._strip_parent_name_collides_with_outlet(host, parent_name)
            or self._strip_parent_name_collides_with_live_children(
                parent_address, parent_name
            )
        )

    def _collect_corrupt_strip_targets(self):
        """Return corrupt live HS300 strip parents plus outlet child addresses.

        Only the 3.3.11 signature is destructive: parent name matches an outlet
        alias (not merely "does not start with SmartStrip"). User-renamed
        parents like ``Living Room | Behind Couch`` are left alone. Stale
        misnamed cfg alone is handled by ``_purge_stale_misnamed_strip_cfg``.
        """
        targets = {}

        def consider(parent_address, host, name):
            parent_address = (parent_address or '').lower()
            if not parent_address or not self._is_misnamed_strip_parent_name(name):
                return
            if not self._strip_parent_has_outlet_alias(parent_address, host, name):
                return
            child_addrs = self._strip_child_addresses(parent_address)
            if not child_addrs:
                child_addrs = self._strip_child_address_pattern(parent_address)
            if not child_addrs:
                return
            targets[parent_address] = {
                'parent_address': parent_address,
                'host': host,
                'name': (name or '').strip() or parent_address,
                'child_addrs': child_addrs,
            }

        for address in list(self.poly.getNodes()):
            node = self.poly.getNode(address)
            if node is None:
                continue
            cfg = getattr(node, 'cfg', None)
            if not self._node_is_strip_parent(node, cfg):
                continue
            name = (getattr(node, 'name', '') or '').strip()
            if not name and cfg:
                name = (cfg.get('name') or '').strip()
            host = getattr(node, 'host', None) or (cfg or {}).get('host')
            consider(address, host, name)

        return list(targets.values())

    def _purge_stale_misnamed_strip_cfg(self):
        """Drop misnamed strip-parent cfg when IoX already has a healthy parent."""
        for key in list(self.Data):
            cfg = self.get_device_cfg(key)
            if cfg is None or not self._is_strip_parent_cfg(cfg):
                continue
            if not self._is_misnamed_strip_parent_name(cfg.get('name')):
                continue
            parent_address = self._strip_parent_address_from_cfg(cfg)
            if parent_address is None:
                continue
            node = self.poly.getNode(parent_address)
            if node is not None and self._node_is_strip_parent(node):
                node_name = (getattr(node, 'name', '') or '').strip()
                if not self._is_misnamed_strip_parent_name(node_name):
                    LOGGER.info(
                        'Purging stale misnamed strip parent cfg for %s '
                        "(IoX name is '%s')",
                        parent_address,
                        node_name,
                    )
                    self.delete_cfg(cfg)
                    continue
            if node is None and self._pg3_node_meta(parent_address) is None:
                LOGGER.info(
                    'Purging orphan misnamed strip parent cfg for %s',
                    parent_address,
                )
                self.delete_cfg(cfg)

    def _is_corrupt_strip_parent(self, strip_node):
        """Detect HS300 parent strips renamed to an outlet alias (3.3.11 bug).

        A healthy strip parent is named ``SmartStrip {model}`` or a user label.
        A corrupt one was renamed to an outlet label like ``Plug 3 Testing``
        while SmartStripPlug children remain under ``{parent}01``..``{parent}06``.
        """
        cfg = getattr(strip_node, 'cfg', None)
        if not self._node_is_strip_parent(strip_node, cfg):
            return False
        parent_name = (getattr(strip_node, 'name', '') or '').strip()
        if not parent_name and cfg:
            parent_name = (cfg.get('name') or '').strip()
        if not self._is_misnamed_strip_parent_name(parent_name):
            return False
        parent_address = getattr(strip_node, 'address', None)
        if parent_address is None:
            return False
        if not self._strip_child_address_pattern(parent_address):
            return False
        host = getattr(strip_node, 'host', None) or (cfg or {}).get('host')
        return self._strip_parent_has_outlet_alias(parent_address, host, parent_name)

    def _forget_node_identity(self, node):
        if node is None:
            return
        cfg = getattr(node, 'cfg', None)
        identity = self._node_identity_key(cfg=cfg) if cfg else None
        if identity and self.nodes_by_mac.get(identity) is node:
            del self.nodes_by_mac[identity]
        address = getattr(node, 'address', None)
        if address:
            prefix = f"address_{address}"
            if self.nodes_by_mac.get(prefix) is node:
                del self.nodes_by_mac[prefix]

    def delete_cfg(self, cfg):
        if cfg is None:
            return
        key = self._cfg_storage_key(cfg)
        try:
            self.Data.delete(key)
        except KeyError:
            pass
        mac_key = self.smac(cfg.get('mac', ''))
        if mac_key and mac_key != key:
            try:
                self.Data.delete(mac_key)
            except KeyError:
                pass

    def _pg3_node_meta(self, addr):
        addr = str(addr or '').strip()
        if not addr or not hasattr(self.poly, 'getNodesFromDb'):
            return None
        try:
            rows = self.poly.getNodesFromDb([addr])
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                return rows[0]
        except Exception:
            LOGGER.debug('PG3 node meta lookup failed for %s', addr, exc_info=True)
        return None

    def _wait_for_pg3_node_gone(self, addr, timeout_sec=5.0):
        """Best-effort wait until PG3 no longer lists *addr* after removenode."""
        addr = str(addr or '').strip()
        if not addr:
            return True
        deadline = time.time() + max(0.1, float(timeout_sec))
        while time.time() < deadline:
            if self._pg3_node_meta(addr) is None:
                return True
            time.sleep(0.05)
        return self._pg3_node_meta(addr) is None

    def remove_device_node(self, address, wait_for_pg3=False):
        """Remove a node from IoX and drop its saved cfg/identity maps."""
        address = str(address or '').strip()
        if not address:
            return False
        node = self.poly.getNode(address)
        if node is None:
            cfg = self.get_device_cfg(address)
            if cfg is None:
                LOGGER.debug('remove_device_node: no node for %s', address)
                return False
            host = cfg.get('host')
            name = cfg.get('name', address)
            LOGGER.warning('Removing cfg-only node %s (%s)', name, address)
            self.delete_cfg(cfg)
            try:
                self.poly.delNode(address)
            except Exception:
                LOGGER.debug('delNode skipped for missing IoX node %s', address)
                return False
            if host:
                try:
                    self.poly.Notices.delete(f"dev_{host}".replace('.', '_'))
                except KeyError:
                    pass
            if wait_for_pg3:
                return self._wait_for_pg3_node_gone(address)
            return True
        cfg = getattr(node, 'cfg', None) or self.get_device_cfg(address)
        host = getattr(node, 'host', None)
        name = getattr(node, 'name', address)
        LOGGER.warning('Removing node %s (%s)', name, address)
        self.delete_cfg(cfg)
        self._forget_node_identity(node)
        try:
            self.poly.delNode(address)
        except Exception:
            LOGGER.error('delNode failed for %s', address, exc_info=True)
            return False
        if host:
            try:
                self.poly.Notices.delete(f"dev_{host}".replace('.', '_'))
            except KeyError:
                pass
        if wait_for_pg3:
            return self._wait_for_pg3_node_gone(address)
        return True

    def _delete_strip_for_recreation(
        self, parent_address, child_addrs, *, reason='corrupt strip cleanup'
    ):
        """Delete outlet children first, then parent, waiting for PG3 each step."""
        parent_address = str(parent_address or '').strip().lower()
        label = reason or 'corrupt strip cleanup'
        if not parent_address:
            return False
        for child_addr in sorted(child_addrs, reverse=True):
            if not self.remove_device_node(child_addr, wait_for_pg3=True):
                LOGGER.warning(
                    'Strip child %s still in PG3 after delete (%s)',
                    child_addr,
                    label,
                )
                return False
            if self.startup_connect_gap:
                time.sleep(self.startup_connect_gap)
        if not self.remove_device_node(parent_address, wait_for_pg3=True):
            LOGGER.warning(
                'Strip parent %s still in PG3 after delete (%s)',
                parent_address,
                label,
            )
            return False
        for child_addr in child_addrs:
            if self._pg3_node_meta(child_addr) is not None:
                LOGGER.warning(
                    'Strip child %s still in PG3 after parent delete (%s)',
                    child_addr,
                    label,
                )
                return False
        if self._pg3_node_meta(parent_address) is not None:
            LOGGER.warning(
                'Strip parent %s still in PG3 after delete (%s)',
                parent_address,
                label,
            )
            return False
        return True

    def cleanup_corrupt_strip_nodes(self):
        """Delete misnamed strip parents and their outlet children.

        PG3 requires children be removed before the parent. Each ``delNode`` is
        followed by a PG3 ack wait (udi-poly-homekit-hub thermostat pattern).
        Discover will recreate the strip tree with ``SmartStrip {model}``
        naming afterward.
        """
        corrupt = self._collect_corrupt_strip_targets()
        if not corrupt:
            return
        for target in corrupt:
            parent_address = target['parent_address']
            child_addrs = target['child_addrs']
            strip_name = target['name']
            host = target.get('host')
            LOGGER.warning(
                "Corrupt HS300 strip '%s' (%s): removing %s outlet child(ren) "
                "then parent so discover can rebuild",
                strip_name,
                parent_address,
                len(child_addrs),
            )
            if self._delete_strip_for_recreation(
                parent_address,
                child_addrs,
                reason='corrupt HS300 strip cleanup',
            ):
                self.set_strip_cleanup_notice(host, strip_name, len(child_addrs))
            else:
                LOGGER.warning(
                    "Corrupt HS300 strip '%s' (%s): cleanup incomplete; "
                    'will retry on next restart',
                    strip_name,
                    parent_address,
                )

    def _strip_cleanup_notice_key(self, host):
        return f"strip_cleanup_{host}".replace('.', '_')

    def set_strip_cleanup_notice(self, host, strip_name, child_count):
        """Post a session-only IoX notice after corrupt strip cleanup."""
        if not host:
            return
        if host in self._strip_cleanup_notified:
            return
        name = (strip_name or '').strip() or host
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        body = (
            f"HS300 strip '{name}' ({host}): removed {child_count} misconfigured "
            f"outlet node(s) and the misnamed strip parent; discover is "
            f"rebuilding it as SmartStrip {{model}}."
        )
        key = self._strip_cleanup_notice_key(host)
        self.Notices[key] = f"[{timestamp}] [cleanup] {body}"
        self._strip_cleanup_notified.add(host)
        LOGGER.warning(
            "Posted strip cleanup notice for %s (%s), %s outlet(s) removed",
            name,
            host,
            child_count,
        )

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

    def _notice_body_from_value(self, value):
        try:
            return value.split('] ', 2)[2]
        except (IndexError, ValueError):
            return value

    def _notice_label_from_body(self, body):
        try:
            return body.split(' (', 1)[0]
        except (IndexError, ValueError):
            return body

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
        if dev is None:
            return
        key = self._notice_key_for_device(dev)
        body = f"{self._device_display_name(dev)} ({getattr(dev, 'host', 'unknown')}): {message}"
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
        # Cooldown: same (host, source) recently written -> skip, unless we
        # are upgrading a generic "Device" label to a real IoX/PG3 name.
        cooldown_key = (key, source)
        last = self._notice_last_write.get(cooldown_key, 0.0)
        now = time.monotonic()
        upgrading_generic_label = False
        if existing is not None:
            old_body = self._notice_body_from_value(existing)
            old_label = self._notice_label_from_body(old_body)
            new_label = self._notice_label_from_body(body)
            upgrading_generic_label = (
                old_label == 'Device'
                and new_label not in (None, '', 'Device')
            )
        if (
            existing is not None
            and now - last < self._NOTICE_WRITE_COOLDOWN_SECS
            and not upgrading_generic_label
        ):
            return
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # Embed source so future calls can compare priority without extra state.
        self.poly.Notices[key] = f"[{timestamp}] [{source}] {body}"
        self._notice_last_write[cooldown_key] = now

    def clear_device_notice(self, dev):
        if dev is None:
            return
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
        s = self._host_state.setdefault(
            host, {'fail': 0, 'next_try': 0.0, 'next_probe': 0.0}
        )
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
            self.set_device_notice(
                self._notice_dev_for_host(host),
                (
                    f'Host unreachable after {s["fail"]} failures; '
                    f'pausing probes for {int(backoff)}s'
                ),
                source='connect',
            )

    def host_record_success(self, host):
        if host is None:
            return
        prev = self._host_state.get(host)
        self._host_state[host] = {'fail': 0, 'next_try': 0.0, 'next_probe': 0.0}
        if prev is not None and prev.get('fail', 0) >= self._host_breaker_threshold:
            LOGGER.info('host %s circuit reset after success', host)
            self.clear_device_notice(self._notice_dev_for_host(host))

    def host_should_quick_probe(self, host):
        """True iff the host is currently circuit-broken and the per-host
        TCP-probe cooldown (`next_probe`) has elapsed.

        Used by SmartDeviceNode._shortPoll_a to opportunistically re-test
        an offline host with a sub-second TCP connect, instead of waiting
        for next_try (up to 15 min) or for the broadcast discovery sweep
        (every ~4 min).
        """
        if host is None:
            return False
        s = self._host_state.get(host)
        if s is None:
            return False
        if s.get('fail', 0) < self._host_breaker_threshold:
            return False
        return time.monotonic() >= s.get('next_probe', 0.0)

    async def host_quick_probe(self, host, port=None, timeout=None):
        """Cheap TCP-level liveness probe for circuit-broken hosts.

        Returns True if the host accepts a TCP connect or refuses with
        RST (both prove the host is alive on the network), False on
        timeout / EHOSTDOWN / EHOSTUNREACH / ENETUNREACH.

        On alive: resets the circuit breaker via host_record_success
        (which only logs at the down -> up transition) so the next
        poll uses the real kasa protocol.
        On dead: bumps `next_probe` by host_quick_probe_interval so the
        probe doesn't fire on every shortPoll cycle.

        Never logs per-call; the only operator-visible log is the
        existing breaker-reset INFO line on the recovery transition.
        """
        if host is None:
            return False
        if port is None:
            port = self.host_quick_probe_port
        if timeout is None:
            timeout = self.host_quick_probe_timeout
        alive = False
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
            alive = True
        except ConnectionRefusedError:
            # Host is on the network and the kernel sent us a RST;
            # the kasa control port may simply be closed (e.g. Tapo
            # devices that listen on 80/443 instead). Treat as alive.
            alive = True
        except (asyncio.TimeoutError, OSError):
            alive = False
        except Exception as ex:  # noqa: BLE001
            LOGGER.debug(
                f'host_quick_probe({host}:{port}) unexpected '
                f'{type(ex).__name__}: {ex}'
            )
            alive = False
        finally:
            if writer is not None:
                try:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:  # noqa: BLE001
                        pass
                except Exception:  # noqa: BLE001
                    pass
        if alive:
            self.host_record_success(host)
        else:
            s = self._host_state.setdefault(
                host, {'fail': 0, 'next_try': 0.0, 'next_probe': 0.0}
            )
            s['next_probe'] = time.monotonic() + self.host_quick_probe_interval
        return alive

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
        existing = self._existing_node_for_dev(dev)
        if existing is not None:
            identity = self._node_identity_key(dev=dev)
            if identity is not None:
                self.devm[identity] = True
            LOGGER.debug(
                f"discover_add_device: already have node for {dev.host} -> {existing.name}"
            )
            return True
        LOGGER.info(f"Got Device\n\tAlias:{dev.alias}\n\tModel:{dev.model}\n\tMac:{dev.mac}\n\tHost:{dev.host}")
        if not await self.update_dev(dev):
            return False
        # Queue for paced addNode on the caller thread (do not block asyncio).
        self.queue_device_add(dev=dev)
        identity = self._node_identity_key(dev=dev)
        if identity is not None:
            self.devm[identity] = True
        LOGGER.debug(f"exit: {dev}")
        return True

    async def _discover(self,target):
        LOGGER.debug(f'enter: target={target}')
        await kasa.Discover.discover(
            credentials=self._kasa_credentials(),
            timeout=self.discover_timeout,
            discovery_packets=10,
            target=target,
            on_discovered=self.discover_add_device
            )
        # make sure all we know about are added in case they didn't respond this time.
        LOGGER.info(f"kasa.Discover.discover({target}) done: checking for previously known devices")
        for key in self.Data:
            LOGGER.debug(f'checking saved cfg key={key}')
            cfg = self.get_device_cfg(key)
            if cfg is None:
                continue
            identity = self._node_identity_key(cfg=cfg)
            # Session-only: skip if we already queued/added this identity.
            if identity is not None and (
                identity in self.devm or self._session_has_device(cfg=cfg)
            ):
                LOGGER.debug(f'already added identity={identity}')
                continue
            LOGGER.debug(f'cfg={cfg}')
            # If it's not in the DB, the user deleted it, so don't add it back.
            cname = self.poly.getNodeNameFromDb(cfg['address'])
            if cname is None:
                LOGGER.warning(
                    "NOT adding previously known device that didn't respond to "
                    f"discover because it was deleted from PG3: {cfg}"
                )
            else:
                # Still must addNode on restart to register the Python node,
                # even when the row already exists in the PG3 DB.
                LOGGER.warning(
                    f"Adding previously known device that didn't respond to discover: {cfg}"
                )
                self.queue_device_add(cfg=cfg)
                if identity is not None:
                    self.devm[identity] = True
        LOGGER.debug('exit')
        #return True

    async def discover_new_add_device(self,dev):
        # Track whether the discovered Device has been adopted by a
        # SmartDeviceNode (i.e. someone is going to keep using its
        # aiohttp ClientSession). Anything else must be disconnected
        # before we drop the reference; otherwise GC reaps an open
        # session and asyncio fires "Unclosed client session" /
        # "Unclosed connector" warnings every long-poll cycle.
        keep_dev = False
        try:
            LOGGER.debug(f'enter: host={dev.host}')
            smac = self.smac(dev.mac)
            LOGGER.debug(f'enter: mac={smac} dev={dev}')
            if self.is_unsupported_discovered_type(dev) and smac not in self.nodes_by_mac:
                self.log_unsupported_discovered_type(dev)
                return False
            existing = self._existing_node_for_dev(dev)
            if existing is not None:
                # Do not authenticate/update the ephemeral discovery Device
                # when we already have a long-lived node for this identity.
                # Parallel discover probes were re-setting host auth notices
                # and racing the node's own poll session.
                LOGGER.debug(
                    f'known device {dev.host} -> existing node {existing.name}'
                )
                if dev.host != existing.host:
                    LOGGER.warning(
                        f"Updating '{existing.name}' host from {existing.host} to {dev.host}"
                    )
                    existing.host = dev.host
                    if getattr(existing, 'cfg', None) is not None:
                        existing.cfg['host'] = dev.host
                if not existing.is_connected():
                    LOGGER.info(
                        f"Reconnecting known node '{existing.name}' on {dev.host}"
                    )
                    await existing.connect_a()
                return
            if not await self.update_dev(dev):
                return False
            LOGGER.debug(f'mac={smac} dev={dev}')
            if smac in self.nodes_by_mac:
                node = self.nodes_by_mac[smac]
                if dev.host != node.host:
                    LOGGER.warning(
                        f"Updating '{node.name}' host from {node.host} to {dev.host}"
                    )
                    node.host = dev.host
                    await node.connect_a()
                elif not node.is_connected():
                    LOGGER.warning(
                        f"Reconnecting '{node.name}' on {node.host}"
                    )
                    await node.connect_a()
            else:
                LOGGER.warning(f'Found a new device {dev.mac}, adding {dev.alias}')
                # Queue for paced addNode on the caller thread; keep the
                # live Device so the new node can adopt it.
                self.queue_device_add(dev=dev)
                keep_dev = True
        except Exception as ex:
            LOGGER.error(f'{ex} {dev}',exc_info=True)
        finally:
            if not keep_dev:
                await self._close_device_quietly(dev)

    async def discover_single(self, host=None):
        host = str(host or '').strip()
        LOGGER.debug(f'enter: host={host}')
        if not host:
            return None
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
                credentials=self._kasa_credentials(),
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
        self._pending_device_adds = []
        targets = self._discover_targets()
        for target in targets:
            LOGGER.info('discover_new target=%s', target)
            future = asyncio.run_coroutine_threadsafe(
                self._discover_new_a(target=target), self.mainloop
            )
            try:
                res = future.result(timeout=self.discover_future_timeout)
            except FutureTimeoutError:
                LOGGER.error(
                    '_discover_new_a(%s) timed out after %ss',
                    target,
                    self.discover_future_timeout,
                    exc_info=True,
                )
                res = None
            LOGGER.debug(f'result={res}')
        self.drain_pending_device_adds()
        self._after_inventory_sync()
        LOGGER.info("exit")

    async def _discover_new_a(self, target=None):
        if target is None:
            target = self.poly.network_interface['broadcast']
        await kasa.Discover.discover(
            credentials=self._kasa_credentials(),
            target=target,
            on_discovered=self.discover_new_add_device
            )

    def _node_identity_key(self, dev=None, cfg=None):
        """Return the stable identity key used by the add-node guard.

        Strip sockets all live behind the parent strip's MAC address, so using
        `mac` alone collapses every child onto the parent node. Prefer the
        child `device_id` for strip sockets. If the child `device_id` is not
        yet available, fall back to the child node address, not the parent MAC.
        All other devices continue to use their device MAC.
        """
        if cfg is not None and cfg.get('type') in ('SmartStripPlug', 'DeviceType.StripSocket'):
            device_id = cfg.get('device_id')
            if device_id:
                return self.smac(device_id)
            if cfg.get('address'):
                return f"address_{cfg['address']}"
            return None
        if dev is not None and str(dev.device_type) == 'DeviceType.StripSocket':
            device_id = getattr(dev, 'device_id', None)
            if device_id:
                return self.smac(device_id)
            return None
        if dev is not None:
            mac = self._dev_attr(dev, 'mac')
            if mac:
                return self.smac(mac)
        if cfg is not None and cfg.get('mac'):
            return self.smac(cfg['mac'])
        return None

    # Add a node based on dev returned from discover or the stored config.
    def add_device_node(self, parent=None, address_suffix_num=None, dev=None, cfg=None):
        LOGGER.debug(f'enter: dev={dev}')
        if parent is None:
            parent = self
        if dev is not None:
            mac  = dev.mac
            type = self._normalize_dev_type(dev)
            name = self._dev_default_name(dev)
            LOGGER.info(f"Got a {type}: {dev}")
            if address_suffix_num is None:
                address = get_valid_node_address(mac)
            else:
                address = get_valid_node_address("{}{:02d}".format(mac,address_suffix_num))
            cfg  = {
                "type": type,
                "name": get_valid_node_name(name),
                "host": dev.host,
                "mac": mac,
                "model": dev.model,
                "address": address,
            }
            if type == 'DeviceType.StripSocket':
                cfg['device_id'] = getattr(dev, 'device_id', None)
        elif cfg is not None:
            if self._is_strip_parent_cfg(cfg):
                model = cfg.get('model') or 'Strip'
                name = get_valid_node_name(f'SmartStrip {model}')
                cfg['name'] = name
            else:
                name = cfg['name']
        else:
            LOGGER.error(f"INTERNAL ERROR: dev={dev} and cfg={cfg}")
            return False
        # Session-only idempotency (issue #25 / rediscover). On restart we
        # always call addNode so PG3 registers the Python node even when the
        # row already exists in the DB. Within one process, track what we
        # already added so rediscover does not addNode again.
        if self._session_has_device(dev=dev, cfg=cfg):
            existing = self._session_node(dev=dev, cfg=cfg)
            LOGGER.debug(
                f"Device already added this session type={cfg['type']} "
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
            elif cfg['type'] in (
                'SmartDimmer',
                'DeviceType.WallSwitch',
                'DeviceType.Dimmer',
            ):
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
            self._remember_session_node(node, dev=dev, cfg=cfg)
        LOGGER.debug(f'exit: dev={dev}')
        return node

    def queue_device_add(self, **kwargs):
        """Queue a device for paced addNode on the caller thread."""
        self._pending_device_adds.append(kwargs)

    def drain_pending_device_adds(self):
        """Serially addNode pending devices with PG3 ACK waits."""
        pending = self._pending_device_adds
        self._pending_device_adds = []
        LOGGER.info('Draining %s pending device add(s)', len(pending))
        for item in pending:
            try:
                self.add_device_node(**item)
            except Exception:
                LOGGER.error('pending device add failed: %r', item, exc_info=True)

    def _session_address_key(self, address):
        address = str(address or '').strip().lower()
        if not address:
            return None
        return f"address_{address}"

    def _session_has_device(self, dev=None, cfg=None):
        return self._session_node(dev=dev, cfg=cfg) is not None

    def _session_node(self, dev=None, cfg=None):
        identity = self._node_identity_key(dev=dev, cfg=cfg)
        if identity and identity in self.nodes_by_mac:
            return self.nodes_by_mac[identity]
        address = None
        if cfg is not None:
            address = cfg.get('address')
        if address is None and dev is not None and getattr(dev, 'mac', None):
            address = get_valid_node_address(dev.mac)
        addr_key = self._session_address_key(address)
        if addr_key and addr_key in self.nodes_by_mac:
            return self.nodes_by_mac[addr_key]
        return None

    def _remember_session_node(self, node, dev=None, cfg=None):
        if node is None:
            return
        identity = self._node_identity_key(dev=dev, cfg=cfg)
        if identity is not None:
            self.nodes_by_mac[identity] = node
        address = getattr(node, 'address', None) or (cfg or {}).get('address')
        addr_key = self._session_address_key(address)
        if addr_key is not None:
            self.nodes_by_mac[addr_key] = node

    def add_node(self, address, node):
        LOGGER.debug(f"Adding: {node.name}")
        self.poly.addNode(node)
        self.wait_for_node_done(address)
        if self.add_node_gap:
            time.sleep(self.add_node_gap)
        gnode = self.poly.getNode(address)
        if gnode is None:
            msg = f'Failed to add node address {address}'
            LOGGER.error(msg)
            #self.inc_error(msg)
        else:
            self.sync_node_name(node, node.name, on_add=True)
        return gnode

    def _ensure_credentials(self):
        """Ensure self.credentials is a usable kasa.Credentials instance."""
        if self.credentials is None:
            self.credentials = kasa.Credentials('none', 'none')
            return
        user = getattr(self.credentials, 'username', None)
        if user is None:
            user = getattr(self.credentials, 'user', None)
        password = getattr(self.credentials, 'password', None)
        user = 'none' if user is None else str(user)
        password = 'none' if password is None else str(password)
        self.credentials = kasa.Credentials(user, password)

    def _kasa_credentials(self):
        self._ensure_credentials()
        return self.credentials

    def smac(self,mac):
        return re.sub(r'[:]+', '', mac)

    def exist_device_param(self,mac):
        return True if self.smac(mac) in self.Data else False

    def _cfg_storage_key(self, cfg):
        """Stable customdata key for a saved device cfg.

        Strip sockets share the parent MAC in cfg['mac']; store each child
        under its unique device_id or IoX address so configs do not overwrite
        each other (and so rediscover can reload every outlet).
        """
        device_id = cfg.get('device_id')
        if cfg.get('type') in ('SmartStripPlug', 'DeviceType.StripSocket') and device_id:
            return self.smac(device_id)
        if cfg.get('address'):
            return cfg['address']
        return self.smac(cfg['mac'])

    def save_cfg(self,cfg):
        key = self._cfg_storage_key(cfg)
        LOGGER.debug(f'Saving config key={key}: {cfg}')
        self.Data[key] = json.dumps(cfg)

    def get_device_cfg(self,key):
        raw = self.Data.get(key)
        if raw is None:
            raw = self.Data.get(self.smac(key))
        if raw is None:
            return None
        return self.cfg_to_dict(raw)
 
    def cfg_to_dict(self,cfg):
        if isinstance(cfg, dict):
            return cfg
        try:
            cfgd = json.loads(cfg)
        except (json.JSONDecodeError, TypeError) as err:
            LOGGER.error('failed to parse cfg=%r Error: %s', cfg, err)
            return None
        if not isinstance(cfgd, dict):
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
        LOGGER.debug('enter: Loading typed data now %s', redact_sensitive_params(params))
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
                msg = (
                    'Kasa credentials not configured — enter your TP-Link email and '
                    'password in Custom Parameters (both case-sensitive). Use none/none '
                    'only for older local-only devices.'
                )
                self.poly.Notices['credentials'] = msg
                LOGGER.error(msg)
            self.credential_error = True
            self.credentials = kasa.Credentials('none', 'none')
        else:
            if self.credential_error:
                self.poly.Notices.delete('credentials')
            self.credential_error = False
            # PG3 may deliver non-str values; kasa requires str credentials.
            self.credentials = kasa.Credentials(
                str(self.Parameters['user']),
                str(self.Parameters['password']),
            )
        #self.check_params()
        self.handler_params_st = True

    def _manual_device_host(self, mdev):
        """Extract host/IP from a manual device typed row."""
        if not isinstance(mdev, dict):
            return None
        address = str(mdev.get('address') or '').strip()
        if not address:
            return None
        match = _MANUAL_HOST_RE.match(address)
        if match:
            return match.group(1)
        return address

    def _normalize_inventory_name(self, name):
        return str(name or '').strip().lower()

    def _normalize_device_row(self, row):
        if not isinstance(row, dict):
            return {'address': '', 'name': ''}
        host = self._manual_device_host(row) or ''
        name = str(row.get('name') or '').strip()
        return {'address': host, 'name': name}

    def _devices_rows_equal(self, current, proposed):
        cur = [self._normalize_device_row(r) for r in (current or [])]
        prop = [self._normalize_device_row(r) for r in (proposed or [])]
        return cur == prop

    def _iter_unique_nodes(self):
        seen = set()
        for node in self.nodes_by_mac.values():
            addr = getattr(node, 'address', None)
            if addr is None or addr in seen:
                continue
            seen.add(addr)
            yield node

    def _record_type_label(self, node, cfg):
        node_id = getattr(node, 'id', None) if node is not None else None
        if node_id:
            return str(node_id)
        cfg = cfg or {}
        return str(cfg.get('type') or '')

    def _record_kasa_type_label(self, node, cfg):
        cfg = cfg or {}
        kasa_type = cfg.get('type')
        if kasa_type:
            return str(kasa_type)
        dev = getattr(node, 'dev', None) if node is not None else None
        device_type = getattr(dev, 'device_type', None) if dev is not None else None
        if device_type is not None:
            return str(device_type)
        return ''

    def _remember_manual_host_lookup(self, host, dev):
        """Cache name/MAC for a manual IP so typed rows can fill before IoX host sync."""
        host = str(host or '').strip()
        if not host or dev is None:
            return
        alias = (getattr(dev, 'alias', None) or '').strip()
        if not alias:
            identity = self._node_identity_key(dev=dev)
            existing = self.nodes_by_mac.get(identity) if identity else None
            if existing is not None:
                alias = (getattr(existing, 'name', None) or '').strip()
                if not alias:
                    existing_cfg = getattr(existing, 'cfg', None) or {}
                    alias = (existing_cfg.get('name') or '').strip()
        if alias:
            self._manual_host_names[host] = alias
        mac = self._dev_attr(dev, 'mac')
        if mac:
            self._manual_host_identity[host] = self.smac(mac)

    def _collect_known_device_records(self):
        """All known IoX devices for config doc and typed-row matching."""
        records = []
        seen_addresses = set()

        for node in self._iter_unique_nodes():
            addr = node.address
            cfg = getattr(node, 'cfg', None) or {}
            host = getattr(node, 'host', None) or cfg.get('host') or ''
            records.append({
                'name': getattr(node, 'name', None) or cfg.get('name') or '',
                'id': addr,
                'type': self._record_type_label(node, cfg),
                'kasa_type': self._record_kasa_type_label(node, cfg),
                'host': str(host or ''),
                'address': addr,
                'mac': cfg.get('mac'),
                'device_id': cfg.get('device_id'),
            })
            seen_addresses.add(addr)

        for key in list(self.Data):
            if str(key).startswith('_'):
                continue
            cfg = self.get_device_cfg(key)
            if not isinstance(cfg, dict):
                continue
            addr = cfg.get('address')
            if not addr or addr in seen_addresses:
                continue
            seen_addresses.add(addr)
            records.append({
                'name': cfg.get('name') or '',
                'id': addr,
                'type': self._record_type_label(None, cfg),
                'kasa_type': self._record_kasa_type_label(None, cfg),
                'host': str(cfg.get('host') or ''),
                'address': addr,
                'mac': cfg.get('mac'),
                'device_id': cfg.get('device_id'),
            })

        return records

    def _apply_host_migration_to_cfg(self, records, norm_name, new_host):
        for rec in records:
            if self._normalize_inventory_name(rec.get('name')) != norm_name:
                continue
            addr = rec.get('address')
            node = self.poly.getNode(addr) if addr else None
            if node is not None:
                node.host = new_host
                cfg = getattr(node, 'cfg', None)
                if isinstance(cfg, dict):
                    cfg['host'] = new_host
                    self.save_cfg(cfg)
            else:
                cfg = self.get_device_cfg(rec.get('mac') or addr)
                if isinstance(cfg, dict):
                    cfg['host'] = new_host
                    self.save_cfg(cfg)
            break

    def _save_manual_device_rows(self, rows):
        if self._devices_rows_equal(self.TypedData.get('devices'), rows):
            return False
        self._syncing_device_inventory = True
        try:
            self.TypedData['devices'] = rows
        finally:
            self._syncing_device_inventory = False
        return True

    def _refresh_manual_device_rows(self):
        """Update name/IP on existing user typed rows only; never add rows."""
        current = self.TypedData.get('devices')
        if not current:
            return False

        records = self._collect_known_device_records()
        live_hosts = {str(r.get('host') or '') for r in records if r.get('host')}
        host_to_name = {
            str(r['host']): r['name']
            for r in records
            if r.get('host') and r.get('name')
        }
        mac_to_name = {}
        for rec in records:
            mac = rec.get('mac')
            rec_name = str(rec.get('name') or '').strip()
            if mac and rec_name:
                mac_to_name[mac] = rec_name
        name_to_host = {}
        for rec in records:
            norm = self._normalize_inventory_name(rec.get('name'))
            host = str(rec.get('host') or '')
            if norm and host:
                name_to_host[norm] = host

        new_rows = []
        for row in current:
            new_row = dict(row) if isinstance(row, dict) else {}
            host = self._manual_device_host(new_row)
            name = str(new_row.get('name') or '').strip()

            if host and not name:
                if host in host_to_name:
                    name = str(host_to_name[host] or '').strip()
                elif host in self._manual_host_names:
                    name = str(self._manual_host_names[host] or '').strip()
                else:
                    manual_mac = self._manual_host_identity.get(host)
                    if manual_mac and manual_mac in mac_to_name:
                        name = str(mac_to_name[manual_mac] or '').strip()
                if name:
                    LOGGER.debug(
                        'Filled manual typed row name for %s -> %s',
                        host,
                        name,
                    )
                    new_row['name'] = name

            norm_name = self._normalize_inventory_name(name)
            if norm_name and norm_name in name_to_host:
                live_host = name_to_host[norm_name]
                stale = (
                    host in self._manual_failed_hosts
                    or host not in live_hosts
                    or self.host_should_skip(host)
                )
                if live_host and host and live_host != host and stale:
                    LOGGER.warning(
                        "Migrating typed device '%s' from %s to %s",
                        name,
                        host,
                        live_host,
                    )
                    new_row['address'] = live_host
                    self._apply_host_migration_to_cfg(records, norm_name, live_host)
                    host = live_host

            new_rows.append(new_row)

        return self._save_manual_device_rows(new_rows)

    def _update_config_doc(self):
        configuration_help = './CONFIG.md'
        if not os.path.isfile(configuration_help):
            return
        records = self._collect_known_device_records()
        sig = tuple(
            sorted(
                (
                    str(r.get('name') or ''),
                    str(r.get('id') or ''),
                    str(r.get('type') or ''),
                    str(r.get('kasa_type') or ''),
                    str(r.get('host') or ''),
                )
                for r in records
            )
        )
        if sig == self._config_doc_table_sig:
            return
        self._config_doc_table_sig = sig

        try:
            with open(configuration_help, 'r', encoding='utf-8') as cfg_file:
                base = markdown2.markdown(cfg_file.read())
        except Exception:
            LOGGER.error('Failed to read CONFIG.md for config doc', exc_info=True)
            return

        html_parts = [
            '<h3>Known Kasa Devices</h3>',
            '<p>All devices discovered or saved by this node server (read-only).</p>',
            '<table border="1" cellpadding="4" cellspacing="0">',
            '<thead><tr><th>Name</th><th>ID</th><th>Type</th><th>Kasa Type</th><th>IP Address</th></tr></thead>',
            '<tbody>',
        ]
        for rec in sorted(records, key=lambda r: (str(r.get('name') or '')).lower()):
            html_parts.append(
                '<tr><td>{name}</td><td>{id_}</td><td>{type_}</td><td>{kasa_type}</td><td>{host}</td></tr>'.format(
                    name=html.escape(str(rec.get('name') or '')),
                    id_=html.escape(str(rec.get('id') or '')),
                    type_=html.escape(str(rec.get('type') or '')),
                    kasa_type=html.escape(str(rec.get('kasa_type') or '')),
                    host=html.escape(str(rec.get('host') or '')),
                )
            )
        html_parts.append('</tbody></table>')
        self.poly.setCustomParamsDoc(base + '\n'.join(html_parts))

    def _after_inventory_sync(self):
        self._refresh_manual_device_rows()
        self._update_config_doc()

    def set_params(self):
        self.TypedParameters.load( 
            [
                {
                    'name': 'devices',
                    'title': 'Kasa Devices',
                    'desc': (
                        'Manually add a host or IP for devices that need direct lookup '
                        '(e.g. other VLANs). The name column is filled automatically after '
                        'the device is found. All discovered devices appear in the Known '
                        'Kasa Devices table below, not here.'
                    ),
                    'isList': True,
                    'params': [
                        {
                            'name': 'address',
                            'title': "Device host or IP",
                            'isRequired': True,
                        },
                        {
                            'name': 'name',
                            'title': 'Device name',
                            'desc': 'Filled automatically after the device at this IP is found',
                            'isRequired': False,
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
        new_hosts = {
            self._manual_device_host(d)
            for d in (self.manual_devices or [])
            if self._manual_device_host(d)
        }

        if self._syncing_device_inventory:
            self._manual_device_hosts = new_hosts
            self.handler_typeddata_st = True
            return

        if self.ready and new_hosts != getattr(self, '_manual_device_hosts', set()):
            self._manual_device_hosts = new_hosts
            self.add_manual_devices()
        else:
            self._manual_device_hosts = new_hosts
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
