// Smoke Outlook map. Everything simulated is labelled; the what-if is a separate layer.
/* global maplibregl, deck */

const C = { IN: "#3987e5", PK: "#d95926", disputed: "#199e70", other: "#6b6a64", whatif: "#9085e9" };
const NAMES = { IN: "India", PK: "Pakistan", disputed: "Disputed area", other: "Other" };
const ORDER = ["IN", "PK", "disputed", "other"]; // fixed order, never by rank
const BLUE = ["#0d366b", "#184f95", "#256abf", "#3987e5", "#6da7ec", "#9ec5f4", "#cde2fb"];
const ORANGE = ["#5a2410", "#8a3716", "#b9491d", "#d95926", "#e98a63", "#f2b29a", "#f8d3c2"];
const $ = (id) => document.getElementById(id);
const api = (p) => fetch(`/api/${p}`).then((r) => (r.ok ? r : Promise.reject(new Error(`${p}: ${r.status}`))));
const json = (p) => api(p).then((r) => r.json());
const bin = (p) => api(p).then((r) => r.arrayBuffer());
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const pct = (x) => `${Math.round(x * 100)}%`;
const hex = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));

function ramp(stops, t) {
  const x = Math.min(0.9999, Math.max(0, t)) * (stops.length - 1);
  const i = Math.floor(x), f = x - i, a = hex(stops[i]), b = hex(stops[i + 1]);
  return a.map((v, k) => Math.round(v + (b[k] - v) * f));
}

const S = {
  meta: null, frame: 0, playing: false, show: { particles: true, field: true, whatif: false, fires: true, wind: false, pbl: false, stations: true },
  field: null, whatif: null, tracks: null, fires: null, stations: null, wind: {}, pbl: null,
  windLevel: "10m", windParts: null, footprint: null, selected: null,
};
let map, overlay;

// ---------- time helpers ----------
const tMs = (i) => Date.parse(S.meta.times[Math.max(0, Math.min(S.meta.times.length - 1, i))]);
function clock() {
  const i = Math.round(S.frame), t = new Date(tMs(i)), now = Date.parse(S.meta.started_at);
  const f = (zone) => t.toLocaleString("en-GB", { timeZone: zone, weekday: "short", hour: "2-digit", minute: "2-digit" });
  $("t-utc").textContent = `${f("UTC")} UTC`;
  $("t-local").textContent = `${f("Asia/Kolkata")} IST · ${f("Asia/Karachi")} PKT`;
  const h = Math.round((t - now) / 36e5);
  $("t-rel").textContent = h === 0 ? "run time" : h < 0 ? `${-h} h before run` : `forecast +${h} h`;
  $("slider").value = i;
}

// ---------- raster layers drawn on canvases (MapLibre canvas sources) ----------
function makeCanvas(id, nx, ny, bounds) {
  const cv = document.createElement("canvas");
  cv.width = nx; cv.height = ny; cv.id = `cv-${id}`; cv.style.display = "none";
  document.body.appendChild(cv);
  const [w, s, e, n] = bounds;
  map.addSource(id, { type: "canvas", canvas: cv.id, animate: true, coordinates: [[w, n], [e, n], [e, s], [w, s]] });
  map.addLayer({ id, type: "raster", source: id, paint: { "raster-resampling": "linear", "raster-fade-duration": 0 } }, "regions-line");
  return cv.getContext("2d");
}
function paint(ctx, nx, ny, bytes, offset, lut) {
  const img = ctx.createImageData(nx, ny), d = img.data;
  for (let y = 0; y < ny; y++) {
    const src = offset + (ny - 1 - y) * nx; // arrays are south-to-north, images top-down
    for (let x = 0; x < nx; x++) {
      const v = bytes[src + x], o = (y * nx + x) * 4, k = v * 4;
      d[o] = lut[k]; d[o + 1] = lut[k + 1]; d[o + 2] = lut[k + 2]; d[o + 3] = lut[k + 3];
    }
  }
  ctx.putImageData(img, 0, 0);
}
function lutFrom(stops, alpha) {
  const lut = new Uint8ClampedArray(1024);
  for (let v = 0; v < 256; v++) {
    const c = ramp(stops, v / 255);
    lut.set([c[0], c[1], c[2], alpha(v / 255)], v * 4);
  }
  return lut;
}
const LUT = {
  smoke: lutFrom(BLUE, (t) => (t < 0.03 ? 0 : 60 + 170 * t)),
  whatif: lutFrom(["#2c2560", "#5a4fc0", "#9085e9", "#cfc9f7"], (t) => (t < 0.03 ? 0 : 60 + 170 * t)),
  foot: lutFrom(["#3a3a37", "#c98500", "#ffe9a8"], (t) => (t < 0.04 ? 0 : 50 + 180 * t)),
  // trapped air: the lower the mixing layer, the stronger the grey shading
  pbl: (() => { const l = new Uint8ClampedArray(1024); for (let v = 0; v < 256; v++) l.set([150, 150, 142, Math.max(0, 110 - v * 3.7)], v * 4); return l; })(),
};
let ctxField, ctxPbl, ctxFoot, lastField = -1, lastPbl = -1, lastVis = "";

