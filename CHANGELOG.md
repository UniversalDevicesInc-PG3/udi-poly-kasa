# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [3.3.24] - 2026-07-07

### Fixed

- **HS103 misclassified as strip:** classify strip parents by live kasa outlet children (`dev.children` / `sys_info['children']`) instead of model prefixes; auto-migrate saved misclassified strip nodes (no outlet children in IoX) to plugs on startup. **Upgrade note:** affected devices are removed from IoX and re-added as plugs on the first restart after upgrade (same address; ISY programs referencing the node address are unchanged).
- **SmartStripNode:** skip `SmartStrip` driver replacement when the live device is not a strip parent.

### Added

- **`strip_models.py`** and **`tests/test_strip_model_detection.py`**.

## [3.3.23] - 2026-07-05

### Added

- **Auth failure notices:** per-device notices now include a consecutive failure count (e.g. `3 consecutive auth failures`) and reset when authentication succeeds.
- **Auth Fail Count driver (GV1):** each device node exposes consecutive auth failures on GV1; resets to 0 on successful authentication.
- **Error driver (ERR):** each device node exposes the current error state (uom 25) with labeled indices for auth, credentials, unreachable, communication, discovery, circuit breaker, and unknown failures. Profile **2.1.0.12** places GV1/ERR last in nodedef status lists.
- **Tests:** auth notice counting and ERR driver classification (`test_auth_notice.py`, `test_device_errors.py`).

### Fixed

- **python-kasa 0.10:** `SmartDeviceException` was renamed to `DeviceError`; added `kasa_compat.py` alias so the plugin starts with the pinned dependency.

### Documentation

- **README:** documented Error index table (UOM 25), Auth Fail Count, and ISY program notes.

## [3.3.22] - 2026-07-05

### Fixed

- **Offline device notices:** `_cfg_for_dev` / `_db_name_for_dev` no longer read `dev.mac` before `update()` succeeds, preventing cascading `You need to await update()` tracebacks when a host is down or unreachable.

## [3.3.21] - 2026-07-05

### Added

- **Plugin tests:** `tests/` covering discovery broadcast targets, add-node pacing, per-host circuit breaker, and bulb command capability guards.

### Removed

- **ISSUES.txt:** deleted stale 2024 bug notes (issues already fixed in prior releases).

## [3.3.20] - 2026-07-05

### Fixed

- **Bulb/dimmer BRT/DIM guards:** `cmd_brt`/`cmd_dim`/`SET_BRI` check `dev is None` and dimmable capability before touching the device.
- **SET_COLOR_XY:** explicitly rejected with a clear log message (not implemented).
- **SmartDimmerNode:** fixed broken `{self.pfx}` log strings in `dim` and error paths.
- **Controller startup:** `startup_in_progress` cleared in `try/finally`; skip discover/manual add when config handlers time out.

## [3.3.19] - 2026-07-05

### Fixed

- **Credential logging:** redact Kasa passwords from plugin and `customparams` debug logs.
- **Auth notices:** distinguish missing credentials from device-rejected login in PG3 notices.
- **Auth log spam:** log the first authentication failure per host at ERROR; repeat failures at DEBUG until the device authenticates successfully.

## [3.3.18] - 2026-07-05

### Fixed

- **Manual device add after `update()` failure:** skip queueing when `update_dev` fails instead of crashing in `get_valid_node_name(None)`.
- **Misclassified HS300 strips:** model-based strip detection (`HS300`, `KP303`, etc.) when Kasa reports `DeviceType.Plug` so strips add as `SmartStrip` nodes with a fallback name.
- **P125M / unknown plugs:** map `DeviceType.Unknown` plug models (e.g. `P125M`) to `DeviceType.Plug` after a successful update.

## [3.3.17] - 2026-07-04

### Added

- **Known Kasa Devices table:** **Kasa Type** column (e.g. `DeviceType.Plug`) alongside the IoX node type.

### Fixed

