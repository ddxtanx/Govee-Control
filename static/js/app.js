"use strict";

/* ============================== helpers ============================== */

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function hslToRgb(h, s = 100, l = 55){
  s /= 100; l /= 100;
  const a = s * Math.min(l, 1 - l);
  const f = n => {
    const k = (n + h / 30) % 12;
    return l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
  };
  return [f(0), f(8), f(4)].map(v => Math.round(v * 255));
}

function rgbToHue(rgb){
  const [r, g, b] = rgb.map(v => v / 255);
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  if (max === min) return 0;
  const d = max - min;
  let h;
  if (max === r) h = ((g - b) / d) % 6;
  else if (max === g) h = (b - r) / d + 2;
  else h = (r - g) / d + 4;
  return (h * 60 + 360) % 360;
}

function rgbToSat(rgb){
  const [r, g, b] = rgb.map(v => v / 255);
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  const d = max - min;
  if (d === 0) return 0;
  const l = (max + min) / 2;
  return Math.round(Math.min(1, d / (1 - Math.abs(2 * l - 1))) * 100);
}

function rgbToLight(rgb){
  const [r, g, b] = rgb.map(v => v / 255);
  return Math.round(((Math.max(r, g, b) + Math.min(r, g, b)) / 2) * 100);
}

function clampInt(v, lo, hi){
  const n = Math.round(Number(v));
  return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : lo;
}

function kelvinTint(k){
  k = Math.min(Math.max(k || 0, 2000), 9000);
  const warm = [255,171,130], white = [255,255,255], cool = [205,224,247];
  const t = k <= 4000 ? (k - 2000) / 2000 : (k - 4000) / 5000;
  const lo = k <= 4000 ? warm : white, hi = k <= 4000 ? white : cool;
  return lo.map((v, i) => Math.round(v + (hi[i] - v) * t));
}

const css = rgb => `rgb(${rgb[0]},${rgb[1]},${rgb[2]})`;
const deviceColor = d => (d.kelvin > 0 ? kelvinTint(d.kelvin) : (d.rgb || [255,255,255]));
const deviceStatus = d => !d
  ? "unknown"
  : d.on
    ? `On · ${d.brightness ?? "?"}%${d.kelvin > 0 ? " · " + d.kelvin + "K" : ""}`
    : "Off";

/* ============================== constants ============================ */

const SCENES = [
  {id:"relax",  name:"Relax",  icon:"🌙", kelvin:2700, brightness:40,  sub:"Warm & low"},
  {id:"focus",  name:"Focus",  icon:"☀️", kelvin:5000, brightness:100, sub:"Crisp daylight"},
  {id:"read",   name:"Read",   icon:"📖", kelvin:4500, brightness:100, sub:"Bright & neutral"},
  {id:"movie",  name:"Movie",  icon:"🎬", kelvin:2200, brightness:25,  sub:"Dim & cozy"},
  {id:"night",  name:"Night",  icon:"🕯️", kelvin:2000, brightness:10,  sub:"Faint amber"},
  {id:"party",  name:"Party",  icon:"🪩", rgb:[255,0,128], brightness:100, sub:"Neon pink"},
];
const SWATCHES = [
  [255,77,109],[255,159,67],[255,213,74],[154,205,50],[72,199,142],[41,183,203],
  [52,152,219],[111,81,226],[175,82,222],[230,97,161],[206,143,255],[189,195,206],
];

/* ============================== state ================================ */

const S = {
  api: null,
  rooms: ["all"],
  roomsMap: {},
  devices: [],
  room: "all",
  selectedIp: null,
  selectedRoom: null,
  view: "home",
  wake: null,
  hues: {},            // last hue/sat used per ip (UI memory)
  sats: {},
  lastKey: "",
  lastWakeKey: "",
  dragging: false,
  busy: false,
};

const KEY = () => localStorage.getItem("goveeApiKey") || "";

/* ============================== api ================================ */

async function api(path, {method = "GET", body} = {}){
  const res = await fetch(path, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(KEY() ? {"X-Api-Key": KEY()} : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok){
    let msg = res.statusText;
    try{ msg = (await res.json()).error || msg; }catch(_){}
    if (res.status === 401) msg = "API key rejected (set it in Settings)";
    throw new Error(msg);
  }
  return res.json();
}

function patchDevice(ip, patch){
  const d = S.devices.find(x => x.ip === ip);
  if (d) Object.assign(d, patch);
}

function patchTarget(id, isRoom, patch){
  if (isRoom){
    const ips = S.roomsMap[id] || [];
    S.devices.forEach(d => { if (ips.includes(d.ip)) Object.assign(d, patch); });
  }else{
    patchDevice(id, patch);
  }
}

/* ---- per-device saved color palette (localStorage, keyed by ip) ---- */
function loadPalette(ip){
  try{
    const p = JSON.parse(localStorage.getItem("goveePal:" + ip) || "[]");
    return Array.isArray(p) ? p : [];
  }catch(_){ return []; }
}
function savePalette(ip, pal){
  try{ localStorage.setItem("goveePal:" + ip, JSON.stringify(pal)); }catch(_){}
}

function toast(msg, ok = false){
  const t = $("#toast");
  t.textContent = msg;
  t.className = ok ? "show" : "show err";
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.className = ""; }, 2600);
}

