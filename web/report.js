/* Field report page: report.html?inv=<investigation_id>&t=<token>
   Renders contracts.ReportView from GET /v1/reports/{id}?t=...
   Approve / Decline -> POST /v1/approvals/{approval_id}/decision?t=...
   Dev: ?fixture=1 (normal) | decided | declined | expired | race (decision says ALREADY_DECIDED). */
(function () {
  "use strict";
  const { $, esc, api, params, safeUrl, usd, niceDate, condition, explain } = FW;
  const page = $("page");
  const inv = params.get("inv") || "", token = params.get("t") || "";
  const fixture = params.get("fixture");
  let view = null;

  const STATUS_WORDS = { likely: "Most likely", ruled_out: "Ruled out", open: "Still possible" };

  function block(cls, title, body) {
    return `<div class="banner ${cls}"><div>${esc(title)}</div>${body ? `<div class="small" style="font-weight:400;color:inherit;margin-top:6px">${esc(body)}</div>` : ""}</div>`;
  }

  async function load() {
    if (fixture) {
      if (fixture === "expired") { const e = new Error("expired"); e.code = "TOKEN_EXPIRED"; throw e; }
      const v = await FW.loadFixture("report_view.json");
      v.overlays.true_color_url = "fixtures/true_color.svg";
      v.overlays.health_url = "fixtures/health.svg";
      v.overlays.mask_url = "fixtures/mask.svg";
      if (fixture === "decided") v.action = { ...v.action, status: "approved", decided_by: "dad" };
      if (fixture === "declined") v.action = { ...v.action, status: "declined", decided_by: "dad" };
      return v;
    }
    if (!inv || !token) { const e = new Error("incomplete"); e.code = "NOT_FOUND"; throw e; }
    return api(`/v1/reports/${encodeURIComponent(inv)}?t=${encodeURIComponent(token)}`);
  }

  function render(v) {
    view = v;
    const a = v.action;
    let h = `<div><h1>${esc(v.summary.headline)}</h1><p class="lead">${esc(v.summary.detail)}</p>
      <p class="small">${esc(v.field.name)} &middot; ${Math.round(v.field.acres)} acres &middot; satellite pass of ${esc(niceDate(v.scene_date))}</p></div>`;
    if (v.updated_since_sent) h += block("warn", "This report was updated after we texted you.", "What you see here is the latest.");

    h += `<div class="block"><h2>See the problem</h2>
      <div class="reveal" id="reveal">
        <img id="imgBase" alt="Satellite photo of the field" src="${esc(safeUrl(v.overlays.true_color_url))}">
        <img id="imgTop" class="top" alt="Crop health view of the field" src="${esc(safeUrl(v.overlays.health_url))}">
        ${v.overlays.mask_url ? `<img id="imgMask" class="mask" alt="Problem area outlined" src="${esc(safeUrl(v.overlays.mask_url))}">` : ""}
        <div class="handle" id="handle"></div>
        <span class="cap l">What the satellite sees</span><span class="cap r">Crop health</span>
      </div>
      <input class="slider-range" id="range" type="range" min="0" max="100" value="50" aria-label="Slide between the satellite photo and the crop health view">
      <p class="small" style="margin:0">Drag the line left and right. Green is strong crop, brown is weak.</p>
      ${v.overlays.mask_url ? `<label class="toggle" for="maskOn"><input type="checkbox" id="maskOn" checked> Show the problem area</label>` : ""}
    </div>`;

    if (v.impact) {
      h += `<div class="block"><h2>What is at risk</h2>
        <div class="money">${usd(v.impact.low_usd)} to ${usd(v.impact.high_usd)}</div>
        <p style="margin:6px 0 0">about ${Math.round(v.impact.affected_acres * 10) / 10} acres, if nothing is done.</p>
        <details style="margin-top:8px"><summary class="small">How we figured this</summary><p class="small">${esc(v.impact.basis)}</p></details></div>`;
    }

    h += `<div id="decision">${decisionHtml(a)}</div>`;

    h += `<div class="block"><h2>What we checked</h2><ul class="checked">` + v.checked.map((c) =>
      `<li class="${esc(c.status)}"><div class="t"><span class="n">${esc(c.label || condition(c.condition))}</span><span class="s ${esc(c.status)}">${esc(STATUS_WORDS[c.status] || c.status)}</span></div><div class="r">${esc(c.reason)}</div></li>`).join("") + `</ul></div>`;

    const nav = safeUrl(v.navigate_url);
    if (nav) h += `<a class="btn secondary" href="${esc(nav)}" target="_blank" rel="noopener">Navigate here</a>`;
    if (fixture) h += `<p class="small">Sample data for testing.</p>`;
    page.innerHTML = h;
    wireReveal();
    wireDecision();
  }

  function decisionHtml(a) {
    if (!a) return "";
    if (a.status === "approved") return block("ok", "Approved" + (a.decided_by ? " by " + a.decided_by : "") + ".", "We are on it: " + a.label + ".");
    if (a.status === "declined") return block("warn", "Declined" + (a.decided_by ? " by " + a.decided_by : "") + ".", "Nothing will be sent. We will keep watching the field.");
    if (a.status === "expired") return block("warn", "This request has expired.", "We will keep watching and text you if the field needs a decision.");
    return `<div class="block"><h2>${esc(a.label)}?</h2>
      <div class="btn-row two"><button class="btn primary" id="approve">Approve</button><button class="btn secondary" id="decline">Decline</button></div>
      <p class="small" id="decisionMsg" role="status"></p></div>`;
  }

  function wireDecision() {
    const ap = $("approve"), de = $("decline");
    if (!ap) return;
    const go = (d) => async () => {
      ap.disabled = de.disabled = true; $("decisionMsg").textContent = "Sending...";
      try {
        if (fixture) {
          if (fixture === "race") { const e = new Error("Already decided by Mom."); e.code = "ALREADY_DECIDED"; throw e; }
        } else {
          await api(`/v1/approvals/${encodeURIComponent(view.action.approval_id)}/decision?t=${encodeURIComponent(token)}`,
            { method: "POST", body: { decision: d } });
        }
        view.action = { ...view.action, status: d === "approve" ? "approved" : "declined", decided_by: "you" };
        $("decision").innerHTML = decisionHtml(view.action);
      } catch (e) {
        if (e.code === "ALREADY_DECIDED") {
          let who = e.message || "";
          if (!fixture) { try { render(await load()); return; } catch (e2) { /* fall through to the message */ } }
          $("decision").innerHTML = block("warn", "Someone already answered this.", who);
        } else {
          const x = explain(e);
          $("decisionMsg").textContent = x.title + ". " + x.body;
          ap.disabled = de.disabled = false;
        }
      }
    };
    ap.onclick = go("approve"); de.onclick = go("decline");
  }

  function wireReveal() {
    const box = $("reveal"), top = $("imgTop"), handle = $("handle"), range = $("range"), base = $("imgBase");
    const set = (pct) => {
      pct = Math.max(0, Math.min(100, pct));
      top.style.clipPath = `inset(0 0 0 ${pct}%)`;
      handle.style.left = pct + "%";
      range.value = pct;
    };
    // The health layer sits on top; size the box from the base photo.
    set(50);
    let drag = false;
    const fromEvent = (e) => { const r = box.getBoundingClientRect(); set(((e.clientX - r.left) / r.width) * 100); };
    box.addEventListener("pointerdown", (e) => { drag = true; box.setPointerCapture(e.pointerId); fromEvent(e); });
    box.addEventListener("pointermove", (e) => { if (drag) fromEvent(e); });
    const end = () => { drag = false; };
    box.addEventListener("pointerup", end); box.addEventListener("pointercancel", end);
    range.addEventListener("input", () => set(Number(range.value)));
    const m = $("maskOn");
    if (m) m.onchange = () => { $("imgMask").style.display = m.checked ? "" : "none"; };
    for (const im of [base, top, $("imgMask")]) if (im) im.onerror = () => { im.style.visibility = "hidden"; };
  }

  load().then(render).catch((e) => {
    const x = explain(e);
    page.innerHTML = block(e.code === "TOKEN_EXPIRED" ? "warn" : "bad", x.title, x.body);
  });
})();
