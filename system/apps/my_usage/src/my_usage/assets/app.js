"use strict";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const $ = (id) => document.getElementById(id);
const sum = (a) => a.reduce((x, y) => x + y, 0);
const state = { days: 0, model: "all", sortKey: "msgs", sortDir: -1, allChats: false };
let DATA = null;

function fmtDay(iso) {
  const [, m, d] = iso.split("-").map(Number);
  return MONTHS[m - 1] + " " + d;
}
function dayInZone(date) {
  return new Intl.DateTimeFormat("en-CA", { timeZone: DATA.timezone, year: "numeric", month: "2-digit", day: "2-digit" }).format(date);
}
function fmtMoment(iso) {
  return new Intl.DateTimeFormat("en-US", { timeZone: DATA.timezone, month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(new Date(iso));
}
function fmtLastActive(iso) {
  if (!iso) return "";
  const day = dayInZone(new Date(iso));
  const today = dayInZone(new Date());
  if (day === today) return "Today";
  return fmtDay(day);
}
function niceMax(v) {
  if (v <= 5) return 5;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const f of [1, 2, 2.5, 5, 10]) if (f * p >= v) return f * p;
  return v;
}
function compact(v) {
  return v >= 1000 ? (v / 1000).toFixed(v % 1000 ? 1 : 0).replace(/\.0$/, "") + "K" : String(v);
}
function el(tag, attrs, text) {
  const node = document.createElement(tag);
  Object.entries(attrs || {}).forEach(([k, v]) => node.setAttribute(k, v));
  if (text !== undefined) node.textContent = text;
  return node;
}
function svg(tag, attrs) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attrs || {}).forEach(([k, v]) => node.setAttribute(k, v));
  return node;
}

const tip = $("tip");
function showTip(x, y, value, label) {
  tip.replaceChildren(el("b", {}, value), el("span", {}, label));
  tip.hidden = false;
  const w = tip.offsetWidth;
  tip.style.left = Math.min(window.innerWidth - w - 8, x + 14) + "px";
  tip.style.top = Math.max(8, y - 54) + "px";
}
function hideTip() {
  tip.hidden = true;
}

function columnChart(host, labels, values, unit) {
  const width = host.clientWidth || 800;
  const height = 190;
  const m = { l: 34, r: 8, t: 16, b: 22 };
  const innerW = width - m.l - m.r;
  const innerH = height - m.t - m.b;
  const n = values.length;
  const top = niceMax(Math.max(...values, 1));
  const slot = innerW / n;
  const bw = Math.min(24, Math.max(2, slot - 2));
  const root = svg("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": unit + " per day" });

  for (const t of [0, top / 2, top]) {
    const y = m.t + innerH - (t / top) * innerH;
    root.appendChild(svg("line", { x1: m.l, x2: width - m.r, y1: y, y2: y, class: t === 0 ? "axis" : "grid" }));
    const label = svg("text", { x: m.l - 6, y: y + 4, "text-anchor": "end" });
    // The middle line can sit between whole numbers (2.5 of 5): label it as it is.
    label.textContent = t >= 1000 ? compact(t) : String(Math.round(t * 10) / 10);
    root.appendChild(label);
  }
  const every = n <= 8 ? 1 : Math.ceil(n / 6);
  for (let i = 0; i < n; i += every) {
    const label = svg("text", { x: m.l + i * slot + slot / 2, y: height - 6, "text-anchor": "middle" });
    label.textContent = fmtDay(labels[i]);
    root.appendChild(label);
  }

  let peak = 0;
  values.forEach((v, i) => (peak = v > values[peak] ? i : peak));
  values.forEach((v, i) => {
    const x = m.l + i * slot + (slot - bw) / 2;
    const h = (v / top) * innerH;
    let bar = null;
    if (h > 0) {
      const y = m.t + innerH - h;
      const r = Math.min(4, h / 2, bw / 2);
      bar = svg("path", {
        d: `M${x},${y + h}L${x},${y + r}Q${x},${y} ${x + r},${y}L${x + bw - r},${y}Q${x + bw},${y} ${x + bw},${y + r}L${x + bw},${y + h}Z`,
        class: "bar",
      });
      root.appendChild(bar);
    }
    const hit = svg("rect", { x: m.l + i * slot, y: m.t, width: slot, height: innerH, class: "hit", tabindex: 0, "aria-label": `${fmtDay(labels[i])}: ${v} ${unit}` });
    const show = (cx, cy) => {
      if (bar) bar.classList.add("on");
      showTip(cx, cy, v.toLocaleString(), `${unit} on ${fmtDay(labels[i])}`);
    };
    hit.addEventListener("pointermove", (e) => show(e.clientX, e.clientY));
    hit.addEventListener("pointerleave", () => { if (bar) bar.classList.remove("on"); hideTip(); });
    hit.addEventListener("focus", () => { const r = hit.getBoundingClientRect(); show(r.left + r.width / 2, r.top + 30); });
    hit.addEventListener("blur", () => { if (bar) bar.classList.remove("on"); hideTip(); });
    root.appendChild(hit);
  });

  if (values[peak] > 0) {
    const px = m.l + peak * slot + slot / 2;
    const py = m.t + innerH - (values[peak] / top) * innerH - 6;
    const peakText = svg("text", { x: px, y: Math.max(11, py), "text-anchor": px > width - 110 ? "end" : "middle", class: "peak" });
    peakText.textContent = `Peak ${values[peak].toLocaleString()} on ${fmtDay(labels[peak])}`;
    root.appendChild(peakText);
  }
  host.replaceChildren(root);
}