function afterAction(){
  // optimistic local state already patched; pull truth from the devices soon
  setTimeout(poll, 900);
}

/* ============================== data =============================== */

async function loadCore(){
  const [info, rooms] = await Promise.all([api("/api"), api("/rooms")]);
  S.api = info;
  const others = Object.keys(rooms).filter(r => r !== "all");
  S.rooms = ["all", ...others];
  S.roomsMap = rooms;
}

function devicesKey(){
  return JSON.stringify(S.devices.map(d =>
    [d.ip, d.on, d.brightness, d.kelvin, d.rgb, d.online]));
}
function wakeKey(){
  const w = S.wake || {};
  return JSON.stringify([
    !!w.running, w.room, w.minutes,
    w.running ? Math.round((w.progress || 0) * 20) : null,
    w.current_brightness,
    w.stop_reason, w.error,
  ]);
}

async function poll(){
  if (S.busy) return;
  S.busy = true;
  const dot = $("#poll-dot");
  try{
    const [r, w] = await Promise.all([
      api("/refresh", {method: "POST"}),
      api("/wake_up"),
    ]);
    S.devices = r.devices;
    S.wake = w;
    if (dot){ dot.classList.add("on"); setTimeout(() => dot.classList.remove("on"), 800); }
    render();
  }catch(e){
    toast("refresh failed: " + e.message);
  }finally{
    S.busy = false;
  }
}

/* ============================== render =============================== */

function greeting(){
  const h = new Date().getHours();
  return h < 5 ? "Good night" : h < 12 ? "Good morning"
       : h < 18 ? "Good afternoon" : "Good evening";
}
function dateStr(){
  return new Date().toLocaleDateString(undefined,
    {weekday:"long", month:"long", day:"numeric"});
}

function switchHtml(el, ip, checked, big, room){
  return `<label class="switch${big ? " big" : ""}" data-stop>
    <input type="checkbox" ${el === "power" ? `data-el="power"` : `data-el="room"`}
      ${ip ? `data-ip="${esc(ip)}"` : ""}
      ${room ? `data-room="${esc(room)}"` : ""} ${checked ? "checked" : ""}>
    <span></span></label>`;
}

function wakeHtml(){
  const w = S.wake || {}, d = (S.api && S.api.wake_defaults) || {};
  const defaults = `${esc(d.room || "bedroom")} · ${d.minutes ?? 15} min` +
    (d.kelvin_start || d.kelvin ? ` · ${d.kelvin_start ?? d.kelvin}K→${d.kelvin ?? d.kelvin_start}K` : "");
  if (w.running){
    const pct = Math.round((w.progress || 0) * 100);
    return `
    <div class="wake">
      <div class="wake-top">
        <span class="wico">🌅</span>
        <div class="wt"><b>Sunrise running</b>
          <p>${esc(w.room || "")} · ${w.current_brightness ?? 0}/${w.target_brightness ?? 100}% · ${pct}%</p></div>
        <button class="btn ghost sm" data-action="wake:cancel">Cancel</button>
      </div>
      <div class="bar"><i style="width:${pct}%"></i></div>
    </div>`;
  }
  const note = w.stop_reason === "lights_off"
    ? `<p class="wnote">⚠️ Auto-cancelled — lights were turned off during the ramp (lights set off)</p>`
    : (w.error ? `<p class="wnote">⚠️ ${esc(w.error)}</p>` : "");
  return `
  <div class="wake">
    <div class="wake-top">
      <span class="wico">🌅</span>
      <div class="wt"><b>Sunrise wake</b>
        <p>${defaults}</p>${note}</div>
      <button class="btn sm" data-action="wake:start">Start</button>
    </div>
  </div>`;
}

function roomCard(room){
  const ips = S.roomsMap[room] || [];
  const devs = S.devices.filter(d => ips.includes(d.ip));
  const onCount = devs.filter(d => d.on).length;
  const tint = devs.length ? css(deviceColor(devs[0])) : "#fff";
  const sub = room === "all"
    ? `${devs.length} lights · ${onCount} on`
    : `${devs.length} light${devs.length === 1 ? "" : "s"} · ${onCount} on`;
  const allOn = devs.length > 0 && onCount === devs.length;
  return `
  <div class="room-card" data-action="room:${esc(room)}">
    <span class="rc-name">
      <span class="dot${allOn ? " on" : ""}"></span>${room === "all" ? "All rooms" : esc(room)}
    </span>
    <div class="rc-sub">${sub}</div>
    ${switchHtml("room", null, allOn, false, room)}
  </div>`;
}

function deviceCard(d){
  const tint = css(deviceColor(d));
  const on = !!d.on;
  return `
  <div class="dev-card${on ? "" : " off"}" data-action="open:${esc(d.ip)}" style="--tint:${tint}">
    <div class="dc-top">
      <span class="dot${d.online ? " on" : " off"}" title="${d.online ? "online" : "offline"}"></span>
      <span class="dc-name">${esc(d.name || d.ip)}</span>
      ${switchHtml("power", d.ip, on, false)}
    </div>
    <div class="dc-mid">
      <span class="swatch" style="background:${tint}"></span>
      ${on
        ? `<span class="dc-bri">${d.brightness ?? "?"}%</span>
           <span class="dc-sub">${d.kelvin > 0 ? d.kelvin + "K" : "RGB"}</span>`
        : `<span class="dc-sub">Off</span>`}
    </div>
  </div>`;
}

