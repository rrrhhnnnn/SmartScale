/* Smart Scale dashboard — talks to the Python backend over fetch + SSE. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    connected: false, lamp: "OFFLINE", basket: [], history: [], library: [],
    config: {}, ports: [], simulator: false, quantity: 0, distinct: 0,
  };
  const samples = [];            // [t, grams, stable]
  const MAX_SAMPLES = 200;
  const WINDOW_S = 60;

  // ------------------------------------------------------------------ api
  async function api(path, method = "POST", body) {
    const res = await fetch(path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) {
      if (data.clash) return data;         // caller handles overlap confirm
      throw new Error(data.error || ("HTTP " + res.status));
    }
    return data;
  }

  function toast(text, kind = "") {
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.textContent = text;
    $("toasts").appendChild(el);
    setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, 2600);
    setTimeout(() => el.remove(), 3000);
  }

  const guard = (fn) => async (...a) => {
    try { await fn(...a); } catch (e) { toast(e.message, "err"); }
  };

  // ---------------------------------------------------------------- render
  function setLamp(lampState) {
    const lamp = $("lamp");
    lamp.dataset.state = lampState;
    $("lampText").textContent = lampState;
  }

  function bump(id, value) {
    const el = $(id);
    if (el.textContent !== String(value)) {
      el.textContent = value;
      el.classList.add("pulse");
      setTimeout(() => el.classList.remove("pulse"), 200);
    }
  }

  function renderWeight(g) {
    $("kg").textContent = (g / 1000).toFixed(3);
    $("grams").textContent = g.toFixed(1) + " g";
  }

  function renderCounters(qty, distinct, expected, drift) {
    bump("qty", qty);
    bump("distinct", distinct);
    const recon = $("recon");
    if (!state.basket.length) {
      recon.innerHTML = '<span class="muted">Platform is empty — place an item to begin.</span>';
      return;
    }
    const cls = Math.abs(drift) <= (state.config.min_event_g || 3) ? "ok" : "warn";
    recon.innerHTML =
      `<span>expected <b>${expected.toFixed(1)} g</b></span>` +
      `<span>measured <b>${(state.weight_g ?? 0).toFixed(1)} g</b></span>` +
      `<span class="${cls}">Δ ${drift >= 0 ? "+" : ""}${drift.toFixed(1)} g</span>`;
  }

  function renderBasket() {
    const ul = $("basket");
    ul.innerHTML = "";
    if (!state.basket.length) {
      ul.innerHTML = '<li class="basket-empty" style="display:block">Nothing recognised yet.<br><span style="font-size:12px">Place an item — it will appear here with its name and count.</span></li>';
      return;
    }
    for (const e of state.basket) {
      const li = document.createElement("li");
      li.innerHTML =
        `<div><div class="name">${esc(e.name)}</div><div class="meta">${e.unit_g.toFixed(1)} g each</div></div>` +
        `<div class="count">× ${e.count}</div>` +
        `<div class="total mono">${e.total_g.toFixed(1)} g</div>`;
      ul.appendChild(li);
    }
  }

  function historyRow(ev, isNew) {
    const tr = document.createElement("tr");
    if (isNew) tr.className = "new";
    const sign = ev.delta_g >= 0 ? "+" : "";
    tr.innerHTML =
      `<td class="mono">${ev.clock}</td>` +
      `<td><span class="pill ${ev.kind}">${ev.kind.replace("_", " ")}</span></td>` +
      `<td>${ev.item ? esc(ev.item) : '<span class="muted">—</span>'}</td>` +
      `<td class="num">${ev.count}</td>` +
      `<td class="num">${sign}${ev.delta_g.toFixed(1)}</td>` +
      `<td class="num">${ev.total_g.toFixed(1)}</td>` +
      `<td class="num">${ev.quantity}</td>` +
      `<td class="num">${ev.distinct}</td>` +
      `<td><span class="pill ${ev.status.split(" ")[0]}">${esc(ev.status)}</span></td>`;
    return tr;
  }

  function renderHistory() {
    const body = $("historyBody");
    body.innerHTML = "";
    const rows = state.history.slice().reverse();
    for (const ev of rows) body.appendChild(historyRow(ev, false));
    $("historyEmpty").classList.toggle("hidden", rows.length > 0);
  }

  function renderLibrary() {
    const body = $("libraryBody");
    body.innerHTML = "";
    const lib = state.library;
    for (const it of lib) {
      const clashes = lib.filter(o => o.id !== it.id && it.low <= o.high && o.low <= it.high);
      const tr = document.createElement("tr");
      if (clashes.length) tr.className = "clash";
      tr.innerHTML =
        `<td>${esc(it.name)}</td>` +
        `<td class="num">${it.weight_g.toFixed(1)}</td>` +
        `<td class="num">${it.tolerance_g.toFixed(1)}</td>` +
        `<td><span class="mono">${it.low.toFixed(1)} – ${it.high.toFixed(1)} g</span>` +
        (clashes.length ? ` <span class="muted">· clashes with ${clashes.map(c => esc(c.name)).join(", ")}</span>` : "") + `</td>` +
        `<td style="text-align:right"><button class="link" data-edit="${it.id}">Edit</button><button class="link danger" data-del="${it.id}">Delete</button></td>`;
      body.appendChild(tr);
    }
    const sel = $("simItem");
    const prev = sel.value;
    sel.innerHTML = lib.map(i => `<option value="${i.id}">${esc(i.name)}  (${i.weight_g.toFixed(1)} g)</option>`).join("");
    if ([...sel.options].some(o => o.value === prev)) sel.value = prev;
  }

  function renderPorts() {
    const sel = $("source");
    const prev = sel.value || state.source || state.config.port;
    const opts = [...state.ports.map(p => [p, p]), ["SIMULATOR", "SIMULATOR (no hardware)"]];
    sel.innerHTML = opts.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
    if (opts.some(([v]) => v === prev)) sel.value = prev;
    else sel.value = state.ports[0] || "SIMULATOR";
    sel.disabled = state.connected || state.connecting;
  }

  function renderConnection() {
    $("btnConnect").textContent = (state.connected || state.connecting) ? "Disconnect" : "Connect";
    $("btnConnect").classList.toggle("primary", !(state.connected || state.connecting));
    $("sourceLabel").textContent = state.connected ? (state.simulator ? "simulator" : state.source) : "not connected";
    $("sessionText").textContent = "session " + (state.session || "");
    setLamp(state.lamp);
  }

  const SETTINGS = [
    ["stability_window", "Stability window", "samples that must agree before a reading counts", "number"],
    ["stability_band_g", "Stability band", "max spread across that window, in grams", "number"],
    ["min_event_g", "Dead-band / min item weight", "changes smaller than this are drift, never items (g)", "number"],
    ["zero_track_g", "Auto zero-tracking band", "silently re-zero when empty and within this of 0 (g)", "number"],
    ["zero_display_g", "Display zero band", "readings between -X and +X are shown as 0.000 (g)", "number"],
    ["allow_negative", "Allow negative readings", "off = anything under 0 g is clamped to 0.000", "checkbox"],
    ["empty_zero_g", "Re-zero when empty", "with 0 items, residual up to this is zeroed automatically (g)", "number"],
    ["default_tolerance_g", "Default tolerance", "minimum ± window for a new item (g)", "number"],
    ["tolerance_pct", "Tolerance percent", "effective tolerance = max(default, weight × %)", "number"],
    ["max_multiple", "Max identical at once", "detect up to N of the same item placed together", "number"],
    ["baud", "Serial baud", "must match the firmware (9600 for the original sketch)", "number"],
    ["device_id", "Device ID", "identifies this scale in the cloud feed", "text"],
    ["cloud_url", "Cloud endpoint URL", "POST target for phase 2 — blank stays offline", "text", "wide"],
    ["cloud_enabled", "Enable cloud push", "", "checkbox"],
  ];

  function renderSettings() {
    const form = $("settingsForm");
    if (form.dataset.built) {                 // don't clobber in-progress edits
      return;
    }
    form.dataset.built = "1";
    form.innerHTML = "";
    for (const [key, label, hint, type, extra] of SETTINGS) {
      const v = state.config[key];
      const lab = document.createElement("label");
      lab.className = (type === "checkbox" ? "check " : "") + (extra || "");
      if (type === "checkbox") {
        lab.innerHTML = `<input type="checkbox" name="${key}" ${v ? "checked" : ""}><b>${label}</b>`;
      } else {
        lab.innerHTML = `<b>${label}</b><input type="${type}" name="${key}" value="${esc(String(v ?? ""))}" ${type === "number" ? 'step="any"' : ""}><small>${hint}</small>`;
      }
      form.appendChild(lab);
    }
  }

  function applyState(s) {
    Object.assign(state, s);
    if (Array.isArray(s.samples) && s.samples.length && samples.length === 0) {
      samples.push(...s.samples.slice(-MAX_SAMPLES));
    }
    renderPorts();
    renderConnection();
    renderWeight(s.weight_g || 0);
    renderBasket();
    renderCounters(s.quantity, s.distinct, s.expected_g, s.drift_g);
    renderHistory();
    renderLibrary();
    renderSettings();
    $("statusText").textContent = s.status || "";
    drawChart();
  }

  // ----------------------------------------------------------------- chart
  const canvas = $("chart");
  const ctx = canvas.getContext("2d");

  function drawChart() {
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
      canvas.width = w * dpr; canvas.height = h * dpr;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const now = Date.now() / 1000;
    const pts = samples.filter(p => now - p[0] <= WINDOW_S);
    // baseline grid
    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.lineWidth = 1;
    for (let i = 1; i < 4; i++) { ctx.beginPath(); ctx.moveTo(0, h * i / 4); ctx.lineTo(w, h * i / 4); ctx.stroke(); }
    if (pts.length < 2) return;

    let lo = Math.min(...pts.map(p => p[1])), hi = Math.max(...pts.map(p => p[1]));
    const pad = Math.max(5, (hi - lo) * 0.15);
    const dataMin = lo;
    lo -= pad; hi += pad;
    if (dataMin >= -2) lo = Math.max(lo, -pad * 0.25);   // don't sink far below zero
    const x = (t) => w - ((now - t) / WINDOW_S) * w;
    const y = (g) => h - ((g - lo) / (hi - lo)) * (h - 8) - 4;

    // area fill
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, "rgba(34,211,238,0.22)");
    grad.addColorStop(1, "rgba(34,211,238,0)");
    ctx.beginPath();
    ctx.moveTo(x(pts[0][0]), h);
    for (const p of pts) ctx.lineTo(x(p[0]), y(p[1]));
    ctx.lineTo(x(pts[pts.length - 1][0]), h);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    // line, coloured by stability per segment
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    for (let i = 1; i < pts.length; i++) {
      ctx.strokeStyle = pts[i][2] ? "#22c55e" : "#f59e0b";
      ctx.beginPath();
      ctx.moveTo(x(pts[i - 1][0]), y(pts[i - 1][1]));
      ctx.lineTo(x(pts[i][0]), y(pts[i][1]));
      ctx.stroke();
    }
    // last point marker
    const last = pts[pts.length - 1];
    ctx.fillStyle = last[2] ? "#22c55e" : "#f59e0b";
    ctx.beginPath(); ctx.arc(x(last[0]), y(last[1]), 3.5, 0, Math.PI * 2); ctx.fill();

    // scale labels
    ctx.fillStyle = "rgba(255,255,255,0.35)";
    ctx.font = "10px JetBrains Mono, monospace";
    ctx.textAlign = "left";
    ctx.fillText(hi.toFixed(0) + " g", 4, 11);
    ctx.fillText(lo.toFixed(0) + " g", 4, h - 3);
  }
  window.addEventListener("resize", drawChart);

  // -------------------------------------------------------------------- SSE
  function connectStream() {
    const es = new EventSource("/events");
    es.addEventListener("state", (e) => applyState(JSON.parse(e.data)));
    es.addEventListener("sample", (e) => {
      const s = JSON.parse(e.data);
      state.weight_g = s.g;
      samples.push([Date.now() / 1000, s.g, s.stable]);
      if (samples.length > MAX_SAMPLES) samples.shift();
      renderWeight(s.g);
      if (state.connected) setLamp(s.stable ? "STABLE" : "MOVING");
      renderCounters(s.quantity, s.distinct, s.expected_g, s.drift_g);
      drawChart();
    });
    es.addEventListener("event", (e) => {
      const ev = JSON.parse(e.data);
      state.history.push(ev);
      $("historyBody").prepend(historyRow(ev, true));
      $("historyEmpty").classList.add("hidden");
      const kind = ev.status === "UNKNOWN" ? "warn" : ev.status === "AMBIGUOUS" ? "warn" : "ok";
      toast(ev.text, kind);
      $("statusText").textContent = ev.text;
    });
    es.addEventListener("unknown", (e) => openTeach(JSON.parse(e.data).weight_g));
    es.addEventListener("status", (e) => { $("statusText").textContent = JSON.parse(e.data).text; });
    es.addEventListener("error", (e) => { if (e.data) toast(JSON.parse(e.data).text, "err"); });
    es.onerror = () => { setLamp("OFFLINE"); $("statusText").textContent = "Lost connection to the backend — is run.py still running?"; };
  }

  // ---------------------------------------------------------------- dialogs
  function openItemDialog(item) {
    $("dlgItemTitle").textContent = item ? "Edit item" : "Add item";
    $("itemName").value = item ? item.name : "";
    $("itemWeight").value = item ? item.weight_g : "";
    $("itemTol").value = item ? item.tolerance_g : "";
    $("dlgItem").dataset.id = item ? item.id : "";
    $("dlgItem").showModal();
    $("itemName").focus();
  }

  $("itemWeight").addEventListener("input", () => {
    if ($("dlgItem").dataset.id) return;
    const w = parseFloat($("itemWeight").value);
    if (w > 0) {
      const tol = Math.max(state.config.default_tolerance_g || 2, w * (state.config.tolerance_pct || 2) / 100);
      $("itemTol").value = tol.toFixed(1);
    }
  });

  $("formItem").addEventListener("submit", guard(async (e) => {
    e.preventDefault();
    const id = $("dlgItem").dataset.id;
    const body = { name: $("itemName").value, weight_g: parseFloat($("itemWeight").value), tolerance_g: parseFloat($("itemTol").value) };
    if (id) { await api("/api/items/" + id, "PUT", body); $("dlgItem").close(); toast("Updated " + body.name, "ok"); return; }
    let r = await api("/api/items", "POST", body);
    if (r.clash) {
      if (!(await confirmClash(body, r.clashes))) return;
      r = await api("/api/items", "POST", { ...body, force: true });
    }
    if (r.ok) { $("dlgItem").close(); toast("Added " + body.name, "ok"); }
  }));

  function confirmClash(body, clashes) {
    return new Promise((resolve) => {
      $("clashText").innerHTML = `<b>${esc(body.name)}</b> overlaps: ` +
        clashes.map(c => `<b>${esc(c.name)}</b> (${c.low.toFixed(1)}–${c.high.toFixed(1)} g)`).join(", ");
      const dlg = $("dlgClash");
      const done = (ok) => { dlg.close(); resolve(ok); };
      $("clashYes").onclick = () => done(true);
      $("clashNo").onclick = () => done(false);
      dlg.showModal();
    });
  }

  let teachWeight = null;
  function openTeach(weight) {
    teachWeight = weight;
    $("teachWeight").textContent = weight.toFixed(1) + " g";
    $("teachName").value = "";
    if (!$("dlgTeach").open) $("dlgTeach").showModal();
    $("teachName").focus();
  }
  $("teachSkip").addEventListener("click", guard(async () => {
    $("dlgTeach").close();
    await api("/api/dismiss_unknown");
  }));
  $("formTeach").addEventListener("submit", guard(async (e) => {
    e.preventDefault();
    const body = { name: $("teachName").value };
    let r = await api("/api/teach", "POST", body);
    if (r.clash) {
      if (!(await confirmClash({ name: body.name }, r.clashes))) return;
      r = await api("/api/teach", "POST", { ...body, force: true });
    }
    if (r.ok) { $("dlgTeach").close(); toast("Learned " + body.name, "ok"); }
  }));

  let calFactor = null;
  $("btnCalibrate").addEventListener("click", () => {
    if (!state.connected) return toast("Connect to the scale first.", "warn");
    calFactor = null; $("calResult").textContent = "—"; $("calApply").disabled = true;
    $("dlgCal").showModal();
  });
  $("calTare").addEventListener("click", guard(async () => { await api("/api/tare"); $("calResult").textContent = "Tared — now place the known weight."; }));
  $("calRead").addEventListener("click", guard(async () => {
    $("calResult").textContent = "Reading…";
    const r = await api("/api/calibrate/read", "POST", { known_g: parseFloat($("calKnown").value) });
    calFactor = r.factor;
    $("calResult").textContent = `raw ${r.raw.toFixed(0)}  →  factor ${r.factor.toFixed(1)}`;
    $("calApply").disabled = false;
  }));
  $("calApply").addEventListener("click", guard(async () => {
    await api("/api/calibrate/apply", "POST", { factor: calFactor });
    $("dlgCal").close(); toast("Calibration applied: " + calFactor.toFixed(1), "ok");
  }));

  // ---------------------------------------------------------------- wiring
  $("btnConnect").addEventListener("click", guard(async () => {
    if (state.connected || state.connecting) await api("/api/disconnect");
    else { samples.length = 0; await api("/api/connect", "POST", { source: $("source").value }); }
  }));
  $("btnTare").addEventListener("click", guard(async () => { await api("/api/tare"); toast("Tared", "ok"); }));
  $("btnUndo").addEventListener("click", guard(async () => { await api("/api/undo"); }));
  $("btnNewSession").addEventListener("click", guard(async () => {
    if (confirm("Clear the basket and history view and start a new session?")) await api("/api/new_session");
  }));
  $("btnAddItem").addEventListener("click", () => openItemDialog(null));
  $("btnTeach").addEventListener("click", guard(async () => {
    if (state.pending_unknown_g != null) return openTeach(state.pending_unknown_g);
    if (!state.connected) return toast("Connect first, then place the item.", "warn");
    if (!state.stable) return toast("Wait for the green STABLE lamp, then try again.", "warn");
    const w = (state.weight_g ?? 0) - (state.expected_g ?? 0);
    if (w < (state.config.min_event_g || 3)) return toast("Place the item on the platform first.", "warn");
    openTeach(w);
  }));
  $("libraryBody").addEventListener("click", guard(async (e) => {
    const edit = e.target.dataset.edit, del = e.target.dataset.del;
    if (edit) openItemDialog(state.library.find(i => String(i.id) === edit));
    if (del && confirm("Delete this item from the library?")) { await api("/api/items/" + del, "DELETE"); toast("Deleted", "ok"); }
  }));

  document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(x => x.classList.toggle("active", x === t));
    document.querySelectorAll(".panel").forEach(p => p.classList.toggle("hidden", p.id !== "tab-" + t.dataset.tab));
  }));

  $("btnSaveSettings").addEventListener("click", guard(async (e) => {
    e.preventDefault();
    const body = {};
    for (const el of $("settingsForm").elements) {
      if (!el.name) continue;
      body[el.name] = el.type === "checkbox" ? el.checked : el.value;
    }
    await api("/api/settings", "POST", body);
    $("settingsForm").dataset.built = "";
    toast("Settings saved", "ok");
  }));

  const simPlace = (sign) => guard(async () => {
    const it = state.library.find(i => String(i.id) === $("simItem").value);
    if (!it) return toast("Add an item to the library first.", "warn");
    const jitter = (Math.random() - 0.5) * it.tolerance_g * 0.8;   // real objects are never exact
    await api("/api/sim/place", "POST", { grams: sign * (it.weight_g + jitter) });
  });
  $("simPlace").addEventListener("click", simPlace(+1));
  $("simRemove").addEventListener("click", simPlace(-1));
  $("simPlaceCustom").addEventListener("click", guard(async () => api("/api/sim/place", "POST", { grams: parseFloat($("simGrams").value) })));
  $("simLiftCustom").addEventListener("click", guard(async () => api("/api/sim/place", "POST", { grams: -parseFloat($("simGrams").value) })));
  $("simClear").addEventListener("click", guard(async () => api("/api/sim/clear")));

  function esc(s) { return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

  document.querySelectorAll("[data-close]").forEach(b => b.addEventListener("click", () => $(b.dataset.close).close()));

  // ------------------------------------------------------------------ boot
  connectStream();
  setInterval(drawChart, 1000);            // keeps the 60 s window sliding while idle
})();
