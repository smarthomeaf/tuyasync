# TuyaSync — Home Assistant Add-on

Scan, sync, and repair Tuya Local devices from a UI in your HA sidebar.

## What it does

- **☁ Sync from Cloud** — pulls your device list and local keys from the Tuya IoT
  cloud (via tinytuya) using your API credentials.
- **⟲ Scan LAN** — broadcast-discovers reachable Tuya devices on your network and
  records their current IPs. Requires `host_network` (set) so it can reach your
  IoT VLAN. Any device Home Assistant has an IP for that doesn't answer the
  broadcast is then probed directly on that IP, so devices on another subnet —
  which can never hear a broadcast — are still found.
- **Scan log** — collapsed under the buttons. Expand it to watch a scan happen
  line by line, or to read back the last run afterwards. It survives a restart,
  and **Copy** puts the whole thing on your clipboard.
- **⌂ Refresh HA** — reads your `tuya_local` config entries and their configured
  `host` (IP).
- **IP Mismatches tab** — diffs each device's *scanned* IP against the IP Home
  Assistant currently has configured, and offers a **per-device one-click Fix**
  that rewrites the entry's host through the Tuya Local options flow. You approve
  each change.

## Why the IP fix matters

Tuya Local pins each device to an IP. After a DHCP shuffle, HA keeps polling the
old address and the device shows *offline / setup_retry* even though it's online
at a new IP. TuyaSync finds those and corrects them without you hand-editing 60+
config entries.

**Fix the root cause too:** set DHCP reservations for your Tuya devices so the IPs
stop moving.

## Install

1. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**.
2. Add this repo URL.
3. Install **TuyaSync**, then open the **Configuration** tab and set:
   - `api_key`, `api_secret`, `api_region` (e.g. `us`), `api_device_id`
     (any one device id from your account).
   - `scan_seconds` (default 18) — how long to listen for device broadcasts.
     Raise it if devices announce themselves rarely. (This replaces
     `scan_retries`, which despite the name was always this same number of
     seconds; the old key is still accepted.)
4. Start the add-on and open it from the sidebar.

## "Not on LAN" for a device that clearly works

Broadcast discovery only ever reaches devices on the add-on's own subnet, and
even there, some devices announce themselves rarely. Tuya Local doesn't care —
it connects straight to a known IP — so a device can be perfectly reachable in
HA and still be silent to a broadcast scan.

TuyaSync now probes those directly (see **Scan LAN** above), so this should
resolve itself. Expand the **Scan log** and look near the end for what the
force-scan said about the device:

- **`Did not find ... by IP Address`** — nothing answered on port 6668 at the IP
  HA has. That points at a genuinely wrong IP, a firewall rule between VLANs,
  or a device that's actually off.
- **`Failed to Force-Scan, FORCED STOP`** with `Device ID = (len:0)` — the
  device *did* answer, so it's definitely there, but the scan ended before the
  identify handshake finished. These show up with a **`probe`** badge: found and
  usable, just not identified by the scan itself. Raising `scan_seconds` gives
  the handshake more room to complete.

## Security notes

- Local keys are LAN control credentials. The UI keeps them blurred by default;
  the whole panel sits behind Home Assistant authentication (ingress).
- Your cloud API secret is stored in Supervisor's protected add-on options, not in
  this repo.
- Output files (`devices.json`, `snapshot.json`) are written to `/share/tuyasync`.

## Typical workflow

1. Make sure devices you care about are powered on.
2. **Sync from Cloud** → gets keys.
3. **Scan LAN** → gets live IPs.
4. **Refresh HA** → loads configured hosts.
5. Open **IP Mismatches**, review, and **Fix** the stale ones per-device.
