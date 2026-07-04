"""
TuyaSync add-on backend.

Responsibilities:
  * Cloud sync  -> pull device list + local keys from Tuya IoT cloud (tinytuya)
  * LAN scan    -> broadcast-discover reachable devices + current IPs (tinytuya)
  * HA read     -> list tuya_local config entries and their configured host/IP
  * IP fix      -> update a single entry's `host` via the options flow (approved per-device)

All Home Assistant calls go through the Supervisor proxy using SUPERVISOR_TOKEN,
so no long-lived user token is needed.
"""

import asyncio
import json
import os
import time
from pathlib import Path

import httpx
import tinytuya
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---- config from environment (populated by run.sh from add-on options) -------
API_KEY = os.environ.get("TUYA_API_KEY", "")
API_SECRET = os.environ.get("TUYA_API_SECRET", "")
API_REGION = os.environ.get("TUYA_API_REGION", "us")
API_DEVICE_ID = os.environ.get("TUYA_API_DEVICE_ID", "")
SCAN_RETRIES = int(os.environ.get("TUYA_SCAN_RETRIES", "6"))
WORKDIR = Path(os.environ.get("TUYA_WORKDIR", "/share/tuyasync"))
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

# Supervisor exposes the HA Core API at this internal hostname.
HA_BASE = "http://supervisor/core/api"
HA_HEADERS = {
    "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
    "Content-Type": "application/json",
}

WORKDIR.mkdir(parents=True, exist_ok=True)
DEVICES_JSON = WORKDIR / "devices.json"
SNAPSHOT_JSON = WORKDIR / "snapshot.json"

app = FastAPI(title="TuyaSync")

# in-memory cache of the last results so the UI can re-render without re-scanning
STATE: dict = {
    "devices": [],      # from cloud sync (devices.json)
    "snapshot": [],     # from LAN scan (snapshot.json)
    "ha_entries": [],   # tuya_local config entries
    "last_scan": None,
    "last_sync": None,
    "version": "",      # add-on version, from Supervisor at startup
}


# ----------------------------- helpers ---------------------------------------
def _norm_cloud(dev: dict) -> dict:
    return {
        "name": dev.get("name") or "(unnamed)",
        "id": dev.get("id") or "",
        "ip": dev.get("ip") or "",
        "key": dev.get("key") or "",
        "ver": str(dev.get("version") or dev.get("ver") or ""),
        "sub": bool(dev.get("sub", False)),
    }


def _norm_scan(dev: dict) -> dict:
    return {
        "name": dev.get("name") or "(unnamed)",
        "id": dev.get("id") or "",
        "ip": dev.get("ip") or "",
        "key": dev.get("key") or "",
        "ver": str(dev.get("ver") or dev.get("version") or ""),
    }


def _load_cached_files() -> None:
    """Load any devices.json / snapshot.json left from previous runs."""
    if DEVICES_JSON.exists():
        try:
            raw = json.loads(DEVICES_JSON.read_text())
            arr = raw if isinstance(raw, list) else raw.get("devices", [])
            STATE["devices"] = [_norm_cloud(d) for d in arr]
        except Exception:
            pass
    if SNAPSHOT_JSON.exists():
        try:
            raw = json.loads(SNAPSHOT_JSON.read_text())
            arr = raw.get("devices", []) if isinstance(raw, dict) else raw
            STATE["snapshot"] = [_norm_scan(d) for d in arr]
        except Exception:
            pass


# ----------------------------- Tuya operations -------------------------------
def _cloud_sync_blocking() -> list:
    """Pull device list + keys from Tuya cloud. Runs in a thread."""
    if not (API_KEY and API_SECRET and API_DEVICE_ID):
        raise RuntimeError(
            "Cloud credentials not set. Add api_key, api_secret and api_device_id "
            "in the add-on Configuration tab."
        )
    cloud = tinytuya.Cloud(
        apiRegion=API_REGION,
        apiKey=API_KEY,
        apiSecret=API_SECRET,
        apiDeviceID=API_DEVICE_ID,
    )
    devices = cloud.getdevices(verbose=False)
    if isinstance(devices, dict) and devices.get("Error"):
        raise RuntimeError(f"Tuya cloud error: {devices.get('Error')} "
                           f"({devices.get('Err')})")
    # persist in the same shape tinytuya wizard writes
    DEVICES_JSON.write_text(json.dumps(devices, indent=2))
    return [_norm_cloud(d) for d in devices]


