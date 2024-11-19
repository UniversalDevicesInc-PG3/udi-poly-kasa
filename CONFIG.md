

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

If set to true the IoX node nodes are changed to match the Kasa device names when the node server is restarted.

#### `auto_discover`

If set to run runs discover looking for new devices on each long poll

#### `discover_timeout`

The number of seconds to wait for a device to respond to discover packets.  Default is 10.
If some supported devices are not being discovered, you can try to increase this value.

### Custom Typed Configuration Parameters

#### Kasa devices

Manually add a device host name or IP address

#### Extra Disovery Networks

By default the node server runs a discover on the default network of the machine running PG3.
If the machine has multiple networks, or you have other networks on your LAN you can run discover
on those networks e.g. 192.162.4.255.


