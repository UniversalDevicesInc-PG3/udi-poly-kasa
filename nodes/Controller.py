
from udi_interface import Node,LOGGER,Custom,LOG_HANDLER
import logging,re,json,asyncio,signal,sys,threading,time,os,markdown2,html
from datetime import datetime
from threading import Thread,Event
from concurrent.futures import TimeoutError as FutureTimeoutError
from node_funcs import get_valid_node_name,get_valid_node_address
from safe_custom import SafeCustom, redact_sensitive_params

CONFIG_MARKDOWN_EXTRAS = ('tables', 'fenced-code-blocks')
from device_errors import (
    ERR_AUTH,
    ERR_CIRCUIT,
    ERR_NO_CREDS,
    ERR_OK,
    ERR_UNKNOWN,
    ERR_UNREACHABLE,
    err_code_for_connect_message,
    err_code_for_kasa_exception,
)
import kasa
from kasa_compat import AuthenticationError, KasaException, apply_kasa_patches
apply_kasa_patches()
from strip_models import (
    cfg_is_misclassified_plug,
    cfg_is_misclassified_strip,
    cfg_is_misclassified_strip_socket,
    dev_has_strip_children,
    dev_is_strip_parent,
    is_auto_misclassified_strip_name,
    is_strip_child_address,
    normalize_model,
    strip_plug_nodedef_id,
    upgrade_misclassified_plug_cfg,
)
from dev_python_kasa_bootstrap import (
    apply_dev_python_kasa,
    clone_dir,
    default_repo_url,
    param_enabled,
    params_require_restart,
    read_marker,
    sync_marker,
    symlink_path,
)
from nodes import SmartStripPlugNode
from nodes import SmartStripNode
from nodes import SmartPlugNode
from nodes import SmartDimmerNode
from nodes import SmartBulbNode
from nodes import SmartLightStripNode
from nodes import SmartCameraNode
from nodes import SmartHubNode
from nodes.SmartDeviceNode import SmartDeviceNode
from camera_helpers import (
    HUB_CHILD_CAMERA_TYPES,
    camera_lan_host,
    camera_model_has_battery,
    camera_snapshot_alias,
    cloud_devices_to_snapshots,
    dev_has_battery,
    hub_child_alias_from_hub_dev,
    hub_child_list_alias,
    is_auto_generated_camera_name,
    is_hub_child_camera_cfg,
    is_hub_child_dev,
    is_hub_deferred_camera_cfg,
    merge_camera_snapshots,
    normalize_mac_for_match,
)
from tapo_cloud import fetch_cloud_camera_roster

#logging.getLogger('pyHS100').setLevel(logging.DEBUG)

# We need an event loop for python-kasa since we run in a
# thread which doesn't have a loop
mainloop = asyncio.get_event_loop()

_MANUAL_HOST_RE = re.compile(r'^(\d+\.\d+\.\d+\.\d+)\s*-\s*(.+)$')