function drawRasters(force) {
  const g = S.meta.grid, i = Math.round(S.frame);
  const which = S.show.whatif ? S.whatif : S.field;
  if (which && (force || i !== lastField)) {
    paint(ctxField, g.nx, g.ny, which, i * g.nx * g.ny, S.show.whatif ? LUT.whatif : LUT.smoke);
    lastField = i;
  }
  const w = S.meta.wind, wi = windIndex();
  if (S.pbl && S.show.pbl && (force || wi !== lastPbl)) {
    paint(ctxPbl, w.nx, w.ny, S.pbl, wi * w.nx * w.ny, LUT.pbl);
    lastPbl = wi;
  }
  const vis = `${S.show.field || S.show.whatif}${S.show.pbl}${!!S.footprint}`;
  if (vis !== lastVis) {
    map.setLayoutProperty("field", "visibility", S.show.field || S.show.whatif ? "visible" : "none");
    map.setLayoutProperty("pbl", "visibility", S.show.pbl ? "visible" : "none");
    map.setLayoutProperty("foot", "visibility", S.footprint ? "visible" : "none");
    lastVis = vis;
  }
}
const windIndex = () => {
  const h = (tMs(Math.round(S.frame)) - Date.parse(S.meta.wind.init)) / 36e5;
  return Math.max(0, Math.min(S.meta.wind.hours - 1, Math.round(h)));
};

// ---------- deck.gl layers ----------
function particleLayers() {
  const tr = S.tracks, T = S.meta.times.length, n = S.meta.tracks.n;
  if (!tr || !n) return [];
  const b = S.meta.tracks, f0 = Math.floor(S.frame), f1 = Math.min(T - 1, f0 + 1), a = S.frame - f0;
  const lon = new Uint16Array(tr, 0, T * n), lat = new Uint16Array(tr, T * n * 2, T * n);
  const state = new Uint8Array(tr, T * n * 4, T * n), assumed = new Uint8Array(tr, T * n * 5, n);
  const out = { low: [], high: [], what: [] };
  const dx = (b.east - b.west) / 65535, dy = (b.north - b.south) / 65535;
  for (let p = 0; p < n; p++) {
    const s0 = state[f0 * n + p], s1 = state[f1 * n + p];
    if (!s0) continue;
    if (assumed[p] && !S.show.whatif) continue;
    const k0 = f0 * n + p, k1 = s1 ? f1 * n + p : k0;
    const x = b.west + (lon[k0] + (lon[k1] - lon[k0]) * a) * dx;
    const y = b.south + (lat[k0] + (lat[k1] - lat[k0]) * a) * dy;
    (assumed[p] ? out.what : s0 === 1 ? out.low : out.high).push(x, y);
  }
  const mk = (id, arr, color, r) => new deck.ScatterplotLayer({
    id, data: { length: arr.length / 2, attributes: { getPosition: { value: new Float32Array(arr), size: 2 } } },
    getFillColor: color, getRadius: r, radiusUnits: "pixels", stroked: false, pickable: false,
  });
  return [mk("p-high", out.high, [205, 226, 251, 70], 1.6), mk("p-low", out.low, [205, 226, 251, 200], 2.2),
    mk("p-what", out.what, [144, 133, 233, 190], 2.2)];
}

function fireLayer() {
  const f = S.fires;
  if (!f) return [];
  S.fireRows ??= f.id.map((id, i) => ({ id, lon: f.lon[i], lat: f.lat[i], frp: f.frp_mw[i], events: f.events[i], country: f.country[i], region: f.region[i] }));
  const rows = S.fireRows;
  return [new deck.ScatterplotLayer({
    id: "fires", data: rows, getPosition: (d) => [d.lon, d.lat], getRadius: (d) => 2 + Math.sqrt(d.frp) * 0.35,
    radiusUnits: "pixels", radiusMaxPixels: 13, getFillColor: (d) => (S.selected === d.id ? [255, 233, 168, 255] : [201, 133, 0, 150]),
    stroked: false, pickable: true,
    updateTriggers: { getFillColor: S.selected },
    onClick: ({ object }) => object && showCluster(object.id),
    onHover: ({ object, x, y }) => tip(object && `Fire cluster, ${esc(object.region ?? "")} (${esc(NAMES[object.country] ?? object.country)})<br>${object.frp} MW · ${object.events} fires seen`, x, y),
  })];
}

