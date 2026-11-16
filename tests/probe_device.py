#!/usr/bin/env python3
"""Live single-lamp protocol probe (read-after-write semantics).

Run this ON the machine hosting govee-server (talks to its localhost
API):

    python3 tests/probe_device.py [--name bathroom] [--url http://localhost:9091]

It drives ONE lamp through a known sequence — off, brightness-30,
brightness-0, off, kelvin(colorwc)-while-off, off — reading what the
DEVICE itself reports (via POST /refresh -> devStatus) after every
step, so we learn field facts, not firmware folklore:

  1. does a brightness packet re-ignite an off lamp (at that level)?
  2. what does a duty-0 packet physically do (hold / min / off)?
  3. does a colorwc (kelvin) packet re-ignite an off lamp?
  4. what fields does a real-model devStatus reply actually carry?

Safety: one device only, ends with power OFF, sends nothing else.
Run it BEFORE deploying the duty0->power-off build: the currently
deployed server forwards brightness 0 raw, which is exactly what
step 2 needs to observe.
"""
import argparse
import json
import sys
import time
import urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--name", default="bathroom",
                help="substring of the device name to probe")
ap.add_argument("--url", default="http://localhost:9091")
args = ap.parse_args()


def api(path, method="GET", body=None):
    req = urllib.request.Request(
        args.url + path, method=method,
        data=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def read_state(ip):
    api("/refresh", "POST")
    time.sleep(0.3)
    return api(f"/devices/{ip}")


fails = []
devices = api("/devices")
if isinstance(devices, dict):
    devices = devices.get("devices", devices)
target = None
for d in devices:
    if args.name.lower() in (d.get("name") or "").lower():
        target = d
        break
if target is None:
    sys.exit(f"no device matching {args.name!r}; "
             f"names: {[d.get('name') for d in devices]}")
ip = target["ip"]
print(f"probing: {target.get('name')!r} @ {ip}")
print(f"baseline state (raw devStatus-derived payload):\n"
      f"  {json.dumps(read_state(ip))}\n")

steps = [
    ("off (turn)",                {"on": False}),
    ("brightness 30 while off",   {"brightness": 30}),
    ("brightness 0 (dead packet?)", {"brightness": 0}),
    ("off (turn)",                {"on": False}),
    ("kelvin 2700 while off",     {"kelvin": 2700}),
    ("off (turn, restore)",       {"on": False}),
]

rows = []
for label, body in steps:
    api(f"/devices/{ip}", "POST", body)
    time.sleep(0.4)
    st = read_state(ip)
    rows.append((label, st))
    print(f"{label:<32} -> on={st['on']!s:<5} "
          f"bri={st['brightness']:<3} kelvin={st.get('kelvin')} "
          f"rgb={st.get('rgb')}")

print()
on_b30 = rows[1][1]["on"]
b0 = rows[2][1]["brightness"]
on_k = rows[4][1]["on"]
final = rows[5][1]["on"]

print("VERDICTS (as reported by the physical lamp):")
print(f"  brightness packet re-ignites off lamp: {on_b30} "
      f"(bri={rows[1][1]['brightness']})")
if rows[2][1]["on"] and b0 == rows[1][1]["brightness"]:
    print("  duty-0 packet: HELD previous level (dead packet) "
          f"— lamp still reports bri={b0}")
elif rows[2][1]["on"] and b0 in (0, 1):
    print(f"  duty-0 packet: lamp clamped to bri={b0} (min glow)")
elif not rows[2][1]["on"]:
    print("  duty-0 packet: lamp actually went OFF (it is honored)")
else:
    print(f"  duty-0 packet: lamp reports on={rows[2][1]['on']} "
          f"bri={b0} — interpret by eye")
print(f"  colorwc/kelvin re-ignites off lamp: {on_k}")
print(f"  lamp left OFF at end: {not final}")
if final:
    fails.append("lamp not off at end")
sys.exit(1 if fails else 0)
