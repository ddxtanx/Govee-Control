#!/usr/bin/env python3
"""On-the-fly lumens lookup against a (fake) govee.com storefront.

Self-contained: spawns the whole rig (fake storefront, three fake
lights, server) as subprocesses, runs, tears down. Point at an
external rig instead with GOVEE_RIG=external.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

BASE = os.environ.get("GOVEE_URL", "http://localhost:9091")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOG_DIR = os.environ.get("GOVEE_LOGDIR", "/tmp/pwtest")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_A = os.path.join(LOG_DIR, "fA.log")
LOG_B = os.path.join(LOG_DIR, "fB.log")
CACHE = os.environ.get("GOVEE_LUMENS_CACHE",
                       os.path.join(LOG_DIR, "lumens_cache.json"))

rig = []
log_files = []
if os.environ.get("GOVEE_RIG") != "external":
    py = sys.executable
    fake_cmds = [
        (LOG_A, [py, os.path.join(HERE, "fake_ip.py"), "127.0.0.1", "FLOODX"], None),
        (LOG_B, [py, os.path.join(HERE, "fake_ip.py"), "127.0.0.2", "H6010"], None),
        (None,  [py, os.path.join(HERE, "fake_ip.py"), "127.0.0.3", "NOISY"], None),
        (None,  [py, os.path.join(HERE, "fake_ip.py"), "127.0.0.5", "REFURBX"], None),
        (None,  [py, os.path.join(HERE, "fake_ip.py"), "127.0.0.6", "DELISTX"], None),
        (None,  [py, os.path.join(HERE, "fake_ip.py"), "127.0.0.7", "DECOYX"], None),
        (None,  [py, os.path.join(HERE, "fake_govee_site.py")],
         {"PORT": "9093", "SITE": "primary"}),
        (None,  [py, os.path.join(HERE, "fake_govee_site.py")],
         {"PORT": "9094", "SITE": "region"}),
    ]
    for logfile, cmd, extra_env in fake_cmds:
        fh = open(logfile, "w") if logfile else subprocess.DEVNULL
        if logfile:
            log_files.append(fh)
        env = dict(os.environ, **(extra_env or {}))
        rig.append(subprocess.Popen(cmd, stderr=fh, env=env))
    env = dict(os.environ,
               GOVEE_SITE="http://127.0.0.1:9093",
               GOVEE_REGIONS="http://127.0.0.1:9094",
               GOVEE_LUMENS_CACHE=CACHE,
               GOVEE_CONFIG=os.path.join(HERE, "lookup_config.json"),
               GOVEE_PORT="9091")
    rig.append(subprocess.Popen([py, os.path.join(ROOT, "govee_server.py")],
                                env=env, stderr=subprocess.DEVNULL))
    for _ in range(50):
        try:
            urllib.request.urlopen(BASE + "/healthz", timeout=1)
            break
        except Exception:
            time.sleep(0.2)


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
check("identify all six skus",
      set(res["skus"].values()) == {"FLOODX", "H6010", "NOISY",
                                    "REFURBX", "DELISTX", "DECOYX"},
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
check("REFURBX resolved via refurb archive page -> 1500",
      by_ip["127.0.0.5"]["lumens"] == 1500
      and "refurbished-refurbx" in str(resolved.get("127.0.0.5")),
      str(resolved.get("127.0.0.5")))
check("DELISTX resolved via regional storefront -> 1300",
      by_ip["127.0.0.6"]["lumens"] == 1300
      and "web" in resolved.get("127.0.0.6", ""),
      str(resolved.get("127.0.0.6")))
check("DECOYX rejected by structured-data gate (nav mention + 999lm "
      "on wrong-SKU page must not leak)",
      by_ip["127.0.0.7"]["lumens"] is None
      and 999 not in (by_ip["127.0.0.7"].get("candidates") or [])
      and by_ip["127.0.0.7"]["scale"] == 1.0,
      str(by_ip["127.0.0.7"]))
check("anchor math: X 0.75, Y 1.0, Z untouched, W 0.8, V 0.92",
      by_ip["127.0.0.1"]["scale"] == 0.75
      and by_ip["127.0.0.2"]["scale"] == 1.0
      and by_ip["127.0.0.3"]["scale"] == 1.0
      and by_ip["127.0.0.5"]["scale"] == 0.8
      and by_ip["127.0.0.6"]["scale"] == 0.92)

cache = json.load(open(CACHE))
check("cache persisted resolved web finds only",
      cache == {"FLOODX": 1600, "REFURBX": 1500, "DELISTX": 1300},
      str(cache))

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
for p in rig:
    p.terminate()
sys.exit(1 if fails else 0)
