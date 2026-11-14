// Reelay live dashboard. No framework, no build step, no external requests.
// All untrusted text (paths, headers, bodies) is inserted with textContent.
"use strict";

(() => {
  const PREFIX = document.body.dataset.prefix;
  const API = (p) => `${PREFIX}/api${p}`;
  const $ = (id) => document.getElementById(id);
  const rows = $("rows");
  const state = { events: [], paused: false, filter: "", selected: null, tab: "response", mode: null };
  const MAX_ROWS = 400;

  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };

  async function api(path, opts = {}) {
    const headers = Object.assign({ "X-Reelay-Control": "1" }, opts.body ? { "Content-Type": "application/json" } : {});
    const res = await fetch(API(path), Object.assign({ headers }, opts));
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    return res.json();
  }

  // ------------------------------------------------------------ rendering
  function matches(ev) {
    if (!state.filter) return true;
    const hay = `${ev.method} ${ev.target} ${ev.status} ${ev.source} ${ev.note || ""} ${ev.chaos || ""}`.toLowerCase();
    return state.filter.split(/\s+/).every((t) => hay.includes(t));
  }

  function row(ev, fresh) {
    const tr = el("tr", fresh ? "fresh" : "");
    tr.dataset.id = ev.id;
    if (state.selected === ev.id) tr.classList.add("sel");
    tr.append(el("td", "time", ev.at.slice(11, 23)));
    tr.append(el("td", `method m-${ev.method}`, ev.method));
    const path = el("td", "path", ev.target);
    path.title = ev.target;
    tr.append(path);
    tr.append(el("td", `status s${String(ev.status)[0]}`, ev.status));
    const src = el("td");
    src.append(el("span", `pill src-${ev.source}`, ev.source));
    tr.append(src);
    tr.append(el("td", "r", `${ev.duration_ms.toFixed(ev.duration_ms < 10 ? 1 : 0)} ms`));
    const notes = [ev.match && ev.match !== "exact" ? ev.match : null, ev.chaos, ev.note].filter(Boolean).join(" · ");
    const n = el("td", "notes", notes);
    n.title = notes;
    tr.append(n);
    tr.addEventListener("click", () => select(ev.id));
    return tr;
  }

  function renderAll() {
    rows.replaceChildren(...state.events.filter(matches).map((e) => row(e, false)));
    $("empty").classList.toggle("show", state.events.length === 0);
  }

  function addEvent(ev) {
    state.events.unshift(ev);
    if (state.events.length > MAX_ROWS) state.events.length = MAX_ROWS;
    if (state.paused) return;
    $("empty").classList.remove("show");
    if (matches(ev)) {
      rows.prepend(row(ev, true));
      while (rows.children.length > MAX_ROWS) rows.lastChild.remove();
    }
  }

  // ------------------------------------------------------------ stats
  function setNum(id, value) {
    const node = $(id);
    const text = String(value);
    if (node.textContent !== text) {
      node.textContent = text;
      node.classList.add("bump");
      setTimeout(() => node.classList.remove("bump"), 300);
    }
  }

  async function refreshStats() {
    try {
      const s = await api("/stats");
      setNum("s-total", s.total);
      for (const k of ["replayed", "recorded", "proxied", "miss"]) setNum(`s-${k}`, s.by_source[k] || 0);
      setNum("s-p50", Math.round(s.latency_ms.p50));
      setNum("s-p95", Math.round(s.latency_ms.p95));
      const meta = $("meta");
      const items = [
        ["tape", s.tape.path || "none"], ["exchanges", s.tape.exchanges], ["target", s.target || "none"],
        ["chaos", s.chaos.enabled && s.chaos.rules ? `${s.chaos.rules} rule(s)` : "off"],
      ];
      meta.replaceChildren(...items.map(([k, v]) => { const sp = el("span", null, `${k} `); sp.append(el("b", null, v)); return sp; }));
      if (s.mode !== state.mode) renderModes(s.mode, s.target, s.tape.path);
    } catch (_) { /* server gone; SSE status shows it */ }
  }

  function renderModes(mode, target, tape) {
    state.mode = mode;
    const nav = $("modes");
    nav.replaceChildren(...["record", "replay", "hybrid", "passthrough"].map((m) => {
      const b = el("button", m === mode ? "on" : "", m);
      b.type = "button";
      b.disabled = (["record", "hybrid", "passthrough"].includes(m) && !target) || (m !== "passthrough" && !tape);
      b.addEventListener("click", async () => {
        try { const r = await api("/mode", { method: "POST", body: JSON.stringify({ mode: m }) }); renderModes(r.mode, target, tape); }
        catch (e) { console.error(e); }
      });
      return b;
    }));
    $("hint").textContent = `curl ${location.origin}/your/endpoint`;
  }

  // ------------------------------------------------------------ details
  function highlightJSON(value) {
    const pre = el("pre", "code");
    const text = JSON.stringify(value, null, 2);
    const re = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;
    let last = 0;
    for (const m of text.matchAll(re)) {
      pre.append(document.createTextNode(text.slice(last, m.index)));
      if (m[1]) { pre.append(el("span", m[2] ? "j-key" : "j-str", m[1])); if (m[2]) pre.append(document.createTextNode(m[2])); }
      else if (m[3]) pre.append(el("span", "j-lit", m[3]));
      else pre.append(el("span", "j-num", m[4]));
      last = m.index + m[0].length;
    }
    pre.append(document.createTextNode(text.slice(last)));
    return pre;
  }

  function bodyView(body) {
    if (!body || body.encoding === "empty") return el("p", "kv", "(empty body)");
    if (body.encoding === "json") return highlightJSON(body.content);
    if (body.encoding === "base64") return el("pre", "code", `<binary, ${Math.round(body.content.length * 0.75)} bytes>`);
    return el("pre", "code", body.content);
  }

  function kv(pairs) {
    const box = el("div", "kv");
    if (!pairs.length) box.append(el("span", "k", "none"));
    for (const [k, v] of pairs) { box.append(el("span", "k", k)); box.append(el("span", "v", v)); }
    return box;
  }

  let detail = null;
  function renderDetail() {
    const body = $("d-body");
    if (!detail) return;
    const ex = detail.exchange;
    const nodes = [];
    if (state.tab === "raw") nodes.push(highlightJSON(detail));
    else if (ex) {
      const part = ex[state.tab];
      if (state.tab === "request") {
        nodes.push(el("h4", null, "Query"), kv(part.query), el("h4", null, "Headers"), kv(part.headers), el("h4", null, "Body"), bodyView(part.body));
      } else {
        nodes.push(el("h4", null, `Status ${part.status} ${part.reason || ""}`), el("h4", null, "Headers"), kv(part.headers), el("h4", null, "Body"), bodyView(part.body));
      }
    }
    body.replaceChildren(...nodes);
  }

  async function select(id) {
    state.selected = id;
    for (const tr of rows.children) tr.classList.toggle("sel", tr.dataset.id === id);
    try { detail = await api(`/traffic/${id}`); } catch (_) { return; }
    $("d-method").textContent = detail.method;
    $("d-method").className = `method m-${detail.method}`;
    $("d-path").textContent = detail.target;
    const badges = [`status ${detail.status}`, detail.source, `${detail.duration_ms} ms`, detail.match, detail.chaos, detail.exchange && detail.exchange.id]
      .filter(Boolean).map((t) => el("span", null, t));
    if (detail.note) badges.push(el("span", null, detail.note));
    $("d-badges").replaceChildren(...badges);
    renderDetail();
    $("drawer").classList.add("open");
    $("drawer").setAttribute("aria-hidden", "false");
  }

  function closeDrawer() {
    $("drawer").classList.remove("open");
    $("drawer").setAttribute("aria-hidden", "true");
    state.selected = null;
    for (const tr of rows.children) tr.classList.remove("sel");
  }

  // ------------------------------------------------------------ wiring
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("on", x === t));
    state.tab = t.dataset.tab;
    renderDetail();
  }));
  $("d-close").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); if (e.key === "/" && document.activeElement !== $("filter")) { e.preventDefault(); $("filter").focus(); } });
  $("filter").addEventListener("input", (e) => { state.filter = e.target.value.trim().toLowerCase(); renderAll(); });
  $("pause").addEventListener("click", (e) => {
    state.paused = !state.paused;
    e.target.setAttribute("aria-pressed", String(state.paused));
    e.target.textContent = state.paused ? "Resume" : "Pause";
    if (!state.paused) renderAll();
  });
  $("clear").addEventListener("click", async () => { await api("/clear", { method: "POST" }); state.events = []; renderAll(); refreshStats(); });
  $("reset").addEventListener("click", () => api("/reset", { method: "POST" }));

  function connect() {
    const es = new EventSource(API("/events"));
    es.addEventListener("open", () => { $("live-dot").className = "dot on"; $("live-text").textContent = "live"; document.body.classList.add("streaming"); });
    es.addEventListener("traffic", (msg) => addEvent(JSON.parse(msg.data)));
    es.addEventListener("error", () => { $("live-dot").className = "dot off"; $("live-text").textContent = "reconnecting"; document.body.classList.remove("streaming"); });
  }

  (async () => {
    try { state.events = await api("/traffic"); } catch (_) { state.events = []; }
    renderAll();
    await refreshStats();
    connect();
    setInterval(refreshStats, 1500);
  })();
})();
