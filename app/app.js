"use strict";
// München Termine – Web-App. Daten: data/events.json (vom Sammler erzeugt),
// data/categories.json, data/sources.json. Eigene Kategorien, Quellen und
// Favoriten liegen vorerst im Browser (localStorage).

const DAY = 86400000;
const LS = {
  get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};

const state = {
  events: [], generated: null,
  baseCats: [], baseSources: [],
  customCats: LS.get("customCats", []),
  customSources: LS.get("customSources", []),
  disabledSources: LS.get("disabledSources", []),
  hiddenCats: LS.get("hiddenCats", []),
  favs: LS.get("favs", []),
  range: LS.get("range", "week"),
  view: "list",
  q: "",
  status: {},
  limit: 150,
};

const $ = (s) => document.querySelector(s);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "style") n.style.cssText = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) n.append(c);
  return n;
};

const allCats = () => [...state.baseCats, ...state.customCats];
const catById = (id) => allCats().find((c) => c.id === id);
const allSources = () => [...state.baseSources, ...state.customSources];

// ---------- Daten laden ----------
async function load(bust) {
  const q = bust ? `?t=${Date.now()}` : "";
  const [ev, cats, src] = await Promise.all([
    fetch(`data/events.json${q}`).then((r) => r.json()),
    fetch(`data/categories.json${q}`).then((r) => r.json()),
    fetch(`data/sources.json${q}`).then((r) => r.json()),
  ]);
  state.events = ev.events.map(prep);
  state.generated = ev.generated;
  state.baseCats = cats;
  state.baseSources = src;
  try {
    const st = await fetch(`data/status.json${q}`).then((r) => r.json());
    state.status = Object.fromEntries((st.sources || []).map((x) => [x.id, x]));
  } catch { state.status = {}; }
}

function prep(e) {
  const start = parseDate(e.start);
  const end = e.end ? parseDate(e.end, true) : (e.allDay ? new Date(start.getTime() + DAY - 1) : new Date(start.getTime() + 2 * 3600000));
  const hay = `${e.title} ${e.description} ${e.venue?.name || ""} ${e.venue?.address || ""}`.toLowerCase();
  return { ...e, _start: start, _end: end, _hay: hay, _long: end - start > 7 * DAY };
}
function parseDate(s, endOfDay) {
  if (s.length === 10) { const d = new Date(s + "T00:00:00"); if (endOfDay) d.setHours(23, 59, 59); return d; }
  return new Date(s);
}

// Kategorien eines Termins: feste Zuordnung + eigene Kategorien über Stichwörter
function catsOf(e) {
  const ids = new Set(e.categories);
  for (const c of state.customCats) {
    if ((c.keywords || []).some((k) => k && e._hay.includes(k.toLowerCase()))) ids.add(c.id);
  }
  return [...ids].filter(catById);
}

// ---------- Filtern ----------
function startOfToday() { const d = new Date(); d.setHours(0, 0, 0, 0); return d; }

function filtered() {
  const t0 = startOfToday();
  const t7 = new Date(t0.getTime() + 7 * DAY);
  const q = state.q.trim().toLowerCase();
  return state.events.filter((e) => {
    if (e._end < t0) return false;
    if (state.disabledSources.includes(e.source)) return false;
    const cs = catsOf(e);
    if (cs.length && cs.every((c) => state.hiddenCats.includes(c))) return false;
    if (q && !q.split(/\s+/).every((w) => e._hay.includes(w))) return false;
    switch (state.range) {
      case "week": return e._start < t7;
      case "upcoming": return e._start >= t7;
      case "fav": return state.favs.includes(e.id);
      default: return true;
    }
  }).sort((a, b) => a._start - b._start);
}

// ---------- Darstellung ----------
const fmtDay = new Intl.DateTimeFormat("de-DE", { weekday: "long", day: "numeric", month: "long" });
const fmtShort = new Intl.DateTimeFormat("de-DE", { day: "2-digit", month: "2-digit", year: "numeric" });
const fmtTime = new Intl.DateTimeFormat("de-DE", { hour: "2-digit", minute: "2-digit" });

