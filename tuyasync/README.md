# TuyaSync — Home Assistant Add-on

Scan, sync, and repair Tuya Local devices from a UI in your HA sidebar.

## What it does

- **☁ Sync from Cloud** — pulls your device list and local keys from the Tuya IoT
  cloud (via tinytuya) using your API credentials.
- **⟲ Scan LAN** — broadcast-discovers reachable Tuya devices on your network and
  records their current IPs. Requires `host_network` (set) so it can reach your
  IoT VLAN.
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
   - `scan_retries` (default 6; raise to ~15–20 if devices are slow to answer).
4. Start the add-on and open it from the sidebar.

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
