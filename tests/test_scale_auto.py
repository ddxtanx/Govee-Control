#!/usr/bin/env python3
"""SKU identify + auto brightness-scale pipeline (anchor semantics).

Manual scales with lumens participate in the anchor: effective output
is lumens*scale, and auto devices normalize to the SMALLEST effective
output across calibrated devices. Manual scale without lumens stays
off-axis (applied raw, contributes nothing).

Rig (D has no fake on purpose — it never needs to answer):
  python3 tests/fake_ip.py 127.0.0.1 FAKEFLOOD 2> fA.log &
  python3 tests/fake_ip.py 127.0.0.2 H6010     2> fB.log &
  python3 tests/fake_ip.py 127.0.0.3           2> fC.log &
  GOVEE_CONFIG=tests/scale_auto_config.json python3 govee_server.py
"""
import json
import os
import re
import sys
import urllib.request

BASE = os.environ.get("GOVEE_URL", "http://localhost:9091")
LOG_A = os.environ.get("FAKE_LOG_A", "fA.log")
LOG_B = os.environ.get("FAKE_LOG_B", "fB.log")


def api(path, method="GET", body=None):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


def bri_values(path, mark):
    vals = []
    for line in open(path).read().splitlines()[mark:]:
        m = re.match(r"RX brightness \{'value': (\d+)\}", line)
        if m:
            vals.append(int(m.group(1)))
    return vals


fails = []


def check(label, cond, detail=""):
    print(f"  {'OK' if cond else 'FAIL'}  {label}  {detail}")
    if not cond:
        fails.append(label)


# 1. identify via LAN scan probe
res = api("/identify", "POST")
check("identify flood A -> FAKEFLOOD",
      res["skus"].get("127.0.0.1") == "FAKEFLOOD", str(res["skus"]))
check("identify bulb B -> H6010",
      res["skus"].get("127.0.0.2") == "H6010")

# 2. anchor = min effective lumens INCLUDING manual C (400*0.5 = 200)
res = api("/scale/auto", "POST", {"identify": False})
by_ip = {s["ip"]: s for s in res["scales"]}
check("B lumens from SKU table", by_ip["127.0.0.2"]["lumens"] == 1200)
check("manual C unchanged (0.5, participates as anchor)",
      by_ip["127.0.0.3"]["scale"] == 0.5
      and by_ip["127.0.0.3"]["status"] == "manual+anchor")
check("A auto-scaled to anchor 200/1600 = 0.12",
      by_ip["127.0.0.1"]["scale"] == 0.12,
      str(by_ip["127.0.0.1"]))
check("B auto-scaled to anchor 200/1200 = 0.17",
      by_ip["127.0.0.2"]["scale"] == 0.17,
      str(by_ip["127.0.0.2"]))
check("trim D off-axis: keeps 0.3, contributes nothing",
      by_ip["127.0.0.4"]["scale"] == 0.3
      and by_ip["127.0.0.4"]["status"] == "manual (off-axis)")

# effective outputs converge within rounding (anchor 200 lm)
effs = [by_ip[ip]["effective_lumens"] for ip in
        ("127.0.0.1", "127.0.0.2", "127.0.0.3")]
check("effective lumens ~equal (within 10% of 200)",
      all(abs(e - 200) <= 20 for e in effs), str(effs))

# 3. room command lands the anchor even across all three fixtures
mA, mB = (len(open(LOG_A).read().splitlines()),
          len(open(LOG_B).read().splitlines()))
api("/rooms/bathroom", "POST", {"brightness": 100})
va, vb = bri_values(LOG_A, mA), bri_values(LOG_B, mB)
exp_a = round(100 * by_ip["127.0.0.1"]["scale"])
exp_b = round(100 * by_ip["127.0.0.2"]["scale"])
check("room 100% -> flood A raw matches its scale",
      va == [exp_a], f"A={va} want={[exp_a]}")
check("room 100% -> bulb B raw matches its scale",
      vb == [exp_b], f"B={vb} want={[exp_b]}")

print()
print("RESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
