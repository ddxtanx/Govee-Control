#!/usr/bin/env python3
"""Fake Govee device bound to a specific loopback IP (multi-device tests).

Usage: fake_ip.py <bind-ip> [sku]
Listens on :4003 (control/status) and :4001 (LAN scan/identify);
replies on :4002 exactly like real devices."""
import json
import socket
import sys

BIND = sys.argv[1]
SKU = sys.argv[2] if len(sys.argv) > 2 else None

ctrl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
ctrl.settimeout(120.0)
ctrl.bind((BIND, 4003))

disc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
disc.bind((BIND, 4001))

state = {
    "onOff": 1,
    "brightness": 50,
    "color": {"r": 200, "g": 150, "b": 100},
    "colorTemInKelvin": 3000,
}

print(f"fake on {BIND}:4003/4001 sku={SKU}", file=sys.stderr, flush=True)
while True:
    ready, _, _ = __import__("select").select([ctrl, disc], [], [], 60.0)
    if not ready:
        break
    for sock in ready:
        payload, addr = sock.recvfrom(4096)
        try:
            msg = json.loads(payload)["msg"]
        except (KeyError, ValueError):
            continue
        cmd, data = msg["cmd"], msg.get("data", {})
        print(f"RX {cmd} {data}", file=sys.stderr, flush=True)
        if cmd == "turn":
            state["onOff"] = data["value"]
        elif cmd == "brightness":
            state["brightness"] = data["value"]
        elif cmd == "colorwc":
            state["color"] = data["color"]
            state["colorTemInKelvin"] = data.get("colorTemInKelvin", 0)
        if cmd == "devStatus":
            ctrl.sendto(
                json.dumps({"msg": {"cmd": "devStatus", "data": state}}).encode(),
                ("127.0.0.1", 4002),
            )
        if cmd == "scan" and SKU:
            ctrl.sendto(json.dumps({"msg": {"cmd": "scan", "data": {
                "ip": BIND,
                "device": "aa:bb:cc:dd:ee:ff",
                "sku": SKU,
            }}}).encode(), ("127.0.0.1", 4002))
