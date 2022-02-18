
[![Build Status](https://travis-ci.org/jimboca/udi-poly-kasa.svg?branch=master)](https://travis-ci.org/jimboca/udi-kasa)

# UDI Polyglot V3 Kasa Nodeserver

This is the [TP Link Kasa](https://www.kasasmart.com/us) Poly for the [Universal Devices Polisy](https://www.universal-devices.com) with [Polyglot Version 3 (PG3)](https://github.com/UniversalDevicesInc/pg3)

(c) JimBoCA aka Jim Searle
MIT license.

This node server is intended to support all devices supported by the [Python Kasa Library](https://github.com/python-kasa/python-kasa)

This nodeserver relies on a mostly undocumented and unofficially supported local API which of course TP-Link could break at any time, and has in the past, but luckily others figure it out.

## Help

If you have any issues are questions you can ask on [PG3 Kasa SubForum](https://forum.universal-devices.com/forum/313-kasa-tp-link/) or report an issue at [PG3 Kasa Github issues](https://github.com/UniversalDevicesInc-PG3/udi-poly-kasa/issues).

## Moving from PG2

There are a few ways to move.

### Backup and Restore

The best way to move from PG2 to PG3 is to backup on PG2 and restore on PG3, but the only option is to do all your nodeservers at once.  I don't have much information on this method, if you have questions please ask on the PG3 forum.

### Delete and add

If you can't or don't want backup/restore then you can delete the NS on PG2 and install on the same slot on PG2.  All node addresses will stay the same so all your programs should work after doing an update and save on each one, or rebooting the ISY, especially any using the Controller node since it's ST value has changed.

### Add then delete

Another option is to install in a new slot then go edit all your programs and scenes that reference the nodes and switch to the new slots. 


## Installation

This nodeserver will only work on a machine running on your local network, it will not work with Polyglot Cloud until TP-Link releases a public API for their cloud interface.

1. Backup Your ISY in case of problems!
   * Really, do the backup, please
2. Go to the Polyglot Store in the UI and install.
3. Open the admin console (close and re-open if you had it open) and you should see a new node 'Kasa Controller'
4. The auto-discover should automatically run and find your devices and add them.  Verify by checking the nodeserver log
   * While this is running you can view the nodeserver log in the Polyglot UI to see what it's doing

## Usage

This node server makes every attempt to handle devices which are not responding for any reason, like they are unplugged or powered off.  When a device is Discovered it is remembered, so if it doesn't respond on the next discovery it will still be an active device and when powered up it will be seen as connected.

The node server does not require that you reserve IP addresses for the devices, the device address is remembered based on it's MAC address, so if the IP address changes, it will be properly handled. (This has not been extensively tested, needs more verification)

## Kasa Devices

### Known working

The known list of supported devices models are:
  - HS100 (US)
  - HS110 (US)
  - HS220 (US)
  - HS300 (US) SmartStrip
  - KL110 (US)
  - KL120 (US)
  - KL130 (US)
  - KL430 (US) LightStrip

If you have another device not listed and it is working properly please let me know.

### Unknown devices

All other simple plug and bulb devices should work, the nodeserver attempts to figure out the capabilities of the device instead of hardcoding based on the model.  But if you have an issue please add to [UDI Poly Kasa Issues](https://github.com/jimboca/udi-poly-kasa/issues) Feel free to Fork this repo and add support as you like and send me a pull request.

## Kasa Controller

This is the main node created by this nodeserver and manages the devices.

### Node Drivers
The settings for this node are

#### Node Server Connected
   * Status of nodeserver process, this should be monitored by a program if you want to know the status
#### TODO: Devices
   * The number of devices currently managed

### Node Commands

The commands for this node

#### Query
   * Poll's all devices and sets all status in the ISY
#### Discover
   * Run's the auto-discover to find your devices
#### Install Profile
   * This uploads the current profile into the ISY.
   * Typically this is not necessary, but sometimes the ISY needs the profile uploaded twice.

## Kasa Devices

The supported Kasa devices can have different status and commands, but these are the common ones.

### Node Drivers
The settings for this node are

#### Status (ST)
  * Status of device, on, off, or brightness.
#### Connected (GV0)
  * True if device is communicating

### Node Commands

The commands for these nodes

#### Query
  * Poll's all devices and sets all status in the ISY
#### On, Off
  * Turn device on or off

# Issues

If you have an issue where the nodes are not showing up properly, open the Polyglot UI and go to Kasa -> Details -> Log, and click 'Download Log Package' and send that to jimboca3@gmail.com as an email attachment, or send it in a PM [Universal Devices Forum](https://forum.universal-devices.com/messenger)

# Upgrading

Restart the Kasa nodeserver by selecting it in the Polyglot dashboard and select Control -> Restart, then watch the log to make sure everything goes well.

# Release Notes
- 3.0.8: 02/1=7/2022
  - query on controller only queries controller not all nodes
    - Use new Query All command instead
   
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
