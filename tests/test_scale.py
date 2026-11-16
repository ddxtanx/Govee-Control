#!/usr/bin/env python3
"""Per-device brightness scale: bulk (room / control / wake-ramp)
brightness is scaled per device; per-device commands stay raw."""
import json
import re
from pathlib import Path
import sys
import time
import urllib.request

import os
BASE = os.environ.get("GOVEE_URL", "http://localhost:9091")
LOG_A = os.environ.get("FAKE_LOG_A", "fakeA.log")
LOG_B = os.environ.get("FAKE_LOG_B", "fakeB.log")
CONFIG = os.environ.get("SCALE_CONFIG", "tests/scale_config.json")


def api(path, method="GET", body=None):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def bri_values(path, mark):
    """brightness values sent to a fake since line-count mark"""
    vals = []
    for line in open(path).read().splitlines()[mark:]:
        m = re.match(r"RX brightness \{'value': (\d+)\}", line)
        if m:
            vals.append(int(m.group(1)))
    return vals


def lines(path):
    return len(open(path).read().splitlines())


fails = []


def check(label, cond, detail=""):
    print(f"  {'OK' if cond else 'FAIL'}  {label}  {detail}")
    if not cond:
        fails.append(label)


ORIGINAL_CFG = Path(CONFIG).read_text()
devices = api("/devices")
a = next(d for d in devices if d["ip"] == "127.0.0.1")
check("device A carries scale 0.7", a.get("scale") == 0.7, f"scale={a.get('scale')}")

# 1. room bulk brightness -> scaled per device
mA, mB = lines(LOG_A), lines(LOG_B)
api("/rooms/bedroom", "POST", {"brightness": 60})
va, vb = bri_values(LOG_A, mA), bri_values(LOG_B, mB)
check("room 60% -> flood(0.7) got 42", va == [42], f"A={va}")
check("room 60% -> bulb got raw 60", vb == [60], f"B={vb}")

# 2. per-device command stays raw
mA = lines(LOG_A)
api("/devices/127.0.0.1", "POST", {"brightness": 60})
va = bri_values(LOG_A, mA)
check("device-direct 60 -> raw 60 (no scale)", va == [60], f"A={va}")

# 3. /control is normalized
mA, mB = lines(LOG_A), lines(LOG_B)
api("/control", "POST", {"room": "bedroom", "brightness": 80})
va, vb = bri_values(LOG_A, mA), bri_values(LOG_B, mB)
check("control room 80% -> flood 56", va == [56], f"A={va}")
check("control room 80% -> bulb 80", vb == [80], f"B={vb}")

# 4. wake ramp lands scaled: final room 100% -> A max 70, B max 100
mA, mB = lines(LOG_A), lines(LOG_B)
api("/wake_up", "POST", {"room": "bedroom", "minutes": 0.15,
                         "brightness": 100})
t0 = time.time()
while time.time() - t0 < 20:
    if not api("/wake_up")["running"]:
        break
    time.sleep(0.3)
va, vb = bri_values(LOG_A, mA), bri_values(LOG_B, mB)
check("ramp: A never exceeds 70, ends at 70",
      bool(va) and max(va) == 70 and va[-1] == 70, f"A tail={va[-3:]}")
check("ramp: 100 steps, monotonic, scaled, starts at duty 1 "
      "(phase-0 is a power-OFF packet now, not a duty-0 send)",
      va == sorted(va) and len(va) == 100 and va[0] == 1 and va[-1] == 70,
      f"n={len(va)} head={va[:2]} tail={va[-2:]}")
check("ramp: B reaches raw 100",
      bool(vb) and vb[-1] == 100, f"B tail={vb[-3:]}")

# 5. config reload picks up a new scale, no restart
Path(CONFIG).write_text(json.dumps({
    "devices": {
        "lamp A": {"ip": "127.0.0.1", "scale": 0.5},
        "lamp B": "127.0.0.2"},
    "rooms": {"bedroom": ["lamp A", "lamp B"]},
    "wake": {"room": "bedroom", "minutes": 15}}))
api("/config/reload", "POST")
mA = lines(LOG_A)
api("/rooms/bedroom", "POST", {"brightness": 60})
va = bri_values(LOG_A, mA)
check("reload: scale 0.5 live -> room 60% sends 30", va == [30], f"A={va}")

# restore the config file we mutated in test 5
Path(CONFIG).write_text(ORIGINAL_CFG)

print()
print("RESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