function renderHome(){
  return `
  <div class="greet"><h1>${greeting()}</h1><p>${dateStr()}</p></div>
  ${wakeHtml()}
  <div class="section-h">Rooms</div>
  <div class="rooms">
    ${S.rooms.map(roomCard).join("")}
  </div>
  <div class="section-h">
    Devices
    <span class="hint">tap a room for group control</span>
  </div>
  ${S.devices.length
    ? `<div class="grid">${S.devices.map(deviceCard).join("")}</div>`
    : `<div class="empty">No devices configured.<br>Add lights to config.json, then hit reload in Settings.</div>`}
  `;
}

function swatchBtns(){
  return SWATCHES.map(c =>
    `<button class="swatch-btn" style="background:${css(c)}"
      data-action="swatch:${c[0]},${c[1]},${c[2]}" title="${css(c)}"></button>`).join("");
}

/* whichever panel is open: device detail or room panel */
function panelTarget(){
  const wrap = $(".hue-wheel-wrap");
  const isRoom = !!(wrap && wrap.dataset.room);
  return { isRoom, id: isRoom ? wrap.dataset.room : S.selectedIp, wrap };
}

/* apply an exact rgb to the open panel (device or room): remember it as the
   panel's hue/sat, move the wheel handle, patch state, send to the device(s). */
function applyPanelColor(id, isRoom, r, g, b){
  const { wrap } = panelTarget();
  const hue = rgbToHue([r, g, b]);
  const sat = rgbToSat([r, g, b]);
  S.hues[id] = hue;
  S.sats[id] = sat;
  if (wrap){
    const rad = hue * Math.PI / 180;
    const h = wrap.querySelector(".wheel-handle");
    const c = wrap.querySelector(".wheel-center");
    if (h){
      h.style.left = (50 + 50 * sat / 100 * Math.sin(rad)).toFixed(2) + "%";
      h.style.top = (50 - 50 * sat / 100 * Math.cos(rad)).toFixed(2) + "%";
    }
    if (c) c.style.background = `hsl(${hue},${sat}%,50%)`;
    const v = $("#val-hue");
    if (v) v.textContent = `${hue}° · ${sat}%`;
  }
  const path = isRoom
    ? `/rooms/${encodeURIComponent(id)}` : `/devices/${id}`;
  patchTarget(id, isRoom, {rgb: [r, g, b], kelvin: 0});
  send(path, {rgb: [r, g, b], kelvin: 0})
    .then(() => afterAction()).catch(err => toast(err.message));
}

function sceneChips(ip){
  return `<div class="chips">${SCENES.map(s =>
    `<button class="chip" data-action="scene:${s.id}" data-ip="${esc(ip)}">
       <span class="cico">${s.icon}</span>${s.name}</button>`).join("")}</div>`;
}

function sceneChipsRoom(room){
  return `<div class="chips">${SCENES.map(s =>
    `<button class="chip" data-action="sceneroom:${s.id}" data-room="${esc(room)}">
       <span class="cico">${s.icon}</span>${s.name}</button>`).join("")}</div>`;
}

function manualColorHtml(target){
  // target = {ip} (device detail) or {room} (room panel)
  let rgb, h, s, bri;
  if (target.ip){
    const d = S.devices.find(x => x.ip === target.ip);
    rgb = (d && d.rgb) || [255,255,255];
    h = S.hues[target.ip] ?? rgbToHue(rgb);
    s = S.sats[target.ip] ?? rgbToSat(rgb);
    bri = (d && d.brightness) ?? 50;
  }else{
    const d0 = S.devices.find(x => (S.roomsMap[target.room]||[]).includes(x.ip));
    rgb = (d0 && d0.rgb) || [255,255,255];
    h = S.hues[target.room] ?? rgbToHue(rgb);
    s = S.sats[target.room] ?? rgbToSat(rgb);
    bri = (d0 && d0.brightness) ?? 50;
  }
  const idAttr = target.ip ? `data-ip="${esc(target.ip)}"` : `data-room="${esc(target.room)}"`;
  return `
  <div class="panel">
    <div class="panel-head"><span class="t">Manual color</span>
      <span class="hint val">L% sets brightness</span></div>
    <div class="manual-row">
      <div class="manual">
        <div class="field"><label>H°</label><input type="number" min="0" max="360" step="1" data-mk="h" value="${Math.round(h)}"></div>
        <div class="field"><label>S%</label><input type="number" min="0" max="100" step="1" data-mk="s" value="${Math.round(s)}"></div>
        <div class="field"><label>L%</label><input type="number" min="0" max="100" step="1" data-mk="l" value="${bri}"></div>
        <button class="apply" data-action="manualhsl" ${idAttr}>Set</button>
      </div>
    </div>
    <div class="manual-row">
      <div class="manual">
        <div class="field"><label>R</label><input type="number" min="0" max="255" step="1" data-mk="r" value="${rgb[0]}"></div>
        <div class="field"><label>G</label><input type="number" min="0" max="255" step="1" data-mk="g" value="${rgb[1]}"></div>
        <div class="field"><label>B</label><input type="number" min="0" max="255" step="1" data-mk="b" value="${rgb[2]}"></div>
        <button class="apply" data-action="manualrgb" ${idAttr}>Set</button>
      </div>
    </div>
  </div>`;
}

