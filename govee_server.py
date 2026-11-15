#!/usr/bin/env python3
"""
Govee LAN control server (UDP protocol, firmware 1.01.x line).

Protocol (per Govee's official LAN guide):
  - Control: fire-and-forget UDP datagram to <device-ip>:4003
      {"msg":{"cmd":"turn","data":{"value":1}}}
  - Status reply: device sends to the client's bound UDP port 4002
      (only "devStatus" gets a reply)
  - No TCP, no handshake -> one-way LAN->IoT UDP is all that's needed.
    Your OpenWrt iot-zone REJECT only blocks device-INITIATED flows,
    so replies (return traffic, conntrack ESTABLISHED) get through.

Improvements over the original single-endpoint version:
  - config.json: devices (name -> IP), rooms, wake defaults in one file;
    devices are referenceable by name, IP, or room in every control call
  - thread-safe UDP sends (original had a race: ramp thread + refresh +
    HTTP handlers sharing one socket with no lock)
  - refresh_device(): matches replies by source IP, waits for all devices,
    no more "break after first timeout"; stale packets are drained
  - fixed increment_brightness() bug (was calling set_brightness(self, x))
  - wake_up: cancellable, restartable, progress state, sunrise option;
    auto-cancels and forces lights OFF if a light is turned off mid-ramp
    (color temperature can ramp with brightness), devices turned ON first
  - one /control endpoint so Shortcuts/NFC need a single parameterized
    HTTP call instead of many tiny shortcuts
  - /scan: probes a subnet and reports which Govee lights answer
    (handy for pinning DHCP reservations / finding a light that moved)
  - optional API key: set GOVEE_API_KEY env var to require X-Api-Key
    on state-changing requests (GET stays open)

Endpoints (JSON in/out):
  GET    /                      -> web UI (Govee-style frontend, static/pages/index.html)
  GET    /api                   -> server info + route summary
  POST   /refresh               -> live status poll (asks devices, returns state)
  GET    /healthz               -> liveness
  GET    /devices               -> all devices with state
  GET    /devices/<ip>          -> one device
  POST   /devices/<ip>          -> {on, brightness, kelvin, rgb}
  GET    /rooms                 -> room names
  GET    /rooms/<room>          -> devices in a room
  POST   /rooms/<room>          -> same controls as /devices/<ip>, room-wide
  POST   /control               -> single call: target + any of the controls
  GET    /wake_up               -> current/last ramp state
  POST   /wake_up               -> start a ramp (cancels any running one)
  DELETE /wake_up               -> cancel the running ramp
  POST   /scan                  -> {subnet: "192.168.3.0/24"} probe
  POST   /config/reload         -> re-read config.json (hot add/remove lights)
  POST   /identify              -> LAN-scan configured devices for their SKU
  POST   /scale/auto            -> auto-compute brightness scales from lumens

Error semantics (all bodies are JSON):
  400 - request body/target problem (unknown target, no controls given,
        bad subnet, unknown wake room)
  404 - unknown URL resource (bad /devices/<ip>, /rooms/<room>, or route)
  405 - wrong HTTP method for a route

Run:
  python3 govee_server.py            # config.json next to this file
  GOVEE_CONFIG=/path/config.json GOVEE_API_KEY=secret python3 govee_server.py
"""

import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import BadRequest, MethodNotAllowed, NotFound

DEVICE_PORT = 4003          # control + status queries go here
DISCOVER_PORT = 4001        # LAN discovery/identify ("scan" cmd -> sku)
RECV_PORT = 4002            # devices reply here (per Govee guide)
RECV_TIMEOUT = 1.5          # seconds to wait for each pending status reply
APP_HOST = os.environ.get("GOVEE_HOST", "0.0.0.0")
APP_PORT = int(os.environ.get("GOVEE_PORT", "9091"))
API_KEY = os.environ.get("GOVEE_API_KEY")   # optional; unset = no auth
CONFIG_PATH = os.environ.get(
    "GOVEE_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
)

# Govee's own storefront is queried directly for lumen specs (it is
# server-rendered — no JS, no third party). Override for region, e.g.
# https://govee.co.uk. Set GOVEE_SITE="" to disable web lookups.
GOVEE_SITE = os.environ.get("GOVEE_SITE", "https://us.govee.com").rstrip("/")
# Other Govee-run storefronts tried when the primary store has no hit —
# SKUs get delisted regionally but often live on at another regional
# store or in Govee's own refurb archive.
GOVEE_REGIONS = [s.rstrip("/") for s in os.environ.get(
    "GOVEE_REGIONS",
    "https://eu.govee.com,https://uk.govee.com,https://ca.govee.com,"
    "https://mea.govee.com").split(",") if s.strip()]
LUMENS_CACHE_PATH = os.environ.get(
    "GOVEE_LUMENS_CACHE",
    os.path.join(os.path.dirname(os.path.abspath(CONFIG_PATH)),
                 "lumens_cache.json"),
)
STATIC_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "static",
)