function when(e) {
  if (e._long || (e.allDay && e.end && e.end !== e.start)) {
    const t0 = startOfToday();
    return e._start < t0 ? `läuft bis ${fmtShort.format(e._end)}` : `${fmtShort.format(e._start)} – ${fmtShort.format(e._end)}`;
  }
  if (e.allDay) return fmtDay.format(e._start) + " (Uhrzeit unklar)";
  let s = `${fmtDay.format(e._start)}, ${fmtTime.format(e._start)}`;
  if (e.end) s += `–${fmtTime.format(e._end)}`;
  return s + " Uhr";
}

function renderChips() {
  const box = $("#chips");
  box.replaceChildren(...allCats().map((c) => el("button", {
    class: "chip", style: `--c:${c.color}`, "aria-pressed": String(!state.hiddenCats.includes(c.id)),
    onclick: () => {
      const h = state.hiddenCats;
      state.hiddenCats = h.includes(c.id) ? h.filter((x) => x !== c.id) : [...h, c.id];
      LS.set("hiddenCats", state.hiddenCats); resetAndRender();
    },
  }, el("span", { class: "dot" }), c.name)));
}

function card(e) {
  const cs = catsOf(e).map(catById);
  const fav = state.favs.includes(e.id);
  let place = [e.venue?.name, e.venue?.address].filter(Boolean).join(", ");
  let desc = e.description;
  // Steht die Adresse nur in der Beschreibung, zeigen wir sie als Ort an.
  if (!place && desc && /(str\.|straße|strasse|platz|weg|allee|ring|gasse)\s*\d+/i.test(desc) && desc.length < 160) {
    place = desc; desc = "";
  }
  const info = [e.cost && e.cost !== "unklar" ? `Kosten: ${e.cost}` : "Kosten: unklar",
    e.access && e.access !== "unklar" ? `Zugang: ${e.access}` : null, e.online ? "auch online" : null].filter(Boolean).join(" · ");
  return el("article", { class: "card", style: `--c:${cs[0]?.color || "var(--line)"}` },
    el("h3", {}, e.title),
    el("div", { class: "meta" },
      el("div", {}, "🗓 ", when(e)),
      el("div", {}, "📍 ", place || "Ort unklar", e.region && e.region !== "München" ? ` (${e.region})` : ""),
      el("div", {}, info)),
    desc ? el("p", { class: "desc" }, desc) : null,
    el("div", { class: "tags" }, cs.map((c) => el("span", { class: "tag", style: `--c:${c.color}` }, c.name))),
    el("div", { class: "actions" },
      el("button", { class: `fav${fav ? " on" : ""}`, onclick: () => toggleFav(e.id), "aria-label": "Favorit" }, fav ? "★ Gemerkt" : "☆ Merken"),
      el("button", { onclick: () => downloadIcs(e) }, "＋ Kalender"),
      e.venue?.lat ? el("button", { onclick: () => showOnMap(e) }, "Karte") : null,
      e.url ? el("a", { href: e.url, target: "_blank", rel: "noopener" }, "Website ↗") : null));
}

function resetAndRender() { state.limit = 150; render(); }
function render() {
  renderChips();
  const list = filtered();
  $("#count").textContent = `${list.length} Termin${list.length === 1 ? "" : "e"}`;
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.range === state.range));
  const box = $("#listView");
  if (!list.length) {
    box.replaceChildren(el("p", { class: "empty" }, state.range === "fav" ? "Noch keine Favoriten. Tippe bei einem Termin auf „Merken“." : "Keine Termine für diese Auswahl."));
  } else {
    const t0 = startOfToday();
    const shown = list.slice(0, state.limit);
    const single = shown.filter((e) => !e._long);
    const ongoing = shown.filter((e) => e._long);
    const out = [];
    let last = "";
    for (const e of single) {
      const d = e._start < t0 ? t0 : e._start;
      const key = fmtDay.format(d);
      if (key !== last) { out.push(el("h2", { class: "day" }, key)); last = key; }
      out.push(card(e));
    }
    if (ongoing.length) {
      out.push(el("h2", { class: "day" }, "Ausstellungen und längere Termine"));
      ongoing.forEach((e) => out.push(card(e)));
    }
    if (list.length > shown.length) {
      out.push(el("button", { class: "more", onclick: () => { state.limit += 150; render(); } },
        `Weitere ${Math.min(150, list.length - shown.length)} von ${list.length - shown.length} anzeigen`));
    }
    box.replaceChildren(...out);
  }
  if (state.view === "map") renderMap(list);
  $("#stamp").textContent = state.generated ? `Stand der Daten: ${new Date(state.generated).toLocaleString("de-DE")}` : "";
}

