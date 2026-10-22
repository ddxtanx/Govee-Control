#!/usr/bin/env python3
"""Fake Govee device variant that emulates a real-world quirk: brightness
and colorwc commands silently POWER THE LAMP BACK ON when it is off
(re-ignition). Used to test wake-ramp off-detection against lamps that
fight back."""
import json
import socket
import sys

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(120.0)
sock.bind(("0.0.0.0", 4003))
state = {
    "onOff": 1,
    "brightness": 50,
    "color": {"r": 200, "g": 150, "b": 100},
    "colorTemInKelvin": 3000,
}


def rx(cmd, data, addr):
    print(f"RX {cmd} {data} from {addr}", file=sys.stderr, flush=True)


while True:
    try:
        payload, addr = sock.recvfrom(4096)
    except socket.timeout:
        break
    try:
        msg = json.loads(payload)["msg"]
    except (KeyError, ValueError):
        continue
    cmd, data = msg["cmd"], msg.get("data", {})
    rx(cmd, data, addr)
    if cmd == "turn":
        state["onOff"] = data["value"]
    elif cmd == "brightness":
        state["brightness"] = data["value"]
        if state["onOff"] == 0 and data.get("value", 0) > 0:
            state["onOff"] = 1
            print("RE-IGNITED by brightness", file=sys.stderr, flush=True)
    elif cmd == "colorwc":
        state["color"] = data["color"]
        state["colorTemInKelvin"] = data.get("colorTemInKelvin", 0)
        if state["onOff"] == 0:
            state["onOff"] = 1
            print("RE-IGNITED by colorwc", file=sys.stderr, flush=True)
    if cmd == "devStatus":
        sock.sendto(
            json.dumps({"msg": {"cmd": "devStatus", "data": state}}).encode(),
            ("127.0.0.1", 4002),
        )