function stationLayer() {
  const s = S.stations;
  if (!s) return [];
  S.stationRows ??= s.lon.map((lon, i) => ({ lon, lat: s.lat[i], pm: s.pm25[i], stale: s.stale[i], ref: s.reference[i], name: s.name[i], age: s.age_h[i] }));
  const rows = S.stationRows;
  return [new deck.ScatterplotLayer({
    id: "stations", data: rows, getPosition: (d) => [d.lon, d.lat], radiusUnits: "pixels",
    getRadius: (d) => (d.ref ? 5 : 3.2), stroked: true, lineWidthMinPixels: 1,
    getFillColor: (d) => (d.stale ? [0, 0, 0, 0] : [...ramp(ORANGE, d.pm / 300), 235]),
    getLineColor: (d) => (d.stale ? [143, 142, 134, 255] : d.ref ? [240, 239, 236, 255] : [26, 26, 25, 255]),
    pickable: true,
    onHover: ({ object: d, x, y }) => tip(d && `${esc(d.name)}<br>${d.stale ? `No recent reading (last ${Math.round(d.age)} h ago)` : `Measured PM2.5 ${d.pm} µg/m³ · ${d.age} h ago`}<br>${d.ref ? "Reference monitor" : "Low-cost sensor (less accurate)"}`, x, y),
  })];
}

function cityLayer() {
  return [new deck.ScatterplotLayer({
    id: "cities", data: S.meta.receptors, getPosition: (d) => [d.lon, d.lat], radiusUnits: "pixels", getRadius: 9,
    getFillColor: [0, 0, 0, 0], stroked: true, getLineColor: [240, 239, 236, 255], lineWidthMinPixels: 2, pickable: true,
    onClick: ({ object }) => object && showCity(object.name),
    onHover: ({ object, x, y }) => tip(object && `${esc(object.name)}${object.skill ? ` — ${SKILL_TEXT[object.skill.verdict] ?? esc(object.skill.verdict)}` : ""} — click for outlook`, x, y),
  })];
}

// Wind: particles drift with the wind field of the slider hour and leave short streaks.
function windLayer(dtMs) {
  const buf = S.wind[S.windLevel];
  if (!S.show.wind || !buf) return [];
  const w = S.meta.wind, n = 1300, size = w.nx * w.ny, off = windIndex() * size;
  const u = new Int8Array(buf, 0, w.hours * size), v = new Int8Array(buf, w.hours * size, w.hours * size);
  if (!S.windParts) {
    S.windParts = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) S.windParts.set([w.west + Math.random() * (w.east - w.west), w.south + Math.random() * (w.north - w.south), Math.random() * 80], i * 3);
  }
  const P = S.windParts, lines = new Array(n);
  const step = 0.00022 * Math.min(dtMs, 50); // degrees per m/s per frame: exaggerated so motion is visible
  for (let i = 0; i < n; i++) {
    let x = P[i * 3], y = P[i * 3 + 1];
    const ix = Math.min(w.nx - 1, Math.max(0, Math.round(((x - w.west) / (w.east - w.west)) * (w.nx - 1))));
    const iy = Math.min(w.ny - 1, Math.max(0, Math.round(((y - w.south) / (w.north - w.south)) * (w.ny - 1))));
    const k = off + iy * w.nx + ix, uu = u[k] / w.scale, vv = v[k] / w.scale;
    const nx2 = x + (uu * step) / Math.cos((y * Math.PI) / 180), ny2 = y + vv * step;
    lines[i] = { s: [x, y], t: [x + (nx2 - x) * 7, y + (ny2 - y) * 7] };
    x = nx2; y = ny2; P[i * 3 + 2] -= 1;
    if (P[i * 3 + 2] < 0 || x < w.west || x > w.east || y < w.south || y > w.north) {
      x = w.west + Math.random() * (w.east - w.west); y = w.south + Math.random() * (w.north - w.south); P[i * 3 + 2] = 40 + Math.random() * 60;
    }
    P[i * 3] = x; P[i * 3 + 1] = y;
  }
  return [new deck.PathLayer({
    id: "wind", data: lines, getPath: (d) => [d.s, d.t], getColor: [240, 239, 236, 160],
    getWidth: 1.4, widthUnits: "pixels", capRounded: true,
  })];
}

