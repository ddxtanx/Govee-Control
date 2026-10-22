#!/usr/bin/env python3
"""User-report scenario: user turns a (re-igniting) light off mid-ramp.
The ramp must abort promptly BEFORE re-igniting the lamp, force off once,
TERMINATE, and then the user's next 'on' must stick."""
import json
import os
import re
import socket
import sys
import time
import urllib.request

BASE = os.environ.get("GOVEE_URL", "http://localhost:9091")
FAKE_LOG = os.environ.get("FAKE_LOG", "fake.log")


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


def wait_done(timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = api("/wake_up")
        if not st["running"]:
            return st
        time.sleep(0.25)
    return api("/wake_up")


fails = []


def check(label, cond, detail=""):
    print(f"  {'OK' if cond else 'FAIL'}  {label}  {detail}")
    if not cond:
        fails.append(label)


print("TEST: off mid-ramp against re-igniting lamp (bug repro + fix)")
api("/wake_up", "POST", {"room": "bedroom", "minutes": 1.0,
                         "brightness": 80})   # 60 s, 0.6 s steps
time.sleep(3)
t_off = time.time()
# user presses off repeatedly (what a person actually does when the
# lamp refuses to stay off); abort must land promptly despite re-ignitions
stop_pressing = time.time() + 8
while time.time() < stop_pressing:
    if not api("/wake_up")["running"]:
        break
    send_device("turn", {"value": 0})
    time.sleep(0.5)
st = wait_done()
abort_latency = time.time() - t_off
check("ramp aborted", st["running"] is False, f"latency={abort_latency:.1f}s")
check("stop_reason=lights_off", st.get("stop_reason") == "lights_off")
check("aborted within 5 s of user's first off", abort_latency < 5.0,
      f"{abort_latency:.1f}s")

r = api("/refresh", "POST")
dev = next((d for d in r["devices"] if d["name"] == "fake lamp"), None)
check("light left OFF after abort", dev and dev["on"] is False, f"dev={dev}")

# user now turns the room back on — it must stick
api("/rooms/bedroom", "POST", {"on": True})
time.sleep(6)                                  # longer than any old watch cycle
r = api("/refresh", "POST")
dev = next((d for d in r["devices"] if d["name"] == "fake lamp"), None)
check("user's ON sticks after abort", dev and dev["on"] is True, f"dev={dev}")

# fake log: the last power command must be the user's turn 1 —
# no turn 0 after it, and no ramp brightness sends after the abort's turn 0
log = open(FAKE_LOG).read().splitlines()
turns = [(i, l) for i, l in enumerate(log) if l.startswith("RX turn")]
last_turn = turns[-1]
check("last power cmd is user's ON", "{'value': 1}" in last_turn[1],
      last_turn[1])
after = log[last_turn[0] + 1:]
strays = [l for l in after if re.match(r"RX (turn|brightness|colorwc)", l)]
check("no ramp traffic after user regained control", not strays,
      f"strays={strays[:3]}")

print()
print("RESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