function paletteHtml(ip){
  const pal = loadPalette(ip);
  return `
  <div class="panel">
    <div class="panel-head"><span class="t">Saved colors</span>
      <button class="btn sm ghost" data-action="palette:save:${esc(ip)}">＋ Save current</button></div>
    ${pal.length
      ? `<div class="palette">${pal.map((c, i) => `
          <div class="pal-swatch" style="background:${css(c)}"
            data-action="palette:use:${esc(ip)}:${i}" title="${css(c)}">
            <button class="pal-x" data-action="palette:del:${esc(ip)}:${i}" title="Remove">×</button>
          </div>`).join("")}</div>`
      : `<div class="empty-pal">No saved colors yet — set a color, then tap “Save current”.</div>`}
  </div>`;
}

function renderDetail(ip){
  const d = S.devices.find(x => x.ip === ip);
  if (!d) return `<div class="empty">Device ${esc(ip)} not found.</div>`;
  const tint = css(deviceColor(d));
  const on = !!d.on;
  const k = d.kelvin > 0 ? d.kelvin : 3500;
  const hue = S.hues[ip] ?? rgbToHue(d.rgb || [255,0,0]);
  const sat = S.sats[ip] ?? rgbToSat(d.rgb || [0,0,0]);
  const hrad = hue * Math.PI / 180;
  const hrr = sat / 100;
  const hx = (50 + 50 * hrr * Math.sin(hrad)).toFixed(2);
  const hy = (50 - 50 * hrr * Math.cos(hrad)).toFixed(2);
  return `
  <div class="view-head">
    <button class="icon-btn" data-action="back">←</button>
    <div><h2>${esc(d.name || ip)}</h2><div class="sub">${esc(ip)}</div></div>
  </div>
  <div class="hero${on ? "" : " off"}" style="--tint:${tint}">
    <div class="hero-swatch" style="background:${tint}"></div>
    <div><h3>${esc(d.name || ip)}</h3>
      <p>${esc(d.online ? deviceStatus(d) : "offline")}</p></div>
  </div>
  <div class="row" style="margin-top:10px">
    <span class="lbl">Power</span>
    ${switchHtml("power", ip, on, true)}
  </div>
  <div class="panel">
    <div class="panel-head"><span class="t">Brightness</span>
      <span class="val" id="val-bri">${d.brightness ?? 0}%</span></div>
    <input type="range" min="0" max="100" value="${d.brightness ?? 50}"
      data-el="bri" data-ip="${esc(ip)}">
  </div>
  <div class="panel">
    <div class="panel-head"><span class="t">White temperature</span>
      <span class="val" id="val-kelvin">${d.kelvin > 0 ? d.kelvin + "K" : "—"}</span></div>
    <input type="range" min="2000" max="9000" step="100" value="${k}"
      class="kelvin-range" data-el="kelvin" data-ip="${esc(ip)}">
    <div class="kelvin-labels"><span>Warm 2000K</span><span>Cool 9000K</span></div>
  </div>
  <div class="panel">
    <div class="panel-head"><span class="t">Color</span>
      <span class="val" id="val-hue">${Math.round(hue)}° · ${sat}%</span></div>
    <div class="hue-wheel-wrap" data-ip="${esc(ip)}">
      <div class="hue-wheel"></div>
      <div class="wheel-center" style="background:hsl(${Math.round(hue)},${sat}%,50%)"></div>
      <div class="wheel-handle" style="left:${hx}%;top:${hy}%"></div>
    </div>
    <div class="swatches">${swatchBtns()}</div>
  </div>
  ${manualColorHtml({ip})}
  ${paletteHtml(ip)}
  <div class="panel">
    <div class="panel-head"><span class="t">Scenes</span><span class="hint val">for this light</span></div>
    ${sceneChips(ip)}
  </div>
  `;
}

