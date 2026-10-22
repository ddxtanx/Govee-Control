# Govee-Control

Self-hosted LAN control for Govee lights: one small Python server plus a
mobile-friendly web UI styled after the Govee app. No cloud, no Govee
account — every packet stays on your LAN.

```
phone / Shortcuts / NFC ──HTTP──▶ govee_server.py ──UDP──▶ Govee devices
                                   (Flask, port 9091)   (out: :4003, replies in: :4002)
```

> Devices need LAN control enabled first: in the Govee app, open a device →
> **Device Settings** → enable **LAN Control** (some firmwares call it
> *Local API_control*). Devices must stay on the same subnet as the server.

## Quick start

```sh
cp config.example.json config.json   # edit device names/IPs + rooms
python3 govee_server.py              # requires: pip install flask
# open http://<server>:9091/
```

`config.json` is gitignored on purpose — it holds your real LAN addresses.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `GOVEE_CONFIG` | `config.json` next to the script | config path |
| `GOVEE_PORT` / `GOVEE_HOST` | `9091` / `0.0.0.0` | web bind |
| `GOVEE_API_KEY` | unset | when set, `X-Api-Key` header is required on write requests |
| `GOVEE_LOGLEVEL` | `INFO` | logging |

## The UI

Single page, no build step: greeting header, wake-up card, room cards →
room panel → device panel. Per device: power, brightness and color-temp
sliders (they stream while you drag), a 2D color wheel (hue = angle,
saturation = radius), manual HSL/RGB entry, scenes, and a per-device saved
palette (kept in browser `localStorage`). The room panel mirrors every
control. Installable as a PWA / Add-to-Home-Screen on iOS and Android.

## Wake-up ramp

`POST /wake_up` ramps a whole room from off → target brightness over N
minutes, optionally warming color temperature (`kelvin_start` → `kelvin`).

- `GET /wake_up` — live progress (`running`, `progress`, `current_brightness`, `stop_reason`)
- `POST /wake_up` — start/restart; body may override `room`, `minutes`, `brightness`, `kelvin_start`, `kelvin`
- `DELETE /wake_up` — cancel

**Auto-cancel:** while ramping the server polls the room's status every few
seconds. If any light reports itself OFF (someone pressed the lamp button
or used the Govee app), the ramp immediately cancels **and turns every
light in the room off** — turning the lights off mid-sunrise is an explicit
"no", and the sunrise must not steamroll it.

## One-call API (Shortcuts / NFC / scripts)

`POST /control` applies power + color + brightness to a device or room in a
single request; sends are ordered power → color → brightness:

```sh
curl -X POST http://<server>:9091/control \
  -H 'Content-Type: application/json' \
  -d '{"device": "living room lamp", "on": true, "rgb": [255, 80, 0], "brightness": 60}'
```

Other endpoints: `GET /api` (self-documenting target list), `GET /healthz`,
`GET /devices`, `POST /refresh`, `GET|POST /devices/<ip-or-name>`,
`GET /rooms`, `GET|POST /rooms/<room>`, `POST /scan` (subnet discovery),
`POST /config/reload`. Error responses include the known device/room list
so a mistyped target is easy to fix from a shortcut.

## Testing

`fake_device.py` is a fake UDP Govee device (listens on 4003, replies on
4002) for testing without hardware — it self-exits after 60 s idle:

```sh
python3 fake_device.py &
GOVEE_CONFIG=test_config.json python3 govee_server.py &
curl -s localhost:9091/refresh
```

## Deploy (systemd)

`govee-server.service` installs the server as unit `govee-server`. Put the
code and your `config.json` under `/opt/govee/` (dedicated non-root user),
then:

```sh
sudo cp govee-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now govee-server
```