let lastTick = performance.now();
function render() {
  const now = performance.now(), dt = now - lastTick;
  lastTick = now;
  if (S.playing) {
    S.frame += dt / 450; // one hour of simulation every 0.45 s
    if (S.frame >= S.meta.times.length - 1) S.frame = 0;
    clock();
  }
  drawRasters(false);
  overlay.setProps({ layers: [
    ...(S.show.fires ? fireLayer() : []), ...(S.show.particles ? particleLayers() : []), ...windLayer(dt),
    ...(S.show.stations ? stationLayer() : []), ...cityLayer(),
  ] });
  if (S.playing || S.show.wind) requestAnimationFrame(render);
}
const redraw = () => { if (!(S.playing || S.show.wind)) requestAnimationFrame(render); };

function tip(html, x, y) {
  const t = $("tip");
  if (!html) { t.style.display = "none"; return; }
  t.innerHTML = html; t.style.display = "block";
  t.style.left = `${Math.min(x + 12, innerWidth - 250)}px`; t.style.top = `${y + 12}px`;
}

// ---------- panels ----------
function tab(name) {
  for (const b of document.querySelectorAll("[role=tab]")) b.setAttribute("aria-selected", b.dataset.tab === name);
  for (const n of ["target", "ledger", "detail"]) $(`tab-${n}`).hidden = n !== name;
  $("side").classList.add("open");
}
const key = (c) => `<i class="key" style="background:${C[c] ?? C.other}"></i>`;
const place = (r) => `${key(r.country)}${esc(r.region ?? "—")}, ${esc(NAMES[r.country] ?? r.country)}`;
// Skill from the 2024 and 2025 hindcasts (docs/validation.md). No skill data = not yet validated.
const SKILL_TEXT = { "useful": "smoke arrival: useful in hindcasts", "not reliable here": "smoke arrival: not reliable here" };
const skillBadge = (k) => !k ? "" : `<span class="badge ${k.verdict === "useful" ? "ok" : "warn"}">${SKILL_TEXT[k.verdict] ?? esc(k.verdict)}</span>`;
const num2 = (x) => (x == null ? "n/a" : x.toFixed(2));
const skillTable = (k) => !k ? "" : `<h2>How well it did in past seasons</h2>
    <table><thead><tr><th>Season</th><th class="num">Days</th><th class="num">Rise match</th><th class="num">Spike score</th><th class="num">CAMS</th><th>Result</th></tr></thead><tbody>
    ${k.seasons.map((q) => `<tr><td>${q.season}</td><td class="num">${q.days ?? ""}</td><td class="num">${num2(q.change_corr)}</td><td class="num">${num2(q.spike_auc)}</td><td class="num">${num2(q.cams_spike_auc)}</td><td>${esc(q.verdict)}${(q.reference_share ?? 1) < 0.5 ? " (low-cost sensors)" : ""}</td></tr>`).join("")}</tbody></table>
    <p class="fine">One run per day, 1 Oct – 30 Nov, using only data published before each run. Rise match: rank correlation of simulated and measured day-to-day change (1 = perfect, 0 = none). Spike score: how well a simulated rise picks out the biggest measured rises (1 = perfect, 0.5 = coin toss); CAMS = the same score for the Copernicus forecast of total PM2.5. Stations measure all pollution; the model has fire smoke only. Amounts are not validated.</p>`;
const range = (lo, hi) => `<span class="fine">${pct(lo)}–${pct(hi)}</span>`;

async function loadTargeting() {
  const t = await json(`targeting/${S.meta.run_id}`);
  $("tab-target").innerHTML = `<h3>Fires that matter most, next ${t.hours} h</h3>
    <p class="fine"><span class="badge">${esc(t.label)}</span> ${esc(t.note)}</p>
    <table><thead><tr><th>#</th><th>Fire cluster</th><th class="num">Power</th><th class="num">Share (range)</th><th>Most affected</th></tr></thead><tbody>
    ${t.rows.map((r) => `<tr class="click" data-id="${r.id}" data-lon="${r.lon}" data-lat="${r.lat}"><td>${r.rank}</td><td>${place(r)}</td>
      <td class="num">${Math.round(r.frp_mw)} MW</td><td class="num">${pct(r.share)} ${range(r.share_min, r.share_max)}</td><td>${esc(r.most_affected)}</td></tr>`).join("")}
    </tbody></table>`;
  for (const tr of $("tab-target").querySelectorAll("tr.click")) {
    tr.onclick = () => { map.flyTo({ center: [+tr.dataset.lon, +tr.dataset.lat], zoom: 7.5 }); showCluster(+tr.dataset.id); };
  }
}

