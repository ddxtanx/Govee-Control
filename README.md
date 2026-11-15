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

### Brightness calibration (mixed hardware in one room)

The LAN protocol's brightness is a raw 0–100 value, not a measured
luminance — a floodlight and a bulb at "60%" can look very different.
Give brighter fixtures a per-device `scale` factor so **bulk** commands
(room controls, scenes, wake ramp, `/control`) land evenly:

```json
"devices": {
  "bath flood": { "ip": "192.168.1.12", "scale": 0.7 },
  "bath bulb":  "192.168.1.13"
}
```

A room-level 60% then sends 42 to the flood and 60 to the bulb
(device slider controls stay raw, so you can still trim one lamp by
hand). Tune by eye: start around 0.65–0.75 for floodlights and adjust
until the room reads even. Apply changes without restarting via
`POST /config/reload`.

#### Auto-scaling from device models

Give fixtures their rated output instead and the server does the math:

```json
"devices": {
  "bath flood": { "ip": "192.168.1.12", "lumens": 1600 },
  "bath bulb":  "192.168.1.13"
}
```

- `POST /identify` (or the Settings → *Identify + auto-scale* button)
  queries each light's SKU with a LAN `scan` packet on port 4001
  (unicast — works under the same firewall rules as status).
- `POST /scale/auto` resolves lumens: config `"lumens"` wins, else a
  small seed table, else `lumens_cache.json`, else a live query
  against Govee's own storefront — SKU into its site search, then the
  **schema.org JSON-LD** block every product page publishes (the
  vendor's machine-readable spec contract, not layout scraping; only
  the SKU string leaves your network). Figures are only auto-applied
  when unanimous or corroborated — otherwise you get candidates + the
  URL to confirm. Then it normalizes every auto device to the
  smallest *effective* output (`lumens × scale`) among calibrated
  lights. A manual `scale` on a lumens-known fixture participates in
  that baseline — your by-eye trim is ground truth — and is never
  overwritten. A manual `scale` without `lumens` stays off the axis
  entirely (escape hatch for one-off trims like a nightlight puck).
- Auto-scales recompute at config load, so persist them by adding the
  `"lumens"` values to config.json.

In a fully calibrated room, the room brightness slider switches to a
**lumens axis** with banded zones: green where every light keeps
pace, orange/red where fixtures pin out at their rated max and fewer
lights follow.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `GOVEE_CONFIG` | `config.json` next to the script | config path |
| `GOVEE_PORT` / `GOVEE_HOST` | `9091` / `0.0.0.0` | web bind |
| `GOVEE_API_KEY` | unset | when set, `X-Api-Key` header is required on write requests |
| `GOVEE_SITE` | `https://us.govee.com` | storefront for lumens lookup; `""` disables web lookups |
| `GOVEE_REGIONS` | eu, uk, ca, mea govee stores | extra Govee-run storefronts tried when the primary has no hit (delistings live on regionally / in the refurb archive) |
| `GOVEE_LUMENS_CACHE` | `lumens_cache.json` next to config | persisted SKU→lumens findings, hand-editable |
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

**Auto-cancel:** while ramping, the server samples the room's status at
least once a second and always immediately before each ramp step's
brightness/color send. If any light reports itself OFF (someone pressed
the lamp button or used the Govee app), the ramp immediately cancels,
every light in the room is turned off, and the ramp worker **exits** —
afterwards the server never touches those lights again and the user is
free to set any state. The per-step check matters because real Govee
lamps often power back on when they receive brightness/color commands
while off; sampling before every send ensures your "off" is detected
before the sunrise can re-ignite the lamp.

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
4002) for testing without hardware — it self-exits after 60 s idle.
`tests/fake_battle.py` is a variant that emulates real lamps which
power back on when they receive brightness/color commands while off.

```sh
python3 fake_device.py 2> fake.log &                 # terminal 1
GOVEE_CONFIG=test_config.json python3 govee_server.py  # terminal 2
python3 tests/test_wake_cancel.py                    # terminal 3

# off-vs-ramp fight (auto-cancel must win even vs re-igniting lamps):
python3 tests/fake_battle.py 2> fake.log &
FAKE_LOG=fake.log python3 tests/test_wake_battle.py
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
