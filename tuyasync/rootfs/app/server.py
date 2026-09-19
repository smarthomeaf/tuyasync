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
import contextlib
import io
import json
import os
import re
import threading
import time
from pathlib import Path

import httpx
import tinytuya
from tinytuya import scanner as tuya_scanner
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---- config from environment (populated by run.sh from add-on options) -------
API_KEY = os.environ.get("TUYA_API_KEY", "")
API_SECRET = os.environ.get("TUYA_API_SECRET", "")
API_REGION = os.environ.get("TUYA_API_REGION", "us")
API_DEVICE_ID = os.environ.get("TUYA_API_DEVICE_ID", "")
# How long to listen for device broadcasts. This was previously exposed as
# `scan_retries`, which was a misnomer: tinytuya passes it straight through as
# `scantime` (seconds), and its own default is 18 — the old default of 6 gave
# slow-announcing devices far too little time to show up.
SCAN_TIME = int(os.environ.get("TUYA_SCAN_TIME")
                or os.environ.get("TUYA_SCAN_RETRIES")
                or tinytuya.SCANTIME)
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
SCANLOG_JSON = WORKDIR / "scanlog.json"

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


# ----------------------------- scan log --------------------------------------
# The LAN scan is the one operation that regularly "fails" in a way the result
# alone can't explain (a device HA talks to fine simply never broadcasts to us).
# So we tee tinytuya's own verbose output into a ring buffer the UI can poll
# live and re-read afterwards.
SCAN_LOG_MAX = 2000

SCAN_LOG: dict = {
    "run_id": 0,        # bumped per scan so the UI can tell runs apart
    "seq": 0,           # monotonic line counter; the UI polls with ?since=
    "running": False,
    "started": None,
    "finished": None,
    "lines": [],        # [{"n": seq, "t": epoch, "msg": str}]
}

_log_lock = threading.Lock()
_scan_lock = threading.Lock()   # one scan at a time (stdout capture is global)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _log(msg: str) -> None:
    """Append one line to the scan log, newest last."""
    msg = _ANSI_RE.sub("", str(msg)).rstrip()
    if not msg:
        return
    with _log_lock:
        SCAN_LOG["seq"] += 1
        SCAN_LOG["lines"].append({"n": SCAN_LOG["seq"], "t": time.time(), "msg": msg})
        # keep the buffer bounded; a force-scan over a /24 is chatty
        del SCAN_LOG["lines"][:-SCAN_LOG_MAX]


class _LogWriter(io.TextIOBase):
    """
    stdout stand-in that turns tinytuya's prints into scan-log lines.

    tinytuya writes progress with plain print(), and redraws some of it with
    carriage returns, so treat both \\n and \\r as line terminators.
    """

    def __init__(self) -> None:
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while True:
            breaks = [p for p in (self._buf.find("\n"), self._buf.find("\r")) if p >= 0]
            if not breaks:
                break
            i = min(breaks)
            line, self._buf = self._buf[:i], self._buf[i + 1:]
            _log(line)
        return len(s)

    def flush(self) -> None:
        if self._buf:
            _log(self._buf)
            self._buf = ""


def _scan_log_begin() -> None:
    with _log_lock:
        SCAN_LOG["run_id"] += 1
        SCAN_LOG["seq"] = 0
        SCAN_LOG["lines"] = []
        SCAN_LOG["running"] = True
        SCAN_LOG["started"] = time.time()
        SCAN_LOG["finished"] = None


def _scan_log_end() -> None:
    with _log_lock:
        SCAN_LOG["running"] = False
        SCAN_LOG["finished"] = time.time()
        snap = {k: SCAN_LOG[k] for k in
                ("run_id", "seq", "started", "finished", "lines")}
    # persist so the last run is still readable after an add-on restart
    try:
        SCANLOG_JSON.write_text(json.dumps(snap))
    except Exception:
        pass


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
    if SCANLOG_JSON.exists():
        try:
            raw = json.loads(SCANLOG_JSON.read_text())
            SCAN_LOG.update({k: raw[k] for k in
                             ("run_id", "seq", "started", "finished", "lines")
                             if k in raw})
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