function renderRoom(room){
  const ips = S.roomsMap[room] || [];
  const devs = S.devices.filter(d => ips.includes(d.ip));
  const onCount = devs.filter(d => d.on).length;
  const on = onCount === devs.length && devs.length > 0;
  const tint = devs.length ? css(deviceColor(devs[0])) : "#8b98a8";
  const k = devs.length && devs[0].kelvin > 0 ? devs[0].kelvin : 3500;
  const hue = S.hues[room] ?? 0;
  const sat = S.sats[room] ?? 100;
  const hrad = hue * Math.PI / 180;
  const hrr = sat / 100;
  const hx = (50 + 50 * hrr * Math.sin(hrad)).toFixed(2);
  const hy = (50 - 50 * hrr * Math.cos(hrad)).toFixed(2);
  const title = room === "all" ? "All rooms" : room;
  return `
  <div class="view-head">
    <button class="icon-btn" data-action="back">←</button>
    <div><h2>${esc(title)}</h2>
      <div class="sub">${devs.length} light${devs.length === 1 ? "" : "s"} · ${onCount} on</div></div>
  </div>
  <div class="hero${on ? "" : " off"}" style="--tint:${tint}">
    <div class="hero-swatch" style="background:${tint}"></div>
    <div><h3>${esc(title)}</h3>
      <p>${devs.length ? (on ? "All lights on" : onCount + " of " + devs.length + " on") : "No lights"}</p></div>
  </div>
  <div class="row" style="margin-top:10px">
    <span class="lbl">Power</span>
    ${switchHtml("power", null, on, true, room)}
  </div>
  <div class="panel">
    <div class="panel-head"><span class="t">Brightness</span>
      <span class="val" id="val-bri">${devs.length ? (devs[0].brightness ?? 0) + "%" : "0%"}</span></div>
    <input type="range" min="0" max="100" value="${devs.length ? (devs[0].brightness ?? 50) : 50}"
      data-el="bri" data-room="${esc(room)}">
  </div>
  <div class="panel">
    <div class="panel-head"><span class="t">White temperature</span>
      <span class="val" id="val-kelvin">${devs.length && devs[0].kelvin > 0 ? devs[0].kelvin + "K" : "—"}</span></div>
    <input type="range" min="2000" max="9000" step="100" value="${k}"
      class="kelvin-range" data-el="kelvin" data-room="${esc(room)}">
    <div class="kelvin-labels"><span>Warm 2000K</span><span>Cool 9000K</span></div>
  </div>
  <div class="panel">
    <div class="panel-head"><span class="t">Color</span>
      <span class="val" id="val-hue">${Math.round(hue)}° · ${sat}%</span></div>
    <div class="hue-wheel-wrap" data-room="${esc(room)}">
      <div class="hue-wheel"></div>
      <div class="wheel-center" style="background:hsl(${Math.round(hue)},${sat}%,50%)"></div>
      <div class="wheel-handle" style="left:${hx}%;top:${hy}%"></div>
    </div>
    <div class="swatches">${swatchBtns()}</div>
  </div>
  ${manualColorHtml({room})}
  <div class="panel">
    <div class="panel-head"><span class="t">Scenes</span><span class="hint val">for whole room</span></div>
    ${sceneChipsRoom(room)}
  </div>
  <div class="panel">
    <div class="panel-head"><span class="t">Lights in this room</span></div>
    <div class="dev-list">
      ${devs.map(d => `
        <div class="dl-row" style="cursor:pointer" data-action="open:${esc(d.ip)}">
          <span class="dot${d.online ? " on" : " off"}"></span>
          <span class="dl-name">${esc(d.name || d.ip)}</span>
          <span class="dl-ip">${esc(d.ip)}</span>
        </div>`).join("")}
    </div>
  </div>
  `;
}

function sceneCard(s){
  const preview = s.rgb ? css(s.rgb) : css(kelvinTint(s.kelvin));
  return `
  <div class="scene-card" data-action="scenehome:${s.id}">
    <div class="sc-top">
      <span class="sc-ico">${s.icon}</span><span class="sc-name">${s.name}</span>
      <span class="swatch" style="background:${preview}"></span>
    </div>
    <div class="sc-sub">${s.sub} · ${s.brightness}%${s.kelvin ? " · " + s.kelvin + "K" : ""}</div>
  </div>`;
}

function renderScenes(){
  const target = S.room === "all" ? "All rooms" : S.room;
  return `
  <div class="greet"><h1>Scenes</h1><p>One tap, whole ${esc(target)}</p></div>
  <div class="section-h">Apply to</div>
  <div class="chips">
    ${S.rooms.map(r =>
      `<button class="chip${S.room === r ? "" : ""}" data-action="scene-room:${esc(r)}">
        ${r === "all" ? "🏠 All rooms" : esc(r)}</button>`).join("")}
  </div>
  <div class="section-h">Scenes <span class="hint">target: ${esc(target)}</span></div>
  <div class="scene-grid">${SCENES.map(sceneCard).join("")}</div>
  `;
}

