/* Pitch editor: the document as it will print, edited in place. Why Bern, what Bern offers, how to start.
   State is the draft JSON; every change saves it. */
(function () {
  "use strict";
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]; }); }
  function host(u) { try { return new URL(u).hostname.replace(/^www\./, ""); } catch (e) { return u; } }
  var EYE = '<svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true"><path d="M1 10s3.5-6 9-6 9 6 9 6-3.5 6-9 6-9-6-9-6z" fill="none" stroke="#111" stroke-width="1.5"/><circle cx="10" cy="10" r="2.5" fill="#111"/></svg>';
  var EYEOFF = '<svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true"><path d="M1 10s3.5-6 9-6 9 6 9 6-3.5 6-9 6-9-6-9-6z" fill="none" stroke="#9A9A9A" stroke-width="1.5"/><path d="M3 17L17 3" stroke="#111" stroke-width="1.5"/></svg>';
  var BIN = '<svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true"><path d="M3 5h14M8 5V3h4v2M5 5l1 12h8l1-12" fill="none" stroke="#111" stroke-width="1.5"/></svg>';
  var PART = {why: "why", matches: "what", infra: "what", visit: "how"};
  var PART_LABEL = {why: "Why", what: "What", how: "How"};
  var GUIDE = {why: "Why should this company look at Bern? Up to three reasons, each from a source.",
    what: "What does Bern offer for each need? The research partners, labs and services, each with its page.",
    how: "How do they start? A draft visit day and the next step. Nothing is arranged yet."};
  var LEDE = {matches: "Research groups and projects in Bern that fit your plans. Each entry links to the public page it is based on.",
    infra: "Facilities and support services you can use in Bern.", sources: "Every statement in this document is based on these public pages."};
  var SOVEREIGN = "Drafted with Apertus on Swiss infrastructure (CSCS); confidential figures and contact data were struck before any text left the agency.";  // pitch/templates/pitch.html.j2 prints the same line
  var HOW = "Nothing is arranged yet. If you plan a visit, tell us which groups interest you most. The agency then asks each group whether they are available and proposes a day in Bern.";
  var AGENCY = "Bern Economic Development Agency";
  var PAGE_W = 794;  // A4 at 96 dpi

  window.initPitch = function (lid, draft, section, meta) {
    var selected = null, saveT = null, undo = null, zoom = null;
    var $ = function (s) { return document.querySelector(s); };
    if (draft.how == null) draft.how = HOW;

    function save() {
      clearTimeout(saveT);
      saveT = setTimeout(function () {
        window.mutzPost("/api/pitch/" + lid, {draft: draft}).then(function (r) { var s = $("#saved"); if (s) s.textContent = "Draft saved " + r.saved; });
      }, 300);
    }
    function part(s) { return s.part || PART[s.id] || null; }
    function cards(sec) { return draft.cards.filter(function (c) { return c.section === sec && !c.deleted; }); }
    function card(id) { return draft.cards.filter(function (c) { return c.card_id === id; })[0]; }
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
    function heading(c) { return c.type === "person" && !c.show_person_name ? "Group at " + (c.unit || "the University of Bern") : c.name; }
    function kindOf(c, i) {
      if (c.section === "why") return "Point " + (i + 1);
      return c.type === "person" ? "Researcher" : (c.type || "match").replace(/^./, function (x) { return x.toUpperCase(); });
    }
    function status(c) {
      if (!(c.sources || []).length) return ["x", "No source: cannot be exported"];
      if (c.checked) return ["ok", "Checked"];
      return [c.edited ? "e" : "", (c.edited ? "Edited by you" : "Verified quote") + " · not checked yet"];
    }
    /* pages: cover, then one page per part (sections of the same part share it), then sources */
    function pages() {
      var out = [{kind: "cover", secs: [sec("cover") || {id: "cover", title: "Cover"}]}], last = null;
      draft.sections.forEach(function (s) {
        if (s.deleted || s.id === "cover" || s.id === "sources") return;
        var p = part(s);
        if (last && last.part === p) last.secs.push(s);
        else { last = {kind: "part", part: p, secs: [s]}; out.push(last); }
      });
      out.push({kind: "sources", secs: [sec("sources") || {id: "sources", title: "Sources"}]});
      var n = 0;
      out.forEach(function (pg) { pg.off = pg.kind === "part" && pg.secs.every(function (s) { return s.hidden; }); pg.n = pg.off ? null : ++n; });
      out.total = n;
      return out;
    }

    /* ---------------- outline */
    function outline() {
      var el = $("#outline"); el.innerHTML = "";
      var lastPart = null;
      draft.sections.forEach(function (s, i) {
        if (s.deleted) return;
        var p = part(s), fixed = s.id === "cover" || s.auto;
        if (p && p !== lastPart) el.insertAdjacentHTML("beforeend", '<p class="ol-h">' + PART_LABEL[p] + "</p>");
        if (!p && lastPart) el.insertAdjacentHTML("beforeend", '<p class="ol-h">&nbsp;</p>');
        lastPart = p;
        var n = cards(s.id).length;
        var d = document.createElement("div");
        d.className = "ol" + (s.hidden ? " off" : "");
        d.draggable = !fixed; d.dataset.i = i; d.dataset.sec = s.id;
        d.innerHTML = '<span aria-hidden="true" style="visibility:' + (fixed ? "hidden" : "visible") + '">⋮⋮</span><a href="#sec-' + esc(s.id) + '" style="flex:1;text-decoration:none;color:inherit">' + esc(s.title) + (s.id === "matches" || s.id === "infra" || s.id === "why" ? " (" + n + ")" : "") + "</a>" +
          (fixed ? '<span class="lb">' + (s.auto ? "auto" : "") + "</span>" : '<button class="ic" data-eye aria-label="' + (s.hidden ? "Show " : "Hide ") + esc(s.title) + '">' + (s.hidden ? EYEOFF : EYE) + '</button><button class="ic" data-bin aria-label="Delete ' + esc(s.title) + '">' + BIN + "</button>");
        d.querySelector("a").addEventListener("click", function (e) { e.preventDefault(); go(s.id); });
        var eye = d.querySelector("[data-eye]"); if (eye) eye.addEventListener("click", function () { s.hidden = !s.hidden; save(); render(); });
        var bin = d.querySelector("[data-bin]"); if (bin) bin.addEventListener("click", function () { s.deleted = true; toast('Section “' + s.title + '” deleted', function () { s.deleted = false; }); save(); render(); });
        d.addEventListener("dragstart", function (e) { e.dataTransfer.setData("text/plain", i); d.classList.add("drag"); });
        d.addEventListener("dragend", function () { d.classList.remove("drag"); });
        d.addEventListener("dragover", function (e) { e.preventDefault(); });
        d.addEventListener("drop", function (e) {
          e.preventDefault(); var from = +e.dataTransfer.getData("text/plain"), to = i;
          var a = draft.sections[from], b = draft.sections[to];
          if (from === to || b.auto || b.id === "cover") return;
          if (part(a) !== part(b)) { toast("Sections stay in their part: why, then what, then how.", null); return; }
          var m = draft.sections.splice(from, 1)[0]; draft.sections.splice(to, 0, m); save(); render();
        });
        el.appendChild(d);
      });
      var vis = allVisible();
      var ck = vis.filter(function (c) { return c.checked; }).length, pc = vis.length ? Math.round(100 * ck / vis.length) : 0;
      $("#checkedcount").innerHTML = '<div class="meter" title="Cards checked: ' + ck + " of " + vis.length + '"><span class="meter-l"><span>Cards checked</span><span class="lb">' + ck + " of " + vis.length + "</span></span>" +
        '<span class="bar" role="meter" aria-label="Cards checked" aria-valuemin="0" aria-valuemax="' + vis.length + '" aria-valuenow="' + ck + '"><i style="width:' + pc + '%"></i></span></div>';
    }
    function toast(msg, undoFn) {
      undo = undoFn;
      var t = $("#toast");
      t.innerHTML = '<div class="toast" style="margin-top:14px"><span style="flex:1">' + esc(msg) + "</span>" + (undoFn ? '<button class="b s" style="background:#111;color:#fff;border-color:#fff">Undo</button>' : "") + "</div>";
      var b = t.querySelector("button");
      if (b) b.addEventListener("click", function () { if (undo) undo(); undo = null; t.innerHTML = ""; save(); render(); });
    }
    function go(id) {
      var el = document.getElementById("sec-" + id); if (!el) return;
      $("#canvas").scrollTo({top: el.getBoundingClientRect().top - $("#canvas").getBoundingClientRect().top + $("#canvas").scrollTop - 16, behavior: "smooth"});
      history.replaceState(null, "", "?section=" + id);
    }

    /* ---------------- the document */
    function foot(pg, total) {
      // the PDF prints the disclaimer once, on the cover, and page numbers on the rest
      var left = pg.kind === "cover" ? "Prototype, not an official Canton of Bern tool" : "";
      return '<div class="pg-ft"><span>' + left + "</span><span>" + (pg.kind === "cover" ? "" : pg.n ? pg.n + " / " + total : "Not in the PDF") + "</span></div>";
    }
    function coverHtml() {
      var o = draft.officer || {};
      return '<div class="pg-in" id="sec-cover">' +
        (meta.logo ? '<img class="pg-logo" src="/logo.png" alt="' + AGENCY + '">' : '<b class="pg-logo">' + AGENCY + "</b>") +
        '<h1 class="ed" contenteditable="true" spellcheck="true" data-f="title" data-ph="Title">' + esc(draft.title || "") + "</h1>" +
        '<p class="pg-for">For ' + esc(meta.company || "") + "</p>" +
        '<table class="pg-meta"><tr><td>Date</td><td>' + esc(meta.date || "") + "</td></tr>" +
        '<tr><td>Contact</td><td><span class="ed" contenteditable="true" data-f="officer.name" data-ph="Contact officer">' + esc(o.name || "") + "</span><br>" + AGENCY +
        '<br><span class="ed" contenteditable="true" data-f="officer.email" data-ph="Officer email">' + esc(o.email || "") + "</span></td></tr></table>" +
        '<p class="pg-intro ed" contenteditable="true" spellcheck="true" data-f="intro" data-ph="Add an opening line, optional. For example what the company is looking for and what this document lists.">' + esc(draft.intro || "") + "</p></div>";
    }
    function sectionHtml(s, first, nums) {
      var h = '<div class="pg-sec' + (s.hidden ? " off" : "") + '" id="sec-' + esc(s.id) + '">' +
        (first ? '<p class="pg-k">' + PART_LABEL[part(s)] + "</p>" : "") +
        '<h2 class="ed" contenteditable="true" spellcheck="true" data-title="' + esc(s.id) + '">' + esc(s.title) + "</h2>" +
        (s.hidden ? '<p class="pg-off">Hidden from the PDF. <button type="button" class="b s" data-show="' + esc(s.id) + '">Show it</button></p>' : "");
      if (s.id === "visit") {
        h += '<p class="pg-lede ed" contenteditable="true" spellcheck="true" data-f="how" data-ph="How the company starts: what to tell you, what the agency does next.">' + esc(draft.how || "") + "</p>" +
          '<table class="pg-visit">' + (draft.visit || []).map(function (r, i) {
            return '<tr><td class="ed" contenteditable="true" data-vt="' + i + '">' + esc(r.time) + '</td><td><span class="ed" contenteditable="true" data-vw="' + i + '">' + esc(r.what) + "</span>" +
              (r.status ? '<span class="pg-st">' + esc(r.status) + "</span>" : "") + '</td><td><button type="button" class="ic pg-x" data-vdel="' + i + '" aria-label="Remove this line">×</button></td></tr>';
          }).join("") + "</table>" +
          '<button type="button" class="b s dash pg-add" data-vadd>Add a line</button>';
        return h + "</div>";
      }
      var list = cards(s.id);
      h += s.id === "why" ? '<p class="pg-lede">' + list.length + " reason" + (list.length === 1 ? "" : "s") + ", each with a source.</p>" : '<p class="pg-lede">' + esc(LEDE[s.id] || "") + "</p>";
      if (!list.length) h += '<p class="pg-empty">No cards in this section. Accept matches on the Matches screen, or ask Bärner Mutz to add one.</p>';
      list.forEach(function (c, i) {
        var st = status(c), n = nums[c.card_id] || [];
        h += '<div class="pc' + (selected === c.card_id ? " sel" : "") + (c.locked ? " lk" : "") + '" data-card="' + esc(c.card_id) + '" tabindex="-1">' +
          '<p class="pc-k">' + esc(kindOf(c, i)) + ' <span class="pc-st ' + st[0] + '">· ' + esc(st[1]) + (c.locked ? " · locked" : "") + "</span></p>" +
          (s.id !== "why" ? "<h3>" + esc(heading(c)) + "</h3>" + (c.unit && c.unit !== heading(c) && c.unit !== c.name ? '<p class="pc-sub">' + esc(c.unit) + "</p>" : "") : "") +
          '<p class="pc-t ed" lang="' + esc(draft.lang || "en") + '"' + (c.locked ? "" : ' contenteditable="true"') + ' spellcheck="true" data-text>' + esc(shown(c, n)) + "</p>" +
          '<p class="pc-src">' + (c.sources || []).map(function (x, k) { return '<a href="' + esc(x.deep_link || x.url) + '" target="_blank" rel="noopener" title="' + esc(x.quote || "") + '">[' + n[k] + "] " + esc(host(x.url)) + "</a>"; }).join(" ") + "</p></div>";
      });
      return h + "</div>";
    }
    function sourcesHtml(nums) {
      var h = '<div class="pg-in" id="sec-sources"><h2>Sources</h2><p class="pg-lede" style="margin-bottom:6px">' + LEDE.sources + '</p><p class="pg-lede">' + SOVEREIGN + '</p><ol class="pg-srcs">', done = {};
      allVisible().forEach(function (c) { (c.sources || []).forEach(function (s, k) { var g = nums[c.card_id][k]; if (done[g]) return; done[g] = 1; h += "<li><b>[" + g + "]</b> " + (s.quote ? "“" + esc(s.quote) + "” " : "") + '<a href="' + esc(s.deep_link || s.url) + '" target="_blank" rel="noopener">' + esc(s.url) + "</a></li>"; }); });
      return h + "</ol></div>";
    }
    function doc() {
      var el = $("#pages"), nums = numbering(), ps = pages();
      el.innerHTML = ps.map(function (pg) {
        var inner = pg.kind === "cover" ? coverHtml() : pg.kind === "sources" ? sourcesHtml(nums)
          : '<div class="pg-in">' + pg.secs.map(function (s, i) { return sectionHtml(s, i === 0, nums); }).join("") + "</div>";
        return '<div class="pg' + (pg.off ? " off" : "") + '" data-n="' + (pg.n || "") + '">' + inner + foot(pg, ps.total) + "</div>";
      }).join("");
      bindDoc(el, nums);
      pageNo();
    }
    function setPath(path, v) { var k = path.split("."), o = draft; for (var i = 0; i < k.length - 1; i++) { o[k[i]] = o[k[i]] || {}; o = o[k[i]]; } o[k[k.length - 1]] = v; }
    function plain(el) { return el.innerText.replace(/ /g, " ").replace(/\n+/g, " ").trim(); }
    function bindDoc(el, nums) {
      el.querySelectorAll("[data-f]").forEach(function (x) { x.addEventListener("blur", function () { setPath(x.dataset.f, plain(x)); save(); }); });
      el.querySelectorAll("[data-title]").forEach(function (x) { x.addEventListener("blur", function () { var s = sec(x.dataset.title), v = plain(x); if (v && v !== s.title) { s.title = v; save(); outline(); } else if (!v) x.textContent = s.title; }); });
      el.querySelectorAll("[data-show]").forEach(function (b) { b.addEventListener("click", function () { sec(b.dataset.show).hidden = false; save(); render(); }); });
      el.querySelectorAll("[data-vt]").forEach(function (x) { x.addEventListener("blur", function () { draft.visit[+x.dataset.vt].time = plain(x); save(); }); });
      el.querySelectorAll("[data-vw]").forEach(function (x) { x.addEventListener("blur", function () { draft.visit[+x.dataset.vw].what = plain(x); save(); }); });
      el.querySelectorAll("[data-vdel]").forEach(function (b) { b.addEventListener("click", function () { var i = +b.dataset.vdel, r = draft.visit.splice(i, 1)[0]; toast("Line “" + (r.what || "").slice(0, 40) + "” removed", function () { draft.visit.splice(i, 0, r); }); save(); render(); }); });
      var add = el.querySelector("[data-vadd]");
      if (add) add.addEventListener("click", function () { draft.visit = draft.visit || []; draft.visit.push({time: "", what: "", status: "draft"}); save(); render(); var t = el.querySelectorAll("[data-vt]"); if (t.length) t[t.length - 1].focus(); });
      el.querySelectorAll("[data-card]").forEach(function (d) {
        var c = card(d.dataset.card), tx = d.querySelector("[data-text]");
        d.addEventListener("focusin", function () { pick(c.card_id); });
        d.addEventListener("click", function (e) { if (!e.target.closest("a")) pick(c.card_id); });
        tx.addEventListener("blur", function () {
          var back = plain(tx); (nums[c.card_id] || []).forEach(function (g, k) { back = back.split("[" + g + "]").join("[[" + (k + 1) + "]]"); });
          back = back.replace(/\[\[(\d+)\]\]/g, "[$1]");
          if (back !== c.text) { c.text = back; c.edited = true; c.checked = false; save(); refresh(c); }
        });
      });
      el.querySelectorAll("[contenteditable]").forEach(function (x) {  // Enter ends the edit: the PDF has no line breaks inside a field
        x.addEventListener("keydown", function (e) { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); x.blur(); } });
        x.addEventListener("paste", function (e) { e.preventDefault(); document.execCommand("insertText", false, (e.clipboardData || window.clipboardData).getData("text/plain")); });
      });
    }
    function refresh(c) {  // after a text edit: status line, outline count, inspector; the page keeps the caret elsewhere
      var d = document.querySelector('[data-card="' + c.card_id + '"] .pc-st'), st = status(c);
      if (d) { d.className = "pc-st " + st[0]; d.textContent = "· " + st[1] + (c.locked ? " · locked" : ""); }
      outline(); if (selected === c.card_id) inspector();
    }
    function pick(id) {
      if (selected === id) return;
      selected = id;
      document.querySelectorAll("#pages .pc").forEach(function (d) { d.classList.toggle("sel", d.dataset.card === id); });
      inspector();
      var c = card(id); if (c && window.mutzSelect) window.mutzSelect({card_id: id, name: c.text});
    }

    /* ---------------- inspector */
    function inspector() {
      var el = $("#inspector"), c = selected && card(selected);
      if (!c || c.deleted) { selected = null; return docPanel(el); }
      var nums = numbering()[c.card_id] || [], i = cards(c.section).indexOf(c);
      var other = (draft.lang || "en") === "de" ? ["en", "In English"] : ["de", "In German"];
      el.innerHTML =
        '<div style="display:flex;align-items:baseline;gap:8px"><span class="lb" style="flex:1">' + esc((sec(c.section) || {}).title || "") + " · " + esc(kindOf(c, i)) + '</span><button type="button" class="lb ins-x" data-close>Close</button></div>' +
        '<h2 style="font-size:18px;margin:4px 0 14px" lang="de">' + esc(c.name || heading(c)) + "</h2>" +
        '<label class="ins-ck"><input type="checkbox" data-check ' + (c.checked ? "checked" : "") + '><span><b>Checked against its sources</b><span class="lb" style="display:block">Every card must be checked before the clean PDF goes out.</span></span></label>' +
        (c.type === "person" ? '<label class="lb" style="display:flex;gap:8px;margin:10px 0 0"><input type="checkbox" data-name ' + (c.show_person_name ? "checked" : "") + "><span>Name the person in the pitch (" + esc(c.name) + "). Off: the card says “Group at " + esc(c.unit || "the University of Bern") + "”.</span></label>" : "") +
        '<h3 class="rh">Sources</h3>' +
        ((c.sources || []).length ? (c.sources || []).map(function (s, k) {
          return '<div class="ins-src"><p style="margin:0"><b>[' + nums[k] + "]</b> " + esc(host(s.url)) + (s.added_by_officer ? ' <span class="lb">· added by you</span>' : "") + "</p>" +
            (s.quote ? '<p class="lb" style="margin:2px 0 4px;font-style:italic;color:#111">“' + esc(s.quote) + "”</p>" : "") +
            '<a href="' + esc(s.deep_link || s.url) + '" target="_blank" rel="noopener" style="font-size:13px">' + (s.quote ? "Open at quote" : "Open page") + "</a></div>";
        }).join("") : '<p class="lb" style="color:#B8000F;margin:0 0 8px">No source: this card cannot be exported. Add one below or delete the card.</p>') +
        '<label class="lb" for="ins-src">Add a source URL, then press Enter</label><input id="ins-src" data-src placeholder="https://" class="in" style="font-size:13px;padding:6px 8px;width:100%;margin-top:4px">' +
        '<h3 class="rh">Rewrite from its sources only</h3>' +
        '<div style="display:flex;gap:6px;flex-wrap:wrap"><button class="b s" data-rw="shorter"' + (c.locked ? " disabled" : "") + '>Shorter</button><button class="b s" data-rw="jargon"' + (c.locked ? " disabled" : "") + '>Less jargon</button><button class="b s" data-rw="' + other[0] + '"' + (c.locked ? " disabled" : "") + ">" + other[1] + "</button></div>" +
        '<p class="lb hidden" data-msg style="margin:8px 0 0"></p>' +
        '<h3 class="rh">Card</h3>' +
        '<div style="display:flex;gap:6px;flex-wrap:wrap"><button class="b s" data-lock>' + (c.locked ? "Allow edits" : "Lock") + '</button><button class="b s" data-del>Delete</button><a class="b s" href="/leads/' + lid + '/map">Show in map</a></div>' +
        '<p style="margin:14px 0 0"><a href="#" data-mutz-ask="Is this claim backed? “' + esc((c.text || "").slice(0, 160)) + '”" style="font-size:13px">Ask Bärner Mutz: is this backed?</a></p>';
      el.querySelector("[data-close]").addEventListener("click", function () { selected = null; document.querySelectorAll("#pages .pc.sel").forEach(function (d) { d.classList.remove("sel"); }); inspector(); });
      el.querySelector("[data-check]").addEventListener("change", function (e) { c.checked = e.target.checked; save(); refresh(c); });
      var nm = el.querySelector("[data-name]"); if (nm) nm.addEventListener("change", function (e) { c.show_person_name = e.target.checked; save(); doc(); });
      el.querySelector("[data-lock]").addEventListener("click", function () { c.locked = !c.locked; save(); render(); });
      el.querySelector("[data-del]").addEventListener("click", function () { c.deleted = true; selected = null; toast('Card “' + (c.name || "card").slice(0, 40) + '” deleted', function () { c.deleted = false; }); save(); render(); });
      el.querySelector("[data-src]").addEventListener("keydown", function (e) {
        if (e.key !== "Enter") return; e.preventDefault();
        var u = e.target.value.trim(); if (!/^https?:\/\/\S+\.\S+/.test(u)) { msg("Enter a full address starting with https://"); return; }
        c.sources = (c.sources || []).concat([{url: u, quote: "", deep_link: u, added_by_officer: true}]); c.edited = true; c.checked = false; save(); render();
      });
      el.querySelectorAll("[data-rw]").forEach(function (b) {
        b.addEventListener("click", function () {
          b.disabled = true; msg("Rewriting from this card's sources only…");
          window.mutzPost("/api/pitch/" + lid + "/rewrite", {card_id: c.card_id, instruction: b.dataset.rw}).then(function (r) {
            if (!r.ok) { msg(r.error); b.disabled = false; return; }
            c.text = r.text; c.checked = false; save(); render();
          }).catch(function () { msg("The Apertus gateway did not answer. The card is unchanged. Try again in a minute."); b.disabled = false; });
        });
      });
      function msg(t) { var m = el.querySelector("[data-msg]"); m.textContent = t; m.classList.remove("hidden"); }
    }
    function docPanel(el) {
      var vis = allVisible(), nos = vis.filter(function (c) { return !(c.sources || []).length; }).length;
      el.innerHTML = '<h2 style="font-size:18px;margin:0 0 4px">Pitch structure</h2><p class="lb" style="margin:0 0 12px">The document answers three questions, in this order.</p>' +
        ["why", "what", "how"].map(function (p) {
          var ss = draft.sections.filter(function (s) { return !s.deleted && part(s) === p; });
          return '<div class="ins-part"><p class="pg-k" style="margin:0 0 2px">' + PART_LABEL[p] + "</p><p style=\"margin:0 0 6px;font-size:14px\">" + GUIDE[p] + "</p>" +
            ss.map(function (s) { var n = s.id === "visit" ? (draft.visit || []).length + " lines" : cards(s.id).length + " card" + (cards(s.id).length === 1 ? "" : "s"); return '<a href="#sec-' + esc(s.id) + '" data-go="' + esc(s.id) + '" class="ins-go' + (s.hidden ? " off" : "") + '"><span>' + esc(s.title) + "</span><span class=\"lb\">" + n + (s.hidden ? " · hidden" : "") + "</span></a>"; }).join("") + "</div>";
        }).join("") +
        '<h3 class="rh">Checks</h3>' +
        '<div class="ins-row"><span>Cards without a source</span><b' + (nos ? ' style="color:#B8000F"' : "") + ">" + nos + "</b></div>" +
        '<p class="lb" style="margin:10px 0 0">Click a card on the page to check it, see its quotes or rewrite it.</p>';
      el.querySelectorAll("[data-go]").forEach(function (a) { a.addEventListener("click", function (e) { e.preventDefault(); go(a.dataset.go); }); });
    }

    /* ---------------- zoom and page number */
    function applyZoom() {
      var cv = $("#canvas"), z = zoom == null ? Math.max(0.5, Math.min(1.25, (cv.clientWidth - 56) / PAGE_W)) : zoom;
      $("#pages").style.zoom = z; $("#zl").textContent = Math.round(z * 100) + "%";
    }
    document.querySelectorAll("[data-z]").forEach(function (b) {
      b.addEventListener("click", function () {
        var cur = parseFloat($("#pages").style.zoom) || 1;
        zoom = b.dataset.z === "fit" ? null : Math.max(0.5, Math.min(2, cur + (b.dataset.z === "+" ? 0.1 : -0.1)));
        applyZoom();
      });
    });
    window.addEventListener("resize", function () { if (zoom == null) applyZoom(); });
    function pageNo() {
      var cv = $("#canvas"), top = cv.getBoundingClientRect().top + 80, cur = null, all = document.querySelectorAll("#pages .pg");
      all.forEach(function (p) { if (p.getBoundingClientRect().top <= top) cur = p; });
      cur = cur || all[0];
      var total = Array.prototype.filter.call(all, function (p) { return p.dataset.n; }).length;
      $("#pgno").textContent = cur && cur.dataset.n ? "Page " + cur.dataset.n + " of " + total : "Hidden page · " + total + " pages in the PDF";
      var at = null; document.querySelectorAll("#pages [id^=sec-]").forEach(function (s) { if (s.getBoundingClientRect().top <= top) at = s.id.slice(4); });
      at = at || "cover";
      document.querySelectorAll("#outline .ol").forEach(function (o) { o.classList.toggle("on", o.dataset.sec === at); });
    }
    $("#canvas").addEventListener("scroll", pageNo, {passive: true});
    $("#canvas").addEventListener("click", function (e) {  // a click on the grey canvas, outside any card, shows the document panel
      if (!e.target.closest(".pc") && !e.target.closest("[contenteditable]") && selected) { selected = null; document.querySelectorAll("#pages .pc.sel").forEach(function (d) { d.classList.remove("sel"); }); inspector(); }
    });

    function render() { outline(); doc(); inspector(); }
    render();
    applyZoom();
    if (section && /[?&]section=/.test(location.search)) setTimeout(function () { go(section); }, 50);
  };
})();