async function loadLedger() {
  const L = await json(`ledger/${S.meta.run_id}`);
  const cities = [...new Set(L.rows.map((r) => r.receptor))];
  const home = Object.fromEntries(S.meta.receptors.map((r) => [r.name, r.name === "Lahore" ? "PK" : "IN"]));
  const block = (city) => {
    const days = [...new Set(L.rows.filter((r) => r.receptor === city).map((r) => r.day))];
    const low = L.rows.find((r) => r.receptor === city)?.low_confidence;
    const rev = L.revision_points?.[city];
    const sk = S.meta.receptors.find((r) => r.name === city)?.skill;
    return `<div class="city-row"><b>${esc(city)}</b>${low ? '<span class="badge warn">low confidence</span>' : ""}${skillBadge(sk)}</div>${rev ? `<div class="fine">Shares typically moved by ±${Math.round(rev.median)} points (up to ±${Math.round(rev.p90)} on 9 days in 10) between a day-ahead forecast and the same-day one.</div>` : ""}` + days.map((d) => {
      const rows = ORDER.map((c) => L.rows.find((r) => r.receptor === city && r.day === d && r.source === c)).filter(Boolean);
      return `<div class="fine">${d} (UTC day)</div><div class="stack" role="img" aria-label="${rows.map((r) => `${NAMES[r.source]} ${pct(r.share)}`).join(", ")}">
        ${rows.map((r) => `<span style="flex:${Math.max(r.share, 0.004)};background:${C[r.source]}" title="${NAMES[r.source]} ${pct(r.share)}"></span>`).join("")}</div>
        <div class="shares">${rows.map((r) => `<span class="nw">${key(r.source)}${NAMES[r.source]} <b>${pct(r.share)}</b> ${range(r.share_min, r.share_max)}</span>`).join(" &nbsp; ")}</div>`;
    }).join("");
  };
  const group = (cc) => cities.filter((c) => home[c] === cc).map(block).join("");
  $("tab-ledger").innerHTML = `<h3>Where each city's fire smoke comes from</h3>
    <p class="fine"><span class="badge">${esc(L.label)}</span> ${esc(L.note)} ${L.revision_points && Object.keys(L.revision_points).length ? esc(L.revision_note) : ""}</p>
    <h2>Cities in India</h2>${group("IN")}<h2>Cities in Pakistan</h2>${group("PK")}
    <details><summary class="fine">Table view</summary><table><thead><tr><th>City</th><th>Day</th><th>Source</th><th class="num">Share</th><th class="num">Range</th></tr></thead><tbody>
    ${L.rows.map((r) => `<tr><td>${esc(r.receptor)}</td><td>${r.day}</td><td>${NAMES[r.source] ?? esc(r.source)}</td><td class="num">${pct(r.share)}</td><td class="num">${pct(r.share_min)}–${pct(r.share_max)}</td></tr>`).join("")}
    </tbody></table></details>`;
}

function chart(d) {
  const W = 360, H = 190, m = { l: 34, r: 8, t: 10, b: 22 }, n = d.times.length;
  const top = Math.max(0.5, ...d.high, ...d.whatif) * 1.08;
  const x = (i) => m.l + (i / (n - 1)) * (W - m.l - m.r), y = (v) => H - m.b - (v / top) * (H - m.t - m.b);
  const line = (a) => a.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const band = `${line(d.high)}${d.low.map((v, i) => `L${x(n - 1 - i).toFixed(1)},${y(d.low[n - 1 - i]).toFixed(1)}`).join("")}Z`;
  const nowI = d.times.findIndex((t) => Date.parse(t) >= Date.parse(d.started_at));
  const ticks = [0, top / 2, top].map((v) => `<line x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="#3a3a37"/><text x="${m.l - 4}" y="${y(v) + 3}" text-anchor="end">${v.toFixed(v < 10 ? 1 : 0)}</text>`).join("");
  const days = d.times.map((t, i) => [t, i]).filter(([t]) => t.slice(11, 13) === "00").map(([t, i]) => `<text x="${x(i)}" y="${H - 6}" text-anchor="middle">${t.slice(8, 10)} ${new Date(t).toLocaleString("en-GB", { month: "short", timeZone: "UTC" })}</text>`).join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Simulated fire smoke at ${esc(d.name)} by hour, µg/m³">
    ${ticks}${days}
    <path d="${band}" fill="#3987e5" opacity=".22"/>
    <path d="${line(d.whatif)}" fill="none" stroke="${C.whatif}" stroke-width="2" stroke-dasharray="5 4"/>
    <path d="${line(d.pm25)}" fill="none" stroke="#3987e5" stroke-width="2"/>
    <line x1="${x(nowI)}" x2="${x(nowI)}" y1="${m.t}" y2="${H - m.b}" stroke="#c3c2b7" stroke-dasharray="2 3"/><text x="${x(nowI) + 3}" y="${m.t + 8}">run time</text>
    <line id="cross" y1="${m.t}" y2="${H - m.b}" stroke="#f0efec" opacity="0"/>
    <rect id="hit" x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}" fill="transparent"/></svg>`;
}