// ---------- Favoriten ----------
function toggleFav(id) {
  state.favs = state.favs.includes(id) ? state.favs.filter((x) => x !== id) : [...state.favs, id];
  LS.set("favs", state.favs); render();
}

// ---------- Kalender (.ics) ----------
function icsDate(d, allDay) {
  const p = (n) => String(n).padStart(2, "0");
  if (allDay) return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}`;
  return d.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
}
function icsEsc(s) { return String(s || "").replace(/[\\,;]/g, (m) => "\\" + m).replace(/\n/g, "\\n"); }
function downloadIcs(e) {
  const allDay = e.allDay || e._long;
  const endExcl = allDay ? new Date(e._end.getFullYear(), e._end.getMonth(), e._end.getDate() + 1) : e._end;
  const lines = [
    "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Muenchen Termine//DE", "BEGIN:VEVENT",
    `UID:${e.id}@muenchen-termine`, `DTSTAMP:${icsDate(new Date())}`,
    allDay ? `DTSTART;VALUE=DATE:${icsDate(e._start, true)}` : `DTSTART:${icsDate(e._start)}`,
    allDay ? `DTEND;VALUE=DATE:${icsDate(endExcl, true)}` : `DTEND:${icsDate(endExcl)}`,
    `SUMMARY:${icsEsc(e.title)}`,
    `LOCATION:${icsEsc([e.venue?.name, e.venue?.address].filter(Boolean).join(", "))}`,
    `DESCRIPTION:${icsEsc([e.description, e.url].filter(Boolean).join("\n"))}`,
    e.url ? `URL:${e.url}` : null, "END:VEVENT", "END:VCALENDAR",
  ].filter(Boolean);
  const blob = new Blob([lines.join("\r\n")], { type: "text/calendar;charset=utf-8" });
  const a = el("a", { href: URL.createObjectURL(blob), download: `${e.id}.ics` });
  document.body.append(a); a.click(); a.remove();
}

// ---------- Karte ----------
let map, layer;
function ensureMap() {
  if (map || typeof L === "undefined") return !!map;
  map = L.map("map").setView([48.137, 11.575], 12);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: "© OpenStreetMap-Mitwirkende",
  }).addTo(map);
  layer = L.layerGroup().addTo(map);
  return true;
}
function renderMap(list) {
  if (!ensureMap()) return;
  layer.clearLayers();
  const pts = [];
  for (const e of list) {
    if (!e.venue?.lat) continue;
    const c = catById(catsOf(e)[0])?.color || "#16324f";
    const m = L.circleMarker([e.venue.lat, e.venue.lng], { radius: 8, color: "#fff", weight: 2, fillColor: c, fillOpacity: .9 });
    const pop = el("div", {}, el("strong", {}, e.title), el("br"), when(e), el("br"), e.venue.name);
    m.bindPopup(pop); m.addTo(layer); m._eid = e.id; pts.push([e.venue.lat, e.venue.lng]);
  }
  setTimeout(() => { map.invalidateSize(); if (pts.length) map.fitBounds(pts, { padding: [30, 30], maxZoom: 14 }); }, 50);
}
function setView(v) {
  state.view = v;
  document.querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  $("#listView").hidden = v !== "list";
  $("#mapView").hidden = v !== "map";
  render();
}
function showOnMap(e) {
  setView("map");
  setTimeout(() => {
    map.setView([e.venue.lat, e.venue.lng], 15);
    layer.eachLayer((m) => { if (m._eid === e.id) m.openPopup(); });
  }, 120);
}

// ---------- Einstellungen: Kategorien und Quellen ----------
function slug(s) { return s.toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "x"; }

function srcStatus(s) {
  const st = state.status[s.id];
  if (!s.builtin) return el("div", { class: "muted small" }, "wird beim nächsten Lauf geprüft");
  if (s.fetch === false) return el("div", { class: "muted small" }, "wird nicht automatisch gelesen");
  if (!st) return null;
  if (!st.ok) return el("div", { class: "small warn" }, "⚠︎ zuletzt nicht lesbar");
  return el("div", { class: "muted small" }, `${st.events} Termine`);
}

function renderSettings() {
  $("#catList").replaceChildren(...allCats().map((c) => el("li", { style: `--c:${c.color}` },
    el("span", { class: "dot" }), el("span", { class: "grow" }, c.name, c.keywords?.length && !c.builtin ? el("span", { class: "muted small" }, ` · ${c.keywords.join(", ")}`) : ""),
    c.builtin ? el("span", { class: "muted small" }, "Standard") : el("button", { onclick: () => {
      state.customCats = state.customCats.filter((x) => x.id !== c.id); LS.set("customCats", state.customCats); renderSettings(); render();
    } }, "Entfernen"))));
  $("#srcCat").replaceChildren(...allCats().map((c) => el("option", { value: c.id }, c.name)));
  $("#srcList").replaceChildren(...allSources().map((s) => {
    const off = state.disabledSources.includes(s.id);
    return el("li", {},
      el("span", { class: "grow" }, el("a", { href: s.url, target: "_blank", rel: "noopener" }, s.name),
        el("span", { class: "muted small" }, ` · ${s.categories.map((id) => catById(id)?.name || id).join(", ")}${s.builtin ? "" : " · eigene"}`),
        srcStatus(s)),
      s.builtin
        ? el("button", { onclick: () => {
            state.disabledSources = off ? state.disabledSources.filter((x) => x !== s.id) : [...state.disabledSources, s.id];
            LS.set("disabledSources", state.disabledSources); renderSettings(); render();
          } }, off ? "Einschalten" : "Ausschalten")
        : el("button", { onclick: () => {
            state.customSources = state.customSources.filter((x) => x.id !== s.id); LS.set("customSources", state.customSources); renderSettings();
          } }, "Entfernen"));
  }));
}

$("#catForm").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  const name = f.get("name").trim();
  if (!name) return;
  let id = "c-" + slug(name);
  while (catById(id)) id += "-2";
  state.customCats.push({ id, name, color: f.get("color"), keywords: f.get("keywords").split(",").map((s) => s.trim()).filter(Boolean), builtin: false });
  LS.set("customCats", state.customCats);
  ev.target.reset(); renderSettings(); render();
});
$("#srcForm").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  state.customSources.push({ id: "s-" + slug(f.get("name")) + "-" + Date.now().toString(36), name: f.get("name").trim(), url: f.get("url").trim(), categories: [f.get("category")], region: "München", active: true, builtin: false });
  LS.set("customSources", state.customSources);
  ev.target.reset(); renderSettings();
});

// ---------- Bedienung ----------
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => { state.range = b.dataset.range; LS.set("range", state.range); resetAndRender(); window.scrollTo(0, 0); }));
document.querySelectorAll(".seg-btn").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
$("#search").addEventListener("input", (e) => { state.q = e.target.value; resetAndRender(); });
$("#settingsBtn").addEventListener("click", () => { renderSettings(); $("#settings").showModal(); });
$("#refreshBtn").addEventListener("click", async () => {
  const b = $("#refreshBtn"); b.disabled = true; b.textContent = "…";
  try { await load(true); render(); } catch (err) { alert("Neu laden fehlgeschlagen: " + err.message); }
  b.disabled = false; b.textContent = "↻";
});

load().then(render).catch((err) => {
  $("#listView").replaceChildren(el("p", { class: "empty" }, "Termine konnten nicht geladen werden. " + err.message));
});

if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