def _scan_blocking() -> list:
    """Broadcast-scan the LAN for reachable Tuya devices. Runs in a thread."""
    # tinytuya reads devices.json (names + local keys) from the CWD to enrich
    # scan results, so run from WORKDIR where cloud sync writes it.
    os.chdir(WORKDIR)
    # deviceScan returns {ip: {...}} keyed by IP
    found = tinytuya.deviceScan(False, SCAN_RETRIES)
    devices = list(found.values())
    snapshot = {"timestamp": time.time(), "devices": devices}
    SNAPSHOT_JSON.write_text(json.dumps(snapshot, indent=2))
    return [_norm_scan(d) for d in devices]


# ----------------------------- HA operations ---------------------------------
# HA config mounted read-only via `map: homeassistant_config:ro`
HA_STORAGE = Path("/homeassistant/.storage/core.config_entries")


def _read_entry_config() -> dict:
    """
    entry_id -> {host, local_key, protocol_version, poll_only, device_id}.
    The config-entries API deliberately never exposes entry data/options
    (local keys are secrets), so read HA's storage file directly (read-only;
    all writes still go through the options flow).
    """
    try:
        raw = json.loads(HA_STORAGE.read_text())
    except Exception:
        return {}
    out = {}
    for e in raw.get("data", {}).get("entries", []):
        if e.get("domain") != "tuya_local":
            continue
        merged = {**(e.get("data") or {}), **(e.get("options") or {})}
        out[e.get("entry_id")] = {
            "host": merged.get("host", ""),
            "local_key": merged.get("local_key", ""),
            "protocol_version": str(merged.get("protocol_version", "")),
            "poll_only": bool(merged.get("poll_only", False)),
            "device_id": merged.get("device_id", ""),
        }
    return out


async def _ha_get_tuya_entries() -> list:
    """List tuya_local config entries with their configured host/IP."""
    async with httpx.AsyncClient(timeout=20) as client:
        # the API gives us runtime state (loaded/setup_retry); host/key/device_id
        # come from the storage file via _read_entry_config()
        r = await client.get(
            "http://supervisor/core/api/config/config_entries/entry",
            headers=HA_HEADERS,
        )
        if r.status_code == 404:
            # older cores: fall back to the websocket bridge
            entries = await _ha_ws_config_entries()
        else:
            r.raise_for_status()
            entries = r.json()
    cfg = _read_entry_config()
    out = []
    for e in entries:
        if e.get("domain") != "tuya_local":
            continue
        c = cfg.get(e.get("entry_id"), {})
        out.append({
            "entry_id": e.get("entry_id"),
            "title": e.get("title"),
            "state": e.get("state"),
            "host": c.get("host", ""),
            "local_key": c.get("local_key", ""),
            "protocol_version": c.get("protocol_version", ""),
            "poll_only": c.get("poll_only", False),
            "device_id": c.get("device_id", ""),
        })
    return out


