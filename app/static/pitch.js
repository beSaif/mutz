/* Pitch builder: the officer has full control. State is the draft JSON; every change saves it. */
(function () {
  "use strict";
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]; }); }
  function host(u) { try { return new URL(u).hostname.replace(/^www\./, ""); } catch (e) { return u; } }
  var EYE = '<svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true"><path d="M1 10s3.5-6 9-6 9 6 9 6-3.5 6-9 6-9-6-9-6z" fill="none" stroke="#111" stroke-width="1.5"/><circle cx="10" cy="10" r="2.5" fill="#111"/></svg>';
  var EYEOFF = '<svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true"><path d="M1 10s3.5-6 9-6 9 6 9 6-3.5 6-9 6-9-6-9-6z" fill="none" stroke="#9A9A9A" stroke-width="1.5"/><path d="M3 17L17 3" stroke="#111" stroke-width="1.5"/></svg>';
  var BIN = '<svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true"><path d="M3 5h14M8 5V3h4v2M5 5l1 12h8l1-12" fill="none" stroke="#111" stroke-width="1.5"/></svg>';
  var SUB = {why: "Up to three reasons, each with a source.", matches: "One card per accepted match, about the group and its work.",
    infra: "Labs, facilities and support services.", visit: "A draft schedule. Nobody has been asked yet.", cover: "", sources: "Every link, numbered."};

  window.initPitch = function (lid, draft, section) {
    var cur = section || "why", selected = null, saveT = null, undo = null;
    var $ = function (s) { return document.querySelector(s); };

    function save() {
      clearTimeout(saveT);
      saveT = setTimeout(function () {
        window.mutzPost("/api/pitch/" + lid, {draft: draft}).then(function (r) { var s = $("#saved"); if (s) s.textContent = "Draft saved " + r.saved; });
      }, 300);
    }
    function cards(sec) { return draft.cards.filter(function (c) { return c.section === sec && !c.deleted; }); }
    function sec(id) { return draft.sections.filter(function (s) { return s.id === id; })[0]; }
    function allVisible() {
      var hidden = {}; draft.sections.forEach(function (s) { if (s.hidden || s.deleted) hidden[s.id] = 1; });
      return draft.cards.filter(function (c) { return !c.deleted && !hidden[c.section]; });
    }
    function numbering() {  // global [n] across the pitch, in section order
      var n = 0, map = {}, seen = {};
      draft.sections.forEach(function (s) {
        if (s.hidden || s.deleted) return;
        cards(s.id).forEach(function (c) { map[c.card_id] = (c.sources || []).map(function (x) { var k = x.url + "|" + (x.quote || ""); if (!seen[k]) seen[k] = ++n; return seen[k]; }); });
      });
      return map;
    }
    function shown(c, nums) {  // card text with its [1],[2] markers renumbered to the pitch-wide numbers
      var t = c.text || "";
      (nums || []).forEach(function (g, i) { t = t.split("[" + (i + 1) + "]").join("[[" + g + "]]"); });
      return t.replace(/\[\[(\d+)\]\]/g, "[$1]");
    }

    /* ---------------- outline */
    function outline() {
      var el = $("#outline"); el.innerHTML = "";
      draft.sections.forEach(function (s, i) {
        if (s.deleted) return;
        var n = cards(s.id).length;
        var d = document.createElement("div");
        d.className = "ol" + (s.id === cur ? " on" : "") + (s.hidden ? " off" : "");
        d.draggable = !s.auto; d.dataset.i = i;
        d.innerHTML = '<span aria-hidden="true">⋮⋮</span><a href="#" style="flex:1;text-decoration:none;color:inherit">' + esc(s.title) + (s.id === "matches" || s.id === "infra" ? " (" + n + ")" : "") + "</a>" +
          (s.auto ? '<span class="lb">auto</span>' : '<button class="ic" data-eye aria-label="' + (s.hidden ? "Show " : "Hide ") + esc(s.title) + '">' + (s.hidden ? EYEOFF : EYE) + '</button><button class="ic" data-bin aria-label="Delete ' + esc(s.title) + '">' + BIN + "</button>");
        d.querySelector("a").addEventListener("click", function (e) { e.preventDefault(); cur = s.id; selected = null; render(); history.replaceState(null, "", "?section=" + s.id); });
        var eye = d.querySelector("[data-eye]"); if (eye) eye.addEventListener("click", function () { s.hidden = !s.hidden; save(); render(); });
        var bin = d.querySelector("[data-bin]"); if (bin) bin.addEventListener("click", function () { s.deleted = true; toast('Section “' + s.title + '” deleted', function () { s.deleted = false; }); save(); render(); });
        d.addEventListener("dragstart", function (e) { e.dataTransfer.setData("text/plain", i); d.classList.add("drag"); });
        d.addEventListener("dragend", function () { d.classList.remove("drag"); });
        d.addEventListener("dragover", function (e) { e.preventDefault(); });
        d.addEventListener("drop", function (e) {
          e.preventDefault(); var from = +e.dataTransfer.getData("text/plain"), to = i;
          if (from === to || draft.sections[to].auto) return;
          var m = draft.sections.splice(from, 1)[0]; draft.sections.splice(to, 0, m); save(); render();
        });
        el.appendChild(d);
      });
      var vis = allVisible();
      $("#checkedcount").textContent = vis.filter(function (c) { return c.checked; }).length + " of " + vis.length + " cards checked";
    }
    function toast(msg, undoFn) {
      undo = undoFn;
      var t = $("#toast");
      t.innerHTML = '<div class="toast" style="margin-top:14px"><span style="flex:1">' + esc(msg) + '</span><button class="b s" style="background:#111;color:#fff;border-color:#fff">Undo</button></div>';
      t.querySelector("button").addEventListener("click", function () { if (undo) undo(); undo = null; t.innerHTML = ""; save(); render(); });
    }

    /* ---------------- editor */
    function editor() {
      var ed = $("#editor"), s = sec(cur) || draft.sections[0];
      var h = '<h1 style="margin:0;font-size:21px">' + esc(s.title) + "</h1>";
      if (s.hidden) h += '<p class="lb" style="margin:0">Hidden from the PDF. Use the eye in the outline to show it again.</p>';
      ed.innerHTML = h;
      if (cur === "cover") return coverEditor(ed);
      if (cur === "visit") return visitEditor(ed);
      if (cur === "sources") return sourcesEditor(ed);
      var list = cards(cur);
      if (!list.length) { ed.insertAdjacentHTML("beforeend", '<p class="lb">No cards in this section. Accept matches on the Matches screen, or ask Mutz to add one.</p>'); return; }
      var nums = numbering();
      list.forEach(function (c, i) {
        var d = document.createElement("div");
        d.className = "card" + (c.locked ? " lk" : "") + (selected === c.card_id ? " lk" : "");
        var kind = c.edited ? "edited by you" : (c.origin || "verified quote");
        var label = (cur === "why" ? "Point " + (i + 1) : (c.type === "person" ? "Person" : (c.type || "match").replace(/^./, function (x) { return x.toUpperCase(); })) + " · " + c.name) + " · " + kind + (c.locked ? " · locked" : "");
        var other = (draft.lang || "en") === "de" ? ["en", "In English"] : ["de", "In German"];
        d.innerHTML =
          '<div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap"><span class="lb">' + esc(label) + '</span><label class="lb"><input type="checkbox" data-check ' + (c.checked ? "checked" : "") + "> Checked</label></div>" +
          '<p style="margin:6px 0 10px" lang="' + esc(draft.lang || "en") + '" ' + (c.locked ? "" : 'contenteditable="true"') + ' data-text spellcheck="true">' + esc(shown(c, nums[c.card_id])) + "</p>" +
          (c.type === "person" ? '<label class="lb" style="display:block;margin:0 0 8px"><input type="checkbox" data-name ' + (c.show_person_name ? "checked" : "") + "> Name the person in the pitch (" + esc(c.name) + ")</label>" : "") +
          '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:0 0 10px">' +
          (c.sources || []).map(function (s, k) { return '<a class="chip' + (s.added_by_officer ? " d" : "") + '" href="' + esc(s.deep_link || s.url) + '" target="_blank" rel="noopener" title="' + esc(s.quote || "") + '">[' + (nums[c.card_id] || [])[k] + "] " + esc(host(s.url)) + (s.added_by_officer ? " · added by you" : "") + "</a>"; }).join("") +
          '<label class="lb" for="src' + c.card_id + '">Add a source URL</label><input id="src' + c.card_id + '" data-src placeholder="https://" class="in" style="font-size:13px;padding:6px 8px;width:200px"></div>' +
          '<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">' +
          '<button class="b s" data-rw="shorter"' + (c.locked ? " disabled" : "") + '>Shorter</button><button class="b s" data-rw="jargon"' + (c.locked ? " disabled" : "") + '>Less jargon</button><button class="b s" data-rw="' + other[0] + '"' + (c.locked ? " disabled" : "") + ">" + other[1] + "</button>" +
          '<button class="b s" data-lock>' + (c.locked ? "Allow edits" : "Lock") + '</button><button class="b s" data-del>Delete</button><span style="flex:1"></span>' +
          '<span class="lb">' + (c.sources || []).length + " source" + ((c.sources || []).length === 1 ? "" : "s") + "</span>" +
          (c.sources && c.sources.length ? "" : '<span class="lb" style="color:#B8000F">No source: cannot be exported</span>') +
          '<a href="/leads/' + lid + '/map" style="font-size:13px">Show in map</a></div>' +
          '<p class="lb hidden" data-msg style="margin:8px 0 0"></p>';
        var tx = d.querySelector("[data-text]");
        d.addEventListener("click", function (e) { if (e.target.closest("button,a,input")) return; if (selected !== c.card_id) { selected = c.card_id; preview(); window.mutzSelect && window.mutzSelect({card_id: c.card_id, name: c.text}); } });
        tx.addEventListener("blur", function () {
          var raw = tx.textContent.trim();
          var back = raw; (nums[c.card_id] || []).forEach(function (g, k) { back = back.split("[" + g + "]").join("[[" + (k + 1) + "]]"); });
          back = back.replace(/\[\[(\d+)\]\]/g, "[$1]");
          if (back !== c.text) { c.text = back; c.edited = true; c.checked = false; save(); render(); }
        });
        d.querySelector("[data-check]").addEventListener("change", function (e) { c.checked = e.target.checked; save(); outline(); });
        var nm = d.querySelector("[data-name]"); if (nm) nm.addEventListener("change", function (e) { c.show_person_name = e.target.checked; save(); });
        d.querySelector("[data-lock]").addEventListener("click", function () { c.locked = !c.locked; save(); render(); });
        d.querySelector("[data-del]").addEventListener("click", function () { c.deleted = true; toast('Card “' + (c.name || "card").slice(0, 40) + '” deleted', function () { c.deleted = false; }); save(); render(); });
        d.querySelector("[data-src]").addEventListener("keydown", function (e) {
          if (e.key !== "Enter") return; e.preventDefault();
          var u = e.target.value.trim(); if (!/^https?:\/\/\S+\.\S+/.test(u)) { msg("Enter a full address starting with https://"); return; }
          c.sources = (c.sources || []).concat([{url: u, quote: "", deep_link: u, added_by_officer: true}]); c.edited = true; save(); render();
        });
        d.querySelectorAll("[data-rw]").forEach(function (b) {
          b.addEventListener("click", function () {
            b.disabled = true; msg("Rewriting from this card's sources only…");
            window.mutzPost("/api/pitch/" + lid + "/rewrite", {card_id: c.card_id, instruction: b.dataset.rw}).then(function (r) {
              if (!r.ok) { msg(r.error); b.disabled = false; return; }
              c.text = r.text; c.checked = false; save(); render();
            }).catch(function () { msg("The Apertus gateway did not answer. The card is unchanged. Try again in a minute."); b.disabled = false; });
          });
        });
        function msg(t) { var m = d.querySelector("[data-msg]"); m.textContent = t; m.classList.remove("hidden"); }
        ed.appendChild(d);
      });
    }
    function coverEditor(ed) {
      var co = draft.company || {};
      ed.insertAdjacentHTML("beforeend",
        '<label class="lb" for="cv-title">Title</label><input id="cv-title" class="in" value="' + esc(draft.title || "") + '">' +
        '<label class="lb" for="cv-intro">Opening line, optional</label><textarea id="cv-intro" class="in" rows="3">' + esc(draft.intro || "") + "</textarea>" +
        '<label class="lb" for="cv-off">Contact officer</label><input id="cv-off" class="in" value="' + esc((draft.officer || {}).name || "") + '">' +
        '<label class="lb" for="cv-mail">Officer email</label><input id="cv-mail" class="in" value="' + esc((draft.officer || {}).email || "") + '">' +
        '<p class="lb" style="margin:0">The cover shows the agency logo, this title, the date and the contact officer. No stock images.</p>');
      [["#cv-title", function (v) { draft.title = v; }], ["#cv-intro", function (v) { draft.intro = v; }],
       ["#cv-off", function (v) { draft.officer = draft.officer || {}; draft.officer.name = v; }], ["#cv-mail", function (v) { draft.officer = draft.officer || {}; draft.officer.email = v; }]]
        .forEach(function (p) { $(p[0]).addEventListener("change", function (e) { p[1](e.target.value); save(); preview(); }); });
    }
    function visitEditor(ed) {
      var v = draft.visit || [];
      var h = '<p class="lb" style="margin:0">A draft. Nothing here says anyone agreed to meet; times fill as researchers reply.</p><div style="border-top:1px solid #111">';
      v.forEach(function (r, i) { h += '<div class="sl" style="display:grid;grid-template-columns:80px 1fr;gap:12px;padding:10px 0;border-bottom:1px solid #D9D9D9"><input class="in" data-vt="' + i + '" value="' + esc(r.time) + '" style="font-size:14px;padding:6px"><span><input class="in" data-vw="' + i + '" value="' + esc(r.what) + '" style="font-size:14px;padding:6px;width:100%"><span class="lb">' + esc(r.status || "") + "</span></span></div>"; });
      ed.insertAdjacentHTML("beforeend", h + "</div>");
      ed.querySelectorAll("[data-vt]").forEach(function (x) { x.addEventListener("change", function () { v[+x.dataset.vt].time = x.value; save(); }); });
      ed.querySelectorAll("[data-vw]").forEach(function (x) { x.addEventListener("change", function () { v[+x.dataset.vw].what = x.value; save(); }); });
    }
    function sourcesEditor(ed) {
      var nums = numbering(), h = '<p class="lb" style="margin:0">Built from the cards. Every link opens the page at the quoted sentence.</p><div style="border-top:1px solid #111">';
      var done = {};
      allVisible().forEach(function (c) { (c.sources || []).forEach(function (s, k) { if (done[nums[c.card_id][k]]) return; done[nums[c.card_id][k]] = 1; h += '<div class="ev"><b>[' + nums[c.card_id][k] + ']</b><div><p style="margin:0;font-style:italic">' + (s.quote ? "“" + esc(s.quote) + "”" : esc(c.name)) + '</p><p class="lb" style="margin:4px 0 0"><a href="' + esc(s.deep_link || s.url) + '" target="_blank" rel="noopener">' + esc(host(s.url)) + "</a></p></div></div>"; }); });
      ed.insertAdjacentHTML("beforeend", h + "</div>");
    }

    /* ---------------- A4 preview */
    function preview() {
      var pv = $("#preview"), s = sec(cur) || draft.sections[0], nums = numbering();
      var order = draft.sections.filter(function (x) { return !x.hidden && !x.deleted; });
      var page = order.indexOf(s) + 1;
      $("#pvlabel").textContent = "A4 preview · page " + (page || "–") + " of " + order.length;
      var left = '<div><b style="font-size:11px">' + esc(s.title) + '</b><p style="margin:6px 0 0;color:#5E5E5E">' + esc(SUB[s.id] || "") + "</p></div>", right = "";
      if (s.id === "cover") {
        left = '<div><b style="font-size:13px">' + esc(draft.title || "") + '</b><p style="margin:6px 0 0;color:#5E5E5E">' + esc(new Date().toLocaleDateString("en-GB", {day: "numeric", month: "long", year: "numeric"})) + "</p></div>";
        right = "<p>" + esc(draft.intro || "") + '</p><p style="color:#5E5E5E">Contact: ' + esc((draft.officer || {}).name || "") + "</p>";
      } else if (s.id === "visit") {
        right = (draft.visit || []).map(function (r) { return "<p><b>" + esc(r.time) + "</b> " + esc(r.what) + "</p>"; }).join("");
      } else if (s.id === "sources") {
        var seen2 = {}; allVisible().forEach(function (c) { (c.sources || []).forEach(function (x, k) { var g = nums[c.card_id][k]; if (seen2[g]) return; seen2[g] = 1; right += "<p>[" + g + "] " + esc(host(x.url)) + "</p>"; }); });
      } else {
        cards(s.id).forEach(function (c) {
          right += '<p class="' + (selected === c.card_id ? "hl" : "") + '">' + (s.id !== "why" ? "<b>" + esc(c.type === "person" && !c.show_person_name ? "Group at " + (c.unit || "the University of Bern") : c.name) + "</b><br>" : "") + esc(shown(c, nums[c.card_id])) + "</p>";
          if (selected === c.card_id) right += '<a href="#" data-mutz-ask="Is this claim backed? “' + esc((c.text || "").slice(0, 160)) + '”" style="align-self:flex-start;font-size:9px;border:1px solid #111;padding:2px 5px;text-decoration:none;background:#fff">Ask Mutz: is this backed?</a>';
        });
      }
      pv.innerHTML = left + '<div style="display:flex;flex-direction:column;gap:8px">' + right + "</div>";
    }
    function render() { outline(); editor(); preview(); }
    render();
  };
})();