function tableFor(host, labels, values, unit) {
  const table = el("table");
  const head = el("tr");
  head.append(el("th", {}, "Day"), el("th", { class: "n" }, unit));
  table.appendChild(el("thead")).appendChild(head);
  const body = el("tbody");
  values.forEach((v, i) => {
    const row = el("tr");
    row.append(el("td", {}, fmtDay(labels[i])), el("td", { class: "n" }, v.toLocaleString()));
    body.appendChild(row);
  });
  table.appendChild(body);
  host.replaceChildren(table);
}

function selectedModels() {
  return state.model === "all" ? Object.keys(DATA.steps_by_model) : [state.model];
}
function firstIndex() {
  return state.days ? Math.max(0, DATA.days.length - state.days) : 0;
}

function renderModels(first) {
  const totals = Object.entries(DATA.steps_by_model)
    .map(([name, values]) => ({ name, v: sum(values.slice(first)) }))
    .filter((t) => t.v > 0)
    .sort((a, b) => b.v - a.v);
  if (!totals.length) {
    $("models").replaceChildren(el("p", { class: "empty" }, "No agent steps in this range."));
    return;
  }
  const max = Math.max(...totals.map((t) => t.v), 1);
  const rows = totals.map((t) => {
    const row = el("div", { class: "barrow" });
    const track = el("div");
    track.appendChild(el("div", { class: "barfill", style: `width:${Math.max(2, (t.v / max) * 100)}%` }));
    row.append(el("span", { class: "tag", title: t.name }, t.name), track, el("span", { class: "barval" }, t.v.toLocaleString()));
    return row;
  });
  $("models").replaceChildren(...rows);
}

