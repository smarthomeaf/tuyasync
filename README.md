# TuyaSync Add-on Repository

Home Assistant add-on repository for **TuyaSync** — scan, sync, and repair
Tuya Local devices from a UI in your HA sidebar.

## Installation

1. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**.
2. Add this repository URL:

   ```
   https://github.com/smarthomeaf/tuyasync
   ```

3. Install **TuyaSync** from the store.

Or click:

[![Add repository to my Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fsmarthomeaf%2Ftuyasync)

## Add-ons

### [TuyaSync](./tuyasync)

Scan, sync and repair Tuya Local devices with a live diff of scanned vs.
configured IPs. Pulls local keys from the Tuya IoT cloud, broadcast-discovers
devices on your LAN, and offers per-device one-click fixes for stale IPs in
your Tuya Local config entries.

See the [add-on README](./tuyasync/README.md) for configuration and usage.
