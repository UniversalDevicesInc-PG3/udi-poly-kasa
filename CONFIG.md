

## Kasa Node Server Configuration

### Short Poll

The short poll time.  Not used for this node server.

### Long Poll

Long poll does the following
- Sends heartbeat DON/DOF to Controller node
- For devices that have poll enabled, updates device status
- If auto_discover is on, runs a discover looking for new devices

### Custom Params

#### `user`

Your Kasa User Name

#### `password`

Your Kasa User Password

#### `change_node_names`

If set to true, IoX node names are changed to match the Kasa device alias when the node is first added and after each successful device update. HS300 outlet names sync from each child device's alias when the parent strip updates or reconnects.

#### `auto_discover`

If set to run runs discover looking for new devices on each long poll

#### `discover_timeout`

The number of seconds to wait for a device to respond to discover packets.  Default is 10.
If some supported devices are not being discovered, you can try to increase this value.

### Custom Typed Configuration Parameters

#### Kasa devices

Manually add a device host name or IP address for devices that need direct lookup (for example on another VLAN). This list is **user-managed only** — the plugin does not add discovered devices here.

- **Device host or IP** — required; the address used for direct discovery.
- **Device name** — optional; filled automatically after the device at that IP is found. If the device moves to a new IP, the plugin may update this row when it can match the same name on the network.

All discovered and saved devices appear in the **Known Kasa Devices** table at the bottom of this configuration page (read-only): name, IoX ID, IoX type, Kasa type (e.g. `DeviceType.Plug`), and IP address.

#### Extra Discovery Networks

By default the node server runs a discover on the default network of the machine running PG3.
If devices are on another VLAN or subnet, add that network's **broadcast** address here.

Use the broadcast address ending in `.255`, for example:

- `192.168.222.255` — correct (broadcast)
- `192.168.222.1` — incorrect (gateway); the plugin will rewrite this to `.255` and log a warning

You can also list individual device IPs under **Kasa devices** for hosts that do not answer broadcast discovery.

The plugin also auto-derives broadcast targets from configured manual device IPs and previously saved device hosts, so devices on other subnets are more likely to be found even if Extra Discovery Networks is incomplete.