function renderSettings(){
  const d = (S.api && S.api.wake_defaults) || {};
  const rooms = S.rooms.filter(r => r !== "all");
  return `
  <div class="greet"><h1>Settings</h1><p>govee-lan-server · UDP 4003/4002</p></div>
  <div class="set-row">
    <div class="grow"><b>API key</b>
      <p>Sent as X-Api-Key on every change; leave empty if GOVEE_API_KEY is not set.</p></div>
    <input type="text" id="api-key" placeholder="(none)" value="${esc(KEY())}"
      style="width:120px" autocomplete="off" spellcheck="false">
  </div>
  <div class="set-row">
    <div class="grow"><b>Subnet scan</b><p>Probe for Govee lights answering on :4003.</p></div>
    <input type="text" id="subnet" placeholder="192.168.3.0/24"
      value="${esc(localStorage.getItem("goveeSubnet") || "192.168.3.0/24")}"
      style="width:130px" autocomplete="off" spellcheck="false">
    <button class="btn sm" data-action="scan">Scan</button>
  </div>
  <pre class="results" id="scan-results" style="display:none"></pre>
  <div class="set-row">
    <div class="grow"><b>Config</b><p>Re-read config.json (hot add/remove lights).</p></div>
    <button class="btn sm" data-action="reload">Reload</button>
  </div>
  <div class="panel">
    <div class="panel-head"><span class="t">Wake defaults</span></div>
    <div class="dev-list">
      ${`<div class="dl-row"><span class="dl-name">room</span><span class="dl-ip">${esc(d.room || "—")}</span></div>`}
      ${`<div class="dl-row"><span class="dl-name">minutes</span><span class="dl-ip">${d.minutes ?? "—"}</span></div>`}
      ${`<div class="dl-row"><span class="dl-name">kelvin ${d.kelvin_start ?? "—"} → ${d.kelvin ?? "—"}</span><span class="dl-ip"></span></div>`}
      ${`<div class="dl-row"><span class="dl-name">target brightness</span><span class="dl-ip">${d.target_brightness ?? "—"}</span></div>`}
    </div>
  </div>
  <div class="panel">
    <div class="panel-head"><span class="t">Devices</span>
      <span class="val">${S.devices.length} total</span></div>
    <div class="dev-list">
      ${S.devices.map(dev => `
        <div class="dl-row">
          <span class="dot${dev.online ? " on" : " off"}"></span>
          <span class="dl-name">${esc(dev.name || dev.ip)}</span>
          <span class="dl-ip">${esc(dev.ip)}</span>
        </div>`).join("")}
    </div>
  </div>
  <div class="panel">
    <div class="panel-head"><span class="t">Rooms</span></div>
    <div class="dev-list">
      ${rooms.map(r => `
        <div class="dl-row">
          <span class="dl-name">${esc(r)}</span>
          <span class="dl-ip">${(S.roomsMap[r] || []).length} lights</span>
        </div>`).join("")}
    </div>
  </div>
  `;
}

function setTabs(){
  const map = {home:"nav:home", scenes:"nav:scenes", settings:"nav:settings"};
  $$("#tabbar button").forEach(b =>
    b.classList.toggle("active", b.dataset.action === map[S.view]));
}

function render(){
  if (S.dragging) return;            // don't reset live sliders mid-drag
  const wk = wakeKey(), dk = devicesKey();
  const changed = dk !== S.lastKey || wk !== S.lastWakeKey || render._forced;
  render._forced = false;
  if (!changed && $("#view").dataset.view === S.view) return;
  S.lastKey = dk; S.lastWakeKey = wk;
  let html;
  if (S.view === "home") html = renderHome();
  else if (S.view === "scenes") html = renderScenes();
  else if (S.view === "settings") html = renderSettings();
  else if (S.view === "room") html = renderRoom(S.selectedRoom);
  else html = renderDetail(S.selectedIp);
  $("#view").innerHTML = html;
  $("#view").dataset.view = S.view;
  setTabs();
  if (S.view === "home" || S.view === "scenes") $("#view").scrollTop = 0;
}

/* ============================== actions ============================== */

async function send(path, body){
  await api(path, {method: "POST", body});
}

async function doScene(s, ip, room){
  const payload = {on: true, brightness: s.brightness};
  if (s.rgb) payload.rgb = s.rgb; else payload.kelvin = s.kelvin;
  const path = ip ? `/devices/${ip}` : `/rooms/${encodeURIComponent(room)}`;
  const target = ip ? null : (room === "all" ? "all rooms" : room);
  if (ip){
    const d = S.devices.find(x => x.ip === ip);
    if (d){
      d.on = true; d.brightness = s.brightness;
      d.kelvin = s.kelvin ?? d.kelvin;
      d.rgb = s.rgb ?? d.rgb;
    }
  }else{
    const ips = S.roomsMap[room] || [];
    S.devices.forEach(d => {
      if (ips.includes(d.ip)){
        d.on = true; d.brightness = s.brightness;
        d.kelvin = s.kelvin ?? d.kelvin;
        d.rgb = s.rgb ?? d.rgb;
      }
    });
  }
  await send(path, payload);
  toast(`${s.icon} ${s.name} → ${ip ? "light" : target}`, true);
  afterAction();
}

async function onControl(el, final = false){
  const elKind = el.dataset.el;
  const ip = el.dataset.ip;
  const room = el.dataset.room;
  let body;
  if (elKind === "power") body = {on: el.checked};
  else if (elKind === "bri") body = {brightness: +el.value};
  else if (elKind === "kelvin") body = {kelvin: +el.value};
  else if (elKind === "room") body = {on: el.checked};
  else return;

  const path = room
    ? `/rooms/${encodeURIComponent(room)}`
    : `/devices/${ip}`;

  if (elKind === "bri" || elKind === "kelvin"){
    // sliders: continuous updates while dragging (120ms throttle, same as
    // the wheel); the release (change) passes final=true so the exact last
    // value is always sent.
    const patch = elKind === "bri" ? {brightness: +el.value} : {kelvin: +el.value};
    patchTarget(room || ip, !!room, patch);
    ctrlSend(path, body, (room || ip) + ":" + elKind, final);
    return;
  }

  // power switches: discrete, one send per toggle
  try{
    await send(path, body);
  }catch(e){
    toast(e.message);
    render._forced = true; render();   // re-render restores the rejected control
    return;
  }
  afterAction();
}

function doNav(view){
  S.view = view;
  if (S.view === "home"){ S.selectedIp = null; S.selectedRoom = null; }
  render();
}

