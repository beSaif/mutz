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
  /* What the wait panel says while the server works, one line per real step; the last line stays until the answer
     comes. The facts: the cards from the accepted matches, Apertus writing them, then suggestions for open plans. */
  var BUILD = {read: ["Reading the matches and pulling out the facts…", 2500], write: ["Writing a card for each match, from its quotes only…", 20000],
    suggest: ["Looking for suggestions for the needs with no match yet…", 0]};
  var WRITE = [["Reading the source pages around each checked fact…", 6000], ["Apertus is writing the paragraphs for {company}…", 40000],
    ["Checking every sentence against its source page…", 0]];
  /* The page shows the document as it prints, so its fixed words are in the pitch language (draft.lang, catalog from
     pitch/locales, passed in as meta.doc); the controls around it stay in the interface language (_t). */
  var DOC = {};
  function _d(s, o) { var r = DOC[s] || s; if (o) for (var k in o) r = r.split("{" + k + "}").join(o[k]); return r; }

  window.initPitch = function (lid, draft, section, meta) {
    var selected = null, saveT = null, undo = null, zoom = null;
    var $ = function (s) { return document.querySelector(s); };
    DOC = meta.doc || {};

    function save() {
      clearTimeout(saveT);
      saveT = setTimeout(function () {
        window.mutzPost("/api/pitch/" + lid, {draft: draft}).then(function (r) { var s = $("#saved"); if (s) s.textContent = _t("Draft saved {date}", {date: r.saved}); });
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
    function groupAt(c) { return _d("Group at {unit}", {unit: c.unit || _d("the University of Bern")}); }
    function heading(c) { return c.type === "person" && !c.show_person_name ? groupAt(c) : c.name; }
    function kindOf(c, i) {
      if (c.section === "why") return draft.written_at ? "" : _d("Point {n}", {n: i + 1});  // written, the reasons read as paragraphs
      return c.type === "person" ? _d("Researcher") : _d((c.type || "match").replace(/_/g, " ").replace(/^./, function (x) { return x.toUpperCase(); }));
    }
    function status(c) {
      if (!(c.sources || []).length) return ["x", _t("No source: cannot be exported")];
      if (c.checked) return ["ok", "✓ " + _t("Checked")];
      return [c.edited ? "e" : "", (c.edited ? _t("Edited by you") + " · " : "") + (c.origin === "suggested" ? _t("Suggested for you to check") + " · " : "") + (c.picked_by === "mutz" ? _t("Picked by Bärner Mutz") + " · " : "") + _t("Not checked yet")];
    }
    /* pages: cover, then one page per part (sections of the same part share it), then sources */
    function pages() {
      var out = [{kind: "cover", secs: [sec("cover") || {id: "cover", title: _d("Cover")}]}], last = null;
      draft.sections.forEach(function (s) {
        if (s.deleted || s.id === "cover" || s.id === "sources") return;
        var p = part(s);
        if (last && last.part === p) last.secs.push(s);
        else { last = {kind: "part", part: p, secs: [s]}; out.push(last); }
      });
      out.push({kind: "sources", secs: [sec("sources") || {id: "sources", title: _d("Sources")}]});
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
        if (p && p !== lastPart) el.insertAdjacentHTML("beforeend", '<p class="ol-h" title="' + esc(_t(GUIDE[p])) + '">' + esc(_t(PART_LABEL[p])) + "</p>");
        if (!p && lastPart) el.insertAdjacentHTML("beforeend", '<p class="ol-h" aria-hidden="true"></p>');
        lastPart = p;
        var n = cards(s.id).length;
        var d = document.createElement("div");
        d.className = "ol" + (p ? " sub" : "") + (s.hidden ? " off" : "");
        d.draggable = !fixed; d.dataset.i = i; d.dataset.sec = s.id;
        if (!fixed) d.title = _t("Drag to reorder");
        d.innerHTML = '<a href="#sec-' + esc(s.id) + '">' + esc(s.title) + "</a>" +
          (s.id === "matches" || s.id === "infra" || s.id === "why" ? "<i>" + n + "</i>" : "") +
          (fixed ? "" : '<button class="ic" data-eye aria-label="' + esc(s.hidden ? _t("Show {name}", {name: s.title}) : _t("Hide {name}", {name: s.title})) + '">' + (s.hidden ? EYEOFF : EYE) + '</button><button class="ic" data-bin aria-label="' + esc(_t("Delete {name}", {name: s.title})) + '">' + BIN + "</button>");
        d.querySelector("a").addEventListener("click", function (e) { e.preventDefault(); go(s.id); });
        var eye = d.querySelector("[data-eye]"); if (eye) eye.addEventListener("click", function () { s.hidden = !s.hidden; save(); render(); });
        var bin = d.querySelector("[data-bin]"); if (bin) bin.addEventListener("click", function () { s.deleted = true; toast(_t("Section “{name}” deleted", {name: s.title}), function () { s.deleted = false; }); save(); render(); });
        d.addEventListener("dragstart", function (e) { e.dataTransfer.setData("text/plain", i); d.classList.add("drag"); });
        d.addEventListener("dragend", function () { d.classList.remove("drag"); });
        d.addEventListener("dragover", function (e) { e.preventDefault(); });
        d.addEventListener("drop", function (e) {
          e.preventDefault(); var from = +e.dataTransfer.getData("text/plain"), to = i;
          var a = draft.sections[from], b = draft.sections[to];
          if (from === to || b.auto || b.id === "cover") return;
          if (part(a) !== part(b)) { toast(_t("Sections stay in their part: why, then what, then how."), null); return; }
          var m = draft.sections.splice(from, 1)[0]; draft.sections.splice(to, 0, m); save(); render();
        });
        el.appendChild(d);
      });
    }
    function toast(msg, undoFn) {
      undo = undoFn;
      var t = $("#toast");
      t.innerHTML = '<div class="toast" style="margin-top:14px"><span style="flex:1">' + esc(msg) + "</span>" + (undoFn ? '<button class="b s" style="background:#111;color:#fff;border-color:#fff">' + esc(_t("Undo")) + '</button>' : "") + "</div>";
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
      var left = pg.kind === "cover" ? esc(_d("Prototype, not an official Canton of Bern tool")) : "";
      return '<div class="pg-ft"><span>' + left + "</span><span>" + (pg.kind === "cover" ? "" : pg.n ? pg.n + " / " + total : esc(_t("Not in the PDF"))) + "</span></div>";
    }
    function coverHtml() {
      var o = draft.officer || {};
      return '<div class="pg-in" id="sec-cover">' +
        (meta.logo ? '<img class="pg-logo" src="/logo.png" alt="' + esc(_d(AGENCY)) + '">' : '<b class="pg-logo">' + esc(_d(AGENCY)) + "</b>") +
        '<h1 class="ed" contenteditable="true" spellcheck="true" data-f="title" data-ph="' + esc(_t("Title")) + '">' + esc(draft.title || "") + "</h1>" +
        '<p class="pg-for">' + esc(_d("For {name}", {name: meta.company || ""})) + "</p>" +
        '<table class="pg-meta"><tr><td>' + esc(_d("Date")) + "</td><td>" + esc(meta.date || "") + "</td></tr>" +
        "<tr><td>" + esc(_d("Contact")) + '</td><td><span class="ed" contenteditable="true" data-f="officer.name" data-ph="' + esc(_t("Contact officer")) + '">' + esc(o.name || "") + "</span><br>" + esc(_d(AGENCY)) +
        '<br><span class="ed" contenteditable="true" data-f="officer.email" data-ph="' + esc(_t("Officer email")) + '">' + esc(o.email || "") + "</span></td></tr></table>" +
        '<p class="pg-intro ed" contenteditable="true" spellcheck="true" data-f="intro" data-ph="' + esc(_t("Add an opening line, optional. For example what the company is looking for and what this document lists.")) + '">' + esc(draft.intro || "") + "</p></div>";
    }
    function sectionHtml(s, first, nums) {
      var h = '<div class="pg-sec' + (s.hidden ? " off" : "") + '" id="sec-' + esc(s.id) + '">' +
        (first ? '<p class="pg-k">' + esc(_d(PART_LABEL[part(s)])) + "</p>" : "") +
        '<h2 class="ed" contenteditable="true" spellcheck="true" data-title="' + esc(s.id) + '">' + esc(s.title) + "</h2>" +
        (s.hidden ? '<p class="pg-off">' + esc(_t("Hidden from the PDF.")) + ' <button type="button" class="b s" data-show="' + esc(s.id) + '">' + esc(_t("Show it")) + '</button></p>' : "");
      if (s.id === "visit") {
        h += '<p class="pg-lede ed" contenteditable="true" spellcheck="true" data-f="how" data-ph="' + esc(_t("How the company starts: what to tell you, what the agency does next.")) + '">' + esc(draft.how || "") + "</p>" +
          '<table class="pg-visit">' + (draft.visit || []).map(function (r, i) {
            return '<tr><td class="ed" contenteditable="true" data-vt="' + i + '">' + esc(r.time) + '</td><td><span class="ed" contenteditable="true" data-vw="' + i + '">' + esc(r.what) + "</span>" +
              (r.status ? '<span class="pg-st">' + esc(_t(r.status)) + "</span>" : "") + '</td><td><button type="button" class="ic pg-x" data-vdel="' + i + '" aria-label="' + esc(_t("Remove this line")) + '">×</button></td></tr>';
          }).join("") + "</table>" +
          '<button type="button" class="b s dash pg-add" data-vadd>' + esc(_t("Add a line")) + '</button>';
        return h + "</div>";
      }
      var list = cards(s.id);
      h += s.id === "why" ? '<p class="pg-lede">' + esc(list.length === 1 ? _d("1 reason, each with a source.") : _d("{n} reasons, each with a source.", {n: list.length})) + "</p>" : '<p class="pg-lede">' + esc(_d(LEDE[s.id] || "")) + "</p>";
      if (!list.length) h += '<p class="pg-empty">' + esc(_t("No cards in this section. Accept matches on the Matches screen, or ask Bärner Mutz to add one.")) + '</p>';
      list.forEach(function (c, i) {
        var st = status(c), n = nums[c.card_id] || [];
        h += '<div class="pc' + (selected === c.card_id ? " sel" : "") + (c.locked ? " lk" : "") + '" data-card="' + esc(c.card_id) + '" tabindex="-1">' +
          '<p class="pc-k">' + esc(kindOf(c, i)) + ' <span class="pc-st ' + st[0] + '">' + (kindOf(c, i) ? "· " : "") + esc(st[1]) + (c.locked ? " · " + esc(_t("locked")) : "") + "</span></p>" +
          (s.id !== "why" ? "<h3>" + esc(heading(c)) + "</h3>" + (c.unit && c.unit !== heading(c) && c.unit !== c.name ? '<p class="pc-sub">' + esc(c.unit) + "</p>" : "") : "") +
          '<p class="pc-t ed" lang="' + esc(draft.lang || "en") + '"' + (c.locked ? "" : ' contenteditable="true"') + ' spellcheck="true" data-text>' + esc(shown(c, n)) + "</p>" +
          '<p class="pc-src">' + (c.sources || []).map(function (x, k) { return '<a href="' + esc(x.deep_link || x.url) + '" target="_blank" rel="noopener" title="' + esc(x.quote || "") + '">[' + n[k] + "] " + esc(host(x.url)) + "</a>"; }).join(" ") + "</p></div>";
      });
      return h + "</div>";
    }
    function sourcesHtml(nums) {
      var h = '<div class="pg-in" id="sec-sources"><h2>' + esc(_d("Sources")) + '</h2><p class="pg-lede" style="margin-bottom:6px">' + esc(_d(LEDE.sources)) + '</p><p class="pg-lede">' + esc(_d(SOVEREIGN)) + '</p><ol class="pg-srcs">', done = {};
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
      el.querySelectorAll("[data-vdel]").forEach(function (b) { b.addEventListener("click", function () { var i = +b.dataset.vdel, r = draft.visit.splice(i, 1)[0]; toast(_t("Line “{name}” removed", {name: (r.what || "").slice(0, 40)}), function () { draft.visit.splice(i, 0, r); }); save(); render(); }); });
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
      if (d) { d.className = "pc-st " + st[0]; d.textContent = (kindOf(c, 0) ? "· " : "") + st[1] + (c.locked ? " · " + _t("locked") : ""); }
      var ck = document.querySelector('[data-card="' + c.card_id + '"] .pc-ev [data-check]');  // an edit undoes the check
      if (ck && !c.checked && ck.textContent.charAt(0) === "✓") { ck.textContent = _t("Yes, looks right"); ck.classList.add("p"); }
      outline(); dock(); docPanel($("#inspector"));
    }
    function pick(id) {
      if (selected === id) return;
      selected = id;
      document.querySelectorAll("#pages .pc").forEach(function (d) { d.classList.toggle("sel", d.dataset.card === id); });
      inspector();
      var c = card(id); if (c && window.mutzSelect) window.mutzSelect({card_id: id, name: c.text});
    }

    function show(id) {  // select a card and bring it into view, from the dock or a ?card= link
      var d = document.querySelector('#pages [data-card="' + id + '"]'); if (!d) return;
      pick(id);
      $("#canvas").scrollTo({top: d.getBoundingClientRect().top - $("#canvas").getBoundingClientRect().top + $("#canvas").scrollTop - 80, behavior: "smooth"});
    }
    function nextUnchecked() {  // the next card down the page that still needs a check, wrapping to the top
      var all = allVisible(), at = all.map(function (c) { return c.card_id; }).indexOf(selected);
      for (var i = 1; i <= all.length; i++) { var c = all[(at + i) % all.length]; if (!c.checked && (c.sources || []).length) return c; }
      return null;
    }
    function acceptAll() {
      var todo = allVisible().filter(function (c) { return !c.checked && (c.sources || []).length; });
      if (!todo.length) return;
      todo.forEach(function (c) { c.checked = true; });
      selected = null; save(); render();
    }

    /* ---------------- review box: drawn under the selected card, never printed */
    function inspector() {
      docPanel($("#inspector"));
      document.querySelectorAll("#pages .pc-ev").forEach(function (x) { x.remove(); });
      var c = selected && card(selected), d = c && document.querySelector('#pages [data-card="' + c.card_id + '"]');
      if (!c || c.deleted || !d) { selected = null; return; }
      var nums = numbering()[c.card_id] || [], lk = c.locked ? " disabled" : "";
      var other = (draft.lang || "en") !== "en" ? ["en", _t("In English")] : ["de", _t("In German")];
      d.insertAdjacentHTML("beforeend", '<div class="pc-ev">' +
        '<p class="pc-ev-h">' + esc(_t("What the sources say")) + '</p>' +
        ((c.sources || []).length ? (c.sources || []).map(function (s, k) {
          return '<p class="pc-q"><b>[' + nums[k] + "]</b><span>" + (s.quote ? "“" + esc(s.quote) + "” " : "") +
            '<a href="' + esc(s.deep_link || s.url) + '" target="_blank" rel="noopener">' + esc(host(s.url)) + " ↗</a></span></p>";
        }).join("") : '<p class="pc-q" style="color:var(--red-h)">' + esc(_t("No source. Add one under More, or delete the card.")) + '</p>') +
        '<p class="pc-ask">' + esc(_t("Does the card say only this?")) + '</p>' +
        '<div class="pc-act">' +
        (c.checked ? '<button type="button" class="b s" data-check>✓ ' + esc(_t("Checked · undo")) + '</button>' : '<button type="button" class="b s p" data-check' + ((c.sources || []).length ? "" : " disabled") + ">" + esc(_t("Yes, looks right")) + "</button>") +
        '<details class="pc-menu"><summary class="b s">' + esc(_t("Rewrite")) + '</summary><div>' +
        '<button type="button" data-rw="shorter"' + lk + '>' + esc(_t("Shorter")) + '</button><button type="button" data-rw="jargon"' + lk + '>' + esc(_t("Less jargon")) + '</button><button type="button" data-rw="' + other[0] + '"' + lk + ">" + esc(other[1]) + "</button></div></details>" +
        '<button type="button" class="b s" data-del>' + esc(_t("Delete")) + '</button>' +
        '<details class="pc-menu"><summary class="b s">' + esc(_t("More")) + '</summary><div>' +
        '<button type="button" data-addsrc>' + esc(_t("Add a source")) + '</button>' +
        '<button type="button" data-lock>' + esc(c.locked ? _t("Unlock the text") : _t("Lock the text")) + "</button>" +
        '<a href="/leads/' + lid + '/map">' + esc(_t("Show in the map")) + '</a>' +
        '<a href="#" data-mutz-ask="' + esc(_t("Is this claim backed? “{text}”", {text: (c.text || "").slice(0, 160)})) + '">' + esc(_t("Ask Bärner Mutz")) + '</a></div></details></div>' +
        '<input data-src class="in hidden" placeholder="' + esc(_t("Paste a https:// address, then press Enter")) + '" aria-label="' + esc(_t("Add a source URL")) + '">' +
        (c.type === "person" ? '<label class="lb pc-nm"><input type="checkbox" data-name ' + (c.show_person_name ? "checked" : "") + "><span>" + esc(_t("Name {name} in the pitch. Off: “{alt}”.", {name: c.name, alt: groupAt(c)})) + "</span></label>" : "") +
        '<p class="lb hidden" data-msg></p></div>');
      var ev = d.querySelector(".pc-ev");
      function msg(t) { var m = ev.querySelector("[data-msg]"); m.textContent = t; m.classList.remove("hidden"); }
      ev.querySelectorAll(".pc-menu").forEach(function (m) {  // one menu open at a time
        m.addEventListener("toggle", function () { if (m.open) ev.querySelectorAll(".pc-menu").forEach(function (o) { if (o !== m) o.open = false; }); });
      });
      ev.querySelector("[data-check]").addEventListener("click", function (e) {
        e.stopPropagation();  // the box is redrawn below; let the click bubble and the card or canvas would take it back
        c.checked = !c.checked; save(); refresh(c);
        var nx = c.checked && nextUnchecked();
        if (nx) show(nx.card_id);
        else { inspector(); dock(); if (c.checked) toast(draft.written_at ? _t("Every card is checked. Continue to export when you are ready.") : _t("Every card is checked. Write the pitch next."), null); }
      });
      ev.querySelector("[data-lock]").addEventListener("click", function () { c.locked = !c.locked; save(); render(); });
      ev.querySelector("[data-del]").addEventListener("click", function () { c.deleted = true; selected = null; toast(_t("Card “{name}” deleted", {name: (c.name || _t("card")).slice(0, 40)}), function () { c.deleted = false; }); save(); render(); });
      ev.querySelector("[data-addsrc]").addEventListener("click", function () { var i = ev.querySelector("[data-src]"); i.classList.remove("hidden"); i.focus(); ev.querySelectorAll(".pc-menu").forEach(function (o) { o.open = false; }); });
      var nm = ev.querySelector("[data-name]"); if (nm) nm.addEventListener("change", function (e) { c.show_person_name = e.target.checked; save(); render(); });
      ev.querySelector("[data-src]").addEventListener("keydown", function (e) {
        if (e.key !== "Enter") return; e.preventDefault();
        var u = e.target.value.trim(); if (!/^https?:\/\/\S+\.\S+/.test(u)) { msg(_t("Enter a full address starting with https://")); return; }
        c.sources = (c.sources || []).concat([{url: u, quote: "", deep_link: u, added_by_officer: true}]); c.edited = true; c.checked = false; save(); render();
      });
      ev.querySelectorAll("[data-rw]").forEach(function (b) {
        b.addEventListener("click", function () {
          b.closest("details").open = false; b.disabled = true; msg(_t("Rewriting from this card's sources only…"));
          window.mutzPost("/api/pitch/" + lid + "/rewrite", {card_id: c.card_id, instruction: b.dataset.rw}).then(function (r) {
            if (!r.ok) { msg(r.error); b.disabled = false; return; }
            c.text = r.text; c.checked = false; save(); render();
          }).catch(function () { msg(_t("The Apertus gateway did not answer. The card is unchanged. Try again in a minute.")); b.disabled = false; });
        });
      });
    }
    function docPanel(el) {  // the side panel: three steps, check the facts, write the pitch, export
      var vis = allVisible(), nos = vis.filter(function (c) { return !(c.sources || []).length; }).length;
      var ck = vis.filter(function (c) { return c.checked; }).length, pc = vis.length ? Math.round(100 * ck / vis.length) : 0;
      var at = ck < vis.length || !vis.length ? 1 : draft.written_at ? 3 : 2;
      var head = {1: [_t("Check the facts"), _t("Click a card on the page and compare it with its sources.")],
        2: [_t("Write the pitch"), _t("Every fact is checked. Apertus now turns them into paragraphs for {company}, from their source pages only.", {company: meta.company || _t("the company")})],
        3: [_t("Read it through"), _t("Edit any line on the page, then export.")]}[at];
      el.innerHTML = '<h2 style="font-size:18px;margin:0 0 4px">' + esc(head[0]) + '</h2><p class="lb" style="margin:0 0 14px">' + esc(head[1]) + "</p>" +
        (at === 1 ? '<div class="meter" title="' + esc(_t("Cards checked") + ": " + _t("{n} of {total}", {n: ck, total: vis.length})) + '"><span class="meter-l"><span>' + esc(_t("Cards checked")) + '</span><span class="lb">' + esc(_t("{n} of {total}", {n: ck, total: vis.length})) + "</span></span>" +
          '<span class="bar" role="meter" aria-label="' + esc(_t("Cards checked")) + '" aria-valuemin="0" aria-valuemax="' + vis.length + '" aria-valuenow="' + ck + '"><i style="width:' + pc + '%"></i></span></div>' : "") +
        (nos ? '<p style="margin:12px 0 0;font-size:14px;color:#B8000F">' + esc(nos === 1 ? _t("1 card has no source. Add one or delete the card.") : _t("{n} cards have no source. Add one or delete the card.", {n: nos})) + "</p>" : "") +
        '<ol class="isteps">' + [_t("Check the facts"), _t("Write the pitch"), _t("Export")].map(function (t, i) {
          var n = i + 1; return '<li class="' + (n < at ? "done" : n === at ? "on" : "") + '"><b>' + (n < at ? "✓" : n) + "</b>" + esc(t) + "</li>";
        }).join("") + "</ol>";
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
      $("#pgno").textContent = cur && cur.dataset.n ? _t("Page {n} of {total}", {n: cur.dataset.n, total: total}) : _t("Hidden page · {n} pages in the PDF", {n: total});
      var at = null; document.querySelectorAll("#pages [id^=sec-]").forEach(function (s) { if (s.getBoundingClientRect().top <= top) at = s.id.slice(4); });
      at = at || "cover";
      document.querySelectorAll("#outline .ol").forEach(function (o) { o.classList.toggle("on", o.dataset.sec === at); });
    }
    $("#canvas").addEventListener("scroll", pageNo, {passive: true});
    document.addEventListener("click", function (e) {  // a click elsewhere closes an open Rewrite or More menu
      document.querySelectorAll(".pc-menu[open]").forEach(function (m) { if (!m.contains(e.target)) m.open = false; });
    });
    document.addEventListener("keydown", function (e) {  // Escape closes the card overlay
      if (e.key !== "Escape" || !selected || e.target.closest("[contenteditable]")) return;
      selected = null; document.querySelectorAll("#pages .pc.sel").forEach(function (d) { d.classList.remove("sel"); }); inspector();
    });
    $("#canvas").addEventListener("click", function (e) {  // a click on the grey canvas, outside any card, shows the document panel
      if (!e.target.closest(".pc") && !e.target.closest("[contenteditable]") && selected) { selected = null; document.querySelectorAll("#pages .pc.sel").forEach(function (d) { d.classList.remove("sel"); }); inspector(); }
    });

    /* ---------------- write the pitch: the facts become paragraphs, then export */
    var writing = false;
    function dock() {
      var el = $("#pdock"), exp = "/leads/" + lid + "/export";
      var left = allVisible().filter(function (c) { return !c.checked && (c.sources || []).length; }).length;
      var next = '<button type="button" class="b p" data-next style="justify-content:center">' + esc(_t("Next card to check")) + '</button>' +
        '<button type="button" class="b s" data-all style="justify-content:center;align-self:center">' + esc(left === 1 ? _t("Accept the last card") : _t("Accept all {n} cards", {n: left})) + '</button>';
      if (draft.written_at) {  // export waits until every card is checked again
        el.innerHTML = (left ? next + '<p class="lb" style="margin:0">' + esc(left === 1 ? _t("1 card left to check against its sources") : _t("{n} cards left to check against their sources", {n: left})) + '</p>'
          : '<a class="b p" href="' + exp + '" style="justify-content:center">' + esc(_t("Continue to export")) + '</a>' +
            '<button type="button" class="b s" data-write style="justify-content:center">' + esc(_t("Write it again")) + '</button>');
      } else if (left) {  // the pitch is written from checked facts only
        el.innerHTML = next + '<p class="lb" style="margin:0">' + esc(left === 1 ? _t("1 card left. Then you can write the pitch.") : _t("{n} cards left. Then you can write the pitch.", {n: left})) + "</p>";
      } else {
        el.innerHTML = '<button type="button" class="b p" data-write style="justify-content:center"' + (writing ? " disabled" : "") + ">" + esc(writing ? _t("Writing the pitch…") : _t("Write the pitch")) + "</button>";
      }
      var b = el.querySelector("[data-write]"); if (b) b.addEventListener("click", write);
      var nx = el.querySelector("[data-next]"); if (nx) nx.addEventListener("click", function () { var c = nextUnchecked(); if (c) show(c.card_id); });
      var all = el.querySelector("[data-all]"); if (all) all.addEventListener("click", acceptAll);
    }
    function write() {
      if (writing) return;
      clearTimeout(saveT);  // the request carries the draft, unsaved edits included
      writing = true; dock(); $("#pages").classList.add("busy");
      wait(WRITE.map(function (l) { return [_t(l[0], {company: meta.company || _t("the company")}), l[1]]; }), _t("This takes up to a minute."));
      var done = function () { writing = false; $("#pages").classList.remove("busy"); wait(null); };
      window.mutzPost("/api/pitch/" + lid + "/write", {draft: draft}).then(function (r) {
        done();
        if (!r.ok) { dock(); toast(r.error, null); return; }
        draft = r.draft; selected = null; render();
        var s = $("#saved"); if (s) s.textContent = _t("Draft saved {date}", {date: r.saved});
        $("#canvas").scrollTo({top: 0, behavior: "smooth"});
      }).catch(function () { done(); dock(); toast(_t("Apertus did not write the pitch this time. Nothing changed. Try again in a minute."), null); });
    }

    /* ---------------- wait panel over the canvas: the facts being drafted, the pitch being written */
    var waitT = null;
    function wait(steps, note) {  // steps: [[line, ms until the next line], ...]; null hides the panel
      clearTimeout(waitT);
      $("#pwait").classList.toggle("hidden", !steps);
      if (!steps) return;
      $("#pw-n").textContent = note || ""; $("#pw-n").classList.remove("hidden"); $("#pw-bar").classList.remove("hidden"); $("#pw-retry").classList.add("hidden");
      $('#pwait [data-pose="wait"]').classList.remove("hidden"); $('#pwait [data-pose="sad"]').classList.add("hidden");
      var i = 0;
      (function line() {
        $("#pw-h").textContent = steps[i][0];
        if (i < steps.length - 1) waitT = setTimeout(function () { i++; line(); }, steps[i][1]);
      })();
    }
    function failed(msg, retry) {  // the panel stays, with what went wrong and a retry
      clearTimeout(waitT);
      $("#pw-h").textContent = msg; $("#pw-n").classList.add("hidden"); $("#pw-bar").classList.add("hidden");
      $('#pwait [data-pose="wait"]').classList.add("hidden"); $('#pwait [data-pose="sad"]').classList.remove("hidden");
      var b = $("#pw-retry"); b.classList.remove("hidden"); b.onclick = retry; b.focus();
    }

    function render() { outline(); doc(); inspector(); dock(); }
    function start() {
      if (draft.how == null) draft.how = _d(HOW);
      wait(null); $("#pdock").classList.remove("hidden");
      render();
      applyZoom();
      var want = new URLSearchParams(location.search).get("card");
      if (want) setTimeout(function () { show(want); }, 50);
      else if (section && /[?&]section=/.test(location.search)) setTimeout(function () { go(section); }, 50);
    }
    function build() {  // the page opened before its draft was ready: ask for it, with a line per step meanwhile
      wait((meta.pending || ["read", "write", "suggest"]).map(function (k) { return [_t(BUILD[k][0]), BUILD[k][1]]; }),
        _t("This takes up to a minute. The cards arrive unchecked, for you to compare with their sources."));
      var oops = _t("The facts for the pitch could not be drafted this time. Nothing was lost. Try again in a minute.");
      window.mutzPost("/api/pitch/" + lid + "/build", {}).then(function (r) {
        if (!r.ok) { failed(r.error || oops, build); return; }
        draft = r.draft; start();
      }).catch(function () { failed(oops, build); });
    }
    if (draft) start();
    else { $("#pdock").classList.add("hidden"); build(); }  // no export link while there is nothing to export
  };
})();
