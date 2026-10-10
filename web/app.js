/* FieldWatch main page: sign-up map, phone panel, live dashboard.
   Port of agent/web/static/index.html. API calls go to FIELDWATCH_CONFIG.apiUrl (same /api/... shapes as
   agent/web/server.py). Live updates come from AppSync Events; without it we poll every 10 s.
   ?demo=1 replays fixtures/feed_replay.json against fixtures/farm.json with no backend at all. */
(function () {
  "use strict";
  const { $, esc, api, cfg, params, condition, agent, toolName, clockTime, apiBase } = FW;
  const POLL_MS = 10000;
  const demo = params.get("demo") === "1";

  let farmId = null, farm = null, busy = false, mode = "connecting";
  let rt = null, pollTimer = null, guardTimer = null, refreshing = false, renderQueued = false;
  let pts = [], markers = [], poly = null, drawFields = [];
  let thread = [];
  const invs = new Map();       // investigation id -> { id, fieldId, items[], hyps, concluded, rt }
  let activeInv = null, pinnedInv = null;
  const fieldState = {};        // field id -> { label, tone } set by the feed
  const seen = new Set();       // card keys already shown (so only new ones animate)
  const fieldLayers = {};
  let fieldsSig = "";

  // ---------- map ----------
  const map = L.map("map").setView([42.16, -96.54], 13);
  L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
    attribution: "Esri World Imagery", maxZoom: 19 }).addTo(map);

  map.on("click", (e) => {
    if (farmId) return; // drawing is only for sign-up
    pts.push([e.latlng.lat, e.latlng.lng]);
    markers.push(L.circleMarker(e.latlng, { radius: 5, color: "#ffeb3b" }).addTo(map));
    if (poly) map.removeLayer(poly);
    poly = L.polygon(pts, { color: "#ffeb3b", weight: 2 }).addTo(map);
    $("addField").disabled = pts.length < 3;
  });
  $("clearPts").onclick = () => {
    pts = []; markers.forEach((m) => map.removeLayer(m)); markers = [];
    if (poly) map.removeLayer(poly);
    poly = null; $("addField").disabled = true;
  };
  $("addField").onclick = () => {
    const name = $("fname").value.trim() || `field ${drawFields.length + 1}`;
    drawFields.push({ name, crop: $("fcrop").value, polygon: pts });
    L.polygon(pts, { color: "#4caf50", weight: 2, fillOpacity: 0.15 }).bindTooltip(name, { permanent: true }).addTo(map);
    $("clearPts").click(); $("fname").value = "";
    $("fieldList").innerHTML = drawFields.map((f) => `<li><span>${esc(f.name)}</span><span>${esc(f.crop)}</span></li>`).join("");
    $("start").disabled = false;
  };

  // ---------- sign-up ----------
  $("start").onclick = async () => {
    $("err").textContent = "";
    if (!$("phone").value.trim()) { $("err").textContent = "Please add the phone number where texts should go."; return; }
    $("start").disabled = true; $("start").textContent = "Pulling the latest satellite pass...";
    try {
      const f = await api("/api/farms", { method: "POST", body: { phone: $("phone").value, farmer: $("farmer").value || "Farmer", fields: drawFields } });
      farmId = f.farm_id;
      location.hash = farmId;
      enterLive();
      applyFarm(f);
      startFeed();
    } catch (e) {
      $("err").textContent = e.message;
      $("start").disabled = false; $("start").textContent = "Start watching";
    }
  };

  function enterLive() {
    $("signupForm").classList.add("hidden"); $("signupControls").classList.add("hidden");
    $("mapTitle").classList.remove("hidden");
    for (const id of ["live", "dashCard", "phoneCard"]) $(id).classList.remove("hidden");
    $("main").classList.add("live");
    $("tagline").textContent = "Your fields, watched from the satellite. Every action waits for your YES.";
    markers.forEach((m) => map.removeLayer(m)); markers = [];
    map.eachLayer((l) => { if (l instanceof L.Polygon) map.removeLayer(l); });
    poly = null;
    setTimeout(() => map.invalidateSize(), 50);
  }

  // ---------- farm data (polling / initial load) ----------
  async function refresh() {
    if (!farmId || demo || refreshing) return;
    refreshing = true;
    try {
      applyFarm(await api(`/api/farms/${encodeURIComponent(farmId)}`));
    } catch (e) {
      if (e.status === 404 && !farm) { fail("We could not find that farm. Check the link, or sign up again."); }
      else if (!farm) fail(e.message);
    } finally { refreshing = false; }
  }
  function fail(msg) {
    farmId = null; history.replaceState(null, "", location.pathname + location.search);
    clearTimeout(guardTimer); stopPolling(); if (rt) { rt.close(); rt = null; }
    for (const id of ["live", "dashCard", "phoneCard"]) $(id).classList.add("hidden");
    $("main").classList.remove("live"); $("mapTitle").classList.add("hidden");
    $("signupForm").classList.remove("hidden"); $("signupControls").classList.remove("hidden");
    $("err").textContent = msg;
  }

  function applyFarm(f) {
    farm = f;
    thread = (f.thread || []).map((m) => ({ who: m.who, text: m.text, at: m.at, kind: m.kind, sms: m.sms, inv: m.investigation_id }));
    ingestServerSteps(f);
    schedule();
  }

  // Poll mode has no feed: rebuild step cards from the saved thread and the last diagnosis.
  function ingestServerSteps(f) {
    const counts = {};
    (f.thread || []).forEach((m) => {
      if (m.kind !== "step" || !m.investigation_id) return;
      const i = getInv(m.investigation_id);
      if (i.rt) return;
      counts[i.id] = (counts[i.id] || 0) + 1;
      addItem(i, { key: "p" + counts[i.id], kind: "step", who: "", text: m.text, quiet: true });
    });
    const d = (f.diagnoses || [])[(f.diagnoses || []).length - 1];
    if (d) {
      const id = (f.pending && f.pending.investigation_id) || lastInvId(f.thread) || "latest";
      const i = getInv(id);
      if (!i.rt) {
        i.hyps = (d.ruled_out || []).map((c) => ({ condition: c, status: "ruled_out" }));
        i.concluded = { summary: `${d.where ? capital(d.where) + ". " : ""}${d.how_sure ? capital(d.how_sure) + "." : ""}`.trim(),
          action: d.action_detail ? { summary: d.action_detail } : null };
        i.fieldName = d.field_name;
      }
    }
  }
  const lastInvId = (t) => { for (let k = (t || []).length - 1; k >= 0; k--) if (t[k].investigation_id) return t[k].investigation_id; return null; };
  const capital = (s) => s.charAt(0).toUpperCase() + s.slice(1);

  // ---------- feed ----------
  function startFeed() {
    if (demo) return;
    refresh();
    const live = cfg.appsyncRealtime && cfg.appsyncApiKey;
    if (!live) { setMode("poll"); startPolling(); return; }
    setMode("connecting");
    guardTimer = setTimeout(() => { if (mode !== "live") { setMode("poll"); startPolling(); } }, 6000);
    rt = FW.Realtime({
      realtimeUrl: cfg.appsyncRealtime, httpUrl: cfg.appsyncHttp, apiKey: cfg.appsyncApiKey,
      channels: [`/fieldwatch/farms/${farmId}/feed`, `/fieldwatch/farms/${farmId}/phone`],
      onMessage: onFeed,
      onStatus: (s) => {
        if (s === "live") { clearTimeout(guardTimer); setMode("live"); stopPolling(); refresh(); }
        else if (s === "down") { setMode("poll"); startPolling(); }
      },
    });
  }
  function startPolling() { if (!pollTimer) pollTimer = setInterval(() => { if (!document.hidden && !busy) refresh(); }, POLL_MS); }
  function stopPolling() { clearInterval(pollTimer); pollTimer = null; }
  function setMode(m) {
    mode = m;
    const b = $("feedBadge");
    b.className = "badge " + (m === "live" ? "live" : m === "poll" ? "poll" : "");
    b.textContent = m === "live" ? "Live" : m === "poll" ? (demo ? "Demo" : "Updating every 10 seconds") : "Connecting";
  }

  function getInv(id) {
    id = id || "latest";
    if (!invs.has(id)) invs.set(id, { id, fieldId: null, fieldName: null, items: [], idx: new Map(), hyps: null, concluded: null, rt: false, updated: 0 });
    return invs.get(id);
  }
  function addItem(i, it) {
    const k = it.key;
    if (k && i.idx.has(k)) { Object.assign(i.items[i.idx.get(k)], it); return; }
    it.id = i.id; it.t = Date.now();
    i.items.unshift(it);
    i.idx = new Map(i.items.map((x, n) => [x.key, n]));
  }
  function touch(i, fieldHint) {
    i.updated = Date.now(); i.rt = true;
    if (fieldHint) { i.fieldId = fieldHint.field_id || fieldHint.id || i.fieldId; i.fieldName = fieldHint.name || fieldHint.field_name || i.fieldName; }
    if (!pinnedInv) activeInv = i.id;
  }
  const fieldOf = (p, m) => (p.field && typeof p.field === "object" ? p.field : p.field_id ? { field_id: p.field_id, name: p.field_name } : m.field_id ? { field_id: m.field_id } : null);

  function onFeed(m) {
    if (!m || !m.type) return;
    const p = m.payload || {};
    const id = m.investigation_id || p.investigation_id;
    switch (m.type) {
      case "investigation_step": {
        const i = getInv(id); touch(i, fieldOf(p, m));
        const ev = p.evidence;
        addItem(i, ev
          ? { key: "e" + ev.evidence_id, kind: "evidence", who: m.agent || p.agent, text: p.text || ev.reason, why: ev.reason !== p.text ? ev.reason : "", hyp: ev.hypothesis, dir: ev.direction, tool: p.tool || ev.source }
          : { key: p.seq != null ? "s" + p.seq : "t" + m.ts + (p.text || ""), kind: p.step === "done" ? "done" : "step", who: m.agent || p.agent, text: p.text || toolName(p.tool), tool: p.tool });
        if (p.hypotheses) i.hyps = p.hypotheses;
        break;
      }
      case "evidence_added": {
        const i = getInv(id); touch(i, fieldOf(p, m));
        const ev = p.evidence || p;
        addItem(i, { key: "e" + ev.evidence_id, kind: "evidence", who: m.agent || p.agent, text: ev.reason, hyp: ev.hypothesis, dir: ev.direction, tool: ev.source });
        break;
      }
      case "hypotheses_updated": {
        const i = getInv(id); touch(i, fieldOf(p, m));
        i.hyps = Array.isArray(p) ? p : p.hypotheses || i.hyps;
        break;
      }
      case "concluded": {
        const i = getInv(id); touch(i, fieldOf(p, m));
        const dg = p.diagnosis || {};
        if (p.hypotheses) i.hyps = p.hypotheses;
        i.concluded = { condition: dg.condition || p.condition, summary: dg.summary || p.summary || "", impact: p.impact, action: p.proposed_action };
        const fid = i.fieldId; if (fid) fieldState[fid] = { label: "Needs a look", tone: "bad" };
        break;
      }
      case "action_created": {
        const i = getInv(id); touch(i, fieldOf(p, m));
        addItem(i, { key: "a" + (p.action_id || m.ts), kind: "done", who: "", text: p.summary || p.instructions || "A repair ticket was sent.", tag: "Ticket sent" });
        break;
      }
      case "message_out":
      case "message_in": {
        const text = p.body || p.text || p.message || "";
        if (!text) break;
        const who = m.type === "message_out" ? "fieldwatch" : "farmer";
        if (!thread.slice(-6).some((x) => x.who === who && x.text === text)) thread.push({ who, text, at: m.ts, kind: "msg" });
        break;
      }
      case "scene_analyzed": {
        sweep();
        const f = fieldOf(p, m); if (f && f.field_id && !fieldState[f.field_id]) fieldState[f.field_id] = { label: "Checked just now", tone: "" };
        setTimeout(refresh, 1500);
        break;
      }
      case "field_status": {
        const f = fieldOf(p, m); const fid = f && f.field_id;
        if (fid) fieldState[fid] = statusFromFeed(p);
        setTimeout(refresh, 1500);
        break;
      }
      default: break;
    }
    schedule();
  }
  function statusFromFeed(p) {
    const raw = String(p.label || p.status || "").toLowerCase();
    if (/flag|alert|problem|attention|stress|bad|look/.test(raw)) return { label: p.label || "Needs a look", tone: "bad" };
    if (/invest|check|running|scan/.test(raw)) return { label: p.label || "Checking closer", tone: "busy" };
    if (/ok|normal|healthy|fine|good|clear/.test(raw)) return { label: p.label || "Looks fine", tone: "good" };
    return { label: p.label || raw.replace(/_/g, " ") || "Watching", tone: "" };
  }
  function sweep() {
    const el = document.createElement("div"); el.className = "sweep"; $("map").appendChild(el);
    setTimeout(() => el.remove(), 2800);
  }

  // ---------- rendering ----------
  function schedule() { if (!renderQueued) { renderQueued = true; requestAnimationFrame(() => { renderQueued = false; render(); }); } }
  function render() { if (!farm && !demo) return; renderFields(); renderMap(); renderThread(); renderDash(); setButtons(); }

  function imgSrc(f, kind) {
    if (demo) return kind === "truecolor" ? "fixtures/true_color.svg" : "fixtures/health.svg";
    return `${apiBase()}/api/farms/${encodeURIComponent(farm.farm_id)}/fields/${encodeURIComponent(f.id)}/${kind}.png?v=${encodeURIComponent((f.stats && f.stats.scene_date) || "")}`;
  }
  function renderFields() {
    $("checked").textContent = farm.last_checked ? `Last checked the catalog ${farm.last_checked.replace("T", " ")}. Checks again every 10 minutes.` : "";
    const sig = JSON.stringify(farm.fields.map((f) => [f.id, f.stats && f.stats.scene_date, f.stats && f.stats.health_index_median]));
    if (sig === fieldsSig) return;
    fieldsSig = sig;
    $("fieldsOut").innerHTML = farm.fields.map((f) => {
      const s = f.stats;
      if (!s) return `<div class="field"><h3>${esc(f.name)}</h3><p class="stat">No clear pass yet.</p></div>`;
      return `<div class="field"><h3>${esc(f.name)} <span class="stat">&middot; ${esc(f.crop)} &middot; pass ${esc(s.scene_date)}</span></h3>
        <div class="imgs">
          <div><img loading="lazy" alt="Satellite photo of ${esc(f.name)}" src="${imgSrc(f, "truecolor")}"><small>What the satellite sees</small></div>
          <div><img loading="lazy" alt="Crop health of ${esc(f.name)}" src="${imgSrc(f, "health")}"><small>Crop health: green strong, brown weak</small></div>
        </div>
        <p class="stat">Weakest in the ${esc(s.weakest_area)} &middot; ${Math.round((s.share_of_field_below_0_3 || 0) * 100)}% of the field looks bare or stressed</p>
        <div class="row"><button class="secondary" data-ask="${esc(f.id)}">Ask FieldWatch about this field</button></div></div>`;
    }).join("");
  }

  function fieldTone(f) {
    for (const i of invs.values()) {
      if (i.fieldId === f.id || (i.fieldName && i.fieldName.toLowerCase() === f.name.toLowerCase())) {
        if (i.concluded) return { label: "Needs a look", tone: "bad" };
        if (i.rt) return { label: "Checking closer", tone: "busy" };
      }
    }
    if (fieldState[f.id]) return fieldState[f.id];
    return f.stats ? { label: "Watching", tone: "" } : { label: "Waiting for a clear pass", tone: "" };
  }
  const TONE_COLOR = { bad: "#d32f2f", busy: "#f9a825", good: "#43a047", "": "#7cb342" };
  function renderMap() {
    let any = false;
    farm.fields.forEach((f) => {
      if (!f.polygon || f.polygon.length < 3) return;
      const t = fieldTone(f), col = TONE_COLOR[t.tone] || TONE_COLOR[""];
      const latlng = f.polygon.map(([lon, lat]) => [lat, lon]);
      if (fieldLayers[f.id]) { fieldLayers[f.id].setStyle({ color: col }); fieldLayers[f.id].setTooltipContent(`${f.name}: ${t.label}`); return; }
      fieldLayers[f.id] = L.polygon(latlng, { color: col, weight: 3, fillOpacity: 0.2 }).bindTooltip(`${f.name}: ${t.label}`, { permanent: true }).addTo(map);
      any = true;
    });
    if (any) map.fitBounds(L.featureGroup(Object.values(fieldLayers)).getBounds().pad(0.2));
  }

  function renderThread() {
    const el = $("thread");
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    el.innerHTML = thread.map((m) => {
      const badge = m.sms && m.sms.ok ? " &middot; texted" : "";
      return `<div class="msg ${esc(m.who)}">${esc(m.text)}${m.who !== "agent" ? `<time>${esc(clockTime(m.at))}${badge}</time>` : ""}</div>`;
    }).join("");
    if (atBottom) el.scrollTop = el.scrollHeight;
    if (farm && farm.muted) $("smsStatus").textContent = "Texts are paused (farmer replied STOP). Reply START to resume.";
  }

  function renderDash() {
    $("fieldChips").innerHTML = farm.fields.map((f) => { const t = fieldTone(f); return `<span class="chip ${t.tone}">${esc(f.name)}: ${esc(t.label)}</span>`; }).join("");
    const list = [...invs.values()].filter((i) => i.items.length || i.hyps || i.concluded).sort((a, b) => b.updated - a.updated);
    $("invTabs").innerHTML = list.length > 1 ? list.map((i, n) =>
      `<button class="${i.id === (activeInv || list[0].id) ? "" : "secondary"}" data-inv="${esc(i.id)}">${esc(i.fieldName || ("Check " + (list.length - n)))}</button>`).join("") : "";
    const cur = list.find((i) => i.id === activeInv) || list[0];
    if (!cur) return;
    let h = "";
    if (cur.hyps && cur.hyps.length) {
      const order = { likely: 0, open: 1, ruled_out: 2 };
      const hs = [...cur.hyps].sort((a, b) => (order[a.status] - order[b.status]) || ((b.probability || 0) - (a.probability || 0)));
      h += `<h3>How sure we are</h3>` + hs.map((x) => {
        const pct = x.probability != null ? Math.round(x.probability * 100) : null;
        const right = x.status === "ruled_out" ? "ruled out" : x.status === "likely" ? (pct != null ? `${pct}% sure, most likely` : "most likely") : pct != null ? `${pct}%` : "still possible";
        return `<div class="hyp ${esc(x.status)}"><div class="name"><span>${esc(condition(x.condition))}</span><span>${esc(right)}</span></div>
          ${pct != null ? `<div class="bar" role="img" aria-label="${pct} percent"><i style="width:${pct}%"></i></div>` : ""}</div>`;
      }).join("");
    }
    if (cur.concluded) {
      const c = cur.concluded, imp = c.impact;
      h += `<div class="concluded"><div class="big">${esc(c.condition ? condition(c.condition) : "What we found")}</div><div>${esc(c.summary || "")}</div>
        ${imp ? `<div style="margin-top:6px"><strong>${FW.usd(imp.low_usd)} to ${FW.usd(imp.high_usd)}</strong> at risk if nothing is done</div>` : ""}
        ${c.action && c.action.summary ? `<div style="margin-top:6px">Next step: ${esc(c.action.summary)}</div>` : ""}</div>`;
    }
    if (cur.items.length) {
      h += `<h3>What we checked</h3><div class="cards">` + cur.items.map((it) => {
        const k = cur.id + "|" + it.key, isNew = !seen.has(k); seen.add(k);
        const tag = it.kind === "evidence" && it.dir ? `<span class="tag ${it.dir}">${it.dir === "refutes" ? "Rules out" : "Points to"} ${esc(condition(it.hyp))}</span>`
          : it.tag ? `<span class="tag done">${esc(it.tag)}</span>` : "";
        return `<div class="ecard${isNew && !it.quiet ? " new" : ""}">${it.who ? `<div class="who">${esc(agent(it.who))}</div>` : ""}<div>${esc(it.text)}</div>${it.why ? `<div class="why">${esc(it.why)}</div>` : ""}${tag}</div>`;
      }).join("") + `</div>`;
    }
    $("invBody").innerHTML = h || `<p class="hint">Nothing is being checked right now.</p>`;
  }
  $("invTabs").addEventListener("click", (e) => { const b = e.target.closest("[data-inv]"); if (b) { pinnedInv = activeInv = b.dataset.inv; schedule(); } });

  // ---------- phone actions ----------
  const BTNS = ["btnAsk", "btnYes", "btnNo", "btnMore", "btnCheck", "btnSend"];
  function setButtons() { for (const b of BTNS) $(b).disabled = busy || !farmId; }
  async function call(path, body, localText) {
    if (busy || !farmId) return;
    busy = true; $("phoneErr").textContent = "";
    if (localText) { thread.push({ who: "farmer", text: localText, at: new Date().toISOString(), kind: "msg" }); }
    schedule(); setButtons();
    try {
      if (demo) { await demoAction(localText || "ASK"); return; }
      const r = await api(path, { method: "POST", body });
      if (r && r.thread) applyFarm(r); else await refresh();
    } catch (e) { $("phoneErr").textContent = e.message; }
    finally { busy = false; setButtons(); if (!demo) refresh(); }
  }
  const reply = (text) => call(`/api/farms/${encodeURIComponent(farmId)}/reply`, { text }, text);
  $("btnAsk").onclick = () => reply("ASK");
  $("btnYes").onclick = () => reply("YES");
  $("btnNo").onclick = () => reply("NO");
  $("btnMore").onclick = () => reply("MORE");
  $("btnCheck").onclick = () => call(`/api/farms/${encodeURIComponent(farmId)}/check`);
  $("replyForm").onsubmit = (e) => { e.preventDefault(); const t = $("replyText").value.trim(); if (t) { $("replyText").value = ""; reply(t); } };
  $("fieldsOut").addEventListener("click", (e) => {
    const b = e.target.closest("[data-ask]");
    if (b) call(`/api/farms/${encodeURIComponent(farmId)}/fields/${encodeURIComponent(b.dataset.ask)}/ask`);
  });

  // ---------- start-up ----------
  if (!document.getElementById("phoneErr")) {
    const p = document.createElement("p"); p.id = "phoneErr"; p.className = "err";
    $("replyForm").after(p);
  }

  async function demoAction(text) {
    const up = text.toUpperCase();
    if (up === "ASK") return runDemo();
    const say = up === "YES" ? "Done. I sent the repair ticket to your brother with a map pin." : up === "NO" ? "Okay, I will not send anything." : up === "MORE" ? "The weak area is a ring on the outer edge, where the pivot's last span runs. Rain, hail and soil were checked and ruled out." : "Got it.";
    await new Promise((r) => setTimeout(r, 400));
    onFeed({ type: "message_out", ts: new Date().toISOString(), farm_id: "demo", payload: { body: say } });
  }
  let demoTimers = [];
  async function runDemo() {
    demoTimers.forEach(clearTimeout); demoTimers = [];
    invs.clear(); activeInv = pinnedInv = null; Object.keys(fieldState).forEach((k) => delete fieldState[k]);
    const script = await FW.loadFixture("feed_replay.json");
    const speed = Number(params.get("speed")) || 1;
    script.forEach((s) => demoTimers.push(setTimeout(() => onFeed(s.msg), s.at_ms / speed)));
    schedule();
  }

  async function boot() {
    if (demo) {
      farm = await FW.loadFixture("farm.json"); farmId = farm.farm_id;
      thread = (farm.thread || []).map((m) => ({ who: m.who, text: m.text, at: m.at, kind: "seed" }));
      $("main").classList.add("live");
      enterLive(); setMode("poll"); $("feedBadge").textContent = "Demo";
      schedule(); setTimeout(runDemo, 800);
      return;
    }
    if (location.hash.length > 1) {
      farmId = decodeURIComponent(location.hash.slice(1));
      enterLive(); startFeed();
    }
  }
  boot().catch((e) => { $("err").textContent = e.message; });
})();
