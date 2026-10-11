/* FieldWatch main page. Five tabs (hash routes): Draw, Plots, Text, Activity, Farm.
   The farm id lives in ?farm=<id> (old index.html#<id> links are rewritten by the inline script in index.html).
   API calls go to FIELDWATCH_CONFIG.apiUrl (same /api/... shapes as agent/web/server.py). Live updates come from
   AppSync Events; without it we poll every 10 s. ?demo=1 replays fixtures/feed_replay.json with no backend at all.
   Field editing goes through FW.fieldsApi (fieldsapi.js). Every piece of server text is escaped with FW.esc. */
(function () {
  "use strict";
  const { $, esc, api, cfg, params, condition, agent, toolName, clockTime, apiBase, safeUrl } = FW;
  const POLL_MS = 10000;
  const TABS = ["draw", "plots", "text", "activity", "farm"];
  const demo = params.get("demo") === "1";

  let farmId = null, farm = null, busy = false, mode = "connecting", tab = "draw";
  let epoch = 0, rt = null, pollTimer = null, guardTimer = null, refreshing = false, renderQueued = false;
  let thread = [], threadErr = "", threadFirst = true;
  const invs = new Map();        // investigation id -> { id, fieldId, fieldName, items[], hyps, concluded, rt, updated, reportUrl, workUrl }
  const fieldState = {};         // field id -> tone set by the feed
  const seen = new Set();        // step keys already shown (so only new ones fade in)
  const viewed = new Set();      // concluded investigations the farmer has looked at
  const openInv = new Set();     // investigation ids the farmer expanded or collapsed
  const closedInv = new Set();
  let selected = null, confirmDel = null, askBusy = null, plotMsg = null, saving = false;
  let actFilter = "all", fitNext = true, drafts = [], draft = { pts: [], target: null }, deferPlots = false;

  const slug = (s) => String(s).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "field";
  const capital = (s) => s.charAt(0).toUpperCase() + s.slice(1);
  const setHtml = (el, html) => { if (el.__h !== html) { el.__h = html; el.innerHTML = html; } };
  const fieldsOf = () => (farm && farm.fields) || [];
  const farmerName = () => (farm && farm.farmer && (farm.farmer.name || farm.farmer)) || "";

  // ---------- map ----------
  const map = L.map("map", { zoomControl: false }).setView([42.16, -96.54], 13);
  const sat = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", { attribution: "Esri World Imagery", maxZoom: 19 });
  const street = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "&copy; OpenStreetMap", maxZoom: 19 });
  sat.addTo(map);
  L.control.zoom({ position: "topright" }).addTo(map);
  const MapTools = L.Control.extend({
    options: { position: "topright" },
    onAdd() {
      const bar = L.DomUtil.create("div", "leaflet-bar");
      const mk = (txt, label, fn) => {
        const b = L.DomUtil.create("button", "fwbtn", bar); b.type = "button"; b.textContent = txt; b.title = label; b.setAttribute("aria-label", label);
        L.DomEvent.disableClickPropagation(b); b.onclick = fn; return b;
      };
      let isSat = true;
      const tg = mk("Map", "Switch between satellite and street map", () => {
        isSat = !isSat; if (isSat) { map.removeLayer(street); sat.addTo(map); } else { map.removeLayer(sat); street.addTo(map); }
        tg.textContent = isSat ? "Map" : "Photo";
      });
      mk("Me", "Show where I am", () => map.locate({ setView: true, maxZoom: 17 }));
      return bar;
    },
  });
  new MapTools().addTo(map);
  // keep the map framed on the fields until the farmer moves it themselves
  let userMoved = false;
  ["pointerdown", "wheel", "touchstart"].forEach((ev) => $("map").addEventListener(ev, () => { userMoved = true; }, { passive: true }));
  if (window.ResizeObserver) new ResizeObserver(() => { map.invalidateSize(); if (!userMoved && Object.keys(layers).length && !draft.pts.length) fitFields(); }).observe($("map"));

  map.on("zoomend viewreset", () => Object.values(layers).forEach((l) => { const t = l.poly.getTooltip(); if (t && t._map) t.update(); }));

  // ---------- routing and tabs ----------
  function route() {
    let t = location.hash.slice(1);
    if (!TABS.includes(t)) t = farmId ? "plots" : "draw";
    if (!farmId && t !== "draw") { t = "draw"; history.replaceState(null, "", location.pathname + location.search + "#draw"); }
    tab = t;
    for (const name of TABS) {
      const on = name === t, el = $("v-" + name);
      el.hidden = !on;
      const b = $("tab-" + name);
      b.setAttribute("aria-selected", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
      b.setAttribute("aria-disabled", !farmId && name !== "draw" ? "true" : "false");
      b.title = !farmId && name !== "draw" ? "Opens after you start watching your fields" : "";
    }
    const mapTab = t === "draw" || t === "plots";
    $("stage").classList.toggle("nomap", t !== "draw");
    $("mapHint").hidden = t !== "draw";
    // one Leaflet map, two homes: full size beside the Draw form, compact in the Plots page
    const wrap = $("mapwrap");
    if (t === "plots" && wrap.parentElement !== $("mapSlot")) { $("mapSlot").appendChild(wrap); fitNext = true; userMoved = false; }
    else if (t === "draw" && wrap.parentElement !== $("stage")) { $("stage").insertBefore(wrap, $("stage").firstChild); fitNext = true; userMoved = false; }
    if (mapTab) setTimeout(() => { map.invalidateSize(); if (fitNext) fitFields(); }, 30);
    if (t === "text") { threadFirst = true; }
    if (t !== "draw" && draft.target) cancelRedraw(true);
    schedule();
    if (t === "text") setTimeout(() => { const th = $("thread"); th.scrollTop = th.scrollHeight; }, 30);
  }
  window.addEventListener("hashchange", route);
  function go(t) { if (location.hash.slice(1) === t) route(); else location.hash = t; }
  $("tabs").addEventListener("click", (e) => {
    const b = e.target.closest("[data-tab]"); if (!b) return;
    if (b.getAttribute("aria-disabled") === "true") return;
    go(b.dataset.tab);
  });
  $("tabs").addEventListener("keydown", (e) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft" && e.key !== "Home" && e.key !== "End") return;
    const ok = TABS.filter((n) => $("tab-" + n).getAttribute("aria-disabled") !== "true");
    let i = ok.indexOf(tab);
    if (e.key === "ArrowRight") i = (i + 1) % ok.length; else if (e.key === "ArrowLeft") i = (i - 1 + ok.length) % ok.length; else if (e.key === "Home") i = 0; else i = ok.length - 1;
    e.preventDefault(); $("tab-" + ok[i]).focus(); go(ok[i]);
  });
  document.addEventListener("click", (e) => { const g = e.target.closest("[data-go]"); if (g) { e.preventDefault(); go(g.dataset.go); } });

  // phone bottom sheet: drag the handle, or tap it to switch between small and tall
  document.querySelectorAll(".grab").forEach((g) => {
    let y0 = null, h0 = 0, moved = false;
    g.addEventListener("pointerdown", (e) => { y0 = e.clientY; h0 = g.parentElement.getBoundingClientRect().height; moved = false; g.setPointerCapture(e.pointerId); });
    g.addEventListener("pointermove", (e) => {
      if (y0 == null) return;
      if (Math.abs(e.clientY - y0) > 5) moved = true;
      if (moved) { const max = $("stage").clientHeight - 80; $("stage").style.setProperty("--sheet", Math.max(120, Math.min(max, h0 + (y0 - e.clientY))) + "px"); map.invalidateSize(); }
    });
    g.addEventListener("pointerup", () => {
      if (y0 != null && !moved) { const tall = parseInt(getComputedStyle($("stage")).getPropertyValue("--sheet")) > 300; $("stage").style.setProperty("--sheet", tall ? "42%" : "78%"); }
      y0 = null;
    });
    g.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("stage").style.setProperty("--sheet", $("stage").style.getPropertyValue("--sheet") === "78%" ? "42%" : "78%"); } });
  });
  const sheetUp = () => { if (tab === "draw" && matchMedia("(max-width: 999px)").matches) $("stage").style.setProperty("--sheet", "70%"); };

  // ---------- drawing ----------
  let drawPoly = null, vtx = [];
  const DRAFT_STYLE = { color: "#ffd54a", weight: 3, fillOpacity: 0.15 };
  map.on("click", (e) => {
    if (tab !== "draw") return;
    draft.pts.push([e.latlng.lat, e.latlng.lng]); redrawDraft();
  });
  function redrawDraft() {
    const ll = draft.pts;
    vtx.forEach((m) => map.removeLayer(m)); vtx = [];
    if (drawPoly) { map.removeLayer(drawPoly); drawPoly = null; }
    if (ll.length > 1) drawPoly = (ll.length > 2 ? L.polygon(ll, DRAFT_STYLE) : L.polyline(ll, DRAFT_STYLE)).addTo(map);
    ll.forEach((p, i) => {
      const m = L.marker(p, { draggable: true, keyboard: false, icon: L.divIcon({ className: "vtx", iconSize: [14, 14] }) }).addTo(map);
      m.on("drag", () => { const q = m.getLatLng(); draft.pts[i] = [q.lat, q.lng]; if (drawPoly) drawPoly.setLatLngs(draft.pts); });
      vtx.push(m);
    });
    drawUi();
  }
  function clearDraft() { draft.pts = []; redrawDraft(); }
  function drawUi() {
    const n = draft.pts.length, signed = !!farmId;
    $("ptCount").textContent = n === 1 ? "1 corner so far" : `${n} corners so far`;
    $("undoPt").disabled = n === 0; $("clearPts").disabled = n === 0;
    $("addField").disabled = n < 3 || saving;
    $("addField").textContent = draft.target ? "Save new outline" : "Finish";
    const addPrimary = n >= 3 || signed;
    $("addField").classList.toggle("primary", addPrimary); $("addField").classList.toggle("secondary", !addPrimary);
    $("start").classList.toggle("primary", !addPrimary); $("start").classList.toggle("secondary", addPrimary);
    $("start").disabled = !drafts.length || saving;
    $("stepText").textContent = draft.target ? "Click the new corners. Drag a corner to move it." : n < 3 ? "Click the corners of your field on the map." : "Name it, pick the crop, then press Finish.";
    $("fname").disabled = !!draft.target; $("fcrop").disabled = !!draft.target;
  }
  $("undoPt").onclick = () => { draft.pts.pop(); redrawDraft(); };
  $("clearPts").onclick = clearDraft;

  let draftLayers = [];
  function renderDrafts() {
    draftLayers.forEach((l) => map.removeLayer(l)); draftLayers = [];
    drafts.forEach((f) => draftLayers.push(L.polygon(f.polygon, { color: "#4FD1A1", weight: 3, fillOpacity: 0.15 }).bindTooltip(f.name, { permanent: true, direction: "center", className: "fwtip" }).addTo(map)));
    $("draftTitle").hidden = !drafts.length;
    $("draftList").innerHTML = drafts.map((f, i) => `<li><span><strong>${esc(f.name)}</strong> <span class="quiet">${esc(f.crop)}</span></span><button class="linkbtn" data-rm="${i}" type="button">Remove</button></li>`).join("");
    drawUi();
  }
  $("draftList").addEventListener("click", (e) => { const b = e.target.closest("[data-rm]"); if (b) { drafts.splice(Number(b.dataset.rm), 1); renderDrafts(); } });

  $("addField").onclick = async () => {
    if (draft.pts.length < 3) return;
    $("drawMsg").textContent = "";
    if (draft.target) return saveOutline();
    const name = $("fname").value.trim() || `field ${(farmId ? fieldsOf().length : drafts.length) + 1}`, crop = $("fcrop").value;
    const polygon = draft.pts.map((p) => [p[0], p[1]]);
    if (!farmId) {
      drafts.push({ name, crop, polygon }); clearDraft(); $("fname").value = ""; renderDrafts();
      return;
    }
    saving = true; drawUi(); $("addField").textContent = "Adding...";
    try {
      let res;
      if (demo) { res = null; farm.fields.push({ id: slug(name) + "-" + (farm.fields.length + 1), name, crop, acres: Math.round(acresOf(polygon)), polygon: polygon.map(([a, b]) => [b, a]), stats: null }); }
      else res = await FW.fieldsApi.add(farmId, { name, crop, polygon });
      const before = new Set(fieldsOf().map((x) => x.id));
      if (res) applyEdited(res);
      const added = fieldsOf().find((x) => !before.has(x.id)) || fieldsOf().find((x) => x.name === name);
      clearDraft(); $("fname").value = "";
      $("drawMsg").className = "meta"; $("drawMsg").innerHTML = `Added ${esc(name)}. <a href="#plots" data-go="plots">See it in Plots</a>`;
      if (added) selected = added.id;
      fitNext = true; userMoved = false; schedule();
    } catch (e) {
      $("drawMsg").className = e.code === "UNAVAILABLE" ? "meta" : "err"; $("drawMsg").textContent = e.message;
    } finally { saving = false; drawUi(); }
  };
  // rough acres from [lat, lon] corners (demo only; the server computes the real number)
  function acresOf(pts) {
    const lat0 = pts.reduce((a, p) => a + p[0], 0) / pts.length, kx = 111320 * Math.cos(lat0 * Math.PI / 180), ky = 110574;
    let a = 0; for (let i = 0; i < pts.length; i++) { const p = pts[i], q = pts[(i + 1) % pts.length]; a += (p[1] * kx) * (q[0] * ky) - (q[1] * kx) * (p[0] * ky); }
    return Math.abs(a) / 2 / 4046.86;
  }
  $("fname").addEventListener("input", () => { $("drawMsg").textContent = ""; });

  async function saveOutline() {
    const f = fieldsOf().find((x) => x.id === draft.target); if (!f) return cancelRedraw();
    const polygon = draft.pts.map((p) => [p[0], p[1]]);
    saving = true; drawUi();
    try {
      if (demo) f.polygon = polygon.map(([a, b]) => [b, a]);
      else applyEdited(await FW.fieldsApi.update(farmId, f.id, { polygon }));
      plotMsg = { id: f.id, text: "New outline saved.", tone: "" }; selected = f.id; fitNext = true; userMoved = false;
      cancelRedraw(true); go("plots");
    } catch (e) { $("drawMsg").className = e.code === "UNAVAILABLE" ? "meta" : "err"; $("drawMsg").textContent = e.message; }
    finally { saving = false; drawUi(); }
  }
  function startRedraw(f) {
    draft = { pts: [], target: f.id }; clearDraft();
    $("redrawNote").hidden = false; $("redrawNote").textContent = `Redrawing ${f.name}. Click its new corners.`;
    $("drawTitle").textContent = "Redraw a field"; renderMap(); go("draw");
    drawUi();
  }
  function cancelRedraw(quiet) {
    draft = { pts: [], target: null }; redrawDraft();
    $("redrawNote").hidden = true; $("drawTitle").textContent = farmId ? "Add a field" : "Draw your fields";
    if (!quiet) go("plots"); renderMap();
  }

  // ---------- sign-up ----------
  $("start").onclick = async () => {
    $("err").textContent = ""; $("phoneErr").textContent = ""; $("phone").removeAttribute("aria-invalid");
    const digits = $("phone").value.replace(/\D/g, "");
    if (digits.length < 10) {
      $("phoneErr").textContent = "Please add the 10-digit phone number where texts should go.";
      $("phone").setAttribute("aria-invalid", "true"); $("phone").focus(); return;
    }
    saving = true; $("start").disabled = true; $("start").textContent = "Pulling the latest satellite pass...";
    try {
      const body = { phone: $("phone").value, farmer: $("farmer").value.trim() || "Farmer", fields: drafts.map((f) => ({ name: f.name, crop: f.crop, polygon: f.polygon })) };
      const f = await api("/api/farms", { method: "POST", body });
      farmId = f.farm_id;
      const q = new URLSearchParams(location.search); q.set("farm", farmId);
      history.replaceState(null, "", location.pathname + "?" + q.toString() + "#plots");
      enterLive(); applyFarm(f); startFeed(); fitNext = true; userMoved = false; route();
    } catch (e) {
      $("err").textContent = e.message;
    } finally { saving = false; $("start").textContent = "Start watching"; drawUi(); }
  };

  function enterLive() {
    $("signupFields").hidden = true; $("drawRule").hidden = true; $("startRow").hidden = true; $("draftWrap").hidden = true;
    $("drawTitle").textContent = "Add a field";
    draftLayers.forEach((l) => map.removeLayer(l)); draftLayers = []; drafts = [];
    clearDraft(); renderFeedStatus();
  }

  // ---------- farm data (polling / initial load) ----------
  async function refresh() {
    if (!farmId || demo || refreshing) return;
    refreshing = true; const at = epoch;
    try { const f = await api(`/api/farms/${encodeURIComponent(farmId)}`); if (at === epoch) applyFarm(f); }
    catch (e) {
      if (e.status === 404 && !farm) fail("We could not find that farm. Check the link, or sign up again.");
      else if (!farm) fail(e.message);
      else feedNote = e.message;
    } finally { refreshing = false; }
  }
  let feedNote = "";
  function fail(msg) {
    farmId = null; farm = null;
    const q = new URLSearchParams(location.search); q.delete("farm");
    history.replaceState(null, "", location.pathname + (q.toString() ? "?" + q.toString() : "") + "#draw");
    clearTimeout(guardTimer); stopPolling(); if (rt) { rt.close(); rt = null; }
    $("signupFields").hidden = false; $("drawRule").hidden = false; $("startRow").hidden = false; $("draftWrap").hidden = false;
    $("drawTitle").textContent = "Draw your fields"; $("err").textContent = msg;
    renderMap(); route();
  }

  // A farm payload that came back from an edit (add, rename, redraw, delete). It beats any poll still in flight.
  function applyEdited(f) { epoch++; applyFarm(f); }
  async function reloadFarm() { epoch++; applyFarm(await api(`/api/farms/${encodeURIComponent(farmId)}`)); }
  function applyFarm(f) {
    farm = f; feedNote = "";
    thread = (f.thread || []).map((m) => ({ who: m.who, text: m.text, at: m.at, kind: m.kind, sms: m.sms, inv: m.investigation_id }));
    ingestServerSteps(f);
    if (selected && !fieldsOf().some((x) => x.id === selected)) selected = null;
    schedule();
  }

  // Poll mode has no feed: rebuild what we can from the saved farm payload.
  //  - thread steps -> "what we checked"
  //  - diagnoses[] (local harness: what, where, why, dollars, how_sure, action) -> the latest diagnosis per field
  //  - thread items kind "concluded" / "action" with investigation_id + field_id (cloud) -> a bare recommendation
  function ingestServerSteps(f) {
    const counts = {}, th = f.thread || [];
    th.forEach((m, n) => {
      if (!m.investigation_id) return;
      const i = getInv(m.investigation_id);
      if (i.rt) return;
      if (m.kind === "step") { counts[i.id] = (counts[i.id] || 0) + 1; addItem(i, { key: "p" + counts[i.id], kind: "step", who: "", text: m.text, quiet: true }); }
      else if (m.kind === "concluded" && !i.diag) {
        i.fieldId = m.field_id || i.fieldId; i.updated = Date.parse(m.at) || n + 1;
        i.concluded = { summary: m.text || "", from: "thread" };
      } else if (m.kind === "action") i.decision = /declin/i.test(m.text || "") ? "declined" : "approved";
    });
    const ds = f.diagnoses || [], lastFor = {};
    ds.forEach((d, n) => { lastFor[String(d.field_name).toLowerCase()] = n; });
    ds.forEach((d, n) => {
      if (lastFor[String(d.field_name).toLowerCase()] !== n) return;
      const newest = n === ds.length - 1;
      const id = newest ? ((f.pending && f.pending.investigation_id) || lastInvId(th) || "latest") : "diag-" + n;
      const i = getInv(id);
      if (i.rt) return;
      const hyps = (d.hypotheses || []).length ? d.hypotheses : (d.ruled_out || []).map((c) => ({ condition: c, status: "ruled_out" }));
      i.diag = d; i.hyps = hyps; i.fieldName = d.field_name; i.updated = i.updated || n + 1;
      i.where = d.where || ""; i.why = d.why || ""; i.howSure = d.how_sure || "";
      i.concluded = {
        condition: d.condition, summary: d.what || "",
        impact: d.dollars_high ? { low_usd: d.dollars_low, high_usd: d.dollars_high, affected_acres: d.affected_acres } : null,
        action: d.action_detail || d.recommended_action ? { summary: d.action_detail || "", type: d.recommended_action } : null,
      };
      if (newest) {
        i.waiting = !!f.pending;
        if (!f.pending) {
          const mine = th.filter((m) => m.who === "farmer");
          const last = mine.length ? String(mine[mine.length - 1].text || "") : "";
          i.decision = /^\s*(no|n)\b/i.test(last) ? "declined" : /^\s*(yes|y)\b/i.test(last) ? "approved" : "";
        } else i.decision = "";
      }
    });
  }
  const lastInvId = (t) => { for (let k = (t || []).length - 1; k >= 0; k--) if (t[k].investigation_id) return t[k].investigation_id; return null; };

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
  const feedWords = () => (mode === "live" ? "Live" : mode === "poll" ? (demo ? "Demo replay" : "Updating every 10 seconds") : "Connecting");
  function setMode(m) { mode = m; renderFeedStatus(); schedule(); }
  function renderFeedStatus() {
    $("feedBadge").className = "feed " + (mode === "live" ? "live" : mode === "poll" ? "poll" : "");
    $("feedText").textContent = feedWords();
  }

  function getInv(id) {
    id = id || "latest";
    if (!invs.has(id)) invs.set(id, { id, fieldId: null, fieldName: null, items: [], idx: new Map(), hyps: null, concluded: null, rt: false, updated: 0, reportUrl: "", workUrl: "" });
    return invs.get(id);
  }
  function addItem(i, it) {
    const k = it.key;
    if (k && i.idx.has(k)) { Object.assign(i.items[i.idx.get(k)], it); return; }
    it.id = i.id; it.t = Date.now();
    i.items.unshift(it);
    i.idx = new Map(i.items.map((x, n) => [x.key, n]));
  }
  function touch(i, fieldHint, p) {
    i.updated = Date.now(); i.rt = true;
    if (fieldHint) { i.fieldId = fieldHint.field_id || fieldHint.id || i.fieldId; i.fieldName = fieldHint.name || fieldHint.field_name || i.fieldName; }
    if (p) {
      const r = safeUrl(p.report_url || (p.links && p.links.report)), w = safeUrl(p.work_order_url || (p.links && p.links.work_order));
      if (r) i.reportUrl = r; if (w) i.workUrl = w;
    }
  }
  const fieldOf = (p, m) => (p.field && typeof p.field === "object" ? p.field : p.field_id ? { field_id: p.field_id, name: p.field_name } : m.field_id ? { field_id: m.field_id } : null);

  function onFeed(m) {
    if (!m || !m.type) return;
    const p = m.payload || {};
    const id = m.investigation_id || p.investigation_id;
    switch (m.type) {
      case "investigation_step": {
        const i = getInv(id); touch(i, fieldOf(p, m), p);
        const ev = p.evidence;
        addItem(i, ev
          ? { key: "e" + ev.evidence_id, kind: "evidence", who: m.agent || p.agent, text: p.text || ev.reason, why: ev.reason !== p.text ? ev.reason : "", hyp: ev.hypothesis, dir: ev.direction, tool: p.tool || ev.source }
          : { key: p.seq != null ? "s" + p.seq : "t" + m.ts + (p.text || ""), kind: p.step === "done" ? "done" : "step", who: m.agent || p.agent, text: p.text || toolName(p.tool), tool: p.tool });
        if (p.hypotheses) i.hyps = p.hypotheses;
        break;
      }
      case "evidence_added": {
        const i = getInv(id); touch(i, fieldOf(p, m), p);
        const ev = p.evidence || p;
        addItem(i, { key: "e" + ev.evidence_id, kind: "evidence", who: m.agent || p.agent, text: ev.reason, hyp: ev.hypothesis, dir: ev.direction, tool: ev.source });
        break;
      }
      case "hypotheses_updated": {
        const i = getInv(id); touch(i, fieldOf(p, m), p);
        i.hyps = Array.isArray(p) ? p : p.hypotheses || i.hyps;
        break;
      }
      case "concluded": {
        const i = getInv(id); touch(i, fieldOf(p, m), p);
        const dg = p.diagnosis || {};
        if (p.hypotheses) i.hyps = p.hypotheses;
        i.concluded = { condition: dg.condition || p.condition, summary: dg.summary || p.summary || "", impact: p.impact, action: p.proposed_action };
        if (demo && !i.reportUrl) i.reportUrl = "report.html?fixture=1";
        if (i.fieldId) fieldState[i.fieldId] = "bad";
        break;
      }
      case "action_created": {
        const i = getInv(id); touch(i, fieldOf(p, m), p);
        i.decision = p.status === "declined" || /declin/i.test(p.summary || p.text || "") ? "declined" : "approved";
        if (demo && !i.workUrl && i.decision === "approved") i.workUrl = "workorder.html?fixture=1";
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
        const f = fieldOf(p, m); if (f && f.field_id && !fieldState[f.field_id]) fieldState[f.field_id] = "";
        setTimeout(refresh, 1500);
        break;
      }
      case "field_status": {
        const f = fieldOf(p, m); const fid = f && f.field_id;
        if (fid) fieldState[fid] = toneFromFeed(p);
        setTimeout(refresh, 1500);
        break;
      }
      default: break;
    }
    schedule();
  }
  function toneFromFeed(p) {
    const raw = String(p.label || p.status || "").toLowerCase();
    if (/flag|alert|problem|attention|stress|bad|look/.test(raw)) return "bad";
    if (/invest|check|running|scan/.test(raw)) return "busy";
    return "";
  }
  function sweep() {
    if (tab !== "plots" && tab !== "draw") return;
    const el = document.createElement("div"); el.className = "sweep"; $("mapwrap").appendChild(el);
    setTimeout(() => el.remove(), 2800);
  }

  // ---------- status words (always a word beside the color) ----------
  const TONE_WORD = { bad: "Needs a look", busy: "Checking closer", wait: "Waiting for a clear pass", "": "Watching" };
  const invMatches = (i, f) => i.fieldId === f.id || (i.fieldName && i.fieldName.toLowerCase() === f.name.toLowerCase());
  const bestInv = (f) => { let b = null; for (const i of invs.values()) if (i.concluded && invMatches(i, f) && (!b || i.updated >= b.updated)) b = i; return b; };
  // The one place that decides a field's status. The map, the field list, the recommendations and Activity all call it.
  function fieldTone(f) {
    let t = null;
    for (const i of invs.values()) {
      if (invMatches(i, f)) {
        if (i.concluded) return "bad";
        if (i.rt) t = "busy";
      }
    }
    if (t) return t;
    if (fieldState[f.id] != null && fieldState[f.id] !== "good") return fieldState[f.id];
    return f.stats ? "" : "wait";
  }
  const MAP_COLOR = { bad: "#FF6B60", busy: "#F2B24A", wait: "#E6E8E2", "": "#4FD1A1" };
  const statusHtml = (tone) => `<span class="status t-${tone || ""}"><i class="dot" aria-hidden="true"></i>${esc(TONE_WORD[tone] || TONE_WORD[""])}</span>`;

  // ---------- rendering ----------
  function schedule() { if (!renderQueued) { renderQueued = true; requestAnimationFrame(() => { renderQueued = false; render(); }); } }
  function render() {
    renderTabs(); renderFeedStatus();
    if (!farm) {
      renderDrafts();
      if (farmId) setHtml($("plotList"), '<div class="skel" style="width:80%"></div><div class="skel" style="width:60%"></div><div class="skel" style="width:70%"></div>');
      return;
    }
    renderPlots(); renderMap(); renderThread(); renderActivity(); renderFarm();
  }
  function renderTabs() {
    const need = [...invs.values()].some((i) => i.concluded && !viewed.has(i.id));
    $("attn").hidden = !need || tab === "activity";
    $("tab-activity").setAttribute("aria-label", need && tab !== "activity" ? "Activity, something needs a look" : "Activity");
  }

  function imgSrc(f, kind) {
    if (demo) return kind === "truecolor" ? "fixtures/true_color.svg" : "fixtures/health.svg";
    return `${apiBase()}/api/farms/${encodeURIComponent(farm.farm_id)}/fields/${encodeURIComponent(f.id)}/${kind}.png?v=${encodeURIComponent((f.stats && f.stats.scene_date) || "")}`;
  }
  const lastPass = (f) => (f.stats && f.stats.scene_date) || f.last_scene_date || "";

  const CROPS = ["corn", "soybeans", "wheat", "milo", "alfalfa"];
  const passDate = (d) => (d ? FW.niceDate(d) : "");

  function plotRow(f) {
    const tone = fieldTone(f), sel = f.id === selected, s = f.stats;
    let h = `<li class="${sel ? "sel" : ""}"><button class="rowbtn" type="button" data-sel="${esc(f.id)}" aria-expanded="${sel}">
      <span class="rowtop"><span class="rowname">${esc(f.name)}</span>${statusHtml(tone)}</span>
      <span class="meta">${esc(f.crop)}${f.acres ? ` &middot; ${Math.round(f.acres)} acres` : ""} &middot; ${lastPass(f) ? `last pass ${esc(passDate(lastPass(f)))}` : "no clear pass yet"}</span></button>`;
    if (sel) {
      h += `<div class="detail">`;
      if (!s) h += `<p class="quiet">No clear pass yet. We will check again when the sky is clear.</p>`;
      else {
        h += `<p class="meta">Weakest in the ${esc(s.weakest_area)}. ${Math.round((s.share_of_field_below_0_3 || 0) * 100)}% of the field looks bare or stressed.</p>
          <div class="pair"><figure><img loading="lazy" alt="Satellite photo of ${esc(f.name)}" src="${esc(imgSrc(f, "truecolor"))}" onerror="this.style.visibility='hidden'"><figcaption>What the satellite sees</figcaption></figure>
          <figure><img loading="lazy" alt="Crop health of ${esc(f.name)}" src="${esc(imgSrc(f, "health"))}" onerror="this.style.visibility='hidden'"><figcaption>Crop health: green strong, brown weak</figcaption></figure></div>`;
      }
      h += `<div class="actions"><button class="btn secondary" type="button" data-ask="${esc(f.id)}" ${askBusy ? "disabled" : ""}>${askBusy === f.id ? "Asking..." : "Ask FieldWatch about this field"}</button></div>
        <div class="editgrid"><div><label for="ename">Name</label><input id="ename" value="${esc(f.name)}" autocomplete="off"></div>
        <div><label for="ecrop">Crop</label><select id="ecrop">${CROPS.concat(CROPS.includes(f.crop) ? [] : [f.crop]).map((c) => `<option${c === f.crop ? " selected" : ""}>${esc(c)}</option>`).join("")}</select></div></div>
        <div class="actions"><button class="btn secondary" type="button" data-save="${esc(f.id)}" ${saving ? "disabled" : ""}>${saving ? "Saving..." : "Save changes"}</button>
          <button class="btn secondary" type="button" data-redraw="${esc(f.id)}">Redraw outline</button></div>`;
      if (confirmDel === f.id) h += `<p class="step">Stop watching ${esc(f.name)}? This cannot be undone.</p>
        <div class="actions"><button class="btn danger" type="button" data-del-yes="${esc(f.id)}">Delete ${esc(f.name)}</button><button class="linkbtn" type="button" data-del-no>Keep it</button></div>`;
      else h += `<div class="actions"><button class="linkbtn bad" type="button" data-del="${esc(f.id)}">Delete this field</button></div>`;
      if (plotMsg && plotMsg.id === f.id) h += `<p class="${plotMsg.tone === "bad" ? "err" : "meta"}" role="status">${esc(plotMsg.text)}</p>`;
      h += `</div>`;
    }
    return h + `</li>`;
  }

  // ---------- recommendations (Plots hero) ----------
  // Everything below comes from the farm payload and the feed. Nothing is made up: a row that has no data is left out.
  const SURE = { pretty: 0.8, fairly: 0.55 }; // same cut-offs as how_sure_phrase() in agent/fieldwatch/schemas.py
  const topHyp = (i) => { const hs = i.hyps || []; return hs.find((h) => h.status === "likely") || [...hs].sort((a, b) => (b.probability || 0) - (a.probability || 0)).find((h) => h.status !== "ruled_out") || null; };
  function sureText(i) {
    const top = topHyp(i), pct = top && top.probability != null ? Math.round(top.probability * 100) : null;
    let word = i.howSure;
    if (!word && pct != null) word = top.probability >= SURE.pretty ? "pretty sure" : top.probability >= SURE.fairly ? "fairly sure" : "not sure, need a photo";
    if (!word) return "";
    return capital(word) + (pct != null && !/photo/.test(word) ? ` (${pct}%)` : "");
  }
  function whyText(i) {
    if (i.why) return i.why;
    const top = topHyp(i), parts = [];
    const sup = i.items.filter((x) => x.kind === "evidence" && x.dir && x.dir !== "refutes" && (!top || !x.hyp || x.hyp === top.condition)).map((x) => x.why || x.text).filter(Boolean);
    if (sup.length) parts.push(sup[sup.length - 1]);
    const out = (i.hyps || []).filter((h) => h.status === "ruled_out").map((h) => condition(h.condition).toLowerCase());
    if (out.length) parts.push(`We ruled out ${out.join(", ")}.`);
    return parts.join(" ");
  }
  const ACTION_WORD = { work_order: "Repair ticket", repair_work_order: "Repair ticket", scout_pin: "Scouting pin", scouting_pin: "Scouting pin", photo_request: "Photo request", request_photo: "Photo request", insurance_notice: "Insurance notice" };
  function recState(i) {
    const a = i.concluded && i.concluded.action, type = a && (a.action_type || a.type);
    if (type === "none") return "none";
    if (i.decision === "declined") return "declined";
    if (i.decision === "approved" || i.workUrl) return "approved";
    return "waiting";
  }
  function recHtml(f, i) {
    const c = i.concluded, imp = c.impact, a = c.action, st = recState(i), type = a && (a.action_type || a.type);
    const where = i.where || (f.stats && f.stats.weakest_area ? `Weakest in the ${f.stats.weakest_area}.` : "");
    const share = f.stats && f.stats.share_of_field_below_0_3 ? `About ${Math.round(f.stats.share_of_field_below_0_3 * 100)}% of the field looks bare or stressed.` : "";
    const why = whyText(i), sure = sureText(i);
    const stateWord = { waiting: "Waiting for your YES", approved: "Approved", declined: "You said no", none: "" }[st];
    const row = (k, v) => (v ? `<div><dt>${k}</dt><dd>${v}</dd></div>` : "");
    let h = `<article class="rec" aria-label="${esc(f.name)}">
      <div class="rec-top"><h3>${esc(f.name)}</h3>${statusHtml("bad")}</div>
      <p class="rec-what">${esc(c.condition ? condition(c.condition) : "Something looks wrong")}${c.summary ? `. ${esc(c.summary)}` : "."}</p>
      <dl class="facts">
        ${row("Where", esc([where, share].filter(Boolean).join(" ")))}
        ${row("Why we think so", esc(why))}
        ${row("How sure we are", esc(sure))}
        ${row("At stake", imp ? `<strong>${esc(FW.usd(imp.low_usd))} to ${esc(FW.usd(imp.high_usd))}</strong> if nothing is done${imp.affected_acres ? ` (about ${esc(Math.round(imp.affected_acres))} acres)` : ""}` : "")}
        ${row("Next step", a && a.summary ? `${esc(a.summary)}${ACTION_WORD[type] ? ` <span class="quiet">(${esc(ACTION_WORD[type])})</span>` : ""}` : "")}
        ${row("Where it stands", stateWord ? `<strong class="st-${st}">${esc(stateWord)}</strong>` : "")}
      </dl><div class="actions">`;
    if (st === "waiting") h += `<a class="btn primary" href="#text" data-go="text">Reply YES in Text</a>`;
    h += `<button class="btn secondary" type="button" data-ask="${esc(f.id)}" ${askBusy ? "disabled" : ""}>${askBusy === f.id ? "Asking..." : "Ask about this field"}</button>`;
    h += `<button class="linkbtn" type="button" data-show="${esc(f.id)}">Show on map</button>`;
    h += `</div>`;
    const links = [i.reportUrl ? `<a href="${esc(i.reportUrl)}">Read the full report</a>` : "", i.workUrl ? `<a href="${esc(i.workUrl)}">Open the work order</a>` : ""].filter(Boolean).join("");
    if (links) h += `<div class="links">${links}</div>`;
    return h + `</article>`;
  }
  function renderRecs() {
    const fs = fieldsOf(), el = $("plotRecs");
    if (!fs.length) { setHtml(el, `<h1 class="display">Latest check</h1><p class="empty">No fields yet. <button class="linkbtn" type="button" data-go="draw">Draw your first one.</button></p>`); return; }
    const dates = fs.map(lastPass).filter(Boolean).sort();
    const latest = dates[dates.length - 1];
    const urgent = [], busy = [], calm = [], waiting = [];
    fs.forEach((f) => {
      const t = fieldTone(f), i = t === "bad" ? bestInv(f) : null;
      if (i) urgent.push({ f, i }); else if (t === "busy") busy.push(f); else if (t === "wait") waiting.push(f); else calm.push(f);
    });
    const rank = (x) => ({ waiting: 0, approved: 1, declined: 2, none: 3 }[recState(x.i)]);
    const money = (x) => (x.i.concluded.impact && x.i.concluded.impact.high_usd) || 0;
    urgent.sort((a, b) => rank(a) - rank(b) || money(b) - money(a));
    const n = urgent.length;
    let h = `<h1 class="display">Latest check</h1>
      <p class="lead">${latest ? `Most recent satellite pass: <strong>${esc(passDate(latest))}</strong>. ` : "No clear satellite pass yet. "}${n ? `${n === 1 ? "One field needs" : n + " fields need"} your attention.` : latest ? "Nothing needs your attention." : "We will check again when the sky is clear."}</p>`;
    if (n) h += `<h2 class="section">${n === 1 ? "What to do" : "What to do, most urgent first"}</h2>` + urgent.map((x) => recHtml(x.f, x.i)).join("");
    const quiet = [];
    busy.forEach((f) => quiet.push(`<li><strong>${esc(f.name)}</strong> <span class="quiet">We are checking this one more closely now.</span></li>`));
    waiting.forEach((f) => quiet.push(`<li><strong>${esc(f.name)}</strong> <span class="quiet">Waiting for a clear satellite pass.</span></li>`));
    calm.forEach((f) => quiet.push(`<li><strong>${esc(f.name)}</strong> <span class="quiet">Looks healthy${lastPass(f) ? ` as of ${esc(passDate(lastPass(f)))}` : ""}.</span></li>`));
    if (quiet.length) h += `${n ? `<h2 class="section">No action needed</h2>` : ""}<ul class="calm">${quiet.join("")}</ul>`;
    setHtml(el, h);
  }
  function renderLegend() {
    const order = ["bad", "busy", "", "wait"], tones = [...new Set(fieldsOf().map(fieldTone))].sort((a, b) => order.indexOf(a) - order.indexOf(b));
    setHtml($("mapLegend"), tones.map((t) => statusHtml(t)).join(""));
  }
  function renderPlots() {
    renderRecs(); renderLegend();
    const el = $("plotList");
    const a = document.activeElement;
    if (el.contains(a) && a.matches("input,select")) { deferPlots = true; return; }
    deferPlots = false;
    const fs = fieldsOf();
    setHtml(el, fs.length ? `<ul class="rows">${fs.map(plotRow).join("")}</ul>` : `<p class="empty">No fields yet. <button class="linkbtn" type="button" data-go="draw">Draw your first one.</button></p>`);
    if (farm.last_checked) $("checked").textContent = `Last checked ${String(farm.last_checked).replace("T", " ")}. We check again every 10 minutes.`;
  }
  $("plotList").addEventListener("focusout", () => { setTimeout(() => { if (deferPlots) schedule(); }, 0); });

  function select(id, zoom) {
    selected = selected === id && !zoom ? null : id; confirmDel = null; plotMsg = null;
    if (selected) { sheetUp(); const l = layers[selected]; if (l && zoom !== false) map.fitBounds(l.poly.getBounds().pad(0.4)); }
    schedule();
    if (selected) setTimeout(() => { const r = document.querySelector(`#plotList [data-sel="${CSS.escape(selected)}"]`); if (r) r.scrollIntoView({ block: "nearest" }); }, 60);
  }

  async function editAction(f, label, fn, okText) {
    saving = true; plotMsg = { id: f.id, text: label, tone: "" }; schedule();
    try {
      const idx = fieldsOf().findIndex((x) => x.id === f.id);
      const r = await fn();
      if (r) applyEdited(r);
      if (selected === f.id && !fieldsOf().some((x) => x.id === f.id) && okText && fieldsOf()[idx]) { selected = fieldsOf()[idx].id; f = fieldsOf()[idx]; }
      plotMsg = okText ? { id: f.id, text: okText, tone: "" } : null;
    } catch (e) { plotMsg = { id: f.id, text: e.message, tone: e.code === "UNAVAILABLE" ? "" : "bad" }; }
    finally { saving = false; schedule(); }
  }
  $("v-plots").addEventListener("click", async (e) => {
    const t = e.target;
    const sel = t.closest("[data-sel]"); if (sel) return select(sel.dataset.sel);
    const show = t.closest("[data-show]"); if (show) { select(show.dataset.show, true); $("mapSlot").scrollIntoView({ block: "nearest", behavior: "smooth" }); return; }
    const f = (id) => fieldsOf().find((x) => x.id === id);
    let b;
    if ((b = t.closest("[data-ask]"))) {
      const fl = f(b.dataset.ask); askBusy = fl.id; plotMsg = null; schedule();
      try {
        if (demo) runDemo(); else await api(`/api/farms/${encodeURIComponent(farmId)}/fields/${encodeURIComponent(fl.id)}/ask`, { method: "POST" });
        plotMsg = { id: fl.id, text: "Asked. The answer will show up in Text.", tone: "" };
        if (!demo) refresh();
      } catch (er) { plotMsg = { id: fl.id, text: er.message, tone: "bad" }; }
      askBusy = null; schedule(); return;
    }
    if ((b = t.closest("[data-save]"))) {
      const fl = f(b.dataset.save), name = $("ename").value.trim(), crop = $("ecrop").value;
      if (!name) { plotMsg = { id: fl.id, text: "Give the field a name first.", tone: "bad" }; return schedule(); }
      if (name === fl.name && crop === fl.crop) { plotMsg = { id: fl.id, text: "Nothing changed.", tone: "" }; return schedule(); }
      return editAction(fl, "Saving...", async () => {
        if (demo) { fl.name = name; fl.crop = crop; return null; }
        return FW.fieldsApi.update(farmId, fl.id, { name, crop });
      }, "Saved.");
    }
    if ((b = t.closest("[data-redraw]"))) return startRedraw(f(b.dataset.redraw));
    if ((b = t.closest("[data-del]"))) { confirmDel = b.dataset.del; return schedule(); }
    if (t.closest("[data-del-no]")) { confirmDel = null; return schedule(); }
    if ((b = t.closest("[data-del-yes]"))) {
      const fl = f(b.dataset.delYes);
      return editAction(fl, "Deleting...", async () => {
        if (demo) { farm.fields = farm.fields.filter((x) => x.id !== fl.id); return null; }
        const r = await FW.fieldsApi.remove(farmId, fl.id); return r;
      }, "").then(() => { confirmDel = null; if (!fieldsOf().some((x) => x.id === fl.id)) selected = null; schedule(); });
    }
  });
  $("btnCheck").onclick = async () => {
    if (busy || !farmId) return;
    busy = true; const b = $("btnCheck"); b.disabled = true; b.textContent = "Pulling the latest satellite pass..."; $("checkMsg").textContent = "";
    try {
      if (demo) { await new Promise((r) => setTimeout(r, 900)); $("checkMsg").className = "meta"; $("checkMsg").textContent = "Demo: no new satellite pass."; }
      else { const r = await api(`/api/farms/${encodeURIComponent(farmId)}/check`, { method: "POST" }); $("checkMsg").className = "meta"; if (r && r.thread) applyFarm(r); else await refresh(); }
    } catch (e) { $("checkMsg").className = "err"; $("checkMsg").innerHTML = `${esc(e.message)} <button class="linkbtn" type="button" id="retryCheck">Try again</button>`; }
    finally { busy = false; b.disabled = false; b.textContent = "Check satellite now"; }
  };
  $("checkMsg").addEventListener("click", (e) => { if (e.target.id === "retryCheck") $("btnCheck").click(); });

  // map polygons
  const layers = {};
  function fitFields() {
    const ls = Object.values(layers).map((l) => l.poly);
    map.invalidateSize();
    if (ls.length) { map.fitBounds(L.featureGroup(ls).getBounds().pad(0.2), { animate: false }); fitNext = false; }
  }
  function renderMap() {
    const fs = fieldsOf(), ids = new Set(fs.map((f) => f.id));
    for (const id of Object.keys(layers)) if (!ids.has(id)) { map.removeLayer(layers[id].halo); map.removeLayer(layers[id].poly); delete layers[id]; }
    fs.forEach((f) => {
      if (!f.polygon || f.polygon.length < 3) return;
      const tone = fieldTone(f), col = MAP_COLOR[tone], sig = JSON.stringify(f.polygon), isSel = f.id === selected, hide = draft.target === f.id;
      const latlng = f.polygon.map(([lon, lat]) => [lat, lon]);
      let l = layers[f.id];
      if (l && l.sig !== sig) { map.removeLayer(l.halo); map.removeLayer(l.poly); l = null; }
      if (!l) {
        const halo = L.polygon(latlng, { color: "#fff", weight: 7, opacity: 0.85, fill: false, interactive: false }).addTo(map);
        const poly = L.polygon(latlng, { color: col, weight: 3, fillOpacity: 0.2 }).bindTooltip("", { permanent: true, direction: "center", className: "fwtip" }).addTo(map);
        poly.on("click", () => { if (tab === "plots") select(f.id, true); });
        l = layers[f.id] = { halo, poly, sig };
        fitNext = fitNext || false;
      }
      l.poly.setStyle({ color: col, weight: isSel ? 5 : 3, fillOpacity: isSel ? 0.3 : 0.2, opacity: hide ? 0 : 1, fillColor: col });
      l.halo.setStyle({ opacity: hide ? 0 : 0.85, weight: isSel ? 9 : 7 });
      l.poly.setTooltipContent(esc(f.name));
    });
    if (fitNext && Object.keys(layers).length && (tab === "plots" || tab === "draw")) fitFields();
  }

  // ---------- Text ----------
  function renderThread() {
    const el = $("thread");
    const atBottom = threadFirst || el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    let h = "", prev = null;
    if (farm && farm.muted) h += `<p class="sys">Texts paused (STOP). Reply START to resume.</p>`;
    thread.forEach((m) => {
      if (m.who === "agent" || m.kind === "step") { h += `<p class="sys">${esc(m.text)}</p>`; prev = null; return; }
      const me = m.who === "farmer", cont = prev === m.who;
      h += `<div class="msg ${me ? "me" : "fw"}${cont ? " cont" : ""}">${cont ? "" : `<span class="who">${me ? "You" : "FieldWatch"} <span class="when">${esc(clockTime(m.at))}${m.sms && m.sms.ok ? " &middot; texted" : ""}</span></span>`}${esc(m.text)}</div>`;
      prev = m.who;
    });
    if (!thread.length) h += `<p class="hint">Nothing yet. FieldWatch texts you after a clear satellite pass, or you can type ASK.</p>`;
    if (threadErr) h += `<p class="sys bad" role="alert">${esc(threadErr)} <button class="linkbtn" type="button" data-retry-send>Try again</button></p>`;
    setHtml(el, `<div class="thread-in">${h}</div>`);
    if (atBottom && tab === "text") { el.scrollTop = el.scrollHeight; threadFirst = false; }
  }

  let lastSend = "";
  async function call(path, body, localText) {
    if (busy || !farmId) return;
    busy = true; threadErr = ""; $("btnSend").disabled = true;
    if (localText) thread.push({ who: "farmer", text: localText, at: new Date().toISOString(), kind: "msg" });
    threadFirst = true; schedule();
    try {
      if (demo) { await demoAction(localText || "ASK"); return; }
      const r = await api(path, { method: "POST", body });
      if (r && r.thread) applyFarm(r); else await refresh();
    } catch (e) { threadErr = e.message; }
    finally { busy = false; $("btnSend").disabled = false; schedule(); if (!demo) refresh(); }
  }
  const reply = (text) => { lastSend = text; return call(`/api/farms/${encodeURIComponent(farmId)}/reply`, { text }, text); };
  $("replyForm").onsubmit = (e) => { e.preventDefault(); const t = $("replyText").value.trim(); if (t) { $("replyText").value = ""; reply(t); } };
  $("thread").addEventListener("click", (e) => { if (e.target.closest("[data-retry-send]") && lastSend) { thread.pop(); reply(lastSend); } });

  // ---------- Activity ----------
  function invField(i) { return fieldsOf().find((f) => f.id === i.fieldId || (i.fieldName && f.name.toLowerCase() === i.fieldName.toLowerCase())); }
  function renderActivity() {
    if (tab === "activity") for (const i of invs.values()) if (i.concluded) viewed.add(i.id);
    const fs = fieldsOf();
    let list = [...invs.values()].filter((i) => i.items.length || i.hyps || i.concluded).sort((a, b) => b.updated - a.updated);
    if (actFilter !== "all") list = list.filter((i) => { const f = invField(i); return f && f.id === actFilter; });
    let h = `<h1 class="display">Activity</h1>`;
    if (fs.length > 1) h += `<div class="filters" role="group" aria-label="Filter by field"><button type="button" data-f="all" aria-pressed="${actFilter === "all"}">All fields</button>${fs.map((f) => `<button type="button" data-f="${esc(f.id)}" aria-pressed="${actFilter === f.id}">${esc(f.name)}</button>`).join("")}</div>`;
    h += `<div class="block"><h2>Your fields right now</h2>` + (fs.length ? fs.filter((f) => actFilter === "all" || f.id === actFilter).map((f) => `<div class="fieldrow"><strong>${esc(f.name)}</strong>${statusHtml(fieldTone(f))}</div>`).join("") : `<p class="empty">No fields yet.</p>`) + `</div>`;
    if (!list.length) h += `<div class="block"><p class="empty">Nothing is being checked right now.</p></div>`;
    list.forEach((i, n) => { h += invHtml(i, n === 0); });
    setHtml($("activityBody"), h);
  }
  function invHtml(i, first) {
    const open = closedInv.has(i.id) ? false : openInv.has(i.id) || first;
    const f = invField(i), title = (i.concluded && i.concluded.condition ? condition(i.concluded.condition) + ": " : "") + (i.fieldName || (f && f.name) || "Latest check");
    let h = `<details class="block" data-inv="${esc(i.id)}"${open ? " open" : ""}><summary class="invsum"><h2>${esc(title)}</h2></summary>`;
    if (i.concluded) {
      const c = i.concluded, imp = c.impact;
      h += `<div class="found"><div class="big">${esc(c.condition ? condition(c.condition) : "What we found")}</div>${c.summary ? `<p>${esc(c.summary)}</p>` : ""}
        ${imp ? `<div class="money">${esc(FW.usd(imp.low_usd))} to ${esc(FW.usd(imp.high_usd))}</div><p class="meta">at risk if nothing is done</p>` : ""}
        ${c.action && c.action.summary ? `<p>Next step: ${esc(c.action.summary)}</p>` : ""}</div>`;
      const links = [i.reportUrl ? `<a href="${esc(i.reportUrl)}">Open the full report</a>` : "", i.workUrl ? `<a href="${esc(i.workUrl)}">Open the work order</a>` : ""].filter(Boolean).join("");
      if (links) h += `<div class="links">${links}</div>`;
    }
    if (i.hyps && i.hyps.length) {
      const order = { likely: 0, open: 1, ruled_out: 2 };
      const hs = [...i.hyps].sort((a, b) => (order[a.status] - order[b.status]) || ((b.probability || 0) - (a.probability || 0)));
      h += `<h3>How sure we are</h3>` + hs.map((x) => {
        const pct = x.probability != null ? Math.round(x.probability * 100) : null;
        const right = x.status === "ruled_out" ? "ruled out" : x.status === "likely" ? (pct != null ? `${pct}% sure, most likely` : "most likely") : pct != null ? `${pct}%` : "still possible";
        return `<div class="hyp ${esc(x.status)}"><div class="nm"><span>${esc(condition(x.condition))}</span><span>${esc(right)}</span></div>${pct != null ? `<div class="bar" role="img" aria-label="${pct} percent"><i style="width:${pct}%"></i></div>` : ""}</div>`;
      }).join("");
    }
    if (i.items.length) {
      h += `<h3>What we checked</h3><ul class="steps">` + i.items.map((it) => {
        const k = i.id + "|" + it.key, isNew = !seen.has(k); seen.add(k);
        const tag = it.kind === "evidence" && it.dir ? `<div class="tagline">${it.dir === "refutes" ? "Rules out" : "Points to"} ${esc(condition(it.hyp))}</div>` : it.tag ? `<div class="tagline">${esc(it.tag)}</div>` : "";
        return `<li class="${isNew && !it.quiet ? "new" : ""}">${it.who ? `<div class="who">${esc(agent(it.who))}</div>` : ""}<div>${esc(it.text)}</div>${it.why ? `<div class="why">${esc(it.why)}</div>` : ""}${tag}</li>`;
      }).join("") + `</ul>`;
    }
    return h + `</details>`;
  }
  $("activityBody").addEventListener("click", (e) => { const b = e.target.closest("[data-f]"); if (b) { actFilter = b.dataset.f; schedule(); } });
  $("activityBody").addEventListener("toggle", (e) => {
    const d = e.target; if (!d.dataset || !d.dataset.inv) return;
    if (d.open) { openInv.add(d.dataset.inv); closedInv.delete(d.dataset.inv); } else { openInv.delete(d.dataset.inv); closedInv.add(d.dataset.inv); }
  }, true);

  // ---------- Farm ----------
  function renderFarm() {
    const theme = document.documentElement.dataset.theme || "auto";
    const link = location.origin + location.pathname + "?farm=" + encodeURIComponent(farmId);
    const h = `<h1 class="display">Your farm</h1>
      <dl class="kv" style="margin-top:24px">
        <div><dt>Name</dt><dd>${esc(farmerName() || "Not set")}</dd></div>
        <div><dt>Phone</dt><dd>${esc((farm && farm.phone) || "Not set")}</dd></div>
        <div><dt>Texts</dt><dd>${farm && farm.muted ? "Paused (you replied STOP)" : "On"}</dd></div>
        <div><dt>Connection</dt><dd><span class="status t-${mode === "live" ? "" : mode === "poll" ? "busy" : "wait"}"><i class="dot" aria-hidden="true"></i>${esc(feedWords())}</span></dd></div>
      </dl>
      ${farm && farm.muted ? `<p class="meta" style="margin-top:8px">Reply START to your FieldWatch number to turn texts back on.</p>` : ""}
      ${feedNote ? `<p class="err">${esc(feedNote)} Showing the last information we have.</p>` : ""}
      <h2 class="section">Appearance</h2>
      <div class="themes" role="group" aria-label="Color theme">${["auto", "light", "dark"].map((t) => `<button type="button" data-theme="${t}" aria-pressed="${theme === t}">${capital(t)}</button>`).join("")}</div>
      <h2 class="section">This farm</h2>
      <div class="actions" style="margin-top:0"><button class="btn secondary" type="button" id="copyLink" data-link="${esc(link)}">Copy link to this farm</button></div>
      <p class="meta" id="copyMsg" role="status"></p>
      <p class="meta" style="margin-top:24px">People who get texts and alert preferences will show up here later.</p>
      <p style="margin-top:16px"><a href="${esc(location.pathname)}">Set up a different farm</a></p>`;
    const el = $("farmBody");
    if (el.contains(document.activeElement) && el.__h && el.__h !== h && document.activeElement.id === "copyLink") return;
    setHtml(el, h);
  }
  $("farmBody").addEventListener("click", async (e) => {
    const t = e.target.closest("[data-theme]");
    if (t) {
      const v = t.dataset.theme;
      if (v === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = v;
      try { v === "auto" ? localStorage.removeItem("fw-theme") : localStorage.setItem("fw-theme", v); } catch (er) { /* storage blocked */ }
      return schedule();
    }
    const c = e.target.closest("#copyLink");
    if (c) {
      try { await navigator.clipboard.writeText(c.dataset.link); $("copyMsg").textContent = "Link copied."; } catch (er) { $("copyMsg").textContent = c.dataset.link; }
    }
  });

  // ---------- demo ----------
  async function demoAction(text) {
    const up = text.toUpperCase();
    if (up === "ASK") return runDemo();
    const say = up === "YES" ? "Done. I sent the repair ticket to your brother with a map pin." : up === "NO" ? "Okay, I will not send anything." : up === "MORE" ? "The weak area is a ring on the outer edge, where the pivot's last span runs. Rain, hail and soil were checked and ruled out." : "Got it.";
    await new Promise((r) => setTimeout(r, 400));
    if (up === "YES" || up === "NO") { const i = [...invs.values()].find((x) => x.concluded); if (i) onFeed({ type: "action_created", ts: new Date().toISOString(), farm_id: "demo", investigation_id: i.id, payload: { status: up === "YES" ? "approved" : "declined", summary: up === "YES" ? "Repair ticket sent." : "You said no." } }); }
    onFeed({ type: "message_out", ts: new Date().toISOString(), farm_id: "demo", payload: { body: say } });
  }
  let demoTimers = [];
  async function runDemo(instant) {
    demoTimers.forEach(clearTimeout); demoTimers = [];
    invs.clear(); Object.keys(fieldState).forEach((k) => delete fieldState[k]);
    const script = await FW.loadFixture("feed_replay.json");
    const speed = Number(params.get("speed")) || 1;
    if (instant === true) script.forEach((s) => onFeed(s.msg)); // everything already happened: Plots shows the finished recommendation at once
    else script.forEach((s) => demoTimers.push(setTimeout(() => onFeed(s.msg), s.at_ms / speed)));
    schedule();
  }

  // ---------- start-up ----------
  async function boot() {
    drawUi(); renderDrafts();
    if (demo) {
      farm = await FW.loadFixture("farm.json"); farmId = farm.farm_id;
      thread = (farm.thread || []).map((m) => ({ who: m.who, text: m.text, at: m.at, kind: "seed" }));
      enterLive(); mode = "poll"; renderFeedStatus(); route(); schedule(); setTimeout(() => runDemo(!params.get("speed") && params.get("replay") !== "1"), 300);
      return;
    }
    farmId = params.get("farm");
    if (farmId) { enterLive(); startFeed(); }
    route();
  }
  boot().catch((e) => { $("err").textContent = e.message; });
})();