async function showCity(name) {
  tab("detail");
  const el = $("tab-detail");
  el.innerHTML = `<p class="fine">Loading ${esc(name)}…</p>`;
  const d = await json(`receptor/${S.meta.run_id}/${name}`);
  const cams = d.cams.times.length ? d.cams.pm25[d.cams.times.findIndex((t) => Date.parse(t) >= Date.now())] : null;
  el.innerHTML = `<h3>${esc(name)} — fire smoke outlook</h3>
    <p><span class="badge">${esc(d.label)}</span> ${skillBadge(d.skill)} ${d.low_confidence ? '<span class="badge warn">low confidence: the two calculation methods disagree here</span>' : ""}</p>
    <dl class="kv">
      <dt>Measured now</dt><dd>${d.measured_now ? `<b>${d.measured_now.pm25} µg/m³</b> PM2.5, ${d.measured_now.stations} ${d.measured_now.reference ? "reference" : "low-cost"} stations (${esc(d.measured_now.note)})` : "no recent reading"}</dd>
      <dt>Model check</dt><dd>${d.self_check ?? "–"} (1.0 = the two methods agree)</dd>
      ${cams != null ? `<dt>CAMS now</dt><dd>${cams} µg/m³ total PM2.5 (Copernicus model, all sources)</dd>` : ""}
    </dl>
    <div class="fine">Simulated fire smoke only, µg/m³ (UTC). Absolute values are known to be too low.</div>
    ${chart(d)}
    <div class="shares">${`<i class="key" style="background:#3987e5"></i>Fires already seen (band = low–high range) &nbsp; <i class="key" style="background:${C.whatif}"></i>What-if: fires continue (dashed)`}</div>
    <p class="fine">${esc(d.whatif_label)}</p>
    <button id="foot-btn">Show where its air comes from</button>
    ${skillTable(d.skill)}
    <h2>Largest sources, next 72 h (fires already seen)</h2>
    <table><tbody>${d.top_clusters.map((c) => `<tr class="click" data-id="${c.id}" data-lon="${c.lon}" data-lat="${c.lat}"><td>${place(c)}</td><td class="num">${pct(c.share)}</td></tr>`).join("")}</tbody></table>
    <details><summary class="fine">Table view</summary><table><thead><tr><th>UTC</th><th class="num">Seen fires</th><th class="num">Low</th><th class="num">High</th><th class="num">What-if</th></tr></thead><tbody>
    ${d.times.map((t, i) => (i % 3 ? "" : `<tr><td>${t.slice(5, 16).replace("T", " ")}</td><td class="num">${d.pm25[i].toFixed(2)}</td><td class="num">${d.low[i].toFixed(2)}</td><td class="num">${d.high[i].toFixed(2)}</td><td class="num">${d.whatif[i].toFixed(2)}</td></tr>`)).join("")}</tbody></table></details>`;
  const svg = el.querySelector("svg"), hit = el.querySelector("#hit"), cross = el.querySelector("#cross");
  hit.onmousemove = (e) => {
    const r = hit.getBoundingClientRect(), i = Math.round(((e.clientX - r.left) / r.width) * (d.times.length - 1));
    const px = 34 + (i / (d.times.length - 1)) * (360 - 42);
    cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("opacity", ".6");
    tip(`${d.times[i].slice(5, 16).replace("T", " ")} UTC<br>Seen fires <b>${d.pm25[i].toFixed(2)}</b> (${d.low[i].toFixed(2)}–${d.high[i].toFixed(2)})<br>What-if <b>${d.whatif[i].toFixed(2)}</b> µg/m³`, e.clientX, e.clientY);
  };
  svg.onmouseleave = () => { cross.setAttribute("opacity", "0"); tip(null); };
  el.querySelector("#foot-btn").onclick = async () => {
    const g = S.meta.grid;
    S.footprint = new Uint8Array(await bin(`footprint/${S.meta.run_id}/${name}.bin`));
    paint(ctxFoot, g.nx, g.ny, S.footprint, 0, LUT.foot);
    redraw();
  };
  for (const tr of el.querySelectorAll("tr.click")) tr.onclick = () => { map.flyTo({ center: [+tr.dataset.lon, +tr.dataset.lat], zoom: 7.5 }); showCluster(+tr.dataset.id); };
}

