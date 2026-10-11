/* "How FieldWatch thinks": draws web/agents.json, which agent/web_export/agents_page.py generates from the agent code.
   Plain JS, no build step. Every value from the JSON goes through esc(). */
(function () {
  "use strict";

  var esc = (window.FW && window.FW.esc) || function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };
  var DOT = " · ";
  var flow = document.getElementById("flow");
  var list = document.getElementById("sources");
  var foot = document.getElementById("foot");

  function isAsk(t) { return t.name.indexOf("ask_") === 0; }

  function toolRow(t, byId) {
    var badges = (t.sources || []).map(function (id) {
      var s = byId[id];
      return '<span class="src">' + esc(s ? s.short : id) + "</span>";
    }).join(" ");
    if (t.gate) badges += ' <span class="src gate">Needs your YES</span>';
    return '<li class="tool" title="' + esc(t.description) + '"><span class="tool-label">' + esc(t.label) +
      "</span>" + (badges ? " " + badges : "") + "</li>";
  }

  function box(block, byId, note) {
    return '<section class="node"><h2 class="node-title">' + esc(block.title) + "</h2>" +
      '<ul class="tools">' + block.tools.map(function (t) { return toolRow(t, byId); }).join("") + "</ul>" +
      (note ? '<p class="note">' + esc(note) + "</p>" : "") + "</section>";
  }

  function render(data) {
    var byId = {};
    data.sources.forEach(function (s) { byId[s.id] = s; });

    var asks = data.supervisor.tools.filter(isAsk);
    var others = data.supervisor.tools.filter(function (t) { return !isAsk(t); });
    var supLine = "Sends " + asks.length + " specialists below to gather evidence." +
      (others.length ? " Also: " + others.map(function (t) { return t.label; }).join(DOT) + "." : "");

    var gateNote = (data.approval.tools[0] && data.approval.tools[0].gate) || "";

    flow.innerHTML =
      '<section class="node sup" aria-label="' + esc(data.supervisor.title) + '">' +
        '<h2 class="node-title">' + esc(data.supervisor.title) + "</h2>" +
        '<p class="meta">' + esc(data.supervisor.model) + "</p>" +
        '<p class="plain">' + esc(supLine) + "</p>" +
      "</section>" +
      '<div class="stem" aria-hidden="true"></div>' +
      '<div class="specs">' +
        data.specialists.map(function (s) {
          return '<article class="node spec"><h2 class="node-title">' + esc(s.title) + "</h2>" +
            '<p class="meta">' + esc(s.role) + DOT + esc(s.model) + "</p>" +
            '<ul class="tools">' + s.tools.map(function (t) { return toolRow(t, byId); }).join("") + "</ul>" +
            "</article>";
        }).join("") +
      "</div>" +
      '<div class="below">' +
        box(data.scorer, byId, "Probabilities come from the scorer and nowhere else.") +
        box(data.approval, byId, gateNote) +
      "</div>";

    list.innerHTML = data.sources.map(function (s) {
      return "<li>" + '<span class="nm">' + esc(s.name) + "</span>" +
        '<span class="gv">' + esc(s.gives) + "</span>" +
        '<span class="up">' + esc(s.updates || "") + "</span></li>";
    }).join("");

    foot.innerHTML = "<strong>Runs on AWS:</strong> " + data.services.map(function (s) {
      return esc(s.name) + (s.role ? " (" + esc(s.role) + ")" : "");
    }).join(", ") + ".";
  }

  function fail() {
    flow.innerHTML = '<p class="how-error">Could not load this page. Reload to try again.</p>';
  }

  fetch("agents.json", { cache: "no-cache" })
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(render)
    .catch(fail);
})();
