#!/usr/bin/env python3
"""Test wake-ramp auto-cancel: external 'turn 0' mid-ramp -> ramp aborts
and server force-turns-off all target lights. Also regression: a normal
ramp still completes, and manual cancel still behaves."""
import json
import os
import socket
import sys
import time
import urllib.request

BASE = os.environ.get("GOVEE_URL", "http://localhost:9091")


def api(path, method="GET", body=None):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def send_device(cmd, data):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(json.dumps({"msg": {"cmd": cmd, "data": data}}).encode(),
             ("127.0.0.1", 4003))
    s.close()


def wake_state():
    return api("/wake_up")


def wait_done(timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = wake_state()
        if not st["running"]:
            return st
        time.sleep(0.5)
    return wake_state()


fails = []


def check(label, cond, detail=""):
    print(f"  {'OK' if cond else 'FAIL'}  {label}  {detail}")
    if not cond:
        fails.append(label)


# ---------- test 1: external off mid-ramp cancels + forces off ----------
print("TEST 1: external off mid-ramp")
ramp = api("/wake_up", "POST", {"room": "bedroom", "minutes": 1,
                                "brightness": 80})
check("ramp running after start", ramp["running"] is True)
time.sleep(5)                                   # a few steps in
send_device("turn", {"value": 0})               # someone turns it OFF
st = wait_done(timeout=10)
check("ramp stopped", st["running"] is False, f"state={st}")
check("stop_reason=lights_off", st.get("stop_reason") == "lights_off")
check("error surfaced", "auto-cancelled" in (st.get("error") or ""))

# confirm the device is actually OFF via the server's status refresh
r = api("/refresh", "POST")
dev = next((d for d in r["devices"] if d["name"] == "fake lamp"), None)
check("device forced OFF", dev is not None and dev["on"] is False,
      f"dev={dev}")

# ---------- test 2: normal ramp completes, stop_reason stays null -------
print("TEST 2: normal ramp completes")
api("/wake_up", "POST", {"room": "bedroom", "minutes": 0.15,
                         "brightness": 60})
st = wait_done(timeout=25)
check("completed", st["running"] is False)
check("no stop_reason", st.get("stop_reason") is None, f"state={st}")
check("no error", st.get("error") is None)
check("brightness reached target", st["current_brightness"] == 60)

# ---------- test 3: manual cancel unchanged -----------------------------
print("TEST 3: manual cancel")
api("/wake_up", "POST", {"room": "bedroom", "minutes": 2})
time.sleep(2)
api("/wake_up", "DELETE")
st = wait_done(timeout=10)
check("stopped", st["running"] is False)
check("no lights_off reason", st.get("stop_reason") is None, f"state={st}")

print()
print("RESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