log = logging.getLogger("govee")
logging.basicConfig(
    level=os.environ.get("GOVEE_LOGLEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

KELVIN_MIN, KELVIN_MAX = 2000, 9000


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Rated lumen output by Govee SKU: verified figures for models Govee's
# own stores no longer expose to search (delisted everywhere, or
# discoverable only in their refurb archive). Unknown SKUs are resolved
# on the fly against Govee's storefronts (see lookup_lumens) and
# persisted to lumens_cache.json, which you can edit by hand.
SKU_LUMENS: dict[str, int] = {
    "H6010": 1200,   # BR30 RGBWW bulb
    "H6004": 800,    # A19/A60 RGBWW bulb (delisted everywhere; 800 lm
                     # corroborated across retailer listings + manual)
    "H607C": 1725,   # Floor Lamp 2 (us.govee.com refurb page: "1725
                     # lumens"; main listing delisted, SKU not in search)
}

_LM_PATTERNS = (
    re.compile(r"(\d[\d,]{0,5})\s*(?:lm|lumens?)\b", re.I),
    re.compile(r"(?:lumens?|lumen output)\s*[:=]\s*(\d[\d,]{0,5})", re.I),
)


def _lm_candidates(text: str) -> list[int]:
    out = []
    for rx in _LM_PATTERNS:
        for m in rx.finditer(text):
            try:
                v = int(m.group(1).replace(",", ""))
            except ValueError:
                continue
            if 50 <= v <= 20000:      # sane lamp range
                out.append(v)
    return out


def _http_get(url: str, timeout: float = 15.0, retries: int = 1) -> str:
    last: Exception = RuntimeError("no attempt")
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                              "Chrome/130.0 Safari/537.36"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError:
            raise                     # 404 etc. is an answer, not a fault
        except Exception as e:        # govee.com can be slow; retry once
            last = e
    raise last


def _lm_votes(html: str) -> dict[int, int]:
    """lumen figures from visible text nodes and meta/title, as votes"""
    votes: dict[int, int] = {}
    texts = []
    for m in re.finditer(r"<title[^>]*>([^<]*)</title>", html):
        texts.append(m.group(1))
    for m in re.finditer(r'<meta[^>]+name="description"[^>]+content="([^"]*)"', html):
        texts.append(m.group(1))
    for m in re.finditer(r">\s*([^<>]{0,90}?(?:lumens?|\blm\b)[^<>]{0,90}?)\s*<",
                         html, re.I):
        texts.append(m.group(1))
    for t in texts:
        for lm in set(_lm_candidates(t)):
            votes[lm] = votes.get(lm, 0) + 1
    return votes


def _ld_json(page: str) -> list:
    """all script type=application/ld+json blocks, parsed"""
    out = []
    for m in re.finditer(
            r'<script type="application/ld\+json"[^>]*>(.*?)</script>',
            page, re.S):
        try:
            d = json.loads(m.group(1))
            out.extend(d if isinstance(d, list) else [d])
        except ValueError:
            continue
    return out


def _lm_from_ld(blocks: list, sku: str) -> dict[int, int]:
    """
    Govee publishes schema.org ProductGroup/Product JSON-LD on every
    product page — the vendor's own machine-readable spec contract
    (what search engines consume). Prefer that over visible-text
    scraping. Lumens ride in the variant names ("... 1200lm") and the
    description ("Model: H6010 ...").
    """
    def walk(x):
        if isinstance(x, str):
            yield x
        elif isinstance(x, dict):
            for v in x.values():
                yield from walk(v)
        elif isinstance(x, list):
            for v in x:
                yield from walk(v)

    for b in blocks:
        if b.get("@type") not in ("ProductGroup", "Product"):
            continue
        if sku.upper() not in json.dumps(b).upper():
            continue
        votes: dict[int, int] = {}
        for t in walk(b):
            for lm in set(_lm_candidates(t)):
                votes[lm] = votes.get(lm, 0) + 1
        return votes
    return {}


def _try_storefront(site: str, sku: str) -> Optional[dict]:
    """search one Govee storefront for <sku>'s product page + lumens"""
    try:
        search_html = _http_get(
            f"{site}/search?q={urllib.parse.quote(sku)}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # no product hits (delisted here / not sold in this
            # region) — an honest miss for THIS storefront
            return None
        log.warning("lumens lookup for %s on %s failed: %s", sku, site, e)
        return None
    # results appear as absolute URLs whose FIRST path segment is
    # /products/ (ld+json / embedded JSON); requiring that excludes
    # cdn.shopify.com image URLs like .../s/files/products/X.jpg
    # and relative nav links. Refurb-archive pages rank after normal
    # ones but stay candidates — they are the archive for delisted
    # SKUs (that is where "Floor Lamp 2 / 1725 lumens" lives).
    normal: list[str] = []
    refurb: list[str] = []
    for m in re.finditer(
            r"https?://[^/\"']+(/products/[a-zA-Z0-9][a-zA-Z0-9-]*)",
            search_html):
        u = m.group(1)
        bucket = refurb if "refurbished" in u else normal
        if u not in normal and u not in refurb:
            bucket.append(u)
    tried = []
    missed = ""
    for link in (normal + refurb)[:4]:
        url = site + link
        try:
            page = _http_get(url)
        except urllib.error.HTTPError:
            continue        # dead candidate page, try next
        tried.append(url)
        blocks = [b for b in _ld_json(page)
                  if b.get("@type") in ("ProductGroup", "Product")]
        # identity gate: the SKU must appear in the page's structured
        # product data, NOT just anywhere — these pages embed the whole
        # site menu (~150 SKUs), so a bare substring match false-
        # matches. Only fall back to whole-page text if no ld blocks.
        if blocks:
            if not any(sku.upper() in json.dumps(b).upper()
                       for b in blocks):
                continue
        elif sku.upper() not in page.upper():
            continue
        votes = _lm_from_ld(blocks, sku)
        if not votes:
            votes = _lm_votes(page)
        if not votes:
            # right product page, but Govee publishes no lumen figure
            # (common for lamps/fixtures) — remember it so the UI can
            # say "check this URL" rather than "no results"
            if not missed:
                missed = url
            continue
        top = max(votes, key=votes.get)
        confident = len(votes) == 1 or max(votes.values()) >= 2
        return {"lumens": top if confident else None,
                "votes": votes.get(top, 0),
                "sources": [url],
                "candidates": sorted(votes)}
    if missed:
        return {"lumens": None, "votes": 0, "sources": [missed],
                "candidates": []}
    return None


def lookup_lumens(sku: str) -> Optional[dict]:
    """
    Ask Govee's OWN storefronts for the SKU's rated lumens (privacy:
    the only thing sent is the SKU string, only to govee.com properties
    — no search engines, no third-party DBs; none with Govee SKU->lumens
    coverage exist anyway):
      1. {GOVEE_SITE}/search?q=<sku>, then GOVEE_REGIONS storefronts
         (SKUs get delisted regionally; the refurb archive keeps the
         old pages) -> product page links (SSR)
      2. product page -> parse the published schema.org ProductGroup
         JSON-LD (spec contract); if it carries no lumens, fall back
         to voting on visible-text lumen mentions
    Marketing copy is noisy, so: require the SKU inside the page's
    structured product data AND either a single unanimous figure or
    >=2 agreeing mentions; otherwise return candidates + URL for a
    human to confirm. Cached afterwards. Returns None on network
    failure; a dict (possibly lumens=None) once every storefront has
    answered.
    """
    if not GOVEE_SITE:
        return None
    first_miss = None
    for site in [GOVEE_SITE] + GOVEE_REGIONS:
        try:
            hit = _try_storefront(site, sku)
        except Exception as e:
            log.warning("lumens lookup for %s on %s failed: %s",
                        sku, site, e)
            return None       # network problem: don't cache an unknown
        if hit:
            if hit["lumens"] is not None or hit["candidates"]:
                return hit
            if first_miss is None:
                first_miss = hit   # page exists here, figure may
                continue           # exist on another storefront
    return first_miss or {"lumens": None, "votes": 0, "sources": [],
                          "candidates": []}


def _load_lumens_cache() -> dict:
    try:
        with open(LUMENS_CACHE_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("lumens cache unreadable (%s): %s",
                    LUMENS_CACHE_PATH, e)
        return {}


def _save_lumens_cache(cache: dict):
    try:
        tmp = LUMENS_CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f, indent=2, sort_keys=True)
        os.replace(tmp, LUMENS_CACHE_PATH)
        log.info("lumens cache updated: %s", LUMENS_CACHE_PATH)
    except OSError as e:
        log.warning("cannot write lumens cache: %s", e)


def persist_lumens() -> list[str]:
    """
    Write each device's resolved lumens into config.json so the
    calibration is the file's own truth instead of resting on the
    seed table or lumens_cache.json. Entries gain a "lumens" key;
    the plain "name": "ip" form is upgraded to
    {"ip": ..., "lumens": ...}. Manual "scale" keys and everything
    else in the file are left exactly as written (the file is
    re-indented, content preserved). The immediately previous file
    is kept as config.json.bak. Returns the device names written.
    """
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as e:
        log.warning("cannot read config for lumens write-back: %s", e)
        return []
    devs = raw.get("devices")
    if not isinstance(devs, dict):
        return []
    written = []
    for d in home.devices.values():
        if d.lumens is None:
            continue
        key = d.name
        entry = devs.get(key)
        if entry is None and isinstance(devs.get(d.ip), dict) and \
                devs[d.ip].get("name") == d.name:
            key, entry = d.ip, devs[d.ip]      # legacy ip-keyed form
        if isinstance(entry, str):
            entry = {"ip": entry}
            devs[key] = entry
        if not isinstance(entry, dict) or entry.get("lumens") == d.lumens:
            continue
        entry["lumens"] = d.lumens
        written.append(d.name)
    if not written:
        return []
    try:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2)
            f.write("\n")
        try:
            shutil.copyfile(CONFIG_PATH, CONFIG_PATH + ".bak")
        except OSError:
            pass
        os.replace(tmp, CONFIG_PATH)
    except OSError as e:
        log.warning("cannot write lumens into config: %s", e)
        return []
    log.info("lumens written into %s: %s", CONFIG_PATH,
             ", ".join(written))
    return written


# ----------------------------------------------------------------------------
# Device
# ----------------------------------------------------------------------------

@dataclass
class Device:
    ip: str
    name: str = ""
    # brightness calibration: relative perceived-output factor.
    # A floodlight brighter than a bulb at the same raw % gets scale < 1
    # (e.g. 0.7) so room-level ("bulk") brightness commands land evenly.
    # Applied only to room/control/ramp fan-out, never to this device's
    # own per-device controls, which stay raw.
    scale: float = 1.0
    # optional rated lumen output (lm) from config; when set and "scale"
    # was not given explicitly, bulk scale is computed as lmin/lumens
    lumens: Optional[int] = None
    sku: Optional[str] = None        # model id from LAN scan/devStatus
    scale_explicit: bool = False     # config carried an explicit scale

    on: Optional[bool] = None
    brightness: Optional[int] = None
    temp: Optional[int] = None          # kelvin if >0, else 0 (pure RGB)
    color: tuple = (0, 0, 0)
    last_seen: Optional[float] = None   # monotonic time of last status reply
    # dataclass fields set after __init__ are fine; socket + lock are shared
    _socket: socket.socket = field(default=None, repr=False)
    _send_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self):
        if self._socket is None:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def _send(self, command, data):
        packet = json.dumps({"msg": {"cmd": command, "data": data}}).encode()
        with self._send_lock:
            try:
                self._socket.sendto(packet, (self.ip, DEVICE_PORT))
            except OSError as e:
                log.warning("send to %s:%d failed: %s", self.ip, DEVICE_PORT, e)

    # --- status ---------------------------------------------------------------

    def request_status(self):
        self._send("devStatus", {})

    def apply_status(self, payload: dict):
        """Update tracked state from a devStatus reply."""
        self.last_seen = time.monotonic()
        self.on = payload.get("onOff", 0) == 1
        self.brightness = payload.get("brightness")
        self.temp = payload.get("colorTemInKelvin", 0)
        sku = payload.get("sku")
        if sku:
            self.sku = sku          # some firmwares include it in devStatus
        c = payload.get("color") or {}
        self.color = (
            c.get("r", 0),
            c.get("g", 0),
            c.get("b", 0),
        )

    @property
    def online(self) -> bool:
        return self.last_seen is not None and (
            time.monotonic() - self.last_seen
        ) < 60

    # --- controls -------------------------------------------------------------

    def set_power(self, on: bool):
        self._send("turn", {"value": 1 if on else 0})
        self.on = on

    def set_brightness(self, brightness: int):
        brightness = int(_clamp(brightness, 0, 100))
        self._send("brightness", {"value": brightness})
        self.brightness = brightness

    def norm_brightness(self, pct: int) -> int:
        """Raw value to send for a normalized (bulk) brightness request."""
        return int(_clamp(round(pct * self.scale), 0, 100))

    def set_color(self, rgb: tuple, kelvin: int):
        """
        rgb: (r, g, b) 0-255.
        kelvin: >0 selects CT mode (2000-9000); 0 = pure RGB mode.
        """
        r, g, b = (_clamp(int(v), 0, 255) for v in rgb)
        kelvin = int(_clamp(kelvin, 0, KELVIN_MAX)) if kelvin > 0 else 0
        if kelvin:
            kelvin = _clamp(kelvin, KELVIN_MIN, KELVIN_MAX)
        self._send("colorwc", {
            "color": {"r": r, "g": g, "b": b},
            "colorTemInKelvin": kelvin,
        })
        self.color = (r, g, b)
        self.temp = kelvin

    def set_kelvin(self, kelvin: int):
        # white point; RGB fields are irrelevant in CT mode but sent anyway
        self.set_color((255, 255, 255), kelvin)

    def toggle_power(self):
        if self.on is None:
            self.request_status()
            return
        self.set_power(not self.on)

    def increment_brightness(self, delta: int):
        if self.brightness is None:
            self.brightness = 0
        self.set_brightness(self.brightness + delta)

    # --- reporting ------------------------------------------------------------

    def state(self) -> dict:
        return {
            "ip": self.ip,
            "name": self.name,
            "on": self.on,
            "brightness": self.brightness,
            "kelvin": self.temp,
            "rgb": list(self.color),
            "online": self.online,
            "scale": self.scale,
            "sku": self.sku,
            "lumens": self.lumens,
            "last_seen": self.last_seen,
        }

    def __repr__(self):
        return (
            f"Device({self.ip}) on={self.on} bri={self.brightness} "
            f"rgb={self.color} k={self.temp}"
        )


# ----------------------------------------------------------------------------
# Home
# ----------------------------------------------------------------------------

class Home:
    def __init__(self):
        self.devices: dict[str, Device] = {}
        self.by_name: dict[str, Device] = {}   # lowercased name -> Device
        self.rooms: dict[str, list[Device]] = {}
        self._recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._recv.bind(("", RECV_PORT))
        self._recv.settimeout(RECV_TIMEOUT)
        self._recv_lock = threading.Lock()   # one reader at a time (refresh/scan)

    # --- configuration --------------------------------------------------------

    @staticmethod
    def _parse_scale(key, raw) -> float:
        if raw is None:
            return 1.0
        try:
            scale = float(raw)
        except (TypeError, ValueError):
            log.warning("device %r: bad scale %r, using 1.0", key, raw)
            return 1.0
        if not 0.05 <= scale <= 2.0:
            log.warning("device %r: scale %r out of 0.05..2.0, clamped",
                        key, scale)
        return float(_clamp(scale, 0.05, 2.0))

    @staticmethod
    def _parse_lumens(key, raw) -> Optional[int]:
        if raw is None:
            return None
        try:
            lm = int(raw)
        except (TypeError, ValueError):
            log.warning("device %r: bad lumens %r, ignoring", key, raw)
            return None
        if lm <= 0:
            log.warning("device %r: lumens %r must be > 0, ignoring",
                        key, lm)
            return None
        return lm

    def apply_config(self, cfg: dict):
        """
        (Re)build device/room/name tables from a config dict.

        The "devices" section maps names to IPs:
            "living room lamp": "192.168.1.10"
        A device may instead be a dict to add calibration:
            "bath flood": {"ip": "192.168.1.11", "scale": 0.7}
        scale (default 1.0) is the relative perceived-brightness factor
        applied to room-level (bulk) brightness commands. Or give the
        fixture's rated output:
            "bath flood": {"ip": "192.168.1.11", "lumens": 1600}
        and scale is auto-computed (dimmest known / own) at config load.
        The legacy format ("<ip>": {"name": ..., "scale": ...,
        "lumens": ...}) is also accepted. Room lists may use names or IPs.
        """
        devices_cfg = cfg.get("devices", {})
        rooms_cfg = cfg.get("rooms", {})

        parsed: dict[str, tuple] = {}   # ip -> (name, scale, lumens, explicit)
        for key, val in devices_cfg.items():
            scale, lumens, explicit = 1.0, None, False
            if isinstance(val, str):
                name, ip = key, val.strip()
            elif isinstance(val, dict):
                if "ip" in val:                    # {"ip":..., "scale":...}
                    name, ip = key, str(val.get("ip", "")).strip()
                else:                              # legacy "<ip>": {...}
                    ip, name = key, (val or {}).get("name", "")
                scale = self._parse_scale(key, val.get("scale"))
                explicit = val.get("scale") is not None
                lumens = self._parse_lumens(key, val.get("lumens"))
            else:
                log.warning("ignoring malformed device entry %r: %r", key, val)
                continue
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                log.warning("device %r: %r is not a valid IP address",
                            name or key, ip)
                continue
            if ip in parsed:
                log.warning("device %s: second name %r for same IP ignored",
                            ip, name)
                continue
            parsed[ip] = (name, scale, lumens, explicit)

        new_devices: dict[str, Device] = {}
        for ip, (name, scale, lumens, explicit) in parsed.items():
            if ip in self.devices:
                dev = self.devices[ip]          # keep runtime state
                dev.name = name
                dev.scale = scale
                dev.lumens = lumens
                dev.scale_explicit = explicit
            else:
                dev = Device(ip=ip, name=name, scale=scale,
                             lumens=lumens, scale_explicit=explicit)
            new_devices[ip] = dev

        removed = set(self.devices) - set(new_devices)
        self.devices = new_devices

        self.by_name = {}
        for dev in new_devices.values():
            if not dev.name:
                continue
            key = dev.name.strip().lower()
            if key in self.by_name:
                log.warning("duplicate device name %r", dev.name)
            self.by_name[key] = dev

        new_rooms: dict[str, list[Device]] = {}
        for room, entries in rooms_cfg.items():
            devs = []
            for entry in entries:
                dev = new_devices.get(entry)
                if dev is None:
                    dev = self.by_name.get(str(entry).strip().lower())
                if dev is None:
                    log.warning("room %r references unknown device %r",
                                room, entry)
                    continue
                devs.append(dev)
            new_rooms[room] = devs
        if "all" not in new_rooms:
            new_rooms["all"] = list(new_devices.values())
        self.rooms = new_rooms

        self._auto_scale()

        for ip in removed:
            log.info("device %s removed from config", ip)
        log.info(
            "config applied: %d devices, rooms: %s",
            len(self.devices), ", ".join(self.rooms),
        )

    def _auto_scale(self):
        """
        Perceptual matching across calibrated fixtures.

        A fixture's effective output at bulk % P is P * lumens * scale.
        Manual scales are perceptual observations, so they participate
        in the baseline: the anchor is the smallest effective lumen
        output among devices with known lumens (manual: lumens*scale,
        auto: lumens), and every auto device normalizes to it. One
        coherent axis: manually dimmed fixtures genuinely do look
        dimmer, and auto-scaled lights must match THAT, not their
        spec-sheet numbers.

        Manual scales themselves are never overwritten. A device with
        a manual scale but no "lumens" stays off the axis entirely
        (its raw % scaling is untouched and it cannot contribute an
        anchor) — that's the escape hatch for one-off trims like a
        nightlight puck that shouldn't drag the whole room dim.
        """
        known = [d for d in self.devices.values() if d.lumens]
        if not known:
            return
        anchor = min(d.lumens * d.scale if d.scale_explicit else d.lumens
                     for d in known)
        for d in known:
            if d.scale_explicit:
                continue
            d.scale = float(_clamp(round(anchor / d.lumens, 2), 0.05, 2.0))

    # --- lookup ---------------------------------------------------------------

    def resolve(self, target: Optional[str]) -> list[Device]:
        """
        target: a device name, an IP, a room name, "all", or None (== all).
        """
        if target is None:
            return list(self.devices.values())
        target = target.strip()
        if target in self.devices:
            return [self.devices[target]]
        dev = self.by_name.get(target.lower())
        if dev is not None:
            return [dev]
        if target in self.rooms:
            return list(self.rooms[target])
        return []

    # --- status refresh -------------------------------------------------------

    def refresh_devices(self, devices: Optional[list[Device]] = None) -> dict:
        """
        Ask devices for status and wait for replies, matched by source IP.
        Returns {ip: "ok" | "no-reply"}.
        """
        if devices is None:
            devices = list(self.devices.values())
        devices = [d for d in devices if d is not None]
        if not devices:
            return {}

        results: dict[str, str] = {}
        with self._recv_lock:
            # drop stale packets from earlier refreshes
            self._recv.settimeout(0)
            try:
                while True:
                    self._recv.recvfrom(4096)
            except (socket.timeout, BlockingIOError, OSError):
                pass

            pending = {d.ip for d in devices}
            for d in devices:
                d.request_status()
                log.debug("status query -> %s", d.ip)

            deadline = time.monotonic() + RECV_TIMEOUT
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._recv.settimeout(min(remaining, RECV_TIMEOUT))
                try:
                    payload, addr = self._recv.recvfrom(4096)
                except socket.timeout:
                    continue
                ip = addr[0]
                if ip not in pending:
                    continue  # late/duplicate reply
                pending.discard(ip)
                results[ip] = "ok"
                if ip in self.devices:
                    try:
                        self.devices[ip].apply_status(
                            json.loads(payload)["msg"]["data"]
                        )
                    except (KeyError, ValueError, TypeError) as e:
                        log.warning("bad status payload from %s: %s", ip, e)
                        results[ip] = "bad-payload"
        for ip in pending:
            results[ip] = "no-reply"
        return results

    def identify(self, devices: Optional[list[Device]] = None) -> dict:
        """
        Ask devices for their model: the LAN "scan" packet goes to :4001
        (unicast — same return path as status replies on :4002, so it
        works under iot-zone firewall policies that block multicast).
        Replies carry {ip, device(mac), sku}. Returns {ip: sku}.
        """
        if devices is None:
            devices = list(self.devices.values())
        if not devices:
            return {}
        found: dict[str, str] = {}
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        pkt = json.dumps({"msg": {"cmd": "scan",
                                  "data": {"account_topic": "reserve"}}}
                         ).encode()
        with self._recv_lock:
            self._recv.settimeout(0)
            try:
                while True:
                    self._recv.recvfrom(4096)
            except (socket.timeout, BlockingIOError, OSError):
                pass
            for d in devices:
                probe.sendto(pkt, (d.ip, DISCOVER_PORT))

            deadline = time.monotonic() + RECV_TIMEOUT * 2
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                self._recv.settimeout(min(remaining, 1.0))
                try:
                    payload, addr = self._recv.recvfrom(4096)
                except (socket.timeout, BlockingIOError, OSError):
                    continue
                try:
                    data = json.loads(payload)["msg"].get("data") or {}
                except (KeyError, ValueError, TypeError):
                    continue
                sku = data.get("sku")
                if not sku:
                    continue
                found[addr[0]] = sku
                dev = self.devices.get(addr[0])
                if dev is not None and dev.sku != sku:
                    dev.sku = sku
                    log.info("identified %s as %s", addr[0], sku)
        probe.close()
        return found

    def get_devices(self):
        return list(self.devices.values())

    def get_devices_in_room(self, room: str):
        return self.rooms.get(room, [])


# ----------------------------------------------------------------------------
# Wake ramp
# ----------------------------------------------------------------------------

class WakeRamp:
    """
    Linear brightness (and optional color-temperature) ramp across a set of
    devices, run on a background thread. A new ramp cancels the old one.

    Mid-ramp, the target lights' status is sampled at least once a second
    and always immediately before each ramp step's brightness/color send:
    if one reports itself OFF (someone pressed the lamp button or used the
    Govee app), the ramp immediately auto-cancels, all target lights are
    forced OFF, and the worker exits — an explicit "no" from the user must
    not be steamrollered, and nothing may touch the lights afterwards.
    Checking before every send also prevents real Govee lamps (which often
    power back on when they receive brightness/color commands) from being
    silently re-ignited between status samples.
    """

    # minimum gap between status polls during a ramp (seconds)
    OFF_CHECK_INTERVAL = 1.0

    def __init__(self, home: Home, defaults: dict):
        self.home = home
        self.defaults = defaults
        self._lock = threading.Lock()
        self._stop: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None
        self._state: dict = {
            "running": False,
            "room": None,
            "minutes": None,
            "started_at": None,
            "ends_at": None,
            "current_brightness": 0,
            "target_brightness": 100,
            "kelvin_start": None,
            "kelvin_end": None,
            "stop_reason": None,
        }

    @property
    def state(self) -> dict:
        with self._lock:
            s = dict(self._state)
        if s["running"]:
            started, ends = s.get("started_at"), s.get("ends_at")
            if started and ends:
                s["progress"] = _clamp(
                    (time.monotonic() - started)
                    / max(ends - started, 1e-9),
                    0.0, 1.0,
                )
        return s

    def start(self, room=None, minutes=None, brightness=100,
              kelvin_start=None, kelvin_end=None) -> dict:
        with self._lock:
            # cancel a running ramp, if any
            if self._stop is not None:
                self._stop.set()
            stop = threading.Event()
            self._stop = stop
            self._state.pop("error", None)
            self._state.update({
                "running": True,
                "room": room,
                "minutes": minutes,
                "started_at": time.monotonic(),
                "ends_at": time.monotonic() + minutes * 60,
                "current_brightness": 0,
                "target_brightness": int(brightness),
                "kelvin_start": kelvin_start,
                "kelvin_end": kelvin_end,
                "stop_reason": None,
            })
            self._thread = threading.Thread(
                target=self._worker, args=(stop,), daemon=True,
            )
        self._thread.start()
        return self.state

    def _worker(self, stop: threading.Event):
        with self._lock:
            room_name = self._state["room"]
            minutes = self._state["minutes"]
            target = self._state["target_brightness"]
            k0 = self._state["kelvin_start"]
            k1 = self._state["kelvin_end"]

        devices = self.home.resolve(room_name)
        if not devices:
            self._finish(stop, ok=False, error="no devices for target")
            return

        # Phase 0: dim before revealing. Brightness 0 first, then color,
        # then power ON last — the light stays off until it's already at
        # zero, so no flash on wake.
        for d in devices:
            d.set_brightness(0)
        if k0 is not None:
            for d in devices:
                d.set_kelvin(k0)
        for d in devices:
            d.set_power(True)
        log.info(
            "wake ramp start: %s (%d devices, %s min, %s->%s K)",
            room_name, len(devices), minutes, k0, k1,
        )

        steps = 100
        interval = (minutes * 60.0) / steps
        # watch for external power-off (lamp button / Govee app mid-ramp);
        # checked before EVERY step's sends so a re-ignition cannot hide it
        last_check = time.monotonic()
        try:
            for step in range(1, steps + 1):
                if stop.is_set():
                    log.info("wake ramp cancelled")
                    return
                # every step checks status immediately before its sends:
                # if the user turned a light off, abort BEFORE our own
                # brightness/color commands can re-ignite it
                last_check = time.monotonic()
                if self._lights_off(devices):
                    self._abort_lights_off(devices, stop)
                    return
                if stop.is_set():
                    return
                bri = int(round(step / steps * target))
                if k1 is not None:
                    kelvin = int(round(k0 + (k1 - k0) * step / steps))
                    for d in devices:
                        d.set_kelvin(kelvin)
                for d in devices:
                    d.set_brightness(d.norm_brightness(bri))
                with self._lock:
                    self._state["current_brightness"] = bri
                # sleep in small slices so cancellation is prompt
                end = time.monotonic() + interval
                while time.monotonic() < end:
                    if stop.is_set():
                        log.info("wake ramp cancelled")
                        return
                    now = time.monotonic()
                    if now - last_check >= self.OFF_CHECK_INTERVAL:
                        last_check = now
                        if self._lights_off(devices):
                            self._abort_lights_off(devices, stop)
                            return
                        if stop.is_set():
                            return
                    time.sleep(min(0.2, max(0.0, end - time.monotonic())))
            with self._lock:
                self._state["current_brightness"] = target
        finally:
            self._finish(stop)

    def _lights_off(self, devices) -> bool:
        """
        Poll status once; True if any target light reports itself OFF.
        Devices that don't reply keep their last known state (which the
        ramp set to on), so an offline light cannot cause a false cancel.
        """
        try:
            self.home.refresh_devices(devices)
        except OSError as e:
            log.warning("wake ramp status watch failed: %s", e)
            return False
        off = [d.name or d.ip for d in devices if d.on is False]
        if off:
            log.info("wake ramp watch: %s report off", ", ".join(off))
        return bool(off)

    def _abort_lights_off(self, devices, stop: threading.Event):
        """User turned a light off mid-ramp: cancel and force all off."""
        stop.set()
        for d in devices:
            d.set_power(False)
        with self._lock:
            self._state["stop_reason"] = "lights_off"
            self._state["error"] = "auto-cancelled: lights turned off"
        log.info("wake ramp auto-cancelled: lights turned off; forced off")

    def _finish(self, stop: threading.Event, ok: bool = True,
                error: Optional[str] = None):
        with self._lock:
            # only the worker of the current ramp may clear running
            if self._stop is stop:
                self._state["running"] = False
                if not ok:
                    self._state["error"] = error
        if ok:
            log.info("wake ramp stopped" if stop.is_set()
                     else "wake ramp finished")

    def cancel(self) -> dict:
        with self._lock:
            running = self._state["running"]
            stop = self._stop
        if running and stop is not None:
            stop.set()
            log.info("wake ramp cancel requested")
        return self.state


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        log.warning("no config at %s; running empty (POST /config/reload "
                     "after creating it)", CONFIG_PATH)
        cfg = {}
    log.info("loaded config from %s", CONFIG_PATH)
    return cfg


# ----------------------------------------------------------------------------
# Flask app
# ----------------------------------------------------------------------------

home = Home()
ramp = None  # set in main()

app = Flask(__name__)


@app.before_request
def _auth():
    if API_KEY and request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get("X-Api-Key") != API_KEY:
            abort(401)


def _body() -> dict:
    if not request.is_json:
        return {}
    return request.get_json(silent=True) or {}


def _parse_rgb(value) -> Optional[tuple]:
    if value is None:
        return None
    if isinstance(value, str):
        value = [int(v) for v in value.split(",")]
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return tuple(int(v) for v in value)
    return None


def _apply_controls(devices: list[Device], body: dict,
                    normalize: bool = False) -> dict:
    """
    Accepts any of: on (bool), brightness (int), kelvin (int),
    rgb ([r,g,b]). Order of sends: power -> color -> brightness, so a
    brightness-only request never changes color, and turning on with a
    color lands the light in the right state.

    normalize=True applies each device's configured brightness scale,
    so one requested % lands perceptually even across mixed hardware
    (room bulk commands, /control, wake ramp). Per-device requests
    stay raw.
    """
    sent = []
    on = body.get("on")
    if on is not None:
        on = bool(on)
        for d in devices:
            d.set_power(on)
        sent.append("on" if on else "off")

    rgb = _parse_rgb(body.get("rgb"))
    kelvin = body.get("kelvin")
    if rgb is not None or kelvin is not None:
        for d in devices:
            if kelvin:
                d.set_kelvin(int(kelvin))
            else:
                r, g, b = rgb if rgb else d.color
                d.set_color((r, g, b), 0)
        sent.append(f"color{kelvin or ''}")

    brightness = body.get("brightness")
    if brightness is not None:
        b = int(brightness)
        for d in devices:
            d.set_brightness(d.norm_brightness(b) if normalize else b)
        sent.append(f"brightness={int(brightness)}")

    if not sent:
        raise _http_error(BadRequest, "no controls specified",
                          known=None)
    return {"sent": sent, "devices": [d.ip for d in devices]}


def _known_targets() -> dict:
    return {
        "device_ip": sorted(home.devices),
        "device_name": sorted(
            d.name for d in home.devices.values() if d.name
        ),
        "room": list(home.rooms),
    }


def _http_error(exc_cls, error, known) -> Exception:
    err = exc_cls()
    payload = {"error": error}
    if known is not None:
        payload["known"] = known
    err.json_payload = payload
    return err


def _target_from(body: dict) -> list[Device]:
    target = body.get("device") or body.get("room")
    devices = home.resolve(target)
    if not devices:
        raise _http_error(
            BadRequest,
            f"unknown target {target!r}",
            _known_targets(),
        )
    return devices


def _json_http_error(e):
    payload = getattr(e, "json_payload", None)
    if payload is not None:
        return jsonify(payload), e.code
    return jsonify({"error": getattr(e, "description", e.name)}), e.code


@app.errorhandler(BadRequest)
def _json_400(e):
    return _json_http_error(e)


@app.errorhandler(NotFound)
def _json_404(e):
    return _json_http_error(e)


@app.errorhandler(MethodNotAllowed)
def _json_405(e):
    return _json_http_error(e)


@app.get("/")
def index():
    path = os.path.join(STATIC_DIR, "pages", "index.html")
    if not os.path.exists(path):
        raise _http_error(
            NotFound,
            "frontend not installed (static/pages/index.html missing)",
            None,
        )
    return send_from_directory(os.path.join(STATIC_DIR, "pages"), "index.html")


@app.get("/css/<path:filename>")
def static_css(filename):
    return send_from_directory(os.path.join(STATIC_DIR, "css"), filename)


@app.get("/js/<path:filename>")
def static_js(filename):
    return send_from_directory(os.path.join(STATIC_DIR, "js"), filename)


@app.get("/pages/<path:filename>")
def static_pages(filename):
    return send_from_directory(os.path.join(STATIC_DIR, "pages"), filename)


@app.get("/favicon.ico")
def favicon_ico():
    return send_from_directory(os.path.join(STATIC_DIR, "pages"), "favicon.ico")


@app.get("/favicon.svg")
def favicon_svg():
    return send_from_directory(os.path.join(STATIC_DIR, "pages"), "favicon.svg")


@app.get("/apple-touch-icon.png")
def apple_touch_icon():
    return send_from_directory(os.path.join(STATIC_DIR, "pages"), "apple-touch-icon.png")


@app.get("/icon-192.png")
def icon_192():
    return send_from_directory(os.path.join(STATIC_DIR, "pages"), "icon-192.png")


@app.get("/icon-512.png")
def icon_512():
    return send_from_directory(os.path.join(STATIC_DIR, "pages"), "icon-512.png")


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(os.path.join(STATIC_DIR, "pages"), "manifest.webmanifest")


@app.get("/api")
def api_info():
    return jsonify({
        "service": "govee-lan-server",
        "protocol": "govee udp 4003/4002",
        "host": APP_HOST,
        "port": APP_PORT,
        "auth": "enabled" if API_KEY else "disabled (no GOVEE_API_KEY set)",
        "devices": len(home.devices),
        "device_names": sorted(
            d.name for d in home.devices.values() if d.name
        ),
        "rooms": list(home.rooms),
        "wake_defaults": dict(ramp.defaults) if ramp else {},
        "routes": [
            "GET  /                    web UI",
            "GET  /api                 server info + route summary",
            "GET  /healthz             liveness",
            "POST /refresh             live status poll",
            "GET  /devices",
            "GET  /devices/<ip>",
            "POST /devices/<ip>        {on, brightness, kelvin, rgb}",
            "GET  /rooms",
            "GET  /rooms/<room>",
            "POST /rooms/<room>",
            "POST /control             {device|room, on, brightness, kelvin, rgb}",
            "GET  /wake_up",
            "POST /wake_up             {room, minutes, brightness, kelvin_start, kelvin}",
            "DELETE /wake_up",
            'POST /scan                {"subnet": "192.168.3.0/24"}',
            "POST /config/reload",
        ],
    })


@app.get("/healthz")
def healthz():
    return {"ok": True, "time": _now_iso()}


@app.get("/devices")
def list_devices():
    return jsonify([d.state() for d in home.get_devices()])


@app.post("/refresh")
def refresh():
    """
    Ask every device for its status now and return the fresh state.
    The web UI polls this; GET /devices only returns cached state.
    """
    results = home.refresh_devices()
    return jsonify({
        "results": results,
        "devices": [d.state() for d in home.get_devices()],
    })


@app.get("/devices/<ip>")
def get_device(ip):
    dev = home.devices.get(ip)
    if dev is None:
        raise _http_error(NotFound, f"unknown device ip {ip!r}",
                          _known_targets())
    return jsonify(dev.state())


@app.post("/devices/<ip>")
def control_device(ip):
    dev = home.devices.get(ip)
    if dev is None:
        raise _http_error(NotFound, f"unknown device ip {ip!r}",
                          _known_targets())
    return jsonify(_apply_controls([dev], _body()))


@app.get("/rooms")
def list_rooms():
    return jsonify({
        room: [d.ip for d in devs] for room, devs in home.rooms.items()
    })


@app.get("/rooms/<room>")
def get_room(room):
    devs = home.rooms.get(room)
    if devs is None:
        raise _http_error(NotFound, f"unknown room {room!r}",
                          _known_targets())
    return jsonify([d.state() for d in devs])


@app.post("/rooms/<room>")
def control_room(room):
    devs = home.rooms.get(room)
    if devs is None:
        raise _http_error(NotFound, f"unknown room {room!r}",
                          _known_targets())
    return jsonify(_apply_controls(devs, _body(), normalize=True))


@app.post("/control")
def control():
    body = _body()
    devices = _target_from(body)
    return jsonify(_apply_controls(devices, body, normalize=True))


# --- wake ramp -----------------------------------------------------------------

@app.get("/wake_up")
def wake_up_status():
    return jsonify(ramp.state)


@app.post("/wake_up")
def wake_up_start():
    body = _body()
    d = ramp.defaults
    room = body.get("room", d.get("room", "bedroom"))
    if not home.resolve(room):
        raise _http_error(
            BadRequest,
            f"unknown target {room!r}",
            _known_targets(),
        )
    minutes = float(body.get("minutes", d.get("minutes", 15)))
    brightness = int(_clamp(body.get("brightness", d.get("target_brightness", 100)), 0, 100))
    kelvin_start = body.get("kelvin_start", d.get("kelvin_start"))
    kelvin_end = body.get("kelvin", d.get("kelvin"))
    # normalize: exactly one given -> constant that temperature;
    # both given -> ramp between them; none -> leave color alone
    if kelvin_start is None and kelvin_end is not None:
        kelvin_start = kelvin_end
    elif kelvin_start is not None and kelvin_end is None:
        kelvin_end = kelvin_start
    return jsonify(ramp.start(
        room=room, minutes=minutes, brightness=brightness,
        kelvin_start=kelvin_start, kelvin_end=kelvin_end,
    )), 202


@app.delete("/wake_up")
def wake_up_stop():
    return jsonify(ramp.cancel())


# --- subnet scan ---------------------------------------------------------------

@app.post("/scan")
def scan():
    """
    Probe a subnet for Govee devices: send devStatus to every host on
    :4003 and report who replies on :4002. Uses the same return-traffic
    path as refresh, so it works under your iot-zone REJECT policy.
    """
    body = _body()
    subnet = body.get("subnet", "192.168.3.0/24")
    try:
        net = ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        raise _http_error(
            BadRequest,
            f"bad subnet {subnet!r}",
            None,
        )

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.settimeout(1)
    results: dict[str, str] = {}
    with home._recv_lock:
        home._recv.settimeout(0)
        try:
            while True:
                home._recv.recvfrom(4096)
        except (socket.timeout, BlockingIOError, OSError):
            pass

        hosts = list(net.hosts())
        log.info("scan %s: %d hosts", subnet, len(hosts))
        for host in hosts:
            probe.sendto(
                json.dumps(
                    {"msg": {"cmd": "devStatus", "data": {}}}
                ).encode(),
                (str(host), DEVICE_PORT),
            )

        deadline = time.monotonic() + RECV_TIMEOUT * 2
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            home._recv.settimeout(min(remaining, 1.0))
            try:
                payload, addr = home._recv.recvfrom(4096)
            except socket.timeout:
                continue
            ip = addr[0]
            if ip in results:
                continue
            try:
                data = json.loads(payload)["msg"]["data"]
            except (KeyError, ValueError, TypeError):
                continue
            results[ip] = (
                f"on={data.get('onOff') == 1} "
                f"bri={data.get('brightness')} "
                f"rgb={data.get('color')}"
            )
        probe.close()
    log.info("scan %s done: %d devices", subnet, len(results))
    return jsonify(results)


# --- config ----------------------------------------------------------------------

@app.post("/identify")
def identify():
    """LAN-scan devices for their SKU (model id). Optional target."""
    body = _body()
    target = body.get("device") or body.get("room")
    devices = _target_from(body) if target else list(home.devices.values())
    found = home.identify(devices)
    return jsonify({"skus": found,
                    "devices": [d.state() for d in devices]})


@app.post("/scale/auto")
def scale_auto():
    """
    Compute brightness scale factors automatically: identify any device
    with no SKU, resolve lumens (config "lumens" wins, then the seed
    table, then lumens_cache.json, then a live lookup against Govee's
    own site), then scale = dimmest effective / own so bulk room
    brightness lands evenly. Explicit config "scale" always wins.
    Resolved lumens are written back into config.json (entries gain a
    "lumens" key, previous file kept as config.json.bak) so the file
    is the source of truth; pass {"persist": false} to skip writing.
    """
    body = _body()
    identified = home.identify() if body.get("identify", True) else {}
    cache = _load_lumens_cache()
    new_entries = False
    resolved: dict[str, str] = {}
    for d in home.devices.values():
        if d.lumens is not None or not d.sku:
            continue
        if d.sku in SKU_LUMENS:
            d.lumens = SKU_LUMENS[d.sku]
            resolved[d.ip] = "seed table"
        elif d.sku in cache:
            d.lumens = int(cache[d.sku])
            resolved[d.ip] = "cache"
        else:
            r = lookup_lumens(d.sku)
            if r and r.get("lumens"):
                d.lumens = r["lumens"]
                cache[d.sku] = r["lumens"]
                new_entries = True
                resolved[d.ip] = (f"web, {r['votes']} agreeing hits"
                                  f" ({(r['sources'] or [''])[0]})")
            elif r:
                cands = "/".join(map(str, r["candidates"][:3])) or "none"
                src = r["sources"][0] if r["sources"] else "no results"
                resolved[d.ip] = (f"web unsure (candidates {cands}; "
                                  f"check {src}) — set \"lumens\" in config")
    if new_entries:
        _save_lumens_cache(cache)
    home._auto_scale()
    written = persist_lumens() if body.get("persist", True) else []
    return jsonify({
        "identified": identified,
        "resolved": resolved,
        "written": written,
        "lookup_source": GOVEE_SITE or None,
        "scales": [
            {"ip": d.ip, "name": d.name, "sku": d.sku,
             "lumens": d.lumens, "scale": d.scale,
             "effective_lumens": (round(d.lumens * d.scale)
                                  if d.lumens else None),
             "status": ("manual+anchor" if d.scale_explicit and d.lumens
                        else "manual (off-axis)" if d.scale_explicit
                        else "auto" if d.lumens
                        else "unknown: add \"lumens\" to config")}
            for d in home.devices.values()
        ],
    })


@app.post("/config/reload")
def config_reload():
    cfg = load_config()
    home.apply_config(cfg)
    ramp.defaults = cfg.get("wake", {})
    if not _body().get("quiet"):
        home.refresh_devices()
    return jsonify({
        "config": CONFIG_PATH,
        "devices": list(home.devices),
        "names": {d.name: d.ip
                  for d in home.devices.values() if d.name},
        "rooms": {r: [d.ip for d in ds] for r, ds in home.rooms.items()},
        "refresh": {d.state()["ip"]: d.online for d in home.devices.values()},
    })


def main():
    global ramp
    cfg = load_config()
    home.apply_config(cfg)
    ramp = WakeRamp(home, cfg.get("wake", {}))

    # background refresh at startup so /devices is useful immediately
    threading.Thread(target=home.refresh_devices, daemon=True).start()

    log.info("listening on %s:%d (config: %s)", APP_HOST, APP_PORT, CONFIG_PATH)
    app.run(host=APP_HOST, port=APP_PORT, threaded=True)


if __name__ == "__main__":
    main()
