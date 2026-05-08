# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