async function showCluster(id) {
  S.selected = id; redraw(); tab("detail");
  const c = await json(`cluster/${S.meta.run_id}/${id}`);
  $("tab-detail").innerHTML = `<h3>Fire cluster — ${esc(c.region ?? "")}, ${esc(NAMES[c.country] ?? c.country)}</h3>
    <p><span class="badge">${esc(c.label)}</span></p>
    <dl class="kv"><dt>Fires seen</dt><dd>${c.events} (last ${c.last_seen.slice(5, 16).replace("T", " ")} UTC)</dd>
      <dt>Fire power</dt><dd>${c.frp_mw} MW</dd>
      <dt>PM2.5 emitted</dt><dd>${c.emission_t.central} t (range ${c.emission_t.low}–${c.emission_t.high} t)</dd>
      <dt>People reached</dt><dd><b>${(c.people_reached / 1e6).toFixed(1)} million</b></dd></dl>
    <p class="fine">${esc(c.people_note)}</p>
    <h2>Where its smoke goes</h2>
    ${c.cities.length ? `<table><thead><tr><th>City</th><th class="num">Share of city's fire smoke</th><th class="num">Peak</th><th>Peak time (UTC)</th></tr></thead><tbody>
      ${c.cities.map((x) => `<tr><td>${esc(x.receptor)}</td><td class="num">${(x.share_of_city_fire_smoke * 100).toFixed(1)}%</td><td class="num">${x.peak_ugm3} µg/m³</td><td>${x.peak_at.slice(5, 16).replace("T", " ")}</td></tr>`).join("")}</tbody></table>`
      : '<p class="fine">Its smoke does not reach any tracked city in this run.</p>'}`;
}