def _scan_blocking(want_ips: list) -> list:
    """
    Discover reachable Tuya devices on the LAN. Runs in a thread.

    Two-stage on purpose. Broadcast discovery (UDP 6666/6667/7000) only ever
    reaches devices on our own subnet, and even there some devices announce
    themselves rarely — which is why a device Tuya Local talks to happily
    (it connects unicast to a known IP) can look completely absent here.
    So we also hand tinytuya the IPs Home Assistant already has configured via
    `wantips`: any of those still unheard from when the broadcast window closes
    gets probed directly over TCP 6668 instead of being written off.

    Note that `wantips` also lets the scan finish as soon as every wanted IP is
    accounted for, so a healthy network returns well before the full window.
    """
    # tinytuya reads devices.json (names + local keys) from the CWD to enrich
    # scan results, so run from WORKDIR where cloud sync writes it.
    os.chdir(WORKDIR)
    _log(f"Broadcast window: {SCAN_TIME}s on UDP 6666/6667/7000")
    if want_ips:
        _log(f"Will directly probe {len(want_ips)} HA-configured IP(s) if they "
             f"stay silent: {', '.join(want_ips)}")
    else:
        _log("No HA hosts known yet — broadcast only. Run 'Refresh HA' first to "
             "enable direct probing of configured IPs.")
    writer = _LogWriter()
    try:
        with contextlib.redirect_stdout(writer):
            # returns {ip: {...}} keyed by IP, same as tinytuya.deviceScan()
            found = tuya_scanner.devices(
                verbose=True,        # the whole point: we want its progress
                color=False,         # no ANSI escapes to strip out
                scantime=SCAN_TIME,
                wantips=want_ips or None,
                show_timer=False,    # its countdown redraw would flood the log
                assume_yes=True,     # never prompt: there is no stdin in here
            )
    finally:
        writer.flush()
    devices = list(found.values())
    _log(f"Scan complete — {len(devices)} device(s) found")
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
    """Diff scanned IP + configured local key (per device) against the cloud."""
    scan_by_id = {d["id"]: d for d in STATE["snapshot"] if d.get("id")}
    # a directly-probed device may come back without a usable gwId, so keep an
    # IP index too — answering on its configured IP is proof enough it is there
    scan_by_ip = {d["ip"]: d for d in STATE["snapshot"] if d.get("ip")}
    # entries carry their device_id (from HA storage); fall back to matching
    # the cloud list by title==name for entries that lack it.
    cloud_by_id = {d["id"]: d for d in STATE["devices"] if d.get("id")}
    cloud_by_name = {d["name"]: d for d in STATE["devices"]}
    rows = []
    for e in STATE["ha_entries"]:
        cloud = cloud_by_name.get(e["title"])
        dev_id = e.get("device_id") or (cloud["id"] if cloud else "")
        scanned = scan_by_id.get(dev_id) or scan_by_ip.get(e["host"])
        scanned_ip = scanned["ip"] if scanned else ""
        # authoritative key from the cloud (only when we actually have it)
        cloud_key = (cloud_by_id.get(dev_id) or cloud or {}).get("key", "")
        ha_key = e["local_key"]
        rows.append({
            "entry_id": e["entry_id"],
            "title": e["title"],
            "state": e["state"],
            "configured_host": e["host"],
            "scanned_ip": scanned_ip,
            "device_id": dev_id,
            "local_key": ha_key,
            "cloud_key": cloud_key,
            "protocol_version": e["protocol_version"],
            "poll_only": e["poll_only"],
            "mismatch": bool(scanned_ip and e["host"] and scanned_ip != e["host"]),
            "key_mismatch": bool(cloud_key and ha_key and cloud_key != ha_key),
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


def _snapshot_labeled() -> list:
    """
    The scan snapshot, with anonymous rows given a name.

    A device that only answered a direct probe comes back with no name and no
    gwId — answering on 6668 proves it is there, but identifying it needs a
    handshake that can fail. Borrow the name from whatever HA has configured at
    that IP so it doesn't show up as an anonymous row, and flag it as probed.
    """
    entry_by_ip = {e["host"]: e for e in STATE["ha_entries"] if e.get("host")}
    out = []
    for s in STATE["snapshot"]:
        probed = not s.get("id")
        e = entry_by_ip.get(s.get("ip"))
        if probed and e:
            s = {**s, "name": e["title"]}
        out.append({**s, "probed": probed})
    return out


def _devices_with_lan() -> list:
    """Cloud device list enriched with LAN IP/version from the last scan
    (the Tuya cloud doesn't return LAN IPs)."""
    scan_by_id = {d["id"]: d for d in STATE["snapshot"] if d.get("id")}
    scan_by_ip = {d["ip"]: d for d in STATE["snapshot"] if d.get("ip")}
    # A probed-only device reports no gwId, so it can never match by id. Route
    # around that through HA: cloud id -> the IP HA has configured -> the scan
    # row at that IP. Without this the device is found by the scan and still
    # reads "not on LAN" here.
    host_by_id, host_by_name = {}, {}
    for e in STATE["ha_entries"]:
        if not e.get("host"):
            continue
        if e.get("device_id"):
            host_by_id[e["device_id"]] = e["host"]
        host_by_name[e["title"]] = e["host"]
    out = []
    for d in STATE["devices"]:
        s = scan_by_id.get(d.get("id"))
        probed = False
        if not s:
            host = host_by_id.get(d.get("id")) or host_by_name.get(d.get("name"))
            s = scan_by_ip.get(host) if host else None
            probed = bool(s)
        if s:
            d = {**d, "ip": s["ip"] or d["ip"], "ver": s["ver"] or d["ver"],
                 "probed": probed}
        out.append(d)
    return out


@app.get("/api/state")
async def get_state():
    return {
        "devices": _devices_with_lan(),
        "snapshot": _snapshot_labeled(),
        "ha_entries": STATE["ha_entries"],
        "mismatches": _build_mismatches() if STATE["ha_entries"] else [],
        "last_scan": STATE["last_scan"],
        "last_sync": STATE["last_sync"],
        "version": STATE["version"],
        "creds_configured": bool(API_KEY and API_SECRET and API_DEVICE_ID),
        "scan_running": SCAN_LOG["running"],
        "scan_time": SCAN_TIME,
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


def _scan_job(want_ips: list) -> list:
    """Run the scan and own the lock for exactly as long as it actually runs."""
    try:
        return _scan_blocking(want_ips)
    except Exception as e:
        _log(f"ERROR: {e}")
        raise
    finally:
        _scan_log_end()
        _scan_lock.release()


@app.post("/api/scan")
async def lan_scan():
    if not _scan_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A scan is already running.")
    try:
        # the IPs HA expects each device at — anything silent gets probed directly
        want_ips = sorted({e["host"] for e in STATE["ha_entries"] if e.get("host")})
        _scan_log_begin()
    except BaseException:
        # nothing has started yet, so this request still owns the lock — if we
        # let it escape held, every later scan answers 409 until a restart
        _scan_lock.release()
        raise
    # From here the worker owns the lock and releases it when the scan really
    # ends. A client that disconnects mid-scan (a reload, or ingress giving up
    # on a slow run) cancels this coroutine, but the thread keeps going;
    # releasing here would let a second scan start on top of the first, and the
    # two would fight over the process-wide cwd and stdout redirect.
    try:
        snapshot = await asyncio.to_thread(_scan_job, want_ips)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    STATE["snapshot"] = snapshot
    STATE["last_scan"] = time.time()
    return {"count": len(snapshot), "snapshot": snapshot}


@app.get("/api/scan/log")
async def scan_log(since: int = 0):
    """Lines newer than `since`. Polled while a scan runs; also serves the
    last completed run so a missed scan can still be read back."""
    with _log_lock:
        return {
            "run_id": SCAN_LOG["run_id"],
            "seq": SCAN_LOG["seq"],
            "running": SCAN_LOG["running"],
            "started": SCAN_LOG["started"],
            "finished": SCAN_LOG["finished"],
            "lines": [l for l in SCAN_LOG["lines"] if l["n"] > since],
        }


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
    # now would still show the old values — update our cache optimistically.
    for e in STATE["ha_entries"]:
        if e["entry_id"] == req.entry_id:
            e["host"] = req.new_host
            e["local_key"] = req.local_key
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