class Controller(Node):
    _CAMERA_ALIASES_KEY = '_camera_aliases'

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
        # Discover status notice: set on start, updated on finish, cleared
        # on the following longPoll (not the same cycle that finishes discover).
        self._discover_notice_clear_on_longpoll = False
        self._unsupported_device_types_logged = set()
        # MAC / device_id tokens for cameras paired under a Tapo hub.
        self._hub_child_identities = set()
        # Devices discovered in the current kasa.Discover pass; hubs first.
        self._discover_batch = []
        self._discover_batch_has_hub = False
        self._deferred_hub_cameras = []
        # Tapo cloud roster refresh (wap.tplinkcloud.com getDeviceList).
        self._cloud_roster_last_fetch = 0.0
        self._cloud_roster_refresh_secs = 300.0
        self._cloud_roster_task = None
        # Tapo aliases learned from hub child lists / discover snapshots.
        self._camera_alias_cache = {}
        self._logged_hub_managed_camera_skip = set()
        self._heartbeat_thread = None
        self._heartbeat_stop = Event()
        self._start_monotonic = time.monotonic()
        # (notice_key, source) -> monotonic timestamp of last write.
        self._notice_last_write = {}
        # host -> consecutive auth failure count; reset on successful update.
        # First failure logs at ERROR; further failures log at DEBUG.
        self._auth_fail_count = {}
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
        # Coalesce hub protocol updates so shortPoll + hub-child cameras do
        # not each run a separate dev.update() against the same H500.
        self.hub_update_coalesce_secs = 25.0
        self._hub_update_locks = {}
        self._hub_update_cache = {}
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
        # Strip outlets are driven by the parent SmartStrip; they have no
        # independent LAN connect path and must not use Controller as primary.
        if str(getattr(node, 'id', '') or '').startswith('SmartStripPlug_'):
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
            cfg = getattr(node, 'cfg', None) or self.get_device_cfg(address)
            if self.should_skip_standalone_camera_cfg(cfg):
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
        LOGGER.info(
            'Kasa Library Version %s (%s)',
            kasa.__version__,
            getattr(kasa, '__file__', 'unknown'),
        )
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
                self._migrate_misclassified_strip_plugs()
            except Exception:
                LOGGER.error('_migrate_misclassified_strip_plugs failed', exc_info=True)
            try:
                self._migrate_misclassified_strip_socket_cfg()
            except Exception:
                LOGGER.error('_migrate_misclassified_strip_socket_cfg failed', exc_info=True)
            try:
                self._purge_standalone_cameras_when_hub_present()
            except Exception:
                LOGGER.error('_purge_standalone_cameras_when_hub_present failed', exc_info=True)
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
                self._sync_hub_children_after_startup()
            except Exception:
                LOGGER.error('_sync_hub_children_after_startup failed', exc_info=True)
            try:
                self._fix_stale_misclassified_strip_names()
            except Exception:
                LOGGER.error('_fix_stale_misclassified_strip_names failed', exc_info=True)
            try:
                self._fix_stale_auto_generated_camera_names()
            except Exception:
                LOGGER.error(
                    '_fix_stale_auto_generated_camera_names failed', exc_info=True
                )
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
            # Clear a prior discover-finished notice on this cycle, before any
            # new auto-discover that may post a fresh start/finish notice.
            self._clear_discover_notice_if_due()
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
                updated = await self.update_dev(dev)
                self._remember_manual_host_lookup(host, dev)
                if (
                    str(getattr(dev, 'device_type', None)) == 'DeviceType.Camera'
                    and self._tapo_hub_known()
                ):
                    if not updated:
                        LOGGER.warning(
                            'Manual camera %s reachable but update failed; '
                            'still nesting under hub (solar cameras may sleep)',
                            host,
                        )
                    self._buffer_hub_cameras_for_adoption([dev])
                    if self.ready:
                        self._try_adopt_deferred_hub_cameras()
                    await self._close_device_quietly(dev)
                    continue
                if not updated:
                    self._manual_failed_hosts.add(host)
                    LOGGER.warning(
                        'Manual device %s reachable but update failed; skipping add',
                        host,
                    )
                    continue
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

    _DISCOVER_NOTICE_KEY = 'discover'

    def _set_discover_notice(self, message):
        """Post or update the session discover status notice."""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        body = str(message or '').strip() or 'Discovery'
        self.Notices[self._DISCOVER_NOTICE_KEY] = (
            f'[{timestamp}] [discover] {body}'
        )

    def _clear_discover_notice(self):
        try:
            self.Notices.delete(self._DISCOVER_NOTICE_KEY)
        except KeyError:
            pass
        except Exception:
            LOGGER.debug('clear discover notice failed', exc_info=True)
        self._discover_notice_clear_on_longpoll = False

    def _clear_discover_notice_if_due(self):
        if not getattr(self, '_discover_notice_clear_on_longpoll', False):
            return
        self._clear_discover_notice()

    def _finish_discover_notice(self, message='Discovery finished'):
        self._set_discover_notice(message)
        self._discover_notice_clear_on_longpoll = True

    def discover(self):
        self.devm = {}
        self._pending_device_adds = []
        self._discover_batch = []
        self._discover_batch_has_hub = False
        targets = self._discover_targets()
        LOGGER.info('enter: targets=%s', targets)
        self._discover_notice_clear_on_longpoll = False
        if targets:
            self._set_discover_notice(
                f'Discovery started ({len(targets)} network'
                f'{"s" if len(targets) != 1 else ""})'
            )
        else:
            self._set_discover_notice('Discovery started')
        try:
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
        finally:
            # Always drain: a mid-discover exception must not strand the hub
            # and other devices already queued via queue_device_add.
            self.drain_pending_device_adds()
            self.discover_done = True
            self._after_inventory_sync()
            self._finish_discover_notice('Discovery finished')
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
            auth_key = host or 'unknown'
            if self._dev_hub_deferred_context(dev):
                LOGGER.warning(
                    'Direct LAN auth failed for hub-deferred camera %s (%s); '
                    'camera may be hub-paired or sleeping: %s',
                    self._device_display_name(dev),
                    host,
                    msg,
                )
                self.host_record_failure(host)
                # Reachable enough to reject the handshake — not Host unreachable.
                err_code = (
                    ERR_NO_CREDS if not self._credentials_configured() else ERR_AUTH
                )
                self._set_host_device_err(host, err_code)
                self.clear_device_notice(dev)
                log = LOGGER.debug if was_broken else LOGGER.warning
                log(
                    'Skipping auth notice for hub-deferred camera %s (%s)',
                    self._device_display_name(dev),
                    host,
                )
            else:
                fail_count = self._auth_fail_count.get(auth_key, 0) + 1
                self._auth_fail_count[auth_key] = fail_count
                notice = self._auth_failure_notice_message(msg, fail_count)
                self.set_device_notice(dev, notice, source='auth', refresh=True)
                self._set_host_auth_fail_count(host, fail_count)
                err_code = ERR_NO_CREDS if not self._credentials_configured() else ERR_AUTH
                self._set_host_device_err(host, err_code)
                log = LOGGER.debug if fail_count > 1 else LOGGER.error
                log(
                    'Failed to authenticate %s (%s) [%s consecutive]: %s',
                    self._device_display_name(dev),
                    host,
                    fail_count,
                    msg,
                )
        except KasaException as ex:
            # KasaException already encodes the actionable bit (e.g. "Host
            # is down", "Timed out") in its message. The traceback is the
            # same vendor stack every time and just bloats the log.
            self.host_record_failure(
                host,
                protocol_fail=self._dev_is_hub(dev),
            )
            if self._dev_is_hub(dev):
                self._invalidate_hub_update_cache(hub_address=self._hub_address_for_dev(dev))
            ex_text = str(ex).lower()
            if self._dev_hub_deferred_context(dev) and (
                'getdeviceinfo not found' in ex_text
                or 'not found in {}' in ex_text
            ):
                self.clear_device_notice(dev)
                self._set_host_device_err(host, ERR_UNREACHABLE)
                log = LOGGER.debug if was_broken else LOGGER.warning
                log(
                    'Hub-deferred camera %s (%s) asleep or not ready on direct LAN: %s',
                    self._device_display_name(dev),
                    host,
                    ex,
                )
            else:
                self.set_device_notice(
                    dev,
                    f'Update failed: {type(ex).__name__}: {ex}',
                    source='update',
                )
                self._set_host_device_err(host, err_code_for_kasa_exception(ex))
                log = LOGGER.debug if was_broken else LOGGER.error
                log(f"Failed to update {ex}: {dev}")
        except KeyError as ex:
            self.host_record_failure(
                host,
                protocol_fail=self._dev_is_hub(dev),
            )
            if self._dev_is_hub(dev):
                self._invalidate_hub_update_cache(hub_address=self._hub_address_for_dev(dev))
            msg = (
                f'Update failed: incomplete child list from device ({ex}); '
                'hub/camera child data may be missing'
            )
            self.set_device_notice(dev, msg, source='update')
            self._set_host_device_err(host, ERR_COMM)
            log = LOGGER.debug if was_broken else LOGGER.error
            log(f"Failed to update {ex!r}: {dev}", exc_info=not was_broken)
        except Exception as ex:
            self.host_record_failure(
                host,
                protocol_fail=self._dev_is_hub(dev),
            )
            if self._dev_is_hub(dev):
                self._invalidate_hub_update_cache(hub_address=self._hub_address_for_dev(dev))
            self.set_device_notice(
                dev,
                f'Update failed: {type(ex).__name__}: {ex}',
                source='update',
            )
            self._set_host_device_err(host, ERR_UNKNOWN)
            if was_broken:
                LOGGER.debug(f"Failed to update {ex}: {dev}", exc_info=True)
            else:
                LOGGER.error(f"Failed to update {ex}: {dev}", exc_info=True)
        if ret:
            self.host_record_success(host)
            if host and host in self._auth_fail_count:
                prior = self._auth_fail_count.pop(host)
                LOGGER.info(
                    'host %s authenticated successfully after %s consecutive failure(s)',
                    host,
                    prior,
                )
            self._set_host_auth_fail_count(host, 0)
            self._set_host_device_err(host, ERR_OK)
            self.clear_device_notice(dev)
            if str(getattr(dev, 'device_type', None)) == 'DeviceType.Hub':
                self._remember_hub_child_aliases(dev)
                self._upgrade_generic_camera_cfg_names()
                self._fix_stale_auto_generated_camera_names()
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
            identity = self._node_identity_key(dev=dev)
            node = self.nodes_by_mac.get(identity) if identity else None
            if node is not None and str(getattr(node, 'id', '')).startswith('SmartCamera_'):
                self._sync_stale_camera_name(node, dev=dev)
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
                cfg = getattr(node, 'cfg', None) or {}
                model = cfg.get('model') or self._dev_model(getattr(node, 'dev', None))
                if (
                    is_auto_misclassified_strip_name(cname, model)
                    and not str(getattr(node, 'id', '')).startswith('SmartStrip_')
                    and node.name
                ):
                    LOGGER.warning(
                        "Replacing stale misclassified strip name '%s' with '%s' for %s",
                        cname,
                        node.name,
                        address,
                    )
                    self._apply_node_name(node, node.name)
                    return
                if (
                    is_auto_generated_camera_name(cname, model)
                    and str(getattr(node, 'id', '')).startswith('SmartCamera_')
                    and node.name
                    and not is_auto_generated_camera_name(node.name, model)
                ):
                    LOGGER.warning(
                        "Replacing stale auto-generated camera name '%s' with '%s' for %s",
                        cname,
                        node.name,
                        address,
                    )
                    self._apply_node_name(node, node.name)
                    return
                if (
                    is_auto_generated_camera_name(cname, model)
                    and str(getattr(node, 'id', '')).startswith('SmartCamera_')
                    and self._sync_stale_camera_name(node)
                ):
                    return
                LOGGER.warning(
                    "Existing node name '%s' for %s does not match requested name '%s', "
                    "NOT changing to match, set change_node_names=true to enable",
                    cname,
                    address,
                    node.name,
                )
                node.name = cname

    def _apply_node_name(self, node, new_name):
        new_name = get_valid_node_name(new_name)
        address = getattr(node, 'address', None)
        if not address or not new_name:
            return False
        try:
            self.poly.renameNode(address, new_name)
        except Exception:
            LOGGER.error('renameNode failed for %s', address, exc_info=True)
            return False
        node.name = new_name
        cfg = getattr(node, 'cfg', None)
        if cfg is not None:
            cfg['name'] = new_name
            self.save_cfg(cfg)
        self._remember_camera_alias(
            mac=(cfg or {}).get('mac'),
            device_id=(cfg or {}).get('device_id'),
            alias=new_name,
            model=(cfg or {}).get('model'),
        )
        return True

    def _fix_stale_auto_generated_camera_names(self):
        """Rename hub cameras still labeled Kasa {model} when a real alias is known."""
        for node in self._iter_unique_nodes():
            if not str(getattr(node, 'id', '')).startswith('SmartCamera_'):
                continue
            self._sync_stale_camera_name(node, dev=getattr(node, 'dev', None))

    def _sync_stale_camera_name(self, node, dev=None):
        """Replace auto-generated camera names when Tapo/manual/hub sources have one."""
        if node is None or not str(getattr(node, 'id', '')).startswith('SmartCamera_'):
            return False
        cfg = getattr(node, 'cfg', None) or {}
        dev = dev if dev is not None else getattr(node, 'dev', None)
        model = cfg.get('model') or self._dev_model(dev)
        current = (getattr(node, 'name', '') or cfg.get('name') or '').strip()
        if hasattr(self.poly, 'getNodeNameFromDb'):
            db_name = (self.poly.getNodeNameFromDb(node.address) or '').strip()
            if db_name:
                current = db_name
        if not is_auto_generated_camera_name(current, model):
            return False
        preferred = self._preferred_hub_camera_name(
            dev=dev,
            cfg=cfg,
            address=node.address,
            model=model,
        )
        if not preferred or is_auto_generated_camera_name(preferred, model):
            return False
        if preferred == current:
            return False
        LOGGER.info(
            'Fixing stale auto-generated camera name %r -> %r for %s',
            current,
            preferred,
            node.address,
        )
        return self._apply_node_name(node, preferred)

    def _fix_stale_misclassified_strip_names(self):
        """Rename IoX nodes still labeled SmartStrip {model} after type correction."""
        for node in self._iter_unique_nodes():
            if self._node_is_strip_parent(node):
                continue
            if self._is_strip_plug_node(node):
                continue
            address = getattr(node, 'address', None)
            if is_strip_child_address(address):
                continue
            cfg = getattr(node, 'cfg', None) or {}
            model = cfg.get('model') or self._dev_model(getattr(node, 'dev', None))
            current = (getattr(node, 'name', '') or cfg.get('name') or '').strip()
            if not is_auto_misclassified_strip_name(current, model):
                if hasattr(self.poly, 'getNodeNameFromDb'):
                    db_name = self.poly.getNodeNameFromDb(node.address)
                    if db_name and is_auto_misclassified_strip_name(db_name, model):
                        current = db_name.strip()
                    else:
                        continue
                else:
                    continue
            dev = getattr(node, 'dev', None)
            if dev is not None:
                new_name = self._dev_default_name(dev)
            elif model:
                new_name = get_valid_node_name(f'Kasa {normalize_model(model)}')
            else:
                continue
            if not new_name or new_name == current:
                continue
            LOGGER.info(
                'Fixing stale misclassified strip name %r -> %r for %s',
                current,
                new_name,
                node.address,
            )
            self._apply_node_name(node, new_name)

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

    def _set_host_auth_fail_count(self, host, count):
        """Push consecutive auth-failure count to every IoX node on this host."""
        if not host:
            return
        try:
            value = int(count)
        except (TypeError, ValueError):
            value = 0
        driver = SmartDeviceNode.AUTH_FAIL_COUNT_DRIVER
        for node in self._nodes_for_host(host):
            try:
                node.setDriver(driver, value)
            except Exception as ex:
                LOGGER.debug(
                    'host %s auth fail count driver update failed: %s',
                    host,
                    ex,
                )

    def _set_host_device_err(self, host, code):
        """Push the ERR driver index to every IoX node on this host."""
        if not host:
            return
        try:
            value = int(code)
        except (TypeError, ValueError):
            value = ERR_OK
        driver = SmartDeviceNode.ERR_DRIVER
        for node in self._nodes_for_host(host):
            try:
                node.setDriver(driver, value)
            except Exception as ex:
                LOGGER.debug(
                    'host %s ERR driver update failed: %s',
                    host,
                    ex,
                )

    def set_host_device_err_from_connect(self, host, msg):
        """Set ERR from a connect-path message when it is more specific."""
        code = err_code_for_connect_message(msg)
        if code is not None:
            self._set_host_device_err(host, code)

    def _cfg_for_host(self, host):
        if not host:
            return None
        host = str(host)
        for key in self.Data:
            cfg = self.get_device_cfg(key)
            if not cfg:
                continue
            if str(cfg.get('host') or '') == host:
                return cfg
            if str(cfg.get('camera_host') or '') == host:
                return cfg
        return None

    def _dev_hub_deferred_context(self, dev, cfg=None):
        """True when *dev* is a hub-paired camera that should not get LAN auth notices."""
        if is_hub_deferred_camera_cfg(cfg):
            return True
        saved = self._cfg_for_dev(dev)
        if is_hub_deferred_camera_cfg(saved):
            return True
        host = self._dev_attr(dev, 'host')
        if host:
            host_cfg = self._cfg_for_host(host)
            if is_hub_deferred_camera_cfg(host_cfg):
                return True
            for node in self._nodes_for_host(host):
                node_cfg = getattr(node, 'cfg', None)
                if is_hub_deferred_camera_cfg(node_cfg):
                    return True
        return False

    def _clear_stale_hub_deferred_auth_notices(self):
        """Drop misleading direct-LAN auth notices left from hub-paired cameras."""
        auth_priority = self._NOTICE_SOURCE_PRIORITY.get('auth', 4)
        for key in list(self.Data):
            cfg = self.get_device_cfg(key)
            if not is_hub_deferred_camera_cfg(cfg):
                continue
            host = str(cfg.get('camera_host') or cfg.get('host') or '').strip()
            if not host:
                continue
            notice_key = f"dev_{host}".replace('.', '_')
            # Custom.__getitem__ returns None for missing keys (no KeyError).
            existing = self.poly.Notices.get(notice_key)
            if not existing:
                continue
            if self._notice_priority_from_value(existing) < auth_priority:
                continue
            if 'auth failure' not in existing.lower():
                continue
            stub = self._notice_dev_for_host(host)
            self.clear_device_notice(stub)
            LOGGER.info(
                'Cleared stale direct-LAN auth notice for hub-deferred camera at %s',
                host,
            )

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

    def _auth_failure_notice_message(self, detail, fail_count=1):
        """User-facing auth notice; differs for missing vs rejected credentials."""
        count_label = (
            f'{fail_count} consecutive auth failure'
            f'{"" if fail_count == 1 else "s"} — '
        )
        if not self._credentials_configured():
            return (
                f'{count_label}Kasa credentials not configured — enter your TP-Link '
                'email and password in Custom Parameters (both case-sensitive). '
                'Use none/none only for older local-only devices.'
            )
        detail_text = str(detail)
        camera_hint = ''
        if '-40211' in detail_text:
            camera_hint = (
                ' For Tapo cameras, use your full TP-Link cloud email (not a '
                'camera-only account), confirm Third-Party Compatibility is on '
                'in the Tapo app, and power-cycle the camera if it was recently '
                'firmware-updated.'
            )
        return (
            f'{count_label}device rejected login. Verify the Kasa email and '
            'password in Custom Parameters (case-sensitive) and confirm this '
            f'device is on the same TP-Link account in the Kasa app.{camera_hint}'
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

    _CFG_TYPE_ID_PREFIX = {
        'SmartStrip': 'SmartStrip_',
        'DeviceType.Strip': 'SmartStrip_',
        'SmartStripPlug': 'SmartStripPlug_',
        'DeviceType.StripSocket': 'SmartStripPlug_',
        'SmartPlug': 'SmartPlug_',
        'DeviceType.Plug': 'SmartPlug_',
        'SmartDimmer': 'SmartDimmer_',
        'DeviceType.WallSwitch': 'SmartDimmer_',
        'DeviceType.Dimmer': 'SmartDimmer_',
        'SmartBulb': 'SmartBulb_',
        'DeviceType.Bulb': 'SmartBulb_',
        'SmartLightStrip': 'SmartLightStrip_',
        'DeviceType.LightStrip': 'SmartLightStrip_',
        'SmartCamera': 'SmartCamera_',
        'DeviceType.Camera': 'SmartCamera_',
        'SmartHub': 'SmartHub_',
        'DeviceType.Hub': 'SmartHub_',
    }

    def _node_matches_cfg_type(self, node, cfg):
        if node is None or not isinstance(cfg, dict):
            return True
        prefix = self._CFG_TYPE_ID_PREFIX.get(cfg.get('type'))
        if not prefix:
            return True
        node_id = getattr(node, 'id', '') or ''
        return node_id.startswith(prefix)

    def _remove_mismatched_node_for_cfg(self, cfg, *, reason='type mismatch'):
        if not isinstance(cfg, dict):
            return False
        address = cfg.get('address')
        if not address:
            return False
        node = self.poly.getNode(address)
        if node is None or self._node_matches_cfg_type(node, cfg):
            return False
        LOGGER.warning(
            'Removing mismatched node %s (%s) type=%s id=%s for cfg type=%s (%s)',
            getattr(node, 'name', address),
            address,
            type(node).__name__,
            getattr(node, 'id', None),
            cfg.get('type'),
            reason,
        )
        return self.remove_device_node(address, wait_for_pg3=True)

    def _dev_model(self, dev):
        return str(getattr(dev, 'model', None) or '')

    def _dev_is_strip_parent(self, dev):
        return dev_is_strip_parent(dev)

    def _upgrade_strip_cfg_if_needed(self, cfg, dev=None):
        """Promote misclassified SmartPlug cfg to DeviceType.Strip when appropriate."""
        if not isinstance(cfg, dict):
            return cfg
        if not cfg_is_misclassified_plug(
            cfg,
            dev=dev,
            dev_is_strip_parent=self._dev_is_strip_parent,
        ):
            return cfg
        model = normalize_model(cfg.get('model') or self._dev_model(dev))
        default_name = (
            get_valid_node_name(f'SmartStrip {model}')
            if model
            else None
        )
        upgraded = upgrade_misclassified_plug_cfg(
            cfg,
            dev=dev,
            default_name=default_name,
        )
        if upgraded.get('name'):
            upgraded['name'] = get_valid_node_name(upgraded['name'])
        LOGGER.info(
            'Reclassifying misclassified plug cfg %s (%s) model=%s to %s',
            cfg.get('name'),
            cfg.get('address'),
            model,
            upgraded.get('type'),
        )
        self.save_cfg(upgraded)
        return upgraded

    def _cfg_has_strip_child_nodes(self, parent_address):
        parent_address = (parent_address or '').lower()
        if not parent_address:
            return False
        candidate_addrs = list(self._strip_child_address_pattern(parent_address))
        for num in range(1, 7):
            addr = f'{parent_address}{num:02d}'
            if addr not in candidate_addrs:
                candidate_addrs.append(addr)
        for addr in candidate_addrs:
            if self.poly.getNode(addr) is not None:
                return True
            if self._pg3_node_meta(addr) is not None:
                return True
        return False

    def _cfg_is_misclassified_strip(self, cfg):
        parent_address = self._strip_parent_address_from_cfg(cfg)
        has_child_nodes = (
            self._cfg_has_strip_child_nodes(parent_address)
            if parent_address
            else False
        )
        return cfg_is_misclassified_strip(
            cfg,
            self._STRIP_PARENT_TYPES,
            has_child_nodes=has_child_nodes,
        )

    def _cfg_as_plug(self, cfg):
        if not isinstance(cfg, dict):
            return cfg
        plug_cfg = dict(cfg)
        plug_cfg['type'] = 'SmartPlug'
        model = normalize_model(plug_cfg.get('model'))
        if model:
            plug_cfg['model'] = model
        if is_auto_misclassified_strip_name(plug_cfg.get('name'), model):
            plug_cfg['name'] = get_valid_node_name(f'Kasa {model}') if model else ''
        return plug_cfg

    def _migrate_misclassified_strip_plugs(self):
        """Rewrite saved strip cfg for plug models and remove bogus SmartStrip nodes."""
        for key in list(self.Data):
            cfg = self.get_device_cfg(key)
            if cfg is None or not self._cfg_is_misclassified_strip(cfg):
                continue
            addr = cfg.get('address')
            if not addr:
                continue
            plug_cfg = self._cfg_as_plug(cfg)
            LOGGER.info(
                'Migrating misclassified strip cfg %s (%s) model=%s to SmartPlug',
                plug_cfg.get('name'),
                addr,
                plug_cfg.get('model'),
            )
            self.remove_device_node(addr, wait_for_pg3=True)
            self.save_cfg(plug_cfg)

    def _migrate_misclassified_strip_socket_cfg(self):
        """Rewrite saved outlet cfg that used strip-parent type or nodedef."""
        for key in list(self.Data):
            cfg = self.get_device_cfg(key)
            if not cfg_is_misclassified_strip_socket(cfg):
                continue
            addr = cfg.get('address')
            if not addr:
                continue
            socket_cfg = dict(cfg)
            socket_cfg['type'] = 'DeviceType.StripSocket'
            # Keep a valid SmartStripPlug_* id so cfg-only re-add does not
            # send empty nodeDefId to PG3 (rejected as "No valid API calls").
            socket_cfg['id'] = strip_plug_nodedef_id(cfg=socket_cfg)
            if 'emeter' not in socket_cfg:
                socket_cfg['emeter'] = socket_cfg['id'].endswith('_E')
            LOGGER.info(
                'Migrating misclassified strip outlet cfg %s (%s) to StripSocket id=%s',
                socket_cfg.get('name'),
                addr,
                socket_cfg['id'],
            )
            self.remove_device_node(addr, wait_for_pg3=True)
            self.save_cfg(socket_cfg)

    def _normalize_dev_type(self, dev, *, parent=None, address_suffix_num=None):
        if parent is not self and address_suffix_num is not None:
            return 'DeviceType.StripSocket'
        dev_type = str(dev.device_type)
        if dev_type == 'DeviceType.StripSocket':
            return 'DeviceType.StripSocket'
        if self._dev_is_strip_parent(dev):
            return 'DeviceType.Strip'
        if dev_type == 'DeviceType.Unknown':
            model = self._dev_model(dev).upper()
            if model.startswith('P1') or model.startswith('KP115') or model.startswith('EP25'):
                return 'DeviceType.Plug'
        return dev_type

    def _dev_default_name(self, dev):
        if str(getattr(dev, 'device_type', None)) == 'DeviceType.StripSocket':
            alias = (getattr(dev, 'alias', None) or '').strip()
            if alias:
                return get_valid_node_name(alias)
            model = self._dev_model(dev)
            if model:
                return get_valid_node_name(f'Kasa {normalize_model(model)}')
        if self._dev_is_strip_parent(dev):
            model = self._dev_model(dev) or 'Strip'
            return get_valid_node_name(f'SmartStrip {model}')
        if str(getattr(dev, 'device_type', None)) == 'DeviceType.Hub':
            model = self._dev_model(dev) or 'Hub'
            alias = (getattr(dev, 'alias', None) or '').strip()
            if alias:
                return get_valid_node_name(alias)
            return get_valid_node_name(f'Tapo Hub {model}')
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

    def _strip_parent_node_for_socket_cfg(self, cfg):
        """Return the SmartStrip parent for a strip-outlet cfg, if present."""
        if not isinstance(cfg, dict):
            return None
        address = str(cfg.get('address') or '').lower()
        if not is_strip_child_address(address):
            return None
        parent = self.poly.getNode(address[:-2])
        if parent is None or not self._node_is_strip_parent(parent):
            return None
        return parent

    def _bind_strip_plug_session_node(self, existing, *, parent=None, dev=None):
        """Rebind a cfg-restored strip outlet to its live child device / parent."""
        if existing is None:
            return existing
        if not self._is_strip_plug_node(existing):
            return existing
        if dev is not None:
            existing.dev = dev
        if parent is not None and parent is not self:
            existing.primary_node = parent
            existing.pfx = f"{getattr(parent, 'name', parent.address)}:{existing.name}:"
        return existing

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
        if self._cfg_is_misclassified_strip(cfg):
            return False
        if cfg.get('type') in self._STRIP_PARENT_TYPES:
            return True
        return False

    @staticmethod
    def _is_misnamed_strip_parent_name(name):
        return not (name or '').strip().lower().startswith('smartstrip')

    def _node_is_strip_parent(self, node, cfg=None):
        if cfg is None:
            cfg = getattr(node, 'cfg', None)
        if cfg and self._cfg_is_misclassified_strip(cfg):
            return False
        if isinstance(node, SmartStripNode):
            return True
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

    def _clear_host_notice(self, host):
        host = str(host or '').strip()
        if not host:
            return
        try:
            self.poly.Notices.delete(f"dev_{host}".replace('.', '_'))
        except KeyError:
            pass
        except Exception:
            LOGGER.debug('clear notice failed for host %s', host, exc_info=True)

    def _forget_camera_alias_tokens(self, cfg):
        """Remove MAC / device_id tokens for a deleted camera from alias cache."""
        cache = getattr(self, '_camera_alias_cache', None)
        if not isinstance(cache, dict) or not cache:
            return
        tokens = set()
        mac_key = self._norm_camera_mac((cfg or {}).get('mac'))
        if mac_key:
            tokens.add(mac_key)
        did = self._norm_camera_device_id((cfg or {}).get('device_id'))
        if did:
            tokens.add(did)
        addr = str((cfg or {}).get('address') or '').strip().lower()
        if addr:
            tokens.add(addr)
        # Also drop any alias token that only pointed at this camera's name
        # under a superseded device_id key.
        name = str((cfg or {}).get('name') or '').strip()
        if name and mac_key:
            for token, alias in list(cache.items()):
                if token in tokens:
                    continue
                if str(alias or '').strip() != name:
                    continue
                token_did = self._norm_camera_device_id(token)
                if token_did and len(token_did) > 12:
                    tokens.add(token)
        removed = False
        for token in tokens:
            if token in cache:
                cache.pop(token, None)
                removed = True
        if removed:
            self._persist_camera_alias_cache()

    def delete_cfg(self, cfg):
        """Delete saved customdata for a device, including camera sibling keys."""
        if cfg is None:
            return
        if self._is_camera_cfg(cfg):
            entries = self._cfg_entries_for_camera(
                mac=cfg.get('mac'),
                device_id=cfg.get('device_id'),
                address=cfg.get('address'),
            )
            hosts = set()
            for key, entry_cfg in entries:
                lan = camera_lan_host(cfg=entry_cfg)
                if lan:
                    hosts.add(lan)
                host = str((entry_cfg or {}).get('host') or '').strip()
                if host:
                    hosts.add(host)
                self._delete_cfg_key(key)
                raw_did = str((entry_cfg or {}).get('device_id') or '').strip()
                if raw_did and raw_did != key:
                    self._delete_cfg_key(raw_did)
            # Address / MAC keys even when no sibling rows were found.
            address = str(cfg.get('address') or '').strip()
            if address:
                self._delete_cfg_key(address)
            mac_key = self.smac(cfg.get('mac', '')) if cfg.get('mac') else None
            if mac_key:
                self._delete_cfg_key(mac_key)
            did = str(cfg.get('device_id') or '').strip()
            if did:
                self._delete_cfg_key(did)
                self._delete_cfg_key(self.smac(did))
            self._forget_camera_alias_tokens(cfg)
            for host in hosts:
                self._clear_host_notice(host)
            self.unregister_hub_child_identity(cfg=cfg)
            LOGGER.info(
                'Deleted camera customdata for %s (mac=%s device_id=%s)',
                cfg.get('name') or cfg.get('address'),
                cfg.get('mac'),
                cfg.get('device_id'),
            )
            return
        key = self._cfg_storage_key(cfg)
        self._delete_cfg_key(key)
        mac_key = self.smac(cfg.get('mac', '')) if cfg.get('mac') else None
        if mac_key and mac_key != key:
            self._delete_cfg_key(mac_key)

    def on_node_deleted(self, node):
        """IoX DELETE handler: drop customdata / notices / identity for *node*."""
        if node is None:
            return
        cfg = getattr(node, 'cfg', None)
        if cfg is None:
            address = getattr(node, 'address', None)
            if address:
                cfg = self.get_device_cfg(address)
        if cfg is None and getattr(node, 'address', None):
            # Build a minimal cfg so camera sibling / alias cleanup still runs.
            cfg = {
                'type': getattr(node, 'id', None) or 'DeviceType.Camera',
                'address': node.address,
                'mac': getattr(node, 'mac', None),
                'host': getattr(node, 'host', None),
                'name': getattr(node, 'name', None),
                'device_id': getattr(node, 'device_id', None),
            }
            if str(cfg.get('type', '')).startswith('SmartCamera'):
                cfg['type'] = 'DeviceType.Camera'
        LOGGER.warning(
            'Node deleted in IoX: %s (%s); clearing customdata',
            getattr(node, 'name', None),
            getattr(node, 'address', None),
        )
        self.delete_cfg(cfg)
        self._forget_node_identity(node)
        host = getattr(node, 'host', None) or (cfg or {}).get('host')
        if host:
            self._clear_host_notice(host)
        lan = camera_lan_host(cfg=cfg) if cfg else None
        if lan and lan != host:
            self._clear_host_notice(lan)

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
                self._clear_host_notice(host)
            lan = camera_lan_host(cfg=cfg)
            if lan and lan != host:
                self._clear_host_notice(lan)
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
            self._clear_host_notice(host)
        lan = camera_lan_host(cfg=cfg) if cfg else None
        if lan and lan != host:
            self._clear_host_notice(lan)
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
            self.cleanup_wrong_type_strip_children()
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

        self.cleanup_wrong_type_strip_children()

    def cleanup_wrong_type_strip_children(self):
        """Remove strip outlet addresses that use a strip-parent nodedef."""
        for address in list(self.poly.getNodes()):
            node = self.poly.getNode(address)
            if node is None:
                continue
            if not is_strip_child_address(address):
                continue
            if self._is_strip_plug_node(node):
                continue
            LOGGER.warning(
                "Corrupt strip outlet %s (%s): removing type=%s id=%s",
                getattr(node, 'name', address),
                address,
                type(node).__name__,
                getattr(node, 'id', None),
            )
            self.remove_device_node(address, wait_for_pg3=True)

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

    def set_device_notice(self, dev, message, source='state', refresh=False):
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
            not refresh
            and existing is not None
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
        if not value or not isinstance(value, str):
            return 0
        try:
            after_ts = value.split('] ', 1)[1]
            tag = after_ts.split(']', 1)[0].lstrip('[')
        except (AttributeError, IndexError, ValueError):
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

    def _dev_is_hub(self, dev):
        return str(getattr(dev, 'device_type', None)) == 'DeviceType.Hub'

    def _hub_address_for_dev(self, dev):
        if dev is None:
            return None
        mac = getattr(dev, 'mac', None)
        if mac:
            return get_valid_node_address(mac)
        host = getattr(dev, 'host', None)
        if not host:
            return None
        node = self._notice_dev_for_host(host)
        return getattr(node, 'address', None) if node is not None else None

    def _hub_update_lock(self, hub_address):
        lock = self._hub_update_locks.get(hub_address)
        if lock is None:
            lock = asyncio.Lock()
            self._hub_update_locks[hub_address] = lock
        return lock

    def _invalidate_hub_update_cache(self, hub_address=None):
        if hub_address:
            self._hub_update_cache.pop(hub_address, None)

    async def hub_node_update_a(self, hub_node):
        """Run at most one hub dev.update() per hub per coalesce window.

        Hub shortPoll and each hub-child camera previously called update_a()
        independently, which could stack many slow H500 queries every 30s.
        """
        if hub_node is None:
            return False
        hub_address = getattr(hub_node, 'address', None)
        if not hub_address:
            ok = await hub_node.update_device_a()
            if ok:
                self._refresh_hub_camera_naming(hub_node)
            return ok
        lock = self._hub_update_lock(hub_address)
        async with lock:
            now = time.monotonic()
            cached = self._hub_update_cache.get(hub_address)
            if cached and (now - cached[0]) < self.hub_update_coalesce_secs:
                return cached[1]
            ok = await hub_node.update_device_a()
            self._hub_update_cache[hub_address] = (time.monotonic(), ok)
            if ok:
                self._refresh_hub_camera_naming(hub_node)
            return ok

    def _refresh_hub_camera_naming(self, hub_node=None):
        """Learn hub child nicknames and rename stale Kasa {model} cameras."""
        self._schedule_tapo_cloud_roster_refresh()
        hub_node = hub_node or self._get_hub_node()
        if hub_node is None:
            return
        hub_dev = getattr(hub_node, 'dev', None)
        if hub_dev is not None:
            self._remember_hub_child_aliases(hub_dev)
        self._upgrade_generic_camera_cfg_names()
        self._fix_stale_auto_generated_camera_names()

    def host_hub_protocol_degraded(self, host):
        """True when the host recently failed a kasa protocol query.

        Used to skip hub-child direct-LAN fallbacks that add more load while
        the H500 is wedged but still answers cheap TCP probes on 443.
        """
        if host is None:
            return False
        s = self._host_state.get(host)
        if s is None:
            return False
        return bool(s.get('protocol_fail')) and s.get('fail', 0) > 0

    def host_record_failure(self, host, protocol_fail=False):
        if host is None:
            return
        s = self._host_state.setdefault(
            host,
            {'fail': 0, 'next_try': 0.0, 'next_probe': 0.0, 'protocol_fail': False},
        )
        s['fail'] += 1
        if protocol_fail:
            s['protocol_fail'] = True
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
            self._set_host_device_err(host, ERR_CIRCUIT)

    def host_record_success(self, host):
        if host is None:
            return
        prev = self._host_state.get(host)
        self._host_state[host] = {
            'fail': 0,
            'next_try': 0.0,
            'next_probe': 0.0,
            'protocol_fail': False,
        }
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
        if s.get('protocol_fail'):
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
            s = self._host_state.get(host)
            if s and s.get('protocol_fail'):
                s['next_probe'] = time.monotonic() + self.host_quick_probe_interval
                LOGGER.debug(
                    'host %s TCP alive but kasa protocol still failing; '
                    'not resetting circuit breaker',
                    host,
                )
            else:
                self.host_record_success(host)
        else:
            s = self._host_state.setdefault(
                host,
                {'fail': 0, 'next_try': 0.0, 'next_probe': 0.0, 'protocol_fail': False},
            )
            s['next_probe'] = time.monotonic() + self.host_quick_probe_interval
        return alive

    def is_unsupported_discovered_type(self, dev):
        """Skip device classes this plugin cannot yet represent as nodes."""
        return False

    def _hub_child_identity_tokens(self, dev=None, cfg=None):
        tokens = set()
        if dev is not None:
            device_id = self._dev_attr(dev, 'device_id')
            if device_id:
                tokens.add(self.smac(device_id))
            mac = self._dev_attr(dev, 'mac')
            if mac:
                tokens.add(self.smac(mac))
        if cfg is not None:
            device_id = cfg.get('device_id')
            if device_id:
                tokens.add(self.smac(device_id))
            mac = cfg.get('mac')
            if mac:
                tokens.add(self.smac(mac))
        return tokens

    def register_hub_child_identity(self, dev=None, cfg=None):
        for token in self._hub_child_identity_tokens(dev=dev, cfg=cfg):
            self._hub_child_identities.add(token)

    def unregister_hub_child_identity(self, dev=None, cfg=None):
        for token in self._hub_child_identity_tokens(dev=dev, cfg=cfg):
            self._hub_child_identities.discard(token)

    def _register_hub_children_from_dev(self, dev):
        """Record hub-paired camera identities so standalone discover can skip them."""
        if dev is None or str(getattr(dev, 'device_type', None)) != 'DeviceType.Hub':
            return
        children = getattr(dev, 'children', None)
        if not children:
            return
        for child in children:
            self.register_hub_child_identity(dev=child)
        LOGGER.info(
            'Registered %s hub child camera identity token(s) from %s',
            len(children),
            SmartDeviceNode._dev_desc(dev),
        )

    def is_registered_hub_child(self, dev=None, cfg=None):
        tokens = self._hub_child_identity_tokens(dev=dev, cfg=cfg)
        return bool(tokens & self._hub_child_identities)

    def _is_hub_cfg(self, cfg):
        if not cfg:
            return False
        return cfg.get('type') in ('SmartHub', 'DeviceType.Hub')

    def _is_standalone_camera_cfg(self, cfg):
        if not cfg or cfg.get('type') not in HUB_CHILD_CAMERA_TYPES:
            return False
        return not is_hub_child_camera_cfg(cfg)

    def _cfg_iox_address(self, cfg):
        """Return the IoX node address for saved cfg (MAC-based when needed)."""
        if not cfg:
            return None
        address = str(cfg.get('address') or '').strip().lower()
        if address:
            return address
        mac = cfg.get('mac')
        if mac:
            return get_valid_node_address(mac)
        return None

    def _has_saved_hub_cfg(self):
        for key in self.Data:
            cfg = self.get_device_cfg(key)
            if self._is_hub_cfg(cfg):
                return True
        return False

    def _has_hub_node(self):
        for addr in self.poly.getNodes():
            node = self.poly.getNode(addr)
            if node is not None and getattr(node, 'id', '') == 'SmartHub_N':
                return True
        return False

    def _tapo_hub_known(self):
        if getattr(self, '_discover_batch_has_hub', False):
            return True
        if self._has_saved_hub_cfg():
            return True
        return self._has_hub_node()

    def _log_hub_managed_camera_skip(self, desc):
        if desc in self._logged_hub_managed_camera_skip:
            return
        self._logged_hub_managed_camera_skip.add(desc)
        LOGGER.warning(
            'Skipping standalone camera %s; Tapo hub manages cameras',
            desc,
        )

    @staticmethod
    def _camera_discover_snapshot(dev):
        return {
            'mac': dev.mac,
            'host': dev.host,
            'model': dev.model,
            'alias': camera_snapshot_alias(dev),
            'device_id': getattr(dev, 'device_id', None),
            'battery': dev_has_battery(dev),
        }

    def _hub_child_cfg_snapshot(self, cfg):
        name = (cfg.get('name') or '').strip()
        model = cfg.get('model')
        alias = (
            name
            if name and not is_auto_generated_camera_name(name, model)
            else None
        )
        return {
            'mac': cfg.get('mac'),
            'host': cfg.get('host'),
            'model': model,
            'alias': alias,
            'device_id': cfg.get('device_id'),
            'battery': bool(
                cfg.get('battery') or camera_model_has_battery(model)
            ),
        }

    def _cfg_for_address(self, address):
        """Return saved cfg for an IoX node address."""
        address = str(address or '').strip().lower()
        if not address:
            return None
        entries = self._cfg_entries_for_camera(address=address)
        if not entries:
            return None
        if len(entries) == 1:
            return entries[0][1]
        return self._merge_camera_cfg_dict(*[cfg for _, cfg in entries])

    def _hub_address(self):
        for addr in self.poly.getNodes():
            node = self.poly.getNode(addr)
            if node is not None and getattr(node, 'id', '') == 'SmartHub_N':
                return addr
        for key in self.Data:
            cfg = self.get_device_cfg(key)
            if self._is_hub_cfg(cfg):
                return cfg.get('address')
        return None

    def _orphan_hub_camera_cfg_from_meta(self, meta, hub_address):
        """Build minimal hub-child cfg when IoX has a node but customdata does not."""
        address = str(meta.get('address') or '').strip().lower()
        if not address:
            return None
        nodedef = str(meta.get('nodeDefId') or '')
        name = (meta.get('name') or '').strip()
        model = None
        if name.lower().startswith('kasa '):
            parts = name.split(None, 1)
            if len(parts) > 1:
                model = parts[1]
        host = None
        manual_name = self._find_manual_device_name(host=None, mac=address)
        for mdev in self.manual_devices or []:
            row_host = self._manual_device_host(mdev)
            if not row_host:
                continue
            row_name = self._manual_device_name_from_row(mdev)
            db_name = (meta.get('name') or '').strip()
            if row_name and db_name and row_name == db_name:
                host = row_host
                break
            if manual_name and row_name == manual_name:
                host = row_host
                break
        if not host:
            for mdev in self.manual_devices or []:
                row_host = self._manual_device_host(mdev)
                if row_host:
                    host = row_host
                    break
        cfg = {
            'type': 'DeviceType.Camera',
            'name': name or manual_name or 'Camera',
            'address': address,
            'mac': address,
            'hub_parent': hub_address,
            'hub_deferred': True,
            'battery': nodedef.endswith('_B'),
            'model': model,
            'host': host or '',
        }
        if camera_model_has_battery(model):
            cfg['battery'] = True
        if host:
            cfg['camera_host'] = host
        cfg['name'] = self._preferred_hub_camera_name(
            cfg=cfg,
            address=address,
            model=model,
        )
        return cfg

    def _restore_orphan_hub_cameras_from_pg3(self):
        """Re-register hub-child cameras that IoX still has but this process does not."""
        if not hasattr(self.poly, 'getNodesFromDb'):
            return
        hub_address = self._hub_address()
        if not hub_address:
            return
        restored = 0
        try:
            rows = self.poly.getNodesFromDb() or []
        except Exception:
            LOGGER.debug('getNodesFromDb failed during orphan camera restore', exc_info=True)
            return
        for meta in rows:
            if not isinstance(meta, dict):
                continue
            address = str(meta.get('address') or '').strip().lower()
            if not address or address == self.address:
                continue
            nodedef = str(meta.get('nodeDefId') or '')
            if not nodedef.startswith('SmartCamera_'):
                continue
            primary = str(meta.get('primaryNode') or '').strip().lower()
            if primary != str(hub_address).lower():
                continue
            if self._session_has_device(cfg={'address': address}):
                continue
            node = self.poly.getNode(address)
            if node is not None and str(
                getattr(node, 'id', '')
            ).startswith('SmartCamera_'):
                continue
            cfg = self._cfg_for_address(address)
            if cfg is None:
                cfg = self._orphan_hub_camera_cfg_from_meta(meta, hub_address)
            if cfg is None:
                continue
            cfg = dict(cfg)
            self._upgrade_hub_child_cfg_name(cfg)
            if not cfg.get('hub_parent'):
                cfg['hub_parent'] = hub_address
            if cfg.get('hub_deferred') is not True:
                cfg['hub_deferred'] = True
            if camera_model_has_battery(cfg.get('model')):
                cfg['battery'] = True
            LOGGER.warning(
                'Restoring orphan hub camera %s (%s) from IoX',
                cfg.get('name') or address,
                address,
            )
            self._queue_device_add_from_cfg(cfg)
            identity = self._node_identity_key(cfg=cfg)
            if identity is not None:
                self.devm[identity] = True
            restored += 1
        if restored:
            LOGGER.info('Queued %s orphan hub camera(s) for restore', restored)
            self.drain_pending_device_adds()

    def _find_saved_camera_name(self, mac=None, device_id=None, model=None):
        """Return a non-generic saved/IoX name for a camera identity."""
        mac_key = self.smac(mac) if mac else None
        did_key = self.smac(device_id) if device_id else None
        for key in self.Data:
            cfg = self.get_device_cfg(key)
            if not isinstance(cfg, dict):
                continue
            cfg_mac = self.smac(cfg.get('mac')) if cfg.get('mac') else None
            cfg_did = self.smac(cfg.get('device_id')) if cfg.get('device_id') else None
            if not (
                (mac_key and cfg_mac == mac_key)
                or (did_key and cfg_did == did_key)
            ):
                continue
            name = (cfg.get('name') or '').strip()
            if name and not is_auto_generated_camera_name(
                name, cfg.get('model') or model
            ):
                return get_valid_node_name(name)
        for addr in self.poly.getNodes():
            node = self.poly.getNode(addr)
            if node is None:
                continue
            node_cfg = getattr(node, 'cfg', None) or {}
            node_mac = self.smac(node_cfg.get('mac')) if node_cfg.get('mac') else None
            node_did = (
                self.smac(node_cfg.get('device_id'))
                if node_cfg.get('device_id')
                else None
            )
            if not (
                (mac_key and node_mac == mac_key)
                or (did_key and node_did == did_key)
            ):
                continue
            db_name = (self.poly.getNodeNameFromDb(addr) or '').strip()
            if db_name and not is_auto_generated_camera_name(db_name, model):
                return get_valid_node_name(db_name)
            node_name = (getattr(node, 'name', '') or '').strip()
            if node_name and not is_auto_generated_camera_name(node_name, model):
                return get_valid_node_name(node_name)
        return None

    def _preferred_hub_camera_name(
        self,
        dev=None,
        cfg=None,
        address=None,
        snapshot=None,
        model=None,
    ):
        """Pick the best hub-child camera IoX name from live and saved sources."""
        model = model or self._dev_model(dev) or (cfg or {}).get('model')
        alias = (getattr(dev, 'alias', None) or '').strip() if dev is not None else ''
        if alias and not is_auto_generated_camera_name(alias, model):
            return get_valid_node_name(alias)
        snap_alias = (snapshot or {}).get('alias')
        if snap_alias and not is_auto_generated_camera_name(snap_alias, model):
            return get_valid_node_name(snap_alias)
        mac = (cfg or {}).get('mac') or self._dev_attr(dev, 'mac')
        device_id = (cfg or {}).get('device_id') or getattr(dev, 'device_id', None)
        host = None
        if cfg:
            host = camera_lan_host(cfg=cfg, dev=dev) or cfg.get('host')
        elif dev is not None:
            host = getattr(dev, 'host', None)
        manual_name = self._find_manual_device_name(
            host=host,
            mac=mac,
            model=model,
        )
        if manual_name:
            return manual_name
        hub_parent = (cfg or {}).get('hub_parent')
        hub_alias = self._find_hub_child_alias(
            mac=mac,
            device_id=device_id,
            model=model,
            hub_address=hub_parent,
        )
        if hub_alias:
            return hub_alias
        cached = self._alias_from_cache(mac=mac, device_id=device_id)
        if cached:
            return cached
        address = address or (cfg or {}).get('address')
        if address:
            db_name = (self.poly.getNodeNameFromDb(address) or '').strip()
            if db_name and not is_auto_generated_camera_name(db_name, model):
                return get_valid_node_name(db_name)
        cfg_name = ((cfg or {}).get('name') or '').strip()
        if cfg_name and not is_auto_generated_camera_name(cfg_name, model):
            return get_valid_node_name(cfg_name)
        saved = self._find_saved_camera_name(
            mac=mac,
            device_id=device_id,
            model=model,
        )
        if saved:
            return saved
        if dev is not None:
            return get_valid_node_name(self._dev_default_name(dev))
        if cfg_name:
            return get_valid_node_name(cfg_name)
        return get_valid_node_name(model or 'Camera')

    def _seed_deferred_hub_cameras_from_saved_cfg(self):
        """Re-queue saved hub-child cameras (e.g. solar) that did not answer discover."""
        if not self._tapo_hub_known():
            return
        seen = {
            self.smac(s['mac'])
            for s in self._deferred_hub_cameras
            if s.get('mac')
        }
        added = 0
        for key in self.Data:
            cfg = self.get_device_cfg(key)
            if not is_hub_child_camera_cfg(cfg):
                continue
            mac = cfg.get('mac')
            if not mac:
                continue
            smac = self.smac(mac)
            if smac in seen:
                continue
            address = cfg.get('address')
            if address and self._session_has_device(cfg=cfg):
                continue
            if address and self.poly.getNode(address) is not None:
                node = self.poly.getNode(address)
                if node is not None and str(
                    getattr(node, 'id', '')
                ).startswith('SmartCamera_'):
                    continue
            self._deferred_hub_cameras.append(self._hub_child_cfg_snapshot(cfg))
            seen.add(smac)
            added += 1
        if added:
            LOGGER.info(
                'Seeded %s saved hub-child camera(s) for adoption', added
            )

    def hub_child_camera_nodes(self, hub_address):
        """Live SmartCamera nodes nested under a hub (by saved hub_parent cfg)."""
        hub_address = str(hub_address or '').strip().lower()
        nodes = []
        for addr in self.poly.getNodes():
            node = self.poly.getNode(addr)
            if node is None or not getattr(node, 'id', '').startswith('SmartCamera_'):
                continue
            node_cfg = getattr(node, 'cfg', None) or {}
            if str(node_cfg.get('hub_parent', '')).lower() == hub_address:
                nodes.append(node)
        nodes.sort(key=lambda n: getattr(n, 'address', ''))
        return nodes

    def _queue_device_add_from_cfg(self, cfg):
        cfg = self._upgrade_strip_cfg_if_needed(cfg)
        if cfg and self._is_camera_cfg(cfg):
            cfg = dict(cfg)
            self._upgrade_hub_child_cfg_name(cfg)
        parent = self
        hub_parent = cfg.get('hub_parent') if cfg else None
        if hub_parent:
            hub_node = self.poly.getNode(hub_parent)
            if hub_node is not None:
                parent = hub_node
        elif cfg and cfg.get('type') in ('SmartStripPlug', 'DeviceType.StripSocket'):
            strip_parent = self._strip_parent_node_for_socket_cfg(cfg)
            if strip_parent is None:
                LOGGER.info(
                    'Deferring strip outlet %s until strip parent is available',
                    cfg.get('address') or cfg.get('name'),
                )
                return
            parent = strip_parent
        self.queue_device_add(parent=parent, cfg=cfg)

    def _upgrade_hub_child_cfg_name(self, cfg):
        """Replace generic hub-camera cfg names when a Tapo alias is known."""
        if not cfg or not self._is_camera_cfg(cfg):
            return cfg
        model = cfg.get('model')
        preferred = self._preferred_hub_camera_name(
            cfg=cfg,
            address=cfg.get('address'),
            model=model,
        )
        if (
            preferred
            and not is_auto_generated_camera_name(preferred, model)
            and is_auto_generated_camera_name((cfg.get('name') or '').strip(), model)
        ):
            cfg['name'] = preferred
        return cfg

    def _buffer_camera_snapshots_for_adoption(self, snapshots, *, source='lan'):
        """Remember camera snapshots to nest under the hub when its LAN list is empty."""
        if not snapshots:
            return
        by_mac = {
            self.smac(s['mac']): idx
            for idx, s in enumerate(self._deferred_hub_cameras)
            if s.get('mac')
        }
        added = 0
        merged = 0
        for snap in snapshots:
            if not isinstance(snap, dict):
                continue
            mac = snap.get('mac')
            if not mac:
                continue
            smac = self.smac(mac)
            alias = snap.get('alias')
            if alias:
                self._remember_camera_alias(
                    mac=mac,
                    device_id=snap.get('device_id'),
                    alias=alias,
                    model=snap.get('model'),
                )
            if smac in by_mac:
                idx = by_mac[smac]
                self._deferred_hub_cameras[idx] = merge_camera_snapshots(
                    self._deferred_hub_cameras[idx],
                    snap,
                )
                merged += 1
            else:
                self._deferred_hub_cameras.append(dict(snap))
                by_mac[smac] = len(self._deferred_hub_cameras) - 1
                added += 1
        if added or merged:
            LOGGER.info(
                'Buffered %s %s camera snapshot(s) for hub adoption'
                '%s',
                added + merged,
                source,
                f' ({merged} merged)' if merged else '',
            )

    def _buffer_hub_cameras_for_adoption(self, camera_devs):
        """Remember LAN cameras to nest under the hub when its child list is empty."""
        if not camera_devs:
            return
        snapshots = [self._camera_discover_snapshot(dev) for dev in camera_devs]
        self._buffer_camera_snapshots_for_adoption(snapshots, source='LAN')

    def _remember_cloud_camera_aliases(self, cloud_cameras):
        for entry in cloud_cameras or []:
            if not isinstance(entry, dict):
                continue
            alias = str(entry.get('alias') or '').strip()
            model = entry.get('device_model')
            if not alias or is_auto_generated_camera_name(alias, model):
                continue
            self._remember_camera_alias(
                mac=entry.get('mac'),
                device_id=entry.get('device_id'),
                alias=alias,
                model=model,
            )

    def _apply_cloud_camera_roster(self, cloud_cameras):
        """Seed alias cache and deferred hub adoption from Tapo cloud roster."""
        if not cloud_cameras:
            return
        self._remember_cloud_camera_aliases(cloud_cameras)
        snapshots = cloud_devices_to_snapshots(cloud_cameras)
        if snapshots:
            self._buffer_camera_snapshots_for_adoption(snapshots, source='cloud')

    def _schedule_tapo_cloud_roster_refresh(self, force=False):
        """Refresh Tapo cloud camera roster when a hub is present."""
        if not self._tapo_hub_known() or not self._credentials_configured():
            return
        now = time.monotonic()
        if not force and (
            now - self._cloud_roster_last_fetch
        ) < self._cloud_roster_refresh_secs:
            return
        task = getattr(self, '_cloud_roster_task', None)
        if task is not None and not task.done():
            return

        async def _run():
            try:
                await self._refresh_tapo_cloud_roster_a(force=force)
            except Exception:
                LOGGER.error('Tapo cloud roster refresh failed', exc_info=True)

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.mainloop:
            self._cloud_roster_task = asyncio.create_task(_run())
        else:
            self._cloud_roster_task = asyncio.run_coroutine_threadsafe(
                _run(),
                self.mainloop,
            )

    async def _refresh_tapo_cloud_roster_a(self, force=False):
        """Fetch Tapo cloud getDeviceList and seed hub camera roster."""
        if not self._credentials_configured() or not self._tapo_hub_known():
            return False
        now = time.monotonic()
        if not force and (
            now - self._cloud_roster_last_fetch
        ) < self._cloud_roster_refresh_secs:
            return False
        user = str(self.Parameters.get('user') or '').strip()
        password = str(self.Parameters.get('password') or '').strip()
        insecure = str(
            self.Parameters.get('tapo_cloud_insecure_tls') or ''
        ).strip().lower() in ('1', 'true', 'yes', 'on')
        loop = asyncio.get_running_loop()
        cloud_cameras = await loop.run_in_executor(
            None,
            lambda: fetch_cloud_camera_roster(
                user,
                password,
                insecure_tls=insecure,
            ),
        )
        self._cloud_roster_last_fetch = time.monotonic()
        LOGGER.info(
            'Tapo cloud roster: %s camera(s) (H500 LAN child list may still be empty)',
            len(cloud_cameras),
        )
        self._apply_cloud_camera_roster(cloud_cameras)
        if self.ready:
            self._try_adopt_deferred_hub_cameras()
            self._upgrade_generic_camera_cfg_names()
            self._fix_stale_auto_generated_camera_names()
        return True

    def _get_hub_node(self):
        for addr in self.poly.getNodes():
            node = self.poly.getNode(addr)
            if node is not None and getattr(node, 'id', '') == 'SmartHub_N':
                return node
        return None

    def _finish_deferred_hub_adoption(self, hub_node, pending_adds):
        """Queue adopted hub cameras and drain on a worker thread."""
        if pending_adds:
            for item in pending_adds:
                self.queue_device_add(**item)
            Thread(
                target=self._drain_deferred_hub_adds,
                args=(hub_node,),
                daemon=True,
            ).start()
        self._deferred_hub_cameras = []

    def _drain_deferred_hub_adds(self, hub_node):
        try:
            self.drain_pending_device_adds()
            hub_node.refresh_child_nodes()
        except Exception:
            LOGGER.error('drain deferred hub adds failed', exc_info=True)

    def _try_adopt_deferred_hub_cameras(self):
        """Attach buffered LAN cameras under the hub when the hub API lists none."""
        self._seed_deferred_hub_cameras_from_saved_cfg()
        snapshots = self._deferred_hub_cameras
        if not snapshots:
            return
        hub_node = self._get_hub_node()
        if hub_node is None:
            LOGGER.debug(
                'Deferred %s hub camera(s) but no hub node yet',
                len(snapshots),
            )
            return
        if not hub_node.is_connected():
            LOGGER.debug(
                'Deferred %s hub camera(s) but hub not connected yet',
                len(snapshots),
            )
            return

        snapshot_copy = list(snapshots)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is self.mainloop:
            # Called from the asyncio thread (e.g. hub reconnected during
            # discover). Blocking on future.result() here deadlocks the loop
            # and causes connect/poll timeouts for every device.
            task = getattr(self, '_deferred_adoption_task', None)
            if task is not None and not task.done():
                LOGGER.debug('Deferred hub camera adoption already in progress')
                return

            async def _adopt_on_loop():
                try:
                    pending_adds = await hub_node.adopt_deferred_cameras_a(
                        snapshot_copy
                    )
                    self._finish_deferred_hub_adoption(hub_node, pending_adds)
                except Exception:
                    LOGGER.error(
                        'Deferred hub camera adoption failed', exc_info=True
                    )

            self._deferred_adoption_task = asyncio.create_task(_adopt_on_loop())
            return

        future = asyncio.run_coroutine_threadsafe(
            hub_node.adopt_deferred_cameras_a(snapshot_copy),
            self.mainloop,
        )
        try:
            pending_adds = future.result(timeout=90)
            if pending_adds:
                for item in pending_adds:
                    self.queue_device_add(**item)
                self.drain_pending_device_adds()
                hub_node.refresh_child_nodes()
            self._deferred_hub_cameras = []
        except Exception:
            LOGGER.error('Deferred hub camera adoption failed', exc_info=True)

    def should_skip_standalone_camera_cfg(self, cfg):
        if not self._is_standalone_camera_cfg(cfg):
            return False
        if not self._tapo_hub_known():
            return False
        name = cfg.get('name') or cfg.get('address') or cfg.get('mac')
        self._log_hub_managed_camera_skip(name)
        return True

    def _purge_standalone_cameras_when_hub_present(self):
        """Drop saved standalone camera cfg/nodes when a Tapo hub is configured."""
        if not self._tapo_hub_known():
            return
        for key in list(self.Data):
            cfg = self.get_device_cfg(key)
            if not self._is_standalone_camera_cfg(cfg):
                continue
            address = cfg.get('address')
            name = cfg.get('name') or address or key
            LOGGER.warning(
                'Purging standalone camera cfg %s (%s); hub manages cameras',
                name,
                address,
            )
            if address:
                self.remove_device_node(address, wait_for_pg3=False)
            else:
                self.delete_cfg(cfg)

    def _sync_hub_children_after_startup(self):
        """Ensure hub child cameras are created after startup connect."""
        self._restore_orphan_hub_cameras_from_pg3()
        self._schedule_tapo_cloud_roster_refresh(force=True)
        for addr in self.poly.getNodes():
            node = self.poly.getNode(addr)
            if node is None or getattr(node, 'id', '') != 'SmartHub_N':
                continue
            if not hasattr(node, 'add_children'):
                continue
            if not node.is_connected():
                LOGGER.debug(
                    'Hub child sync skipped for %s: hub not connected',
                    getattr(node, 'name', addr),
                )
                continue
            try:
                node.add_children()
            except Exception:
                LOGGER.error(
                    'Hub child sync failed for %s',
                    getattr(node, 'name', addr),
                    exc_info=True,
                )
        self._try_adopt_deferred_hub_cameras()
        self._fix_stale_auto_generated_camera_names()

    def should_skip_standalone_camera_discover(self, dev):
        if dev is None or str(dev.device_type) != 'DeviceType.Camera':
            return False
        if is_hub_child_dev(dev):
            return True
        if self.is_registered_hub_child(dev=dev):
            return True
        if self._tapo_hub_known():
            self._log_hub_managed_camera_skip(SmartDeviceNode._dev_desc(dev))
            return True
        return False

    def migrate_standalone_camera_to_hub_child(self, child_dev):
        """Remove a top-level camera node when the same device is a hub child."""
        mac = self._dev_attr(child_dev, 'mac')
        if not mac:
            return
        standalone_addr = get_valid_node_address(mac)
        node = self.poly.getNode(standalone_addr)
        if node is None:
            return
        node_cfg = getattr(node, 'cfg', None) or {}
        if node_cfg.get('hub_parent'):
            return
        if not getattr(node, 'id', '').startswith('SmartCamera_'):
            return
        LOGGER.info(
            'Migrating standalone camera %s to hub child (device_id=%s)',
            getattr(node, 'name', standalone_addr),
            self._dev_attr(child_dev, 'device_id'),
        )
        self.remove_device_node(standalone_addr, wait_for_pg3=True)
        self.register_hub_child_identity(dev=child_dev)

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

    async def discover_add_device(self, dev):
        LOGGER.debug(f"enter: {dev}")
        if self.is_unsupported_discovered_type(dev):
            self.log_unsupported_discovered_type(dev)
            return False
        self._discover_batch.append(dev)
        LOGGER.debug(f'buffered discover device: {SmartDeviceNode._dev_desc(dev)}')
        return True

    async def _process_discover_batch(self):
        batch = self._discover_batch
        self._discover_batch = []
        if not batch:
            return
        hubs = []
        cameras = []
        others = []
        for dev in batch:
            dt = str(getattr(dev, 'device_type', None))
            if dt == 'DeviceType.Hub':
                hubs.append(dev)
            elif dt == 'DeviceType.Camera':
                cameras.append(dev)
            else:
                others.append(dev)
        LOGGER.info(
            'Processing discover batch: %s hub(s), %s camera(s), %s other device(s)',
            len(hubs),
            len(cameras),
            len(others),
        )
        self._discover_batch_has_hub = bool(hubs)
        for dev in hubs:
            await self._adopt_discovered_device(dev)
        for dev in others:
            await self._adopt_discovered_device(dev)
        if cameras:
            if self._tapo_hub_known():
                self._buffer_hub_cameras_for_adoption(cameras)
            else:
                for dev in cameras:
                    await self._adopt_discovered_device(dev)

    async def _adopt_discovered_device(self, dev):
        LOGGER.debug(f"enter: {dev}")
        if self.should_skip_standalone_camera_discover(dev):
            LOGGER.debug(
                'Skipping standalone camera discover; managed as hub child: %s',
                SmartDeviceNode._dev_desc(dev),
            )
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
        LOGGER.info(
            f"Got Device\n\tAlias:{dev.alias}\n\tModel:{dev.model}\n\tMac:{dev.mac}\n\tHost:{dev.host}"
        )
        if not await self.update_dev(dev):
            return False
        if str(getattr(dev, 'device_type', None)) == 'DeviceType.Hub':
            self._register_hub_children_from_dev(dev)
        self.queue_device_add(dev=dev)
        identity = self._node_identity_key(dev=dev)
        if identity is not None:
            self.devm[identity] = True
        LOGGER.debug(f'exit: {dev}')
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
        await self._process_discover_batch()
        # make sure all we know about are added in case they didn't respond this time.
        LOGGER.info(f"kasa.Discover.discover({target}) done: checking for previously known devices")
        for key in self.Data:
            LOGGER.debug(f'checking saved cfg key={key}')
            cfg = self.get_device_cfg(key)
            if cfg is None:
                continue
            if self.should_skip_standalone_camera_cfg(cfg):
                continue
            if is_hub_child_camera_cfg(cfg):
                LOGGER.debug(
                    'skipping saved hub-child camera %s; deferred hub adoption handles it',
                    cfg.get('name') or key,
                )
                continue
            # HS300 outlets are children of the strip parent. Restoring them
            # as top-level nodes parents them under the controller (no
            # is_connected) and leaves node.dev unbound until add_children.
            if cfg.get('type') in ('SmartStripPlug', 'DeviceType.StripSocket'):
                LOGGER.debug(
                    'skipping saved strip outlet %s; parent strip add_children handles it',
                    cfg.get('name') or key,
                )
                continue
            address = self._cfg_iox_address(cfg)
            if not address:
                LOGGER.debug('saved cfg has no IoX address, skipping key=%s', key)
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
            cname = self.poly.getNodeNameFromDb(address)
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
                cfg = dict(cfg)
                self._upgrade_hub_child_cfg_name(cfg)
                self._queue_device_add_from_cfg(cfg)
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
            if self.should_skip_standalone_camera_discover(dev):
                LOGGER.debug(
                    'Skipping standalone camera discover; managed as hub child: %s',
                    SmartDeviceNode._dev_desc(dev),
                )
                if (
                    str(getattr(dev, 'device_type', None)) == 'DeviceType.Camera'
                    and self._tapo_hub_known()
                ):
                    self._buffer_hub_cameras_for_adoption([dev])
                return False
            existing = self._existing_node_for_dev(dev)
            if existing is not None:
                existing_cfg = getattr(existing, 'cfg', None) or {}
                upgraded_cfg = self._upgrade_strip_cfg_if_needed(existing_cfg, dev=dev)
                if upgraded_cfg.get('type') != existing_cfg.get('type'):
                    LOGGER.warning(
                        'Reclassifying existing node %s (%s) from %s to %s',
                        getattr(existing, 'name', existing.address),
                        existing.address,
                        existing_cfg.get('type'),
                        upgraded_cfg.get('type'),
                    )
                    self.remove_device_node(existing.address, wait_for_pg3=True)
                    self.queue_device_add(dev=dev, cfg=upgraded_cfg)
                    keep_dev = True
                    return
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
        except kasa.UnsupportedDeviceError as ex:
            LOGGER.info(
                'discover_single unsupported for %s (%s); trying direct connect',
                host,
                ex,
            )
            dev = await kasa.Discover.try_connect_all(
                host,
                credentials=self._kasa_credentials(),
                timeout=self.discover_single_timeout,
            )
            if dev is None:
                self.host_record_failure(host)
                raise
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
        self._discover_notice_clear_on_longpoll = False
        if targets:
            self._set_discover_notice(
                f'Discovery started ({len(targets)} network'
                f'{"s" if len(targets) != 1 else ""})'
            )
        else:
            self._set_discover_notice('Discovery started')
        try:
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
            self._schedule_tapo_cloud_roster_refresh(force=True)
            self._try_adopt_deferred_hub_cameras()
            self._after_inventory_sync()
        finally:
            self._finish_discover_notice('Discovery finished')
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
        Hub-child cameras use the same device_id rule. All other devices
        continue to use their device MAC.
        """
        if cfg is not None and is_hub_child_camera_cfg(cfg):
            device_id = cfg.get('device_id')
            if device_id:
                return self.smac(device_id)
            if cfg.get('address'):
                return f"address_{cfg['address']}"
            return None
        if cfg is not None and cfg.get('type') in ('SmartStripPlug', 'DeviceType.StripSocket'):
            device_id = cfg.get('device_id')
            if device_id:
                return self.smac(device_id)
            if cfg.get('address'):
                return f"address_{cfg['address']}"
            return None
        if dev is not None and is_hub_child_dev(dev):
            device_id = getattr(dev, 'device_id', None)
            if device_id:
                return self.smac(device_id)
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
    def add_device_node(
        self,
        parent=None,
        address_suffix_num=None,
        dev=None,
        cfg=None,
        hub_deferred=False,
        camera_snapshot=None,
    ):
        LOGGER.debug(f'enter: dev={dev}')
        if parent is None:
            parent = self
        if cfg is not None and cfg.get('hub_parent'):
            hub_node = self.poly.getNode(cfg['hub_parent'])
            if hub_node is not None:
                parent = hub_node
        elif (
            cfg is not None
            and cfg.get('type') in ('SmartStripPlug', 'DeviceType.StripSocket')
            and parent is self
        ):
            strip_parent = self._strip_parent_node_for_socket_cfg(cfg)
            if strip_parent is None:
                LOGGER.info(
                    'Deferring strip outlet %s until strip parent is available',
                    cfg.get('address') or cfg.get('name'),
                )
                return False
            parent = strip_parent
        if cfg is not None and cfg.get('hub_deferred'):
            hub_deferred = True
        if dev is not None:
            mac  = dev.mac
            type = self._normalize_dev_type(
                dev,
                parent=parent,
                address_suffix_num=address_suffix_num,
            )
            if parent is not self and type == 'DeviceType.Camera':
                name = self._preferred_hub_camera_name(
                    dev=dev,
                    snapshot=camera_snapshot,
                    model=self._dev_model(dev),
                )
            else:
                name = self._dev_default_name(dev)
            LOGGER.info(f"Got a {type}: {dev}")
            if (
                parent is not self
                and type == 'DeviceType.Camera'
            ):
                address = get_valid_node_address(mac)
            elif address_suffix_num is None:
                address = get_valid_node_address(mac)
            elif parent is not self:
                address = get_valid_node_address(
                    "{}{:02d}".format(parent.address, address_suffix_num)
                )
            else:
                address = get_valid_node_address(
                    "{}{:02d}".format(mac, address_suffix_num)
                )
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
            elif type == 'DeviceType.Camera' and parent is not self:
                cfg['device_id'] = getattr(dev, 'device_id', None)
                cfg['hub_parent'] = parent.address
                if hub_deferred:
                    cfg['hub_deferred'] = True
                    if dev_has_battery(dev) or camera_model_has_battery(dev.model):
                        cfg['battery'] = True
                    lan_host = camera_lan_host(cfg=cfg, dev=dev, hub_host=parent.host)
                    if lan_host:
                        cfg['camera_host'] = lan_host
                else:
                    cfg['host'] = parent.host
                    lan_host = camera_lan_host(cfg=cfg, dev=dev, hub_host=parent.host)
                    if lan_host:
                        cfg['camera_host'] = lan_host
            elif type == 'DeviceType.Hub':
                pass
        elif cfg is not None:
            cfg = self._upgrade_strip_cfg_if_needed(cfg, dev=dev)
            if is_hub_child_camera_cfg(cfg) and cfg.get('mac'):
                cfg['address'] = get_valid_node_address(cfg['mac'])
                if camera_model_has_battery(cfg.get('model')):
                    cfg['battery'] = True
                cfg['name'] = self._preferred_hub_camera_name(
                    dev=dev,
                    cfg=cfg,
                    address=cfg['address'],
                    snapshot=camera_snapshot,
                    model=cfg.get('model'),
                )
            if self._is_strip_parent_cfg(cfg):
                model = cfg.get('model') or 'Strip'
                name = get_valid_node_name(f'SmartStrip {model}')
                cfg['name'] = name
            else:
                name = cfg['name']
                if is_auto_misclassified_strip_name(name, cfg.get('model')):
                    model = normalize_model(cfg.get('model'))
                    name = get_valid_node_name(f'Kasa {model}') if model else name
                    cfg['name'] = name
        else:
            LOGGER.error(f"INTERNAL ERROR: dev={dev} and cfg={cfg}")
            return False
        # Session-only idempotency (issue #25 / rediscover). On restart we
        # always call addNode so PG3 registers the Python node even when the
        # row already exists in the DB. Within one process, track what we
        # already added so rediscover does not addNode again.
        self._remove_mismatched_node_for_cfg(cfg, reason='before add_device_node')
        if self._session_has_device(dev=dev, cfg=cfg):
            existing = self._session_node(dev=dev, cfg=cfg)
            LOGGER.debug(
                f"Device already added this session type={cfg['type']} "
                f"address={cfg['address']} name='{cfg['name']}'"
            )
            # Cfg-only restore can leave strip outlets with dev=None and the
            # controller as primary; rebind when the parent strip re-adds.
            return self._bind_strip_plug_session_node(
                existing, parent=parent, dev=dev,
            )
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
            elif cfg['type'] == 'SmartCamera' or cfg['type'] == 'DeviceType.Camera':
                self.add_node(
                    cfg['address'],
                    SmartCameraNode(
                        self, parent.address, cfg['address'], cfg['name'],
                        dev=dev, cfg=cfg,
                    ),
                )
                if cfg.get('hub_parent'):
                    self.register_hub_child_identity(dev=dev, cfg=cfg)
            elif cfg['type'] == 'SmartHub' or cfg['type'] == 'DeviceType.Hub':
                self.add_node(
                    cfg['address'],
                    SmartHubNode(self, cfg['address'], cfg['name'], dev=dev, cfg=cfg),
                )
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
            if is_hub_child_camera_cfg(cfg):
                node_cfg = getattr(node, 'cfg', None) or cfg
                if camera_model_has_battery(node_cfg.get('model')):
                    node_cfg['battery'] = True
                try:
                    self.save_cfg(node_cfg)
                except Exception:
                    LOGGER.error(
                        'save_cfg failed for hub camera %s',
                        cfg.get('address'),
                        exc_info=True,
                    )
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
            node = self.nodes_by_mac[identity]
            if cfg is not None and not self._node_matches_cfg_type(node, cfg):
                return None
            return node
        address = None
        if cfg is not None:
            address = cfg.get('address')
        if address is None and dev is not None and getattr(dev, 'mac', None):
            address = get_valid_node_address(dev.mac)
        addr_key = self._session_address_key(address)
        if addr_key and addr_key in self.nodes_by_mac:
            node = self.nodes_by_mac[addr_key]
            if cfg is not None and not self._node_matches_cfg_type(node, cfg):
                return None
            return node
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
        node_id = str(getattr(node, 'id', None) or '').strip()
        if not node_id:
            LOGGER.error(
                'Refusing to add node %s (%s): empty nodeDefId '
                '(PG3 rejects addnode with nodeDefId="")',
                getattr(node, 'name', None),
                address,
            )
            return None
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

    def _norm_camera_mac(self, mac):
        return self.smac(mac).lower() if mac else None

    def _norm_camera_device_id(self, device_id):
        return self.smac(device_id).upper() if device_id else None

    def exist_device_param(self,mac):
        return True if self.smac(mac) in self.Data else False

    def _remember_camera_alias(self, mac=None, device_id=None, alias=None, model=None):
        """Cache a non-generic Tapo alias for later cfg / IoX name resolution."""
        alias = str(alias or '').strip()
        if not alias or is_auto_generated_camera_name(alias, model):
            return
        valid = get_valid_node_name(alias)
        if not hasattr(self, '_camera_alias_cache'):
            self._camera_alias_cache = {}
        mac_key = self._norm_camera_mac(mac)
        changed = False
        if mac_key and self._camera_alias_cache.get(mac_key) != valid:
            self._camera_alias_cache[mac_key] = valid
            changed = True
        did_key = self._norm_camera_device_id(device_id)
        if did_key and self._camera_alias_cache.get(did_key) != valid:
            self._camera_alias_cache[did_key] = valid
            changed = True
        if changed:
            self._persist_camera_alias_cache()

    def _load_camera_alias_cache(self):
        """Restore persisted Tapo aliases from customdata."""
        if not hasattr(self, '_camera_alias_cache'):
            self._camera_alias_cache = {}
        raw = self.Data.get(self._CAMERA_ALIASES_KEY)
        if not raw:
            return
        try:
            store = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            LOGGER.warning('Ignoring invalid %s customdata', self._CAMERA_ALIASES_KEY)
            return
        if not isinstance(store, dict):
            return
        for key, value in store.items():
            name = str(value or '').strip()
            if key and name:
                self._camera_alias_cache[str(key)] = name

    def _persist_camera_alias_cache(self):
        cache = getattr(self, '_camera_alias_cache', None) or {}
        if not cache:
            # Camera deletes can empty the cache; drop the customdata key too.
            if self._CAMERA_ALIASES_KEY in self.Data:
                self._delete_cfg_key(self._CAMERA_ALIASES_KEY)
            return
        payload = json.dumps(cache)
        if self.Data.get(self._CAMERA_ALIASES_KEY) == payload:
            return
        self.Data[self._CAMERA_ALIASES_KEY] = payload

    def _seed_camera_aliases_from_cfg(self):
        """Promote known Tapo names from saved camera cfg into the alias store."""
        for key in list(self.Data):
            if key == self._CAMERA_ALIASES_KEY:
                continue
            cfg = self.get_device_cfg(key)
            if not self._is_camera_cfg(cfg):
                continue
            name = (cfg.get('name') or '').strip()
            model = cfg.get('model')
            if name and not is_auto_generated_camera_name(name, model):
                self._remember_camera_alias(
                    mac=cfg.get('mac'),
                    device_id=cfg.get('device_id'),
                    alias=name,
                    model=model,
                )

    def _seed_camera_aliases_from_manual_devices(self):
        """Promote configured Kasa device row names into the alias store."""
        for mdev in self.manual_devices or []:
            host = self._manual_device_host(mdev)
            name = self._manual_device_name_from_row(mdev)
            if not host or not name:
                continue
            self._manual_host_names[host] = name
            row_mac = self._manual_host_identity.get(host)
            if row_mac:
                self._remember_camera_alias(mac=row_mac, alias=name)
            for key in list(self.Data):
                if key == self._CAMERA_ALIASES_KEY:
                    continue
                cfg = self.get_device_cfg(key)
                if not self._is_camera_cfg(cfg):
                    continue
                cfg_host = camera_lan_host(cfg=cfg) or cfg.get('host')
                if cfg_host != host:
                    continue
                self._remember_camera_alias(
                    mac=cfg.get('mac'),
                    device_id=cfg.get('device_id'),
                    alias=name,
                    model=cfg.get('model'),
                )

    def _snapshot_camera_cfg_names(self):
        """Capture non-generic camera names before a PG3 customdata reload."""
        names = {}
        for key in list(self.Data):
            if key == self._CAMERA_ALIASES_KEY:
                continue
            cfg = self.get_device_cfg(key)
            if not self._is_camera_cfg(cfg):
                continue
            name = (cfg.get('name') or '').strip()
            model = cfg.get('model')
            if not name or is_auto_generated_camera_name(name, model):
                continue
            valid = get_valid_node_name(name)
            mac_key = self._norm_camera_mac(cfg.get('mac'))
            did_key = self._norm_camera_device_id(cfg.get('device_id'))
            if mac_key:
                names[mac_key] = valid
            if did_key:
                names[did_key] = valid
        return names

    def _restore_camera_cfg_names_after_load(self, prior_names):
        """Keep Tapo aliases when PG3 reloads stale generic camera cfg."""
        if not prior_names:
            return
        for key in list(self.Data):
            if key == self._CAMERA_ALIASES_KEY:
                continue
            cfg = self.get_device_cfg(key)
            if not self._is_camera_cfg(cfg):
                continue
            model = cfg.get('model')
            current = (cfg.get('name') or '').strip()
            if not is_auto_generated_camera_name(current, model):
                continue
            mac_key = self._norm_camera_mac(cfg.get('mac'))
            did_key = self._norm_camera_device_id(cfg.get('device_id'))
            better = prior_names.get(mac_key) or prior_names.get(did_key)
            if not better:
                continue
            cfg = dict(cfg)
            cfg['name'] = better
            self.save_cfg(cfg)

    def _merge_camera_alias_cache(self, prior_aliases):
        """Never drop learned Tapo aliases on a PG3 customdata reload."""
        if not prior_aliases:
            return
        if not hasattr(self, '_camera_alias_cache'):
            self._camera_alias_cache = {}
        changed = False
        for key, name in prior_aliases.items():
            alias = str(name or '').strip()
            if not key or not alias:
                continue
            if self._camera_alias_cache.get(key) != alias:
                self._camera_alias_cache[key] = alias
                changed = True
        if changed:
            self._persist_camera_alias_cache()

    def _upgrade_generic_camera_cfg_names(self):
        """Rewrite generic Kasa {model} camera cfg when a Tapo alias is known."""
        upgraded = 0
        for key in list(self.Data):
            if key == self._CAMERA_ALIASES_KEY:
                continue
            cfg = self.get_device_cfg(key)
            if not self._is_camera_cfg(cfg):
                continue
            model = cfg.get('model')
            current = (cfg.get('name') or '').strip()
            if not is_auto_generated_camera_name(current, model):
                continue
            preferred = self._preferred_hub_camera_name(
                cfg=cfg,
                address=cfg.get('address'),
                model=model,
            )
            if (
                not preferred
                or is_auto_generated_camera_name(preferred, model)
                or preferred == current
            ):
                continue
            cfg['name'] = preferred
            self.save_cfg(cfg)
            upgraded += 1
        if upgraded:
            LOGGER.info('Upgraded %s generic camera cfg name(s) from saved aliases', upgraded)

    def _alias_from_cache(self, mac=None, device_id=None):
        cache = getattr(self, '_camera_alias_cache', None) or {}
        mac_key = self._norm_camera_mac(mac)
        if mac_key:
            alias = cache.get(mac_key)
            if alias:
                return alias
        did_key = self._norm_camera_device_id(device_id)
        if did_key:
            alias = cache.get(did_key)
            if alias:
                return alias
        return None

    def _remember_hub_child_aliases(self, hub_dev):
        """Populate alias cache from a hub's child_device_list response."""
        if hub_dev is None:
            return
        last_update = getattr(hub_dev, '_last_update', None) or {}
        try_get = getattr(hub_dev, '_try_get_response', None)
        if not callable(try_get):
            return
        for method in ('getChildDeviceList', 'get_child_device_list'):
            child_info = try_get(last_update, method, {}) or {}
            if not isinstance(child_info, dict):
                continue
            for info in child_info.get('child_device_list') or []:
                if not isinstance(info, dict):
                    continue
                alias = hub_child_list_alias(
                    [info],
                    mac=info.get('mac') or info.get('hw_id'),
                    device_id=info.get('device_id'),
                    model=info.get('model') or info.get('device_model'),
                )
                if alias:
                    self._remember_camera_alias(
                        mac=info.get('mac') or info.get('hw_id'),
                        device_id=info.get('device_id'),
                        alias=alias,
                        model=info.get('model') or info.get('device_model'),
                    )

    def _is_camera_cfg(self, cfg):
        return str((cfg or {}).get('type', '')) in (
            'SmartCamera',
            'DeviceType.Camera',
        )

    def _cfg_entries_for_camera(self, mac=None, device_id=None, address=None):
        """Return (storage_key, cfg) pairs for the same camera identity."""
        mac_key = self._norm_camera_mac(mac)
        did_key = self._norm_camera_device_id(device_id)
        addr_key = str(address or '').strip().lower() or None
        if mac_key and not addr_key:
            try:
                addr_key = get_valid_node_address(mac).lower()
            except Exception:
                pass
        matches = []
        seen_keys = set()
        for key in list(self.Data):
            if key == self._CAMERA_ALIASES_KEY:
                continue
            cfg = self.get_device_cfg(key)
            if not self._is_camera_cfg(cfg):
                continue
            cfg_mac = self._norm_camera_mac(cfg.get('mac'))
            cfg_did = self._norm_camera_device_id(cfg.get('device_id'))
            cfg_addr = str(cfg.get('address') or '').strip().lower()
            storage_key = str(key or '').strip()
            storage_did = self._norm_camera_device_id(storage_key)
            storage_mac = self._norm_camera_mac(storage_key)
            matched = False
            if mac_key and cfg_mac and cfg_mac == mac_key:
                matched = True
            elif mac_key and storage_mac and storage_mac == mac_key:
                matched = True
            elif did_key and cfg_did and cfg_did == did_key:
                matched = True
            elif did_key and storage_did and storage_did == did_key:
                matched = True
            elif addr_key and cfg_addr and cfg_addr == addr_key:
                matched = True
            elif addr_key and storage_key.lower() == addr_key:
                matched = True
            if matched and key not in seen_keys:
                seen_keys.add(key)
                matches.append((key, cfg))
        return matches

    def _pick_camera_cfg_name(self, cfgs, model=None):
        for cfg in cfgs:
            if not isinstance(cfg, dict):
                continue
            name = (cfg.get('name') or '').strip()
            entry_model = model or cfg.get('model')
            if name and not is_auto_generated_camera_name(name, entry_model):
                return name
        return None

    def _preferred_camera_device_id(self, *cfgs):
        """Prefer the longest cloud-style device_id when duplicates disagree.

        Hub/cloud roster IDs are typically longer than older short IDs left in
        customdata after re-adoption. First-wins merge previously kept the
        stale short ID and could re-create that customdata key forever.
        """
        best = None
        best_score = (-1, -1)
        for cfg in cfgs:
            if not isinstance(cfg, dict):
                continue
            device_id = str(cfg.get('device_id') or '').strip()
            if not device_id:
                continue
            normalized = self._norm_camera_device_id(device_id) or device_id
            # Prefer longer IDs; break ties with lexicographic order for stability.
            score = (len(normalized), normalized)
            if score > best_score:
                best = device_id
                best_score = score
        return best

    def _merge_camera_cfg_dict(self, *cfgs):
        """Merge duplicate camera cfg rows, keeping the richest identity fields."""
        merged = {}
        for cfg in cfgs:
            if not isinstance(cfg, dict):
                continue
            for key, value in cfg.items():
                if value is None or value == '':
                    continue
                if key == 'device_id':
                    continue
                if key not in merged or merged[key] in (None, ''):
                    merged[key] = value
        model = merged.get('model')
        best_name = self._pick_camera_cfg_name(cfgs, model)
        if best_name:
            merged['name'] = best_name
        preferred_did = self._preferred_camera_device_id(*cfgs)
        if preferred_did:
            merged['device_id'] = preferred_did
        for cfg in cfgs:
            if not isinstance(cfg, dict):
                continue
            lan = camera_lan_host(cfg=cfg)
            if lan and not camera_lan_host(cfg=merged):
                merged['camera_host'] = lan
                if not merged.get('host'):
                    merged['host'] = lan
        if merged.get('mac'):
            merged['address'] = get_valid_node_address(merged['mac'])
        return merged

    def _prune_stale_camera_alias_keys(self, cfg, *, keep_device_id=None):
        """Drop superseded device_id tokens from ``_camera_aliases`` for one MAC."""
        cache = getattr(self, '_camera_alias_cache', None)
        if not isinstance(cache, dict) or not cache:
            return
        mac_key = self._norm_camera_mac((cfg or {}).get('mac'))
        keep = self._norm_camera_device_id(
            keep_device_id or (cfg or {}).get('device_id')
        )
        if not mac_key:
            return
        stale = []
        for token in list(cache):
            if token == mac_key or token == keep:
                continue
            # Only prune other device_id-shaped tokens that point at this camera.
            if token == self._CAMERA_ALIASES_KEY:
                continue
            token_did = self._norm_camera_device_id(token)
            if not token_did or token_did == keep:
                continue
            # device_id tokens are uppercase hex without separators and longer
            # than a MAC (12 hex). Keep MAC aliases.
            if len(token_did) <= 12:
                continue
            # If this token is also a live camera cfg key for another MAC, keep it.
            other = self.get_device_cfg(token)
            if self._is_camera_cfg(other):
                other_mac = self._norm_camera_mac(other.get('mac'))
                if other_mac and other_mac != mac_key:
                    continue
                if other_mac == mac_key and token_did != keep:
                    stale.append(token)
                    continue
            # Alias-only stale device_id for this camera (same alias name).
            alias = str(cache.get(token) or '').strip()
            mac_alias = str(cache.get(mac_key) or '').strip()
            if alias and mac_alias and alias == mac_alias:
                stale.append(token)
        if not stale:
            return
        for token in stale:
            cache.pop(token, None)
            LOGGER.info(
                'Pruned stale camera alias key %s for mac=%s (keep device_id=%s)',
                token,
                mac_key,
                keep,
            )
        self._persist_camera_alias_cache()

    def _purge_stale_camera_cfg_keys(self, canonical_key, cfg):
        """Delete superseded customdata rows for the same camera.

        Removes sibling rows matched by MAC/device_id/address, plus any
        leftover storage key that equals a non-canonical device_id from those
        siblings (covers races where PG3 reloads an old snapshot).
        """
        entries = self._cfg_entries_for_camera(
            mac=cfg.get('mac'),
            device_id=cfg.get('device_id'),
            address=cfg.get('address'),
        )
        stale_keys = set()
        for key, entry_cfg in entries:
            if key != canonical_key:
                stale_keys.add(key)
            entry_did = self._norm_camera_device_id(
                (entry_cfg or {}).get('device_id')
            )
            canonical_did = self._norm_camera_device_id(cfg.get('device_id'))
            if (
                entry_did
                and canonical_did
                and entry_did != canonical_did
                and entry_did != canonical_key
            ):
                stale_keys.add(entry_did)
                # Also try the raw device_id spelling from the entry.
                raw = str((entry_cfg or {}).get('device_id') or '').strip()
                if raw and raw != canonical_key:
                    stale_keys.add(raw)
        cache = getattr(self, '_camera_alias_cache', None)
        for key in stale_keys:
            if key == canonical_key:
                continue
            LOGGER.info(
                'Purging stale camera customdata key %s (canonical=%s)',
                key,
                canonical_key,
            )
            self._delete_cfg_key(key)
            if isinstance(cache, dict):
                did = self._norm_camera_device_id(key)
                if did and did != self._norm_camera_device_id(cfg.get('device_id')):
                    cache.pop(did, None)
                    cache.pop(key, None)
        if isinstance(cache, dict):
            self._persist_camera_alias_cache()
        self._prune_stale_camera_alias_keys(
            cfg, keep_device_id=cfg.get('device_id')
        )

    def _dedupe_camera_cfg(self):
        """Collapse duplicate customdata rows for the same hub camera."""
        seen_macs = set()
        merged_count = 0
        for key in list(self.Data):
            if key == self._CAMERA_ALIASES_KEY:
                continue
            cfg = self.get_device_cfg(key)
            if not self._is_camera_cfg(cfg):
                continue
            mac_key = self._norm_camera_mac(cfg.get('mac'))
            if not mac_key:
                # Fall back to address-only grouping when mac is missing/malformed.
                addr = str(cfg.get('address') or key or '').strip().lower()
                if not addr or addr in seen_macs:
                    continue
                mac_key = addr
            if mac_key in seen_macs:
                continue
            seen_macs.add(mac_key)
            entries = self._cfg_entries_for_camera(
                mac=cfg.get('mac'),
                device_id=cfg.get('device_id'),
                address=cfg.get('address') or key,
            )
            if len(entries) <= 1:
                name = (cfg.get('name') or '').strip()
                if name and not is_auto_generated_camera_name(
                    name, cfg.get('model')
                ):
                    self._remember_camera_alias(
                        mac=cfg.get('mac'),
                        device_id=cfg.get('device_id'),
                        alias=name,
                        model=cfg.get('model'),
                    )
                continue
            cfgs = [entry_cfg for _, entry_cfg in entries]
            merged = self._merge_camera_cfg_dict(*cfgs)
            preferred = self._preferred_hub_camera_name(
                cfg=merged,
                address=merged.get('address'),
                model=merged.get('model'),
            )
            if preferred and not is_auto_generated_camera_name(
                preferred, merged.get('model')
            ):
                merged['name'] = preferred
            canonical = self._cfg_storage_key(merged)
            self._purge_stale_camera_cfg_keys(canonical, merged)
            self.Data[canonical] = json.dumps(merged)
            self._remember_camera_alias(
                mac=merged.get('mac'),
                device_id=merged.get('device_id'),
                alias=merged.get('name'),
                model=merged.get('model'),
            )
            merged_count += 1
            LOGGER.info(
                'Merged %s camera cfg entries for %s -> %r (device_id=%s)',
                len(entries),
                mac_key,
                merged.get('name'),
                merged.get('device_id'),
            )
        if merged_count:
            self._fix_stale_auto_generated_camera_names()

    def _delete_cfg_key(self, key):
        try:
            self.Data.delete(key)
        except AttributeError:
            try:
                del self.Data[key]
            except KeyError:
                pass
        except KeyError:
            pass

    def _cfg_storage_key(self, cfg):
        """Stable customdata key for a saved device cfg.

        Strip sockets share the parent MAC in cfg['mac']; store each child
        under its unique device_id or IoX address so configs do not overwrite
        each other (and so rediscover can reload every outlet).
        """
        device_id = cfg.get('device_id')
        if is_hub_child_camera_cfg(cfg) and device_id:
            return self.smac(device_id)
        if cfg.get('type') in ('SmartStripPlug', 'DeviceType.StripSocket') and device_id:
            return self.smac(device_id)
        if cfg.get('address'):
            return cfg['address']
        return self.smac(cfg['mac'])

    def save_cfg(self,cfg):
        if self._is_camera_cfg(cfg):
            siblings = self._cfg_entries_for_camera(
                mac=cfg.get('mac'),
                device_id=cfg.get('device_id'),
                address=cfg.get('address'),
            )
            if siblings:
                cfg = self._merge_camera_cfg_dict(
                    cfg,
                    *[entry_cfg for _, entry_cfg in siblings],
                )
            model = cfg.get('model')
            better = self._find_saved_camera_name(
                mac=cfg.get('mac'),
                device_id=cfg.get('device_id'),
                model=model,
            )
            if not better:
                better = self._alias_from_cache(
                    mac=cfg.get('mac'),
                    device_id=cfg.get('device_id'),
                )
            if better and is_auto_generated_camera_name(cfg.get('name'), model):
                cfg['name'] = better
            preferred = self._preferred_hub_camera_name(
                cfg=cfg,
                address=cfg.get('address'),
                model=model,
            )
            if preferred and is_auto_generated_camera_name(
                cfg.get('name'), model
            ) and not is_auto_generated_camera_name(preferred, model):
                cfg['name'] = preferred
        key = self._cfg_storage_key(cfg)
        LOGGER.debug(f'Saving config key={key}: {cfg}')
        address = cfg.get('address')
        if address and address != key:
            self._delete_cfg_key(address)
        if self._is_camera_cfg(cfg):
            self._purge_stale_camera_cfg_keys(key, cfg)
            self._remember_camera_alias(
                mac=cfg.get('mac'),
                device_id=cfg.get('device_id'),
                alias=cfg.get('name'),
                model=cfg.get('model'),
            )
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
        prior_aliases = dict(getattr(self, '_camera_alias_cache', {}) or {})
        prior_names = self._snapshot_camera_cfg_names()
        if data is None:
            LOGGER.warning("No custom data")
        else:
            self.Data.load(data)
        self._load_camera_alias_cache()
        self._merge_camera_alias_cache(prior_aliases)
        self._restore_camera_cfg_names_after_load(prior_names)
        self._dedupe_camera_cfg()
        self._seed_camera_aliases_from_cfg()
        self._seed_camera_aliases_from_manual_devices()
        self._upgrade_generic_camera_cfg_names()
        self._seed_hub_child_identities()
        self._clear_stale_hub_deferred_auth_notices()
        self._fix_stale_auto_generated_camera_names()
        self.handler_data_st = True

    def _seed_hub_child_identities(self):
        """Rebuild hub-child dedup tokens from saved device cfg."""
        for key in list(self.Data):
            cfg = self.get_device_cfg(key)
            if cfg and is_hub_child_camera_cfg(cfg):
                self.register_hub_child_identity(cfg=cfg)

    def _plugin_dir(self):
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _dev_python_kasa_state_mismatch(self, plugin_dir, enabled):
        link = symlink_path(plugin_dir)
        repo = clone_dir(plugin_dir)
        if enabled:
            return not os.path.islink(link)
        return os.path.lexists(link) or os.path.exists(repo)

    def _dev_python_kasa_notice(self, enabled, repo_url, result):
        if enabled:
            short = (result.get('head') or '')[:12]
            repo = result.get('repo') or repo_url
            msg = f'Enabled dev python-kasa from {repo}'
            if short:
                msg += f' ({short})'
            msg += '; restarting Node Server to load updated library.'
            return msg
        return (
            'Disabled dev python-kasa; restarting Node Server to use '
            'pip-installed library.'
        )

    def _sync_dev_python_kasa(self):
        plugin_dir = self._plugin_dir()
        enabled = param_enabled(self.Parameters.get('dev_python_kasa'))
        repo_url = default_repo_url(self.Parameters.get('dev_python_kasa_repo'))
        old_marker = read_marker(plugin_dir)
        restart_needed = params_require_restart(old_marker, enabled, repo_url)
        state_mismatch = self._dev_python_kasa_state_mismatch(plugin_dir, enabled)
        if not restart_needed and not state_mismatch:
            return

        result = apply_dev_python_kasa(plugin_dir, enabled, repo_url=repo_url)
        sync_marker(plugin_dir, enabled, repo_url, result)

        if result.get('error'):
            self.poly.Notices['dev_python_kasa'] = (
                f'Dev python-kasa setup failed: {result["error"]}'
            )
            return

        if restart_needed or state_mismatch or result.get('changed'):
            self.poly.Notices['dev_python_kasa'] = self._dev_python_kasa_notice(
                enabled, repo_url, result
            )
            self.poly.restart()

    def handler_params(self,params):
        LOGGER.debug('enter: Loading typed data now %s', redact_sensitive_params(params))
        self.Parameters.load(params)
        #
        # Make sure params exist
        #
        defaults = {
            "change_node_names": "false",
            "dev_python_kasa": "false",
            "dev_python_kasa_repo": default_repo_url(None),
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
        self._sync_dev_python_kasa()
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

    def _manual_device_name_from_row(self, mdev, model=None):
        """Return a user-provided manual row name, including ``IP - Name`` forms."""
        if not isinstance(mdev, dict):
            return None
        name = str(mdev.get('name') or '').strip()
        if name and not is_auto_generated_camera_name(name, model):
            return name
        address = str(mdev.get('address') or '').strip()
        match = _MANUAL_HOST_RE.match(address)
        if match:
            embedded = str(match.group(2) or '').strip()
            if embedded and not is_auto_generated_camera_name(embedded, model):
                return embedded
        return None

    def _find_manual_device_name(self, host=None, mac=None, model=None):
        """Return a configured manual-device name for a host or MAC."""
        host = str(host or '').strip()
        mac_key = normalize_mac_for_match(mac)
        for mdev in self.manual_devices or []:
            mhost = self._manual_device_host(mdev)
            name = self._manual_device_name_from_row(mdev, model=model)
            if not name:
                continue
            if host and mhost == host:
                return get_valid_node_name(name)
            if mac_key:
                row_mac = self._manual_host_identity.get(mhost) if mhost else None
                if row_mac and row_mac == mac_key:
                    return get_valid_node_name(name)
        if host:
            cached = str(self._manual_host_names.get(host) or '').strip()
            if cached and not is_auto_generated_camera_name(cached, model):
                return get_valid_node_name(cached)
        return None

    def _find_hub_child_alias(self, mac=None, device_id=None, model=None, hub_address=None):
        """Return a Tapo alias from the hub child list when LAN update is incomplete."""
        cached = self._alias_from_cache(mac=mac, device_id=device_id)
        if cached:
            return cached
        hub_node = None
        if hub_address:
            hub_node = self.poly.getNode(hub_address)
        if hub_node is None:
            hub_node = self._get_hub_node()
        if hub_node is None:
            return None
        alias = hub_child_alias_from_hub_dev(
            getattr(hub_node, 'dev', None),
            mac=mac,
            device_id=device_id,
            model=model,
        )
        if alias:
            return get_valid_node_name(alias)
        return None

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
            base = markdown2.markdown_path(
                configuration_help,
                extras=list(CONFIG_MARKDOWN_EXTRAS),
            )
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
        self.poly.setCustomParamsDoc(base + '\n<hr/>\n' + '\n'.join(html_parts))

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

        self._seed_camera_aliases_from_manual_devices()
        self._upgrade_generic_camera_cfg_names()
        self._fix_stale_auto_generated_camera_names()
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