// ---------- start ----------
async function start() {
  S.meta = await json("meta");
  const m = S.meta, g = m.grid;
  $("label").textContent = m.label;
  $("slider").max = m.times.length - 1;
  S.frame = Math.max(0, m.times.findIndex((t) => Date.parse(t) >= Date.parse(m.started_at)));
  $("fresh").innerHTML = m.freshness.map((f) => `<li class="${f.stale ? "stale" : ""}"><span>${esc(f.source)}</span><span>${f.age_h == null ? "none" : `${f.age_h} h ago`}${f.stale ? " ⚠ stale" : ""}</span></li>`).join("");
  $("runinfo").textContent = `Run ${m.run_id}, weather ${m.gfs_init.slice(0, 13)}Z, ${m.members.length} forecast runs, ${m.n_events} fires in ${m.n_clusters} clusters. ${m.unofficial}`;
  const inv = (b) => Math.expm1((b / 255) * Math.log1p(m.field.max));
  $("legend").innerHTML = `<div class="bar ramp-blue"></div><div class="row"><span>${inv(20).toFixed(1)}</span><span>${inv(128).toFixed(0)}</span><span>${inv(255).toFixed(0)} µg/m³ fire smoke</span></div>
    <div class="bar ramp-orange"></div><div class="row"><span>0</span><span>150</span><span>300+ µg/m³ measured</span></div>
    <div class="fine">Hollow grey ring = no recent reading. White ring = reference monitor.</div>`;

  map = new maplibregl.Map({
    container: "map", center: [75.3, 30.2], zoom: 5.2, minZoom: 2, maxZoom: 10, attributionControl: false,
    style: {
      version: 8, projection: { type: "globe" }, glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
      sources: { b: { type: "geojson", data: "/api/boundaries" } },
      layers: [
        { id: "bg", type: "background", paint: { "background-color": "#111110" } },
        { id: "land", type: "fill", source: "b", filter: ["==", ["get", "kind"], "country"], paint: { "fill-color": "#242422" } },
        { id: "disputed", type: "fill", source: "b", filter: ["==", ["get", "kind"], "disputed"], paint: { "fill-color": "#2c2c29" } },
        { id: "regions-line", type: "line", source: "b", filter: ["==", ["get", "kind"], "region"], paint: { "line-color": "#3a3a37", "line-width": 0.6 } },
        { id: "country-line", type: "line", source: "b", filter: ["==", ["get", "kind"], "country"], paint: { "line-color": "#8f8e86", "line-width": 1 } },
        { id: "disputed-line", type: "line", source: "b", filter: ["==", ["get", "kind"], "disputed"], paint: { "line-color": "#8f8e86", "line-width": 1, "line-dasharray": [2, 2] } },
        { id: "region-name", type: "symbol", source: "b", minzoom: 5.5, filter: ["==", ["get", "kind"], "region"],
          layout: { "text-field": ["get", "name"], "text-font": ["Noto Sans Regular"], "text-size": 10.5 }, paint: { "text-color": "#8f8e86", "text-halo-color": "#111110", "text-halo-width": 1 } },
        { id: "disputed-name", type: "symbol", source: "b", minzoom: 4, filter: ["==", ["get", "kind"], "disputed"],
          layout: { "text-field": ["concat", ["get", "name"], "\n(disputed)"], "text-font": ["Noto Sans Regular"], "text-size": 10 }, paint: { "text-color": "#8f8e86", "text-halo-color": "#111110", "text-halo-width": 1 } },
      ],
    },
  });
  map.addControl(new maplibregl.AttributionControl({ compact: true, customAttribution: "Boundaries: Natural Earth (disputed areas shown as disputed) · Fires: NASA FIRMS · Weather: NOAA GFS · Stations: OpenAQ, CPCB and others" }));
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
  await new Promise((r) => map.on("load", r));
  document.querySelector(".maplibregl-ctrl-attrib")?.classList.remove("maplibregl-compact-show"); // start collapsed

  const w = m.wind;
  ctxPbl = makeCanvas("pbl", w.nx, w.ny, [w.west, w.south, w.east, w.north]);
  ctxFoot = makeCanvas("foot", g.nx, g.ny, [g.west, g.south, g.east, g.north]);
  ctxField = makeCanvas("field", g.nx, g.ny, [g.west, g.south, g.east, g.north]);
  map.addSource("cities", { type: "geojson", data: { type: "FeatureCollection", features: m.receptors.map((r) => ({ type: "Feature", geometry: { type: "Point", coordinates: [r.lon, r.lat] }, properties: { name: r.name + (r.low_confidence || (r.skill && r.skill.verdict !== "useful") ? " ⚠" : "") + (r.skill ? `\n${r.skill.verdict === "useful" ? "useful in hindcasts" : "not reliable here"}` : "") } })) } });
  map.addLayer({ id: "city-name", type: "symbol", source: "cities", layout: { "text-field": ["get", "name"], "text-font": ["Noto Sans Regular"], "text-size": 13, "text-offset": [0, 1.2], "text-anchor": "top", "text-allow-overlap": true }, paint: { "text-color": "#f0efec", "text-halo-color": "#111110", "text-halo-width": 1.4 } });

  overlay = new deck.MapboxOverlay({ interleaved: false, layers: [] });
  map.addControl(overlay);

  // Data: small things first, then the big arrays.
  [S.fires, S.stations] = await Promise.all([json(`fires/${m.run_id}`), json("stations")]);
  clock(); redraw(); loadTargeting(); loadLedger();
  bin(`field/${m.run_id}/pm25.bin`).then((b) => { S.field = new Uint8Array(b); drawRasters(true); redraw(); });
  bin(`tracks/${m.run_id}.bin`).then((b) => { S.tracks = b; redraw(); });

  // Controls
  $("slider").oninput = (e) => { S.frame = +e.target.value; clock(); redraw(); };
  $("play").onclick = () => { S.playing = !S.playing; $("play").textContent = S.playing ? "❚❚" : "▶"; lastTick = performance.now(); requestAnimationFrame(render); };
  for (const cb of document.querySelectorAll("[data-layer]")) {
    cb.onchange = async () => {
      const k = cb.dataset.layer;
      S.show[k] = cb.checked;
      if (k === "whatif" && cb.checked && !S.whatif) S.whatif = new Uint8Array(await bin(`field/${m.run_id}/whatif.bin`));
      if (k === "whatif") { $("label").textContent = cb.checked ? m.whatif_label : m.label; }
      if (k === "pbl" && cb.checked && !S.pbl) S.pbl = new Uint8Array(await bin("pbl.bin"));
      if (k === "wind" && cb.checked) await loadWind();
      drawRasters(true); lastTick = performance.now(); requestAnimationFrame(render);
    };
  }
  $("wind-level").onchange = async (e) => { S.windLevel = e.target.value; if (S.show.wind) await loadWind(); };
  for (const b of document.querySelectorAll("[role=tab]")) b.onclick = () => tab(b.dataset.tab);
  $("btn-layers").onclick = () => { $("layers").classList.toggle("open"); $("side").classList.remove("open"); };
  $("btn-info").onclick = () => { $("side").classList.toggle("open"); $("layers").classList.remove("open"); };
  map.on("click", () => { if (innerWidth <= 900) { $("layers").classList.remove("open"); } });
  window.smoke = { state: S, map, overlay }; // for debugging and automated checks
  document.body.dataset.ready = "1";
}
async function loadWind() {
  if (!S.wind[S.windLevel]) S.wind[S.windLevel] = await bin(`wind/${S.windLevel}.bin`);
}

start().catch((e) => { $("label").textContent = `Could not load: ${e.message}`; console.error(e); });
if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
