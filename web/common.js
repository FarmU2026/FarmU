/* Shared helpers for every FieldWatch page. Plain JS, no build step.
   Config comes from config.js (window.FIELDWATCH_CONFIG), generated at deploy. */
(function () {
  "use strict";

  const cfg = window.FIELDWATCH_CONFIG || {};
  const params = new URLSearchParams(location.search);

  // Words the farmer sees. Never NDVI / anomaly / confidence.
  const CONDITIONS = {
    pivot_failure: "Pivot not watering",
    hail: "Hail damage",
    drought: "Dry weather",
    ponding: "Standing water",
    nitrogen: "Low nitrogen",
    herbicide_drift: "Spray drift",
    planter_skip: "Planter skips",
    disease_pest: "Disease or pests",
    sandy_soil_water_stress: "Sandy soil drying out",
    unknown: "Something else",
  };
  const AGENTS = {
    supervisor: "Lead agent",
    imagery: "Satellite check",
    environment: "Weather and land",
    agronomy: "Crop advice",
  };

  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const $ = (id) => document.getElementById(id);

  function apiBase() {
    return String(cfg.apiUrl == null ? "" : cfg.apiUrl).replace(/\/+$/, "");
  }

  // Only let http(s) and geo links out of data we did not write ourselves.
  function safeUrl(u) {
    if (!u) return "";
    const s = String(u).trim();
    if (/^(https?:\/\/|geo:|\/|\.\/|[A-Za-z0-9_\-./]+$)/i.test(s) && !/^javascript:/i.test(s)) return s;
    return "";
  }

  /** Fetch JSON from the API. Throws an Error with .code (API error code), .status and .detail. */
  async function api(path, opts) {
    opts = opts || {};
    const init = { method: opts.method || "GET", headers: {} };
    if (opts.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    let r;
    try {
      r = await fetch(apiBase() + path, init);
    } catch (e) {
      const err = new Error("Can't reach FieldWatch right now. Check your signal and try again.");
      err.code = "NETWORK";
      throw err;
    }
    const text = await r.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
    if (!r.ok) {
      // Two shapes: {error:{code,message}} from the cloud API, {detail} from the local harness.
      const eb = data && data.error;
      const err = new Error((eb && eb.message) || (data && typeof data.detail === "string" && data.detail) || text || "Something went wrong (" + r.status + ")");
      err.status = r.status;
      err.code = (eb && eb.code) || "HTTP_" + r.status;
      err.detail = eb || data;
      throw err;
    }
    return data;
  }

  const usd = (n) => "$" + Math.round(Number(n) || 0).toLocaleString("en-US");

  function niceDate(iso) {
    if (!iso) return "";
    const d = new Date(String(iso).length <= 10 ? iso + "T12:00:00" : iso);
    if (isNaN(d)) return String(iso);
    return d.toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" });
  }

  function clockTime(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return String(iso || "").slice(11, 16);
    return d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  }

  function toolName(t) {
    return String(t || "").replace(/^get_/, "").replace(/_/g, " ");
  }

  /** Plain words for an API error on the phone pages. */
  function explain(e) {
    const c = e && e.code;
    if (c === "TOKEN_EXPIRED") return { title: "This link has expired", body: "Text LINK to FieldWatch and we will send you a new one." };
    if (c === "TOKEN_SCOPE") return { title: "This link opens something else", body: "Use the link from your latest FieldWatch text." };
    if (c === "NOT_FOUND" || (e && e.status === 404)) return { title: "We could not find that page", body: "The link may be cut off. Use the link from your latest FieldWatch text." };
    if (c === "INVALID_INPUT") return { title: "Something was missing", body: (e && e.message) || "Please try again." };
    if (c === "NETWORK") return { title: "No signal", body: e.message };
    return { title: "Something went wrong", body: (e && e.message) || "Please try again in a minute." };
  }

  async function loadFixture(name) {
    const r = await fetch("fixtures/" + name);
    if (!r.ok) throw new Error("fixture " + name + " not found");
    return r.json();
  }

  window.FW = { cfg, params, CONDITIONS, AGENTS, esc, $, apiBase, safeUrl, api, usd, niceDate, clockTime, toolName, loadFixture, explain,
    condition: (c) => CONDITIONS[c] || String(c || "").replace(/_/g, " "),
    agent: (a) => AGENTS[a] || (a ? String(a) : "") };
})();
