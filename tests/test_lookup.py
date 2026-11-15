#!/usr/bin/env python3
"""On-the-fly lumens lookup against a (fake) govee.com storefront.

Rig:
  python3 tests/fake_govee_site.py &                     # :9093
  python3 tests/fake_ip.py 127.0.0.1 FLOODX 2> fA.log &
  python3 tests/fake_ip.py 127.0.0.2 H6010   2> fB.log &
  python3 tests/fake_ip.py 127.0.0.3 NOISY   2> fC.log &
  GOVEE_SITE=http://127.0.0.1:9093 \
  GOVEE_LUMENS_CACHE=/tmp/pwtest/lumens_cache.json \
  GOVEE_CONFIG=tests/lookup_config.json python3 govee_server.py
"""
import json
import os
import re
import sys
import urllib.request

BASE = os.environ.get("GOVEE_URL", "http://localhost:9091")
LOG_A = os.environ.get("FAKE_LOG_A", "fA.log")
LOG_B = os.environ.get("FAKE_LOG_B", "fB.log")
CACHE = os.environ.get("GOVEE_LUMENS_CACHE", "/tmp/pwtest/lumens_cache.json")


def api(path, method="GET", body=None):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


fails = []


def check(label, cond, detail=""):
    print(f"  {'OK' if cond else 'FAIL'}  {label}  {detail}")
    if not cond:
        fails.append(label)


if os.path.exists(CACHE):
    os.remove(CACHE)

res = api("/identify", "POST")
check("identify all three skus",
      set(res["skus"].values()) == {"FLOODX", "H6010", "NOISY"},
      str(res["skus"]))

res = api("/scale/auto", "POST", {"identify": False})
by_ip = {s["ip"]: s for s in res["scales"]}
resolved = res["resolved"]
check("web lookup resolved FLOODX -> 1600 (title + agreeing copy)",
      by_ip["127.0.0.1"]["lumens"] == 1600
      and "web" in resolved.get("127.0.0.1", ""), str(resolved))
check("H6010 via seed table -> 1200",
      by_ip["127.0.0.2"]["lumens"] == 1200
      and resolved.get("127.0.0.2") == "seed table")
check("NOISY: disagreeing page refused, candidates surfaced",
      by_ip["127.0.0.3"]["lumens"] is None
      and "unsure" in resolved.get("127.0.0.3", "")
      and "150/1200" in resolved.get("127.0.0.3", ""),
      str(resolved.get("127.0.0.3")))
check("anchor math: X 0.75, Y 1.0, Z untouched 1.0",
      by_ip["127.0.0.1"]["scale"] == 0.75
      and by_ip["127.0.0.2"]["scale"] == 1.0
      and by_ip["127.0.0.3"]["scale"] == 1.0)

cache = json.load(open(CACHE))
check("cache persisted FLOODX: 1600 only",
      cache == {"FLOODX": 1600}, str(cache))

mA = len(open(LOG_A).read().splitlines())
mB = len(open(LOG_B).read().splitlines())
api("/rooms/bathroom", "POST", {"brightness": 80})


def bri_values(path, mark):
    vals = []
    for line in open(path).read().splitlines()[mark:]:
        m = re.match(r"RX brightness \{'value': (\d+)\}", line)
        if m:
            vals.append(int(m.group(1)))
    return vals


va, vb = bri_values(LOG_A, mA), bri_values(LOG_B, mB)
check("room 80% -> flood X raw 60", va == [60], f"A={va}")
check("room 80% -> bulb Y raw 80", vb == [80], f"B={vb}")

print()
print("RESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