async function doWake(action){
  try{
    if (action === "start"){
      await api("/wake_up", {method: "POST", body: {}});
      toast("🌅 sunrise started", true);
    }else{
      await api("/wake_up", {method: "DELETE"});
      toast("sunrise cancelled");
    }
  }catch(e){ toast(e.message); }
  render._forced = true; render();
  afterAction();
}

async function doScan(){
  const subnet = ($("#subnet").value || "").trim() || "192.168.3.0/24";
  localStorage.setItem("goveeSubnet", subnet);
  const box = $("#scan-results");
  box.style.display = "block";
  box.textContent = `scanning ${subnet}…`;
  toast("scan started — takes a couple of seconds");
  try{
    const res = await api("/scan", {method: "POST", body: {subnet}});
    const ips = Object.keys(res);
    box.textContent = ips.length
      ? ips.map(ip => `${ip}  ${res[ip]}`).join("\n")
      : "no Govee devices answered";
  }catch(e){
    box.textContent = "scan failed: " + e.message;
  }
}

async function doReload(){
  try{
    const r = await api("/config/reload", {method: "POST", body: {quiet: true}});
    toast(`config reloaded: ${r.devices.length} devices`, true);
    await loadCore();
    S.room = "all";
    S.selectedIp = null;
    S.selectedRoom = null;
    render._forced = true; render();
    afterAction();
  }catch(e){ toast(e.message); }
}

/* ============================== events =============================== */

const phone = $("#phone");

function wheelPoint(wrap, x, y){
  const r = wrap.getBoundingClientRect();
  const dx = x - (r.left + r.width / 2);
  const dy = y - (r.top + r.height / 2);
  const hue = Math.round((Math.atan2(dx, -dy) * 180 / Math.PI + 360) % 360);
  const sat = Math.round(Math.min(1, Math.hypot(dx, dy) / (r.width / 2)) * 100);
  return {hue, sat};
}

const wheel = { _t: {} };

function wheelSet(wrap, x, y, final){
  const isRoom = !!wrap.dataset.room;
  const id = isRoom ? wrap.dataset.room : wrap.dataset.ip;
  const {hue, sat} = wheelPoint(wrap, x, y);
  // color value carries hue+saturation only, at full intensity (l=50);
  // luminance of the actual output is the separate brightness argument.
  const rgb = hslToRgb(hue, sat, 50);
  S.hues[id] = hue;
  S.sats[id] = sat;
  const handle = wrap.querySelector(".wheel-handle");
  if (handle){
    const rad = hue * Math.PI / 180;
    handle.style.left = (50 + 50 * sat / 100 * Math.sin(rad)).toFixed(2) + "%";
    handle.style.top = (50 - 50 * sat / 100 * Math.cos(rad)).toFixed(2) + "%";
  }
  const center = wrap.querySelector(".wheel-center");
  if (center) center.style.background = `hsl(${hue},${sat}%,50%)`;
  const v = $("#val-hue");
  if (v) v.textContent = `${hue}° · ${sat}%`;
  patchTarget(id, isRoom, {rgb, kelvin: 0});
  const now = Date.now();
  if (final || now - (wheel._t[id] || 0) > 120){   // throttle mid-drag sends
    wheel._t[id] = now;
    const path = isRoom
      ? `/rooms/${encodeURIComponent(id)}` : `/devices/${id}`;
    send(path, {rgb, kelvin: 0})
      .then(() => afterAction()).catch(err => toast(err.message));
  }
}

/* Continuous-send throttle for sliders (same 120ms cadence as the color wheel).
   final=true forces an immediate send (used on release so the exact final
   value always lands, throttled or not). */
const ctrl = { _t: {} };
function ctrlSend(path, body, id, final){
  const now = Date.now();
  if (final || now - (ctrl._t[id] || 0) > 120){
    ctrl._t[id] = now;
    send(path, body).then(() => afterAction()).catch(err => toast(err.message));
  }
}

phone.addEventListener("pointerdown", e => {
  const wrap = e.target.closest(".hue-wheel-wrap");
  if (!wrap) return;
  e.preventDefault();
  S.dragging = true;
  wheelSet(wrap, e.clientX, e.clientY, false);
  const move = ev => wheelSet(wrap, ev.clientX, ev.clientY, false);
  const up = ev => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    window.removeEventListener("pointercancel", up);
    S.dragging = false;
    wheelSet(wrap, ev.clientX, ev.clientY, true);   // exact final color
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
  window.addEventListener("pointercancel", up);
}, {passive: false});