function renderApps() {
  const apps = DATA.apps;
  const body = [];
  if (!apps.has_events) {
    body.push(el("p", { class: "empty" }, "App opens and visits are not recorded in this workspace yet. They start to show here after the next workspace update."));
  } else {
    const names = [...new Set([...Object.keys(apps.window_opens), ...apps.requests.map((r) => r.service)])].sort();
    const table = el("table");
    const head = el("tr");
    ["App", "Opens", "You", "Visitors", "Agents", "Denied"].forEach((h, i) => head.appendChild(el("th", i ? { class: "n" } : {}, h)));
    table.appendChild(el("thead")).appendChild(head);
    const tbody = el("tbody");
    names.forEach((name) => {
      const r = apps.requests.find((x) => x.service === name) || { owner: 0, visitor: 0, agent: 0, denied: 0 };
      const tr = el("tr");
      tr.append(el("td", {}, name), ...[apps.window_opens[name] || 0, r.owner, r.visitor, r.agent, r.denied].map((v) => el("td", { class: "n" }, v.toLocaleString())));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    body.push(table);
  }
  if (apps.installed.length) {
    body.push(el("div", { class: "chips" }));
    apps.installed.forEach((name) => body[body.length - 1].appendChild(el("span", { class: "chip" }, name)));
  }
  $("apps").replaceChildren(...body);
}

function chatRows(first) {
  const models = selectedModels();
  return DATA.chats.map((c) => ({
    id: c.id,
    name: c.name,
    harness: c.harness,
    msgs: sum(c.messages_by_day.slice(first)),
    steps: sum(models.map((m) => sum((c.steps_by_model_day[m] || []).slice(first)))),
    last: c.last_active,
  })).filter((r) => (state.model === "all" ? r.msgs + r.steps > 0 : r.steps > 0));
}

function renderChats(rows) {
  const key = state.sortKey;
  const sorted = [...rows].sort((a, b) => {
    const av = key === "name" ? a.name.toLowerCase() : a[key];
    const bv = key === "name" ? b.name.toLowerCase() : b[key];
    return (av > bv ? 1 : av < bv ? -1 : 0) * state.sortDir;
  });
  const shown = state.allChats ? sorted : sorted.slice(0, 10);
  const body = shown.map((r) => {
    const tr = el("tr", { "data-chat": r.id });
    const raw = el("button", { class: "rawbtn", "data-raw": r.id }, "View raw");
    const action = el("td");
    action.appendChild(raw);
    tr.append(el("td", { class: "name", title: r.name }, r.name), el("td", { class: "tag" }, r.harness), el("td", { class: "n" }, r.msgs.toLocaleString()), el("td", { class: "n" }, r.steps.toLocaleString()), el("td", {}, fmtLastActive(r.last)), action);
    return tr;
  });
  $("chats").replaceChildren(...body);
  const more = $("moreChats");
  more.hidden = rows.length <= 10;
  more.textContent = state.allChats ? "Show top 10" : `Show all ${rows.length} chats`;
}

function render() {
  const first = firstIndex();
  const labels = DATA.days.slice(first);
  const msgs = DATA.messages.slice(first);
  const models = selectedModels();
  const steps = labels.map((_, i) => sum(models.map((m) => DATA.steps_by_model[m][first + i])));
  $("span").textContent = `${fmtDay(labels[0])} to ${fmtDay(labels[labels.length - 1])}, ${labels[labels.length - 1].slice(0, 4)}`;
  $("heroN").textContent = sum(msgs).toLocaleString();
  $("heroNote").hidden = state.model === "all";
  const rows = chatRows(first);
  const active = labels.filter((_, i) => (state.model === "all" ? msgs[i] > 0 || steps[i] > 0 : steps[i] > 0)).length;
  const tiles = [
    [sum(steps).toLocaleString(), "agent steps"],
    [`${active} of ${labels.length}`, "days with activity"],
    [String(rows.length), "chats active"],
  ].map(([v, l]) => {
    const t = el("div", { class: "tile" });
    t.append(el("div", { class: "v" }, v), el("div", { class: "l" }, l));
    return t;
  });
  $("tiles").replaceChildren(...tiles);
  columnChart($("chartA"), labels, msgs, "messages");
  columnChart($("chartB"), labels, steps, "agent steps");
  tableFor($("tableA"), labels, msgs, "Messages");
  tableFor($("tableB"), labels, steps, "Agent steps");
  renderModels(first);
  renderChats(rows);
}

function renderCollection() {
  const c = DATA.collection;
  if (!c.run_count) {
    $("fresh").textContent = "Nothing collected yet. Run the local analytics copy to fill this page.";
    return;
  }
  const errors = c.feed_error_count ? `${c.feed_error_count} feed errors` : "no errors";
  $("fresh").textContent = `Last collected ${fmtMoment(c.last_collected_at)} · ${c.run_count} collections · ${c.record_count.toLocaleString()} records · ${errors}`;
}

function init() {
  renderCollection();
  renderApps();
  if (!DATA.days.length) {
    $("content").hidden = true;
    $("empty").hidden = false;
    return;
  }
  Object.keys(DATA.steps_by_model).forEach((name) => $("model").appendChild(el("option", { value: name }, name)));
  render();
}

$("range").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b || !DATA) return;
  state.days = Number(b.dataset.days);
  $("range").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
  render();
});
$("model").addEventListener("change", (e) => { state.model = e.target.value; render(); });
document.querySelectorAll("[data-table-for]").forEach((b) => b.addEventListener("click", () => {
  const k = b.dataset.tableFor.toUpperCase();
  const showTable = $("table" + k).hidden;
  $("table" + k).hidden = !showTable;
  $("chart" + k).hidden = showTable;
  b.textContent = showTable ? "Chart" : "Table";
}));
document.querySelectorAll("th [data-sort]").forEach((b) => b.addEventListener("click", () => {
  state.sortDir = state.sortKey === b.dataset.sort ? -state.sortDir : -1;
  state.sortKey = b.dataset.sort;
  render();
}));
$("moreChats").addEventListener("click", () => { state.allChats = !state.allChats; render(); });

// The original records of one chat, exactly as the copy stores them.
let rawRequest = 0;
$("chats").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-raw]");
  if (!b) return;
  const request = ++rawRequest;
  $("rawSrc").textContent = "Loading the original records...";
  $("rawBody").replaceChildren();
  $("raw").hidden = false;
  const response = await fetch(`/api/chats/${encodeURIComponent(b.dataset.raw)}/raw`).catch(() => null);
  // A later click owns the panel now.
  if (request !== rawRequest) return;
  const raw = response && response.ok ? await response.json() : null;
  if (request !== rawRequest) return;
  if (!raw || !raw.records.length) {
    $("rawSrc").textContent = "No original records found for this chat.";
    return;
  }
  $("rawSrc").textContent = `The newest ${raw.records.length} collected records of ${raw.name}, exactly as stored. Each shows the file it is in.`;
  $("rawBody").replaceChildren(...raw.records.flatMap((r) => [
    el("p", { class: "src" }, `data/.skills/local-analytics-copy/${r.file}, byte ${r.offset}`),
    el("pre", {}, JSON.stringify(r.record, null, 2)),
  ]));
});
$("rawClose").addEventListener("click", () => { $("raw").hidden = true; });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("raw").hidden = true; });

let resizeTimer = 0;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => DATA && DATA.days.length && render(), 120); });

fetch("/api/usage")
  .then((r) => r.json())
  .then((d) => { DATA = d; init(); })
  .catch(() => { $("fresh").textContent = "Could not load your usage data. Reload the page to try again."; });
