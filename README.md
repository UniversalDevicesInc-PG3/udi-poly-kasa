
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

### Configuration Parameters

#### change_node_names

This defaults to false. When set to true, IoX node names are changed to match the Kasa app alias on first add and after each successful device update (short poll, long poll, and reconnect). HS300 outlet names sync when the parent strip updates.
Note: there is currently a bug in PG3 so renames during long poll may not persist until the node server is restarted.

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
#### Poll Device
  * If the device is going to be unplugged for a while, set this to False so node server will stop attempting to poll.
#### Many others
  * Depending on the type of device there will be many other drivers, which should be self explanitary.

### Node Commands

The commands for these nodes

#### Query
  * Poll's all devices and sets all status in the ISY
#### On, Off
  * Turn device on or off

# Issues

If you have an issue where the nodes are not showing up properly, open the Polyglot UI and go to Kasa -> Details -> Log, and click 'Download Log Package' and send that to JimBo.Automates@gmail.com as an email attachment, or send it in a PM [Universal Devices Forum](https://forum.universal-devices.com/messenger)

# Upgrading

Restart the Kasa nodeserver by selecting it in the Polyglot dashboard and select Control -> Restart, then watch the log to make sure everything goes well.

# Changelog

See [CHANGELOG.md](CHANGELOG.md) for release notes.