phone.addEventListener("click", e => {
  const el = e.target.closest("[data-action]");
  if (!el) return;
  if (e.target.closest(".switch")) return;   // let the switch handle itself
  const [act, arg] = el.dataset.action.split(":");

  if (act === "nav"){ doNav(arg); return; }
  if (act === "back"){ S.view = "home"; render(); return; }
  if (act === "refresh"){ poll(); return; }
  if (act === "open"){ S.selectedIp = arg; S.view = "detail"; render(); return; }
  if (act === "room"){
    S.selectedRoom = arg; S.view = "room"; render._forced = true; render(); return;
  }
  if (act === "swatch"){
    const [r, g, b] = arg.split(",").map(Number);
    const { id, isRoom } = panelTarget();
    applyPanelColor(id, isRoom, r, g, b);
    return;
  }
  if (act === "manualhsl"){
    const { id, isRoom } = panelTarget();
    const row = el.closest(".manual-row");
    const h = clampInt(row.querySelector('[data-mk="h"]').value, 0, 360);
    const s = clampInt(row.querySelector('[data-mk="s"]').value, 0, 100);
    const l = clampInt(row.querySelector('[data-mk="l"]').value, 0, 100);
    const rgb = hslToRgb(h, s, 50);
    // L% maps to brightness (luminance), kept separate from the color value.
    patchTarget(id, isRoom, {rgb, kelvin: 0, brightness: l});
    const path = isRoom ? `/rooms/${encodeURIComponent(id)}` : `/devices/${id}`;
    S.hues[id] = h; S.sats[id] = s;
    const wrap = panelTarget().wrap;
    if (wrap){
      const rad = h * Math.PI / 180;
      const hh = wrap.querySelector(".wheel-handle");
      const cc = wrap.querySelector(".wheel-center");
      if (hh){ hh.style.left = (50 + 50*s/100*Math.sin(rad)).toFixed(2)+"%";
               hh.style.top = (50 - 50*s/100*Math.cos(rad)).toFixed(2)+"%"; }
      if (cc) cc.style.background = `hsl(${h},${s}%,50%)`;
      const v = $("#val-hue"); if (v) v.textContent = `${h}° · ${s}%`;
      const vb = $("#val-bri"); if (vb) vb.textContent = l + "%";
    }
    send(path, {rgb, kelvin: 0, brightness: l})
      .then(() => afterAction()).catch(err => toast(err.message));
    return;
  }
  if (act === "manualrgb"){
    const { id, isRoom } = panelTarget();
    const row = el.closest(".manual-row");
    const r = clampInt(row.querySelector('[data-mk="r"]').value, 0, 255);
    const g = clampInt(row.querySelector('[data-mk="g"]').value, 0, 255);
    const b = clampInt(row.querySelector('[data-mk="b"]').value, 0, 255);
    applyPanelColor(id, isRoom, r, g, b);
    return;
  }
  if (act === "palette"){
    // action = palette:<sub>:<ip>:<index>
    const parts = el.dataset.action.split(":");
    const sub = parts[1];
    const ip = parts[2];
    const i = parts[3];
    if (sub === "save"){
      const pal = loadPalette(ip);
      // save the panel's current hue/saturation color at l=50
      const rgb = hslToRgb(S.hues[ip] ?? 0, S.sats[ip] ?? 100, 50);
      pal.push(rgb);
      savePalette(ip, pal);
      toast("Saved to palette", true);
      render._forced = true; render();
    }else if (sub === "del"){
      const pal = loadPalette(ip);
      pal.splice(+i, 1);
      savePalette(ip, pal);
      render._forced = true; render();
    }else if (sub === "use"){
      const pal = loadPalette(ip);
      const rgb = pal[+i];
      if (!rgb) return;
      applyPanelColor(ip, false, rgb[0], rgb[1], rgb[2]);
    }
    return;
  }
  if (act === "scene"){
    const s = SCENES.find(x => x.id === arg);
    doScene(s, el.dataset.ip, null).catch(err => toast(err.message));
    return;
  }
  if (act === "sceneroom"){
    const s = SCENES.find(x => x.id === arg);
    doScene(s, null, el.dataset.room).catch(err => toast(err.message));
    return;
  }
  if (act === "scenehome"){
    const s = SCENES.find(x => x.id === arg);
    doScene(s, null, S.room).catch(err => toast(err.message));
    return;
  }
  if (act === "scene-room"){
    S.room = arg; render._forced = true; render(); return;
  }
  if (act === "wake"){ doWake(arg); return; }
  if (act === "scan"){ doScan(); return; }
  if (act === "reload"){ doReload(); return; }
});

phone.addEventListener("change", e => {
  const el = e.target;
  if (el.dataset && el.dataset.el && ["power","bri","kelvin","room"].includes(el.dataset.el)){
    S.dragging = false;
    onControl(el, true).catch(() => {});   // release: guaranteed exact final send
  }
  if (el.id === "api-key"){
    localStorage.setItem("goveeApiKey", el.value.trim());
    toast("API key saved", true);
  }
});

phone.addEventListener("input", e => {
  const el = e.target;
  if (el.type !== "range") return;
  S.dragging = true;
  if (el.dataset.el === "bri") $("#val-bri").textContent = el.value + "%";
  if (el.dataset.el === "kelvin") $("#val-kelvin").textContent = el.value + "K";
  if (el.dataset.el === "bri" || el.dataset.el === "kelvin"){
    // continuous updates while dragging (throttled to ~120ms, same as the wheel)
    onControl(el).catch(() => {});
  }
});

window.addEventListener("focus", poll);
window.addEventListener("online", poll);

/* ============================== boot ================================ */

(async function boot(){
  try{
    await loadCore();
  }catch(e){
    $("#view").innerHTML =
      `<div class="empty">Cannot reach the govee server.<br>${esc(e.message)}</div>`;
    return;
  }
  render._forced = true;
  render();
  poll();
  setInterval(poll, 5000);
})();