async def _ha_ws_config_entries() -> list:
    """WebSocket fallback for reading config entries."""
    import websockets
    uri = "ws://supervisor/core/websocket"
    async with websockets.connect(uri) as ws:
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": SUPERVISOR_TOKEN}))
        await ws.recv()  # auth_ok
        await ws.send(json.dumps({"id": 1, "type": "config_entries/get"}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 1 and msg.get("type") == "result":
                return msg.get("result", [])


async def _ha_update_host(entry_id: str, new_host: str,
                          local_key: str, protocol_version: str,
                          poll_only: bool) -> None:
    """
    Update a tuya_local entry's host via the options flow.
    The options flow is a single 'user' step whose schema is
    {local_key, host, protocol_version, poll_only}; we must submit all four.
    """
    import websockets
    uri = "ws://supervisor/core/websocket"
    async with websockets.connect(uri) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": SUPERVISOR_TOKEN}))
        await ws.recv()
        mid = 0

        async def call(payload):
            nonlocal mid
            mid += 1
            payload["id"] = mid
            await ws.send(json.dumps(payload))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == mid and msg.get("type") == "result":
                    if not msg.get("success", False):
                        raise RuntimeError(msg.get("error", {}).get("message", "ws error"))
                    return msg.get("result")

        # 1) start options flow
        flow = await call({
            "type": "config_entries/options/flow",
            "handler": entry_id,
        })
        flow_id = flow["flow_id"]
        # 2) submit the form with the corrected host, preserving other fields
        await call({
            "type": "config_entries/options/flow",
            "flow_id": flow_id,
        })
        # configure step
        mid += 1
        await ws.send(json.dumps({
            "id": mid,
            "type": "config_entries/options/flow",
            "flow_id": flow_id,
            "user_input": {
                "local_key": local_key,
                "host": new_host,
                "protocol_version": protocol_version,
                "poll_only": poll_only,
            },
        }))
        # NOTE: the configure call for options flows is a POST in REST; over WS
        # the second options/flow with user_input completes it. Some HA cores
        # require the REST configure endpoint instead — see _ha_update_host_rest.


async def _ha_update_host_rest(entry_id: str, new_host: str, local_key: str,
                               protocol_version: str, poll_only: bool) -> None:
    """
    Update the entry's host via the options flow. The form schema varies by
    device (hubs/sub-devices carry extra fields), so pre-fill every field
    from the flow's own suggested/default values and only override `host`.
    """
    # tuya_local stores protocol_version as a float (3.3) except "auto";
    # the options flow rejects the stringified form, so coerce it back.
    try:
        protocol_version = float(protocol_version)
    except (TypeError, ValueError):
        pass
    fallbacks = {
        "host": new_host,
        "local_key": local_key,
        "protocol_version": protocol_version,
        "poll_only": poll_only,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        # 1) init the options flow to get the form and its current values
        r = await client.post(
            "http://supervisor/core/api/config/config_entries/options/flow",
            headers=HA_HEADERS,
            json={"handler": entry_id, "show_advanced_options": True},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"options flow init failed: {r.text[:300]}")
        flow = r.json()
        flow_id = flow["flow_id"]
        user_input = {}
        for field in flow.get("data_schema") or []:
            name = field.get("name")
            if not name:
                continue
            desc = field.get("description") or {}
            if name == "host":
                user_input[name] = new_host
            elif "suggested_value" in desc:
                user_input[name] = desc["suggested_value"]
            elif "default" in field:
                user_input[name] = field["default"]
            elif name in fallbacks:
                user_input[name] = fallbacks[name]
            # optional fields with no current value stay unset
        # 2) submit the form
        r2 = await client.post(
            f"http://supervisor/core/api/config/config_entries/options/flow/{flow_id}",
            headers=HA_HEADERS,
            json=user_input,
        )
        if r2.status_code >= 400:
            raise RuntimeError(
                f"options flow rejected {sorted(user_input)}: {r2.text[:300]}"
            )
        result = r2.json()
        if result.get("type") == "form":
            errs = result.get("errors") or {}
            if errs:
                raise RuntimeError(f"options flow errors: {errs}")
            raise RuntimeError(
                f"options flow needs another step ({result.get('step_id')}) — "
                "please finish this one in the HA UI"
            )


def _build_mismatches() -> list:
    """Diff scanned IP (by device id) against HA's configured host."""
    scan_by_id = {d["id"]: d for d in STATE["snapshot"] if d.get("id")}
    # entries carry their device_id (from HA storage); fall back to matching
    # the cloud list by title==name for entries that lack it.
    cloud_by_name = {d["name"]: d for d in STATE["devices"]}
    rows = []
    for e in STATE["ha_entries"]:
        cloud = cloud_by_name.get(e["title"])
        dev_id = e.get("device_id") or (cloud["id"] if cloud else "")
        scanned = scan_by_id.get(dev_id)
        scanned_ip = scanned["ip"] if scanned else ""
        rows.append({
            "entry_id": e["entry_id"],
            "title": e["title"],
            "state": e["state"],
            "configured_host": e["host"],
            "scanned_ip": scanned_ip,
            "device_id": dev_id,
            "local_key": e["local_key"],
            "protocol_version": e["protocol_version"],
            "poll_only": e["poll_only"],
            "mismatch": bool(scanned_ip and e["host"] and scanned_ip != e["host"]),
            "found_on_lan": bool(scanned_ip),
        })
    return rows


# ----------------------------- API routes ------------------------------------
class FixRequest(BaseModel):
    entry_id: str
    new_host: str
    local_key: str
    protocol_version: str
    poll_only: bool = False


def _devices_with_lan() -> list:
    """Cloud device list enriched with LAN IP/version from the last scan
    (the Tuya cloud doesn't return LAN IPs)."""
    scan_by_id = {d["id"]: d for d in STATE["snapshot"] if d.get("id")}
    out = []
    for d in STATE["devices"]:
        s = scan_by_id.get(d.get("id"))
        if s:
            d = {**d, "ip": s["ip"] or d["ip"], "ver": s["ver"] or d["ver"]}
        out.append(d)
    return out


@app.get("/api/state")
async def get_state():
    return {
        "devices": _devices_with_lan(),
        "snapshot": STATE["snapshot"],
        "ha_entries": STATE["ha_entries"],
        "mismatches": _build_mismatches() if STATE["ha_entries"] else [],
        "last_scan": STATE["last_scan"],
        "last_sync": STATE["last_sync"],
        "version": STATE["version"],
        "creds_configured": bool(API_KEY and API_SECRET and API_DEVICE_ID),
    }


@app.post("/api/sync")
async def cloud_sync():
    try:
        devices = await asyncio.to_thread(_cloud_sync_blocking)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    STATE["devices"] = devices
    STATE["last_sync"] = time.time()
    return {"count": len(devices), "devices": devices}


@app.post("/api/scan")
async def lan_scan():
    try:
        snapshot = await asyncio.to_thread(_scan_blocking)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    STATE["snapshot"] = snapshot
    STATE["last_scan"] = time.time()
    return {"count": len(snapshot), "snapshot": snapshot}


@app.post("/api/ha/refresh")
async def ha_refresh():
    try:
        STATE["ha_entries"] = await _ha_get_tuya_entries()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"HA read failed: {e}")
    return {"count": len(STATE["ha_entries"]), "mismatches": _build_mismatches()}


@app.post("/api/fix")
async def fix_host(req: FixRequest):
    try:
        await _ha_update_host_rest(
            req.entry_id, req.new_host, req.local_key,
            req.protocol_version, req.poll_only,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Fix failed: {e}")
    # HA persists config entries with a delayed write, so re-reading storage
    # now would still show the old host — update our cache optimistically.
    for e in STATE["ha_entries"]:
        if e["entry_id"] == req.entry_id:
            e["host"] = req.new_host
    return {"ok": True, "entry_id": req.entry_id, "new_host": req.new_host}


# ----------------------------- static UI -------------------------------------
app.mount("/static", StaticFiles(directory="/app/static"), name="static")


@app.middleware("http")
async def _no_cache_ui(request, call_next):
    """The UI is a few KB; forbid caching so updates show up on plain reload
    (browsers cache aggressively inside the ingress iframe)."""
    response = await call_next(request)
    if not request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
async def index():
    return FileResponse("/app/static/index.html")


@app.on_event("startup")
async def _startup():
    _load_cached_files()
    # our own version, for display in the UI
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "http://supervisor/addons/self/info",
                headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
            )
            STATE["version"] = (r.json().get("data") or {}).get("version", "")
    except Exception:
        pass
    # best-effort HA read on boot so the UI has data immediately
    try:
        STATE["ha_entries"] = await _ha_get_tuya_entries()
    except Exception:
        pass
