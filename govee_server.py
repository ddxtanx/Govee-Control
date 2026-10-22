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
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import BadRequest, MethodNotAllowed, NotFound

DEVICE_PORT = 4003          # control + status queries go here
RECV_PORT = 4002            # devices reply here (per Govee guide)
RECV_TIMEOUT = 1.5          # seconds to wait for each pending status reply
APP_HOST = os.environ.get("GOVEE_HOST", "0.0.0.0")
APP_PORT = int(os.environ.get("GOVEE_PORT", "9091"))
API_KEY = os.environ.get("GOVEE_API_KEY")   # optional; unset = no auth
CONFIG_PATH = os.environ.get(
    "GOVEE_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
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


# ----------------------------------------------------------------------------
# Device
# ----------------------------------------------------------------------------

@dataclass
class Device:
    ip: str
    name: str = ""

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

    def apply_config(self, cfg: dict):
        """
        (Re)build device/room/name tables from a config dict.

        The "devices" section maps names to IPs:
            "living room lamp": "192.168.1.10"
        The legacy format ("<ip>": {"name": ...}) is still accepted.
        Room lists may use names or IPs.
        """
        devices_cfg = cfg.get("devices", {})
        rooms_cfg = cfg.get("rooms", {})

        parsed: dict[str, str] = {}   # ip -> name
        for key, val in devices_cfg.items():
            if isinstance(val, str):
                name, ip = key, val.strip()
            elif isinstance(val, dict):            # legacy format
                ip, name = key, (val or {}).get("name", "")
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
            parsed[ip] = name

        new_devices: dict[str, Device] = {}
        for ip, name in parsed.items():
            if ip in self.devices:
                dev = self.devices[ip]          # keep runtime state
                dev.name = name
            else:
                dev = Device(ip=ip, name=name)
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

        for ip in removed:
            log.info("device %s removed from config", ip)
        log.info(
            "config applied: %d devices, rooms: %s",
            len(self.devices), ", ".join(self.rooms),
        )

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
                    d.set_brightness(bri)
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


def _apply_controls(devices: list[Device], body: dict) -> dict:
    """
    Accepts any of: on (bool), brightness (int), kelvin (int),
    rgb ([r,g,b]). Order of sends: power -> color -> brightness, so a
    brightness-only request never changes color, and turning on with a
    color lands the light in the right state.
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
        for d in devices:
            d.set_brightness(int(brightness))
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
    return jsonify(_apply_controls(devs, _body()))


@app.post("/control")
def control():
    body = _body()
    devices = _target_from(body)
    return jsonify(_apply_controls(devices, body))


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
