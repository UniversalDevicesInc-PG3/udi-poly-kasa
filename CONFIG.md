

## Kasa Node Server Configuration

### Short Poll

The short poll time.  Not used for this node server.

### Long Poll

Long poll does the following
- Sends heartbeat DON/DOF to Controller node
- For devices that have poll enabled, updates device status
- If auto_discover is on, runs a discover looking for new devices
- Clears a prior **Discovery finished** notice (posted when Discover completes) so the notice stays visible until the next long poll

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

#### `dev_python_kasa`

**Advanced / development only.** When set to `true`, the Node Server clones or updates a nested `python-kasa` git repository inside the plugin directory and creates a `kasa` symlink so unreleased library fixes load instead of the pip-installed package. Default is `false` (use pip `python-kasa` from `requirements.txt`).

Changing this parameter or the repo URL triggers an automatic Node Server restart so the process reloads the library. On each restart while enabled, the plugin runs `git pull --ff-only` before importing kasa.

Requires `git` on the PG3 host (typically `/usr/local/bin/git` on FreeBSD) and write access to the plugin directory. The Node Server process often has a minimal `PATH`; the plugin resolves common git locations automatically.

#### `dev_python_kasa_repo`

Git URL for the nested `python-kasa` clone when `dev_python_kasa` is `true`. Default is `https://github.com/jimboca/python-kasa.git`. Leave blank to use that default.

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

### Tapo cameras and hubs

Tapo cameras (`DeviceType.Camera`) and hubs (`DeviceType.Hub`, e.g. H500) are discovered like other Kasa/Tapo devices. Use the same **user** and **password** (TP-Link cloud account) as for plugs and bulbs.

**Camera nodes** expose:

| IoX status | Meaning |
|------------|---------|
| Camera State | On when the privacy lens mask is off (streaming enabled) |
| Connected | Device responded to the last poll |
| Motion Detection | Motion alerts armed in Tapo (config), not a live motion alarm |
| Notifications | Tapo/Kasa app push notifications enabled |
| Battery Level | Present on battery models (`SmartCamera_B`) |
| Error | Same labeled error indices as other Kasa device nodes |

**Camera commands:** **On** / **Off** toggle the privacy lens (Camera State). **Set Notifications** enables or disables Tapo app push alerts only — recording and motion detection continue when notifications are off.

**Hub nodes** manage child cameras paired to the hub. Cameras appear nested under the hub in IoX (each child uses its own camera MAC as the node address; the hub remains the parent node).

If hub-paired **Camera State** (privacy lens) or **Set Notifications** commands fail with a protocol error, the plugin may retry the same command directly on the camera's LAN IP when that address is known.

**Deduplication:** A powered camera that answers discovery on the LAN *and* is listed under a hub is represented once — as a hub child. When an H500 hub is configured, the plugin skips LAN camera discovery, purges stale standalone camera nodes on restart, and removes duplicate standalone nodes when the hub connects.

**RTSP / external viewing:** IoX does not show video. For VLC or NVR software, configure a **Camera Account** in the Tapo app (per camera → Advanced Settings → Camera Account). python-kasa can build an RTSP URL when those credentials are available; the plugin does not store RTSP URLs in IoX drivers in this release.

**Offline solar cameras:** Hub-child cameras that sleep may show **Connected** = false until they wake; this is expected.

**Solar cameras (e.g. C675D) and Discover:** Battery/solar models often **do not answer UDP broadcast discovery** even when awake. They may still work by **IP**. Add each camera under **Kasa devices** (host `192.168.x.x`) while the camera is awake in the Tapo app, then restart the node server or run **Discover**. The plugin connects by IP and nests the camera under your H500 hub. Set the **Device name** column (or use `192.168.x.x - TapoName` in the host field) so IoX does not keep a generic `Kasa C675D` label when the first connect happens before Tapo returns the alias.

**Troubleshooting camera auth / SSL failures**

| Symptom | Likely cause | What to try |
|--------|----------------|-------------|
| C200 auth error `-40211` | Wrong account type or Tapo third-party API disabled | Use your **TP-Link cloud email** (same as Tapo/Kasa app login), not a per-camera RTSP account. In Tapo app: Me → Tapo Lab → **Third-Party Compatibility** → On. Power-cycle the camera after firmware updates. |
| C260 SSL handshake failure on direct IP | Camera is hub-paired; direct LAN SSL is not used | Pair cameras to the **H500 hub**; they appear as hub children. The plugin discovers the hub first and skips duplicate standalone nodes. |
| Hub found but no child cameras | H500 FW 1.3.x often returns empty LAN `getChildDeviceList` even when cameras are paired | The plugin also queries **TP-Link cloud** `getDeviceList` (same account as **user**/**password**) to learn camera names and MACs, then adds hub-child nodes (offline placeholders when a camera is asleep). Restart or run **Discover** after pairing changes. On TLS errors set custom parameter `tapo_cloud_insecure_tls` to `true`. |

Hub-paired cameras should **not** be added twice (standalone + hub child). If you previously had standalone camera nodes, delete them in IoX and run Discover after the hub is online.

