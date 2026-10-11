/* Work order page: workorder.html?id=<action_id>&t=<token>
   Renders contracts.WorkOrderView from GET /v1/work-orders/{id}?t=...
   "Mark done" -> POST /v1/work-orders/{id}/done?t=...
   Dev: ?fixture=1 (sent) | done | expired. */
(function () {
  "use strict";
  const { $, esc, api, params, safeUrl, explain } = FW;
  const page = $("page");
  const id = params.get("id") || "", token = params.get("t") || "";
  const fixture = params.get("fixture");
  let view = null;

  const banner = (cls, title, body) =>
    `<div class="banner ${cls}"><div>${esc(title)}</div>${body ? `<div class="small" style="font-weight:400;color:inherit;margin-top:6px">${esc(body)}</div>` : ""}</div>`;

  async function load() {
    if (fixture) {
      if (fixture === "expired") { const e = new Error("expired"); e.code = "TOKEN_EXPIRED"; throw e; }
      const v = await FW.loadFixture("work_order_view.json");
      if (fixture === "done") v.status = "done";
      return v;
    }
    if (!id || !token) { const e = new Error("incomplete"); e.code = "NOT_FOUND"; throw e; }
    return api(`/v1/work-orders/${encodeURIComponent(id)}?t=${encodeURIComponent(token)}`);
  }

  function render(v) {
    view = v;
    const nav = safeUrl(v.navigate_url);
    let h = `<div><h1>Repair job: ${esc(v.field_name)}</h1></div>`;
    h += `<div class="block"><h2>What to check</h2><p class="lead" style="margin:0">${esc(v.instructions)}</p></div>`;
    h += `<div class="block"><h2>Where</h2><div id="wmap" role="img" aria-label="Map showing where to go"></div>
      ${nav ? `<div class="btn-row" style="margin-top:12px"><a class="btn secondary" href="${esc(nav)}" target="_blank" rel="noopener">Navigate here</a></div>` : ""}</div>`;
    h += `<div id="doneBlock">${doneHtml(v.status)}</div>`;
    if (fixture) h += `<p class="small">Sample data for testing.</p>`;
    page.innerHTML = h;
    drawMap(v.pin);
    wireDone();
  }

  function doneHtml(status) {
    if (status === "done") return banner("ok", "Marked done. Thank you.", "FieldWatch has told the family.");
    return `<div class="btn-row"><button class="btn primary" id="done">Mark done</button></div><p class="small" id="doneMsg" role="status"></p>`;
  }

  function drawMap(pin) {
    const el = $("wmap");
    if (!el || !window.L || !pin) { if (el) el.style.display = "none"; return; }
    const ll = [pin[1], pin[0]]; // contract pin is [lon, lat]
    const m = L.map(el, { scrollWheelZoom: false }).setView(ll, 16);
    L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", { attribution: "Esri World Imagery", maxZoom: 19 }).addTo(m);
    L.circleMarker(ll, { radius: 12, color: "#fff", weight: 3, fillColor: "#d32f2f", fillOpacity: 1 }).addTo(m);
  }

  function wireDone() {
    const b = $("done"); if (!b) return;
    b.onclick = async () => {
      b.disabled = true; $("doneMsg").textContent = "Sending...";
      try {
        if (!fixture) await api(`/v1/work-orders/${encodeURIComponent(view.action_id)}/done?t=${encodeURIComponent(token)}`, { method: "POST", body: {} });
        $("doneBlock").innerHTML = doneHtml("done");
      } catch (e) {
        const x = explain(e);
        $("doneMsg").textContent = x.title + ". " + x.body;
        b.disabled = false;
      }
    };
  }

  load().then(render).catch((e) => {
    const x = explain(e);
    page.innerHTML = banner(e.code === "TOKEN_EXPIRED" ? "warn" : "bad", x.title, x.body);
  });
})();