- **Manual typed row names:** name fill now uses the result of manual `discover_single` (including when the device is already known at a different IP), matches by MAC identity, and runs again after startup connects so rows like `192.168.1.23` get the device name once connect completes.

## [3.3.16] - 2026-07-04

### Added

- **Kasa Devices typed params:** optional **Device name** column; the plugin fills names on existing user rows after connect (never auto-adds discovered devices to this list).
- **Stale IP migration:** when a manual row’s IP stops responding and the same device name is found at a new address, the row’s address is updated automatically.
- **Configuration doc inventory:** live **Known Kasa Devices** HTML table (name, IoX ID, type, IP) appended below CONFIG.md on the Polyglot configuration page (udi-poly-notification pattern).

### Fixed

- **Typed data feedback loop (#25):** plugin updates to manual device rows use compare-before-save, a re-entrancy guard, and an early return in `handler_typed_data` so name/IP refreshes do not re-trigger `add_manual_devices`.

## [3.3.15] - 2026-07-04

### Fixed

- **PG3 overload on restart:** device nodes no longer call `poly.ready()` (only the controller does once). Each `ready()` was re-requesting full `getAll` config for every `addNode`.
- **addNode pacing:** wait for `ADDNODEDONE` for the specific address (not an arbitrary queue entry), clear stale queue entries at startup, and pause briefly between adds. Discover queues registrations and drains them on the startup thread so the asyncio mainloop is not blocked.
- **VLAN / multi-network discover:** Extra Discovery Networks entries that are host or gateway IPs (e.g. `192.168.222.1`) are normalized to broadcast (`192.168.222.255`). Discover also derives broadcast targets from manual device IPs and saved device hosts, and long-poll discover uses the same target list.
- **Manual device `discover_single` failures:** coerce host and credentials to strings before calling python-kasa (fixes `encoding without a string argument`), and do not start discover until custom params/credentials are loaded.
- **False-positive HS300 strip cleanup:** only treat a strip parent as corrupt when its name matches an outlet alias (3.3.11 signature). User-renamed parents such as `Living Room | Behind Couch` are no longer deleted.

### Changed

- Restart still always calls `addNode` for known devices so PG3 registers the Python node even when the DB row already exists. Within a session, identity/address tracking prevents duplicate `addNode` on rediscover.

## [3.3.14] - 2026-07-03

### Fixed

- **Repeated HS300 strip cleanup notice:** corrupt strip removal now waits for PG3/IoX to finish each outlet `delNode` before deleting the parent (same pattern as udi-poly-homekit-hub thermostat recreation), posts the cleanup notice only after parent and children are confirmed gone, dedupes the notice per host per session, purges stale misnamed strip-parent cfg without deleting healthy `SmartStrip` trees, requires a live misnamed strip parent (or outlet-alias signature) before destructive cleanup, normalizes strip parent names when re-adding from saved cfg, and runs a single cleanup pass after startup connects.

## [3.3.13] - 2026-06-21

### Fixed

- **HS300 auth notice stuck on outlet name:** long-poll discover no longer authenticates ephemeral discovery devices when a node already exists for that identity, which was re-setting host auth notices even after successful polls. Strip parent nodes no longer sync their IoX name from a child outlet alias, strip configs are stored per-outlet (not all under the parent MAC), and strip nodes normalize rediscovered `DeviceType.Plug` handles back to `SmartStrip` on connect.
- **Corrupt HS300 strip tree cleanup on startup:** detects misnamed strip parents (for example `Plug 3 Testing`) from saved cfg and IoX child address patterns even before SmartStrip node classes finish loading, removes outlet nodes first then the parent via `delNode`, runs again after startup connects, lets discover rebuild the strip as `SmartStrip {model}`, and posts a session-only IoX cleanup notice (cleared on restart).

## [3.3.12] - 2026-06-21

### Fixed

- **Restart flood overwhelming PG3:** restored Ecobee-style serialized `addNode` via `ADDNODEDONE`/`wait_for_node_done`, and deferred per-node startup connects into a controller drain queue so restart no longer fires parallel connect/setDriver bursts that overflow PG3's MQTT Bottleneck queue.
- **Auth/update notices showed `None` instead of device name:** device notices and auth error logs now resolve a friendly label from the IoX node name or saved cfg when `dev.alias` is unavailable (common on SMART/Tapo authentication failures before the device fully responds).

## [3.3.11] - 2026-06-17

### Fixed

- **`change_node_names` on reconnect:** when `change_node_names=true`, IoX node names now sync from the live Kasa alias after every successful device update, not only when a node is first added. HS300 strip outlet names sync from each child device's alias when the parent strip updates or reconnects.

## [3.3.10] - 2026-06-18

### Fixed

- **Poll crash on SMART/Tapo auth failures:** when a device returned a `SmartErrorCode` instead of device info (common on HS200 and other SMART-protocol gear with missing or wrong cloud credentials), debug logging that interpolated `dev={self.dev}` triggered python-kasa's `Device.__repr__`, which raised `TypeError: 'SmartErrorCode' object is not subscriptable` and aborted every shortPoll. Logging now uses a safe `_dev_desc()` helper, and saving config no longer assumes `dev.model` is readable.
- **HS300 strip longPoll recursion on stale child nodes:** `SmartStripNode._set_energy_a` now skips non-`SmartStripPlug_*` children, matching the existing shortPoll guard in `set_children_drivers_a`, so wrong-class strip child nodes cannot recurse until stack overflow.

## [3.3.9] - 2026-06-17

### Fixed

- **Dimmable wall switches not added on discovery:** newer python-kasa reports HS220-style dimmers as `DeviceType.Dimmer` instead of the legacy `DeviceType.WallSwitch`, so discovery logged `Device type not yet supported: DeviceType.Dimmer` and skipped the node. `Controller.add_device_node` now maps `DeviceType.Dimmer` to the existing `SmartDimmerNode`, and `SmartDimmerNode` restores nodedef/drivers correctly when reloaded from saved config without a live `dev` object.

## [3.3.8] - 2026-05-25

### Fixed

- **HS300 strip child poll crash after 3.3.7:** some strip child nodes could still be reused as the wrong node class during restart/upgrade, leaving a `SmartStripNode` in a strip parent's `child_nodes` list and causing repeated `_shortPoll_a` crashes like `AttributeError: 'SmartStripNode' object has no attribute 'set_drivers_a'` for `Plug 1`. Strip-socket identity now falls back to the child address instead of the parent MAC when `device_id` is unavailable, and strip parents now refuse to retain non-`SmartStripPlug_*` children in `child_nodes`.
- **Defensive handling for stale wrong-class strip child nodes:** if PG3 already contains a strip child node with the wrong node definition, `SmartStripNode` now detects that the backing device is not actually a strip parent and safely treats it as a leaf device instead of trying to manage children, which stops the recurring poll exception until the stale node can be rebuilt cleanly.

## [3.3.7] - 2026-05-25

### Fixed

- **HS300 power strip child nodes were not being added:** the idempotency guard added for issue `#25` keyed devices by MAC address, but `DeviceType.StripSocket` children share the parent strip's MAC in the plugin path. On first discovery of `TP-LINK_Power Strip_8C16`, each `add_children()` call therefore matched the already-added parent strip node and returned it as if the child outlet already existed, so none of the six plug nodes were created. Strip sockets now use their unique `device_id` as the add-node identity key while other devices continue to use MACs.
- **Strip reconnect/startup child bookkeeping:** `SmartStripNode.add_children()` now rebuilds `self.child_nodes` from the actual add results instead of appending forever, preventing duplicate/stale entries across reconnects and avoiding follow-on poll errors caused by the wrong node type being retained in the child list.

## [3.3.6] - 2026-05-10

### Fixed

- **Long-poll spam from offline / circuit-broken hosts:** once a host had tripped the per-host circuit breaker, every 4-minute longPoll still fell through to `update_a → controller.update_dev → dev.update()` and paid the full ~12 s kasa-protocol TCP timeout for each offline node, generating `Controller:update_dev: Failed to update ... [Errno 64] Host is down` ERROR records and the recurring `mainloop is under pressure` warnings. `SmartDeviceNode._longPoll_a` now early-exits when `controller.host_should_skip(self.host)`, and `SmartDeviceNode.connect_a`'s breaker check is no longer gated on `self.dev is None` — both paths now fast-fail without touching the network.

### Added

- **Cheap TCP liveness probe inside shortPoll for fast reconnect.** New `Controller.host_quick_probe(host, port=9999, timeout=1.0s)` does a single `asyncio.open_connection` against the legacy Kasa control port (TCP 9999) and treats either a successful connect *or* a `ConnectionRefusedError` as "host alive" — the latter case is what proves a Tapo SMART device on 80/443 is reachable even though 9999 is closed. On alive, the per-host circuit breaker is reset via the existing `host_record_success` (so the next normal poll runs the full kasa probe); on dead, `next_probe` is bumped by `host_quick_probe_interval` (30 s default) and we return silently. Companion `Controller.host_should_quick_probe(host)` gates the cadence so a wall of offline hosts can't dominate the mainloop. `SmartDeviceNode._shortPoll_a` calls these for circuit-broken hosts and falls through to `connect_a()` on a successful probe. Net effect: an offline device that comes back online recovers within ~one shortPoll period (≤ ~30 s) instead of waiting up to the 15-minute backoff cap or the next 4-minute broadcast discovery sweep, while paying at most a 1 s TCP-connect attempt per host every 30 s on the asyncio mainloop.

### Changed

- **`Controller.update_dev` failure log demoted to DEBUG when the host was already circuit-broken before the attempt.** The first failure (and the threshold-trip `host %s circuit-broken` WARNING from `host_record_failure`) still surfaces at WARNING/ERROR; subsequent steady-state failures from the same offline host no longer flood the log. The recovery transition continues to log `host %s circuit reset after success` at INFO via `host_record_success`.

## [3.3.5] - 2026-05-10

### Fixed

- **`asyncio: Unclosed client session / Unclosed connector` log spam:** every long-poll discovery cycle (every ~4 minutes) was leaking one or more `aiohttp.ClientSession` / `aiohttp.connector.TCPConnector` per known device, surfacing as ERROR-level lines in the IoX UI under `Controller:_asyncio_exception_handler`. Root cause: `kasa.Discover.discover` produces a fresh `Device` for every responding host on every cycle. For an already-known mac the plugin keeps `node.dev` (so it can re-use the existing session) and dropped the freshly-discovered `Device`. Because `update_dev(dev)` had already opened the new device's lazy `HttpClient → ClientSession`, GC then reaped it and aiohttp emitted `Unclosed client session` through the loop exception handler. `discover_new_add_device` now tracks whether the discovered device was adopted by a `SmartDeviceNode` and explicitly `await dev.disconnect()`s it (via the new `_close_device_quietly` helper) on every other path, including the `update_dev → False` and exception paths.

### Added

- **`Controller._close_device_quietly(dev)`:** async helper that calls `dev.disconnect()` inside a try/except so callers can drop a python-kasa `Device` without leaking its underlying aiohttp session, regardless of whether the session was ever materialised.
- **Shutdown device cleanup:** `_handle_signal` now sweeps `self.nodes_by_mac` and disconnects each `node.dev` on the still-running mainloop before calling `poly.stop()`, so SIGTERM/reload doesn't strand long-lived sessions either.

### Changed

- **`_asyncio_exception_handler` demotes `Unclosed client session` / `Unclosed connector` to DEBUG.** These are aiohttp GC-cleanup notices, not real failures; the actual leak source is fixed above. Any residual emissions (e.g. from a shutdown race) no longer look like errors in the IoX UI.

## [3.3.4] - 2026-05-09

### Fixed

- **`SmartErrorCode` poisoning the device repr (post-mortem of overnight crashes 2026-05-08/09):** when a Tapo device (commonly C-series cameras and H500 hubs) returned a `SmartErrorCode` for the `get_device_info` sub-method of an otherwise-successful multi-method query, that error sentinel got cached in the device's `_last_update`. From then on every f-string containing the device (e.g. `LOGGER.debug(f'... dev={self.dev}')` in `connect_a`) evaluated `__repr__ → self.model → device_info → _get_device_info → di["model"]` and raised `TypeError: 'SmartErrorCode' object is not subscriptable`. Patched the vendored `kasa/smart/smartdevice.py:_get_device_info` to detect a non-dict `get_device_info`/`component_nego` and raise a clean `DeviceError`, and to fall back to safe defaults for any individual field that's missing.
- **`Device.__repr__` now exception-safe (vendored):** even with the patch above, `__repr__` used to be able to raise out of any logging call. It now degrades to `<DeviceType at host - repr unavailable: <ExceptionName>>` instead, so logging a device never tears down the calling thread.
- **Polling thread no longer dies on coroutine errors:** `SmartDeviceNode._run_coro` only caught `FutureTimeoutError`, so any other exception from the coroutine propagated up to `handler_poll`. When that thread died, Python's default `threading.excepthook` wrote the multi-line annotated Python 3.11 traceback to stderr, and `udi_interface`'s stderr capture turned each character of every `^` caret line into its own `ERROR udi_interface:write: ^` log record (~27,000 lines per crash, dominating the log). `_run_coro` now catches `Exception`, logs one structured record with `exc_info`, and returns the caller's `default` so the polling thread keeps running.

### Added

- **Global exception hooks:** the controller now installs `threading.excepthook`, `sys.excepthook`, and `loop.set_exception_handler` so any otherwise-uncaught exception (including async tasks that fall off the loop) is routed through `LOGGER.error` as a single record. This is a defence-in-depth measure to keep stderr-per-character spam from ever reappearing if a new path leaks an exception in the future.

## [3.3.3] - 2026-05-08

### Fixed

- **`discover_timeout` TypeError (issue [#21](https://github.com/UniversalDevicesInc-PG3/udi-poly-kasa/issues/21)):** PG3 Custom Parameters always come back as strings, but `kasa.Discover.discover` passes `timeout` straight to `asyncio.sleep()`, which raises `TypeError: '<=' not supported between instances of 'str' and 'int'` on newer python-kasa. `handler_params` now coerces `discover_timeout` to `int` and falls back to the default if the operator typed something non-numeric.

## [3.3.2] - 2026-05-08

### Fixed

- **Manual device discovery (issue [#24](https://github.com/UniversalDevicesInc-PG3/udi-poly-kasa/issues/24)):** `_add_manual_devices` was calling `self.discover_single(address=...)`, but the method signature is `discover_single(host=None)`. The unsupported keyword raised `TypeError: discover_single() got an unexpected keyword argument 'address'` and prevented every manual host from being added. Fixed by passing `host=...`. Especially impactful when broadcast discovery is unreliable (e.g. eisy on a different VLAN/subnet from the Kasa devices).
- **Manual device re-add loop (issue [#25](https://github.com/UniversalDevicesInc-PG3/udi-poly-kasa/issues/25)):** every successful `addNode` triggered PG3 to save `customdata`, which re-fired `handler_typed_data`, which called `add_manual_devices` again, which re-added the same node. The cycle filled `debug.log` with megabytes of `Adding manual device / Got a DeviceType.Plug / interface:addNode / custom:_save` per minute and made IoX unresponsive. Added an idempotency guard at the top of `add_device_node` that returns the existing node when looked up by mac (canonical) or address.
- **`discover_single` returning `None`:** `_add_manual_devices` now skips with a warning when `discover_single` short-circuits (e.g. circuit-broken host from 3.3.1) instead of blindly calling `add_device_node(dev=None)` and emitting an "INTERNAL ERROR" line.

## [3.3.1] - 2026-05-08

### Fixed

- **Silent crash hardening:** the plugin could be SIGKILL'd by the PG3 watchdog with no traceback when several Kasa hosts went offline at once. Multiple unreachable hosts each cost ~5s on the single asyncio mainloop per poll, eventually starving paho-mqtt's keepalive.

### Added

- **Observability:**
  - SIGTERM/SIGINT/SIGHUP handlers now log `received signal ...; shutting down` so external kills are visible.
  - A daemon heartbeat thread emits a single `alive uptime=...` line every 60s; gaps in this stream pinpoint mainloop freezes.
  - Per-cycle `query/shortPoll/longPoll/connect/update/set_*` calls now warn when they take >= 5s ("mainloop is under pressure"), well before the full timeout.
- **Per-host circuit breaker:** after 3 consecutive failures, a host is skipped on per-node `connect_a`/`discover_single` paths with exponential backoff (60s → 15min cap). The longPoll-driven discovery sweep still re-tests at its 4-minute cadence, so recovery is automatic.

### Changed

- **Bounded blocking time on offline hosts:** `kasa.Discover.discover_single` is now called with an explicit `discovery_timeout=3` (was the kasa default of 5).
- **Split future timeouts:** `async_future_timeout` (per-device ops) is now 30s; `discover_future_timeout` (network broadcast) keeps the previous 180s. A single hung host can no longer hold a worker thread for 3 minutes.
- **Notice churn reduced:** writes for the same `(host, source)` are coalesced inside a 60s window, so transient exception-text wobble doesn't bounce through the udi_interface MQTT Notices channel every poll.
- **Quieter `kasa.discover` logger:** demoted to WARNING; per-packet "Got error: [Errno 64] Host is down" no longer floods the log on networks with offline devices.
- **`update_dev` exceptions:** drop the redundant traceback on `KasaException` (the message already carries the actionable bit); generic `Exception` paths still keep `exc_info=True`.

## [3.3.0] - 2026-05-07

### Changed

- **Reliability:** added timeouts to async waits, exception-safe poll/discovery flags, and typed exceptions in hot paths.
- **Compatibility:** bulb color/temperature/brightness commands now use the current `python-kasa` API.
- **Operability:** repetitive poll/discover log spam is reduced to one-shot state-change logs.
- **PG3 notices:** each device uses a single timestamped notice, with source priority so specific failures such as auth/update win over generic echoes.
- **Discovery:** unsupported device types such as Camera and Hub are ignored instead of being repeatedly added every cycle.
- **Versioning:** `nodes/__init__.py` `VERSION` is `3.3.0`.

### Fixed

- **Vendored python-kasa:** handle paginated SMART/SMARTCAM child responses missing list fields to prevent `StopIteration`/`KeyError` crashes on Tapo H500.

## Legacy Release Notes

- 3.2.4: 12/23/2024
  - BETA: Please only install to test
  - Upgrade to python-kasa>=0.8.0<0.9.0
- 3.1.4: 12/18/2023
  - Fix: [HS300 outlet power not updated on longpoll or query all](https://github.com/UniversalDevicesInc-PG3/udi-poly-kasa/issues/20)
- 3.1.3: 12/05/2023
  - With latest IoX Updates this now works with latest python-kasa 0.5.4 https://github.com/python-kasa/python-kasa/releases/tag/0.5.4
- 3.1.2: 11/28/2023
  - Force python-kasa 0.5.3 to avoid needing rust compiler for now
- 3.1.1: 11/25/2023
  - Fix: [Crash on startup when poll is called on smartstrip](https://github.com/UniversalDevicesInc-PG3/udi-poly-kasa/issues/19)
- 3.1.0: 09/22/2023
  - Added [Configuration Help](/CONFIG.md) describing all new parameters
  - Fix: [change_node_names not working](https://github.com/UniversalDevicesInc-PG3/udi-poly-kasa/issues/17)
    - NOTE: You will see an error on the log related to this: ERROR interface:_message: Failed to update internal nodelist: None :: 'NoneType' object is not subscriptable
    - This is a PG3x issue which will be fixed in the next release of PG3x after 3.2.7.
  - Fix: [Deleted devices always return(https://github.com/UniversalDevicesInc-PG3/udi-poly-kasa/i]ssues/14)
  - Enhancement: [Add configurable discover timeout](https://github.com/UniversalDevicesInc-PG3/udi-poly-kasa/i]ssues/18)
  - Enhancement: [Allow manually adding device host name or IP address](https://github.com/UniversalDevicesInc-PG3/udi-poly-kasa/i]ssues/9)
    - Also allows adding extra networks to run discover on
  - Fix adding SmartStrip Plug nodes when device is added
  - Tested with python-kasa 0.5.3, hopefully this version will discover previously undiscovered devices
    - BUT this doesn't yet work for the new firmware devices, hopefully that will be released soon.
  - Added all driver names so they now show up in PG3x UI.
  - Fix log message for "ready to poll" when device is not responding
- 3.0.21: 03/28/2023
  - Fix dumb error in print statement added in last release
- 3.0.20: 03/19/2023
  - Fix to trap bug in current python-kasa library
- 3.0.18: 12/10/2022
  - Try and fix issues for devices that don't respond to first discover, then come alive later.
- 3.0.17: 12/10/2022
  - Move to new udi_interface rename_node method instead of previous hack
  - Node renames are only done on restart of the node server
- 3.0.16: 11/17/2022
  - Fix [Crash in handler_params](https://github.com/UniversalDevicesInc-PG3/udi-poly-kasa/issues/12)
- 3.0.14: 11/15/2022
  - Trap exception when adding a new device and it fails to update
- 3.0.13: 11/14/2022
  - Fixed [Add option to disable polling a device](https://github.com/UniversalDevicesInc-PG3/udi-poly-kasa/issues/11)
    - If a device is going to be unplugged for a while, set the Poll Device to False.
  - Also, reworked how polling is done so one device not responding doesn't slow down polling other devices.
- 3.0.12: 11/12/2022
  - Fixed [Add rename nodes option](https://github.com/UniversalDevicesInc-PG3/udi-poly-kasa/issues/10)
    - See [Configuration Parameters](#configuration-parameters)
- 3.0.11: 07/23/2022
  - Add debugging for: [SmartStripPlugNode not retrieving proper status](https://github.com/UniversalDevicesInc-PG3/udi-poly-kasa/issues/8)
- 3.0.10: 04/13/2022
  - Fixed: [Add support for KL420L5](https://github.com/UniversalDevicesInc-PG3/udi-poly-kasa/issues/7)
    - Although can't be sure until user confirms since I don't have one
  - Fixed: [add_node should remove bad characters from node names](https://github.com/UniversalDevicesInc-PG3/udi-poly-kasa/issues/4)
  - Fixed: [Set power numbers when device is off](https://github.com/UniversalDevicesInc-PG3/udi-poly-kasa/issues/6)
- 3.0.9: 02/20/2022
  - Profile fixes
- 3.0.8: 02/17/2022
  - query on controller only queries controller not all nodes
    - Use new Query All command instead
  - [Fix Status for multiplug devices](https://github.com/UniversalDevicesInc-PG3/udi-poly-kasa/issues/3)
- 3.0.7: 12/14/2021
  - Added conn_status to Controller so ST is properly set
    - Existing users will need to delete the controller node in the Polyglot UI and restart the NS
- 3.0.6: 12/14/2021
  - Fixed profile for Status of all devices that have energy values
- 3.0.5: 11/23/2021
  - Fix Set XY initial values to not use any driver to fix UDMobile error
- 3.0.4: 11/22/2021
  - More Duration fixes (RR) in nodedef, but still not completely working
- 3.0.3: 11/21/2021
  - Fix Duration Time, although not completly working
- 3.0.2: 11/13/2021
  - Discover new devices added while nodeserver is already running now works
- 3.0.1: 11/13/2021
  - Smartstrips working properly
- 3.0.0: 11/13/2021
  - First PG3 release!
- 2.5.0: 04/28/2021
  - [Added HS220 support](https://github.com/jimboca/udi-poly-kasa/pull/18) Thanks to @albrandwood
- 2.4.7: 12/10/2020
  - Check python version on startup
- 2.4.6: 11/17/2020
  - [Power Strip Plugs status reverts](https://github.com/jimboca/udi-poly-kasa/issues/12)
- 2.4.5: 11/16/2020
  - Many fixes for KL430 LED light strips
  - Fixes for Power on all devices
- 2.4.4: 10/30/2020
  - Fixed bulb on off status
- 2.4.3: 10/29/2020
  - SmartPlugStrip working better now
- 2.4.2: 10/19/2020
  - SmartPlugStrip now working again
- 2.4.1: 10/01/2020
  - Added Support for Smart Light Strip
- 2.4.0: 08/15/2020
  - [Issue 6](https://github.com/jimboca/udi-poly-kasa/issues/6) Convert to new [python-kasa library](https://github.com/python-kasa/python-kasa)
    - Currently must install manually: pip3 install --user python-kasa --pre --no-warn-script-location
    - And for Polyisy: sudo -u polyglot pip3 install --user python-kasa --pre --no-warn-script-location
  - Now requires Python 3.7 which is available on Polisy by default, but it means your RPi must be on Buster!
- 2.3.3: 02/18/2020
  - Fixed https://github.com/jimboca/udi-poly-kasa/issues/5
- 2.3.2: 02/01/2020
  - Make sure bulb is on before adjusting other values. Temporary workaround to be fixed better later.
- 2.3.1: 01/27/2020
  - Fix race condition between discover adding smart strip and shortPoll accessing it
  - Dim and Brighten should be working, although tested minimally
- 2.3.0: 01/05/2020
  - Full Color control working on KL130
  - Small speed improvement when setting brightness and color temp at the same time.
- 2.2.6: 01/02/2019
  - Controller long/short Poll runs in threads so main program is more responsive
- 2.2.5: 12/31/2019
  - Moved main short/long poll into threads so main thread is more responsive
- 2.2.4: 12/30/2019
  - KL110 and KL120 can be added to scenes, but still more functions to implement
- 2.2.3: 12/27/2019
  - Add support for KL120 (Dimmable Color Temperature with Energy)
  - Switch to locally checked out pyHS100 with discovery fix for Polisy
- 2.2.2 11/06/2019
  - Fix to reconnect to device that wasn't responding when nodeserver started
- 2.2.1 10/15/2019
  - Fix crash when discover takes a long time to complete and devices are not yet initialized for shortPoll
- 2.2.0 10/13/2019
  - Lot's of rework to allow supporting any Kasa device based on the capabilities instead of hardcoding the model names.
  - Set Brightness also working.
- 2.1.1 10/01/2019
  - Remove from cloud
- 2.1.0 09/21/2019
  - Merge changes from @eagleco to support plugs, thank you!
  - Fixed to work for those with emeter (HS110) and those without (HS100)
  - Adding support for SmartBulbs, only limited support currently.
- 2.0.3 04/21/2019
  - Fixed controller naming, sorry if you are using this you will need to:
    - Go to Polyglot Web page
    - Update the Nodeserver in the Store
    - Delete the Nodeserver
    - Add it again
- 2.0.2 03/29/2019
  - Fixed shortPoll to properly update
- 2.0.1 03/28/2019
  - Update ST on shortPoll, added heartbeat
- 2.0.0 03/27/2019
  - Initial version
