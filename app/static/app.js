/* mutz app: plain JS, no build step, no CDN. Mutz floats on every screen; the rest are small page hooks. */
(function () {
  "use strict";

  function $(s, r) { return (r || document).querySelector(s); }
  function $$(s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]; }); }
  function post(url, body) {
    return fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})})
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); });
  }
  window.mutzPost = post;
  window.$$ = $$;

  /* ------------------------------------------------------------ Mutz */
  var root = $("#mutz"), card = $("#mutzcard"), btn = $("#mutzbtn"), msgs = $("#mutzmsgs"), form = $("#mutzform"), input = $("#mutzask");
  var ctx = root ? {screen: root.dataset.screen, lead: root.dataset.lead, need: root.dataset.need} : {};
  var key = ctx.lead || "global";
  var selection = null;
  var loaded = false, busy = false;

  function open(q) {
    card.classList.remove("hidden"); root.classList.add("chat");
    btn.setAttribute("aria-expanded", "true");
    var b = $("#mutzbubble"); if (b) b.remove();
    if (!loaded) loadThread();
    if (q) { input.value = q; }
    input.focus();
    if (q && q.indexOf("[name]") < 0) send(q);
    else if (q) { input.setSelectionRange(q.indexOf("[name]"), q.indexOf("[name]") + 6); }
  }
  function close() { card.classList.add("hidden"); root.classList.remove("chat"); btn.setAttribute("aria-expanded", "false"); btn.focus(); }
  window.mutzOpen = open;

  function loadThread() {
    loaded = true;
    fetch("/api/mutz/thread?key=" + encodeURIComponent(key)).then(function (r) { return r.json(); }).then(function (d) {
      msgs.innerHTML = "";
      if (!d.messages.length) {
        addBot({text: "Grüessech! Bärner si gmüetlech. I bi d Usnahm.", note: _t("Hello! Bernese people take it slow. I'm the exception.")});
      }
      d.messages.forEach(function (m) { if (m.role === "user") addMe(m.text, m.file); else addBot(m); });
      var running = null;
      try { running = sessionStorage.getItem(DRAFT); } catch (x) {}
      if (running) drafting(running);  // a draft started on another screen is still being written
    }).catch(function () {});
  }

  function addMe(t, f) {
    var d = document.createElement("div"); d.className = "m me";
    d.innerHTML = '<span class="w">' + esc(_t("You")) + "</span>" + (f ? docCard(f) : "") + (t ? "<p>" + esc(t) + "</p>" : "");
    msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight;
    return d;
  }
  // a file sent in the chat, as a card that opens it; without a url it is still on its way
  function kb(n) { return n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB"; }
  function docCard(f) {
    var meta = [f.kind || "", f.pages ? _t(f.pages === 1 ? "1 page" : "{n} pages", {n: f.pages}) : "", f.size ? kb(f.size) : ""].filter(Boolean).join(" · ");
    var inner = '<span class="mzdoc-i" aria-hidden="true">' + esc((f.kind || "").slice(0, 4)) + '</span><span class="mzdoc-t"><b>' + esc(f.name) +
      '</b><span class="lb">' + (f.url ? esc(meta) : esc(_t("Sending…"))) + "</span></span>";
    return f.url ? '<a class="mzdoc" href="' + esc(f.url) + '" target="_blank" rel="noopener" title="' + esc(_t("Open the file")) + '">' + inner + "</a>"
                 : '<span class="mzdoc sending">' + inner + "</span>";
  }
  function cite(text, sources) {
    var h = esc(text);
    (sources || []).forEach(function (s) {
      h = h.split("[" + s.n + "]").join('<a href="' + esc(s.url) + '" target="_blank" rel="noopener">[' + s.n + "]</a>");
    });
    return h.replace(/\n/g, "<br>");
  }
  function addBot(m) {
    var d = document.createElement("div"); d.className = "m bot";
    var h = '<span class="w">Bärner Mutz</span><p>' + cite(m.text, m.sources) + "</p>";
    if (m.note) h += '<span class="lb">' + esc(m.note) + "</span>";
    if (m.table) {
      h += '<table class="sc"><thead><tr>' + m.table.head.map(function (x) { return "<th>" + esc(x) + "</th>"; }).join("") + "</tr></thead><tbody>";
      m.table.rows.forEach(function (r) { h += "<tr>" + r.map(function (c, i) { var hl = m.table.hl && m.table.hl.indexOf(r[0]) >= 0 && i === 1; return "<td" + (hl ? ' class="hl"' : "") + ">" + esc(c) + "</td>"; }).join("") + "</tr>"; });
      h += "</tbody></table>";
      if (m.table.note) h += '<span class="lb">' + esc(m.table.note) + "</span>";
    }
    if (m.sources && m.sources.length) {
      h += '<div style="display:flex;gap:6px;flex-wrap:wrap">' + m.sources.map(function (s) {
        return '<a class="chip" href="' + esc(s.url) + '" target="_blank" rel="noopener">[' + s.n + "] " + esc(s.label || s.url) + "</a>"; }).join("") + "</div>";
    }
    m.actions = (m.actions || []).filter(function (a) { return !(a.kind === "link" && a.href === location.pathname); });  // no link to this very screen
    if (m.actions.length) {
      h += '<div style="display:flex;gap:6px;flex-wrap:wrap">' + m.actions.map(function (a, i) {
        return '<button type="button" class="b s ' + (a.primary === false ? "" : "p") + '" data-act="' + i + '">' + esc(a.label) + "</button>"; }).join("") +
        (m.actions.some(function (a) { return a.kind !== "link"; }) ? '<button type="button" class="b s" data-cancel>' + esc(_t("Cancel")) + '</button>' : "") + "</div>";
    }
    if (m.after) h += '<span class="lb">' + esc(m.after) + "</span>";
    d.innerHTML = h;
    (m.actions || []).forEach(function (a, i) {
      var b = d.querySelector('[data-act="' + i + '"]');
      b.addEventListener("click", function () {
        if (a.kind === "link") { location.href = a.href; return; }
        b.disabled = true;
        post("/api/mutz/confirm", {key: key, action: a, ctx: ctx}).then(function (r) {
          b.parentNode.innerHTML = '<span class="lb">' + esc(r.message || _t("Done.")) + "</span>";
          if (r.reload) setTimeout(function () { location.reload(); }, 700);
        }).catch(function () { b.disabled = false; b.parentNode.insertAdjacentHTML("beforeend", '<span class="lb">' + esc(_t("That did not work. Try again.")) + '</span>'); });
      });
    });
    var c = d.querySelector("[data-cancel]");
    if (c) c.addEventListener("click", function () { c.parentNode.innerHTML = '<span class="lb">' + esc(_t("Cancelled. Nothing changed.")) + '</span>'; });
    msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight;
    return d;
  }

  // what the officer sees: visible text of the page, minus form fields and passages struck from the brief
  var SKIP = "script,style,template,textarea,input,select,svg,#mutz,#mutzcard,.ps.x,[data-mutz-skip],[aria-hidden=true]";
  function pageText() {
    var out = [];
    [$(".casebar"), $(".view")].forEach(function (root) {
      if (!root) return;
      var w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {acceptNode: function (n) {
        var p = n.parentElement;
        if (!p || !n.nodeValue.trim() || p.closest(SKIP)) return NodeFilter.FILTER_REJECT;
        if (p.checkVisibility ? !p.checkVisibility() : !p.offsetParent) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }});
      while (w.nextNode()) out.push(w.currentNode.nodeValue.trim());
    });
    return out.join(" ").replace(/\s+/g, " ").slice(0, 8000);
  }

  // a pitch rewrite (translate, shorten, …) takes a while; say so instead of "searching" (app/pitchrewrite.py routes these)
  var REWRITE = /translat|übersetz|tradu|rewrite|umschreib|riscriv|shorter|kürzer|breve|formal|german|deutsch|tedesco|english|englisch|inglese|italian|italien|french|franz|français|francese/i;

  /* a PDF, a text file or a pasted one pager goes straight to a pitch: Mutz reads it, searches, picks the matches and
     drafts (POST /api/mutz/pitch, app/main.py). The file goes into the chat as a card; a progress card then shows what
     is happening, need by need, and the pitch opens when it is written. */
  var file = null, LONG = 400, DRAFT = "mutz-draft", POTN = 0;
  function setFile(f) {
    file = f || null;
    $("#mutzfile").classList.toggle("hidden", !file);
    $("#mutzfilename").textContent = file ? file.name + " · " + kb(file.size) : "";
    input.placeholder = file ? _t("Add a note or the company website, or just press Ask") : input.dataset.ph;
  }
  function pot(cov) {  // the honey pot of _ui.html: full for strong, a little for thin, empty for none
    var id = "qpj" + (++POTN), body = "M30 20C14 22 6 34 6 54V86C6 106 22 118 50 118C78 118 94 106 94 86V54C94 34 86 22 70 20Z";
    return '<svg viewBox="0 0 100 122" width="14" height="16" aria-hidden="true">' + (cov === "strong" || cov === "thin" ?
      '<clipPath id="' + id + '"><path d="' + body + '"/></clipPath><path clip-path="url(#' + id + ')" fill="#FFCC00" d="M0 ' + (cov === "strong" ? 36 : 96) + 'q12.5-4 25 0t25 0t25 0t25 0V122H0Z"/>' : "") +
      '<path d="' + body + '" fill="none" stroke="#111" stroke-width="7.5" stroke-linejoin="round"/><rect x="25" y="3" width="50" height="17" rx="4" fill="#111"/></svg>';
  }
  function clock(s) { s = Math.max(0, Math.round(s)); return Math.floor(s / 60) + ":" + ("0" + s % 60).slice(-2); }
  var STEP = {read: 0, search: 1, write: 2};
  function progress(st) {  // 0..1: reading is short, the search moves need by need, writing runs about a minute
    var ns = st.needs || [], done = ns.filter(function (n) { return n.state === "done"; }).length, se = st.step_elapsed || 0;
    if (st.step === "read") return 0.03 + 0.15 * Math.min(se / 45, 1);
    if (st.step === "search") return 0.2 + 0.6 * (ns.length ? (done + 0.5 * Math.min(se / 30 / Math.max(done, 1), 1)) / ns.length : 0);
    return 0.82 + 0.16 * Math.min(se / 60, 1);
  }
  function stepsHtml(st) {
    var at = STEP[st.step] || 0, b = st.brief || {}, ns = st.needs || [], f = st.file;
    var done = ns.filter(function (n) { return n.state === "done"; }).length;
    function li(i, title, detail, extra) {
      var cls = i < at ? "done" : i === at ? "on" : "";
      return '<li class="' + cls + '"><span class="qp-d" aria-hidden="true"></span><div><b>' + esc(title) + "</b>" +
        (detail ? '<span class="lb">' + esc(detail) + "</span>" : "") + (extra || "") + "</div></li>";
    }
    var read = at === 0
      ? (f ? _t("Opening {name}", {name: f.name}) + (f.pages ? " · " + _t(f.pages === 1 ? "1 page" : "{n} pages", {n: f.pages}) : "") : st.source === "website" ? _t("Reading the company website") : _t("Reading the pasted text")) +
        ". " + _t("Revenue, shareholders and contact data are struck here first; Apertus 70B reads the rest.")
      : [b.company, b.sector, _t("needs found: {n}", {n: ns.length}), b.local ? _t("kept back: {n}", {n: b.local}) : ""].filter(Boolean).join(" · ");
    var rows = ns.map(function (n) {
      var right = n.state === "done" ? (n.top ? esc(n.top) + (n.more ? ' <span class="lb">+' + n.more + "</span>" : "") : '<span class="lb">' + esc(_t("No strong match")) + "</span>")
                : n.state === "search" ? '<span class="lb">' + esc(_t("Searching…")) + "</span>" : "";
      return '<li class="' + n.state + '"><span class="qp-n-i">' + (n.state === "done" ? pot(n.coverage) : n.state === "search" ? '<span class="qp-spin"></span>' : '<span class="qp-o"></span>') +
        '</span><span class="qp-n-t"><b>' + esc(n.label) + '</b><span class="lb">' + esc(n.text.replace(/^[-–•*\s]+/, "")) + '</span></span><span class="qp-n-r">' + right + "</span></li>";
    }).join("");
    var picks = (st.picks || []).map(function (p) {
      return '<li class="done"><span class="qp-n-i"><span class="qp-ok"></span></span><span class="qp-n-t"><b>' + esc(p.name) + '</b><span class="lb">' + esc(p.unit || "") +
        '</span></span><span class="qp-n-r lb">' + esc(p.need || "") + "</span></li>"; }).join("");
    return li(0, _t("Read the brief"), read) +
      li(1, _t("Search the Bern sources"), at >= 1 ? _t("Needs searched: {n} of {total}", {n: done, total: ns.length}) : "", rows ? '<ul class="qp-n">' + rows + "</ul>" : "") +
      li(2, _t("Pick the matches and write the pitch"), at === 2 ? _t("Apertus writes each card from its own quotes; code checks every quote.") : "",
         picks ? '<ul class="qp-n">' + picks + "</ul>" : "");
  }
  function drafting(lid) {
    var wait = document.createElement("div"); wait.className = "m bot qp"; wait.setAttribute("role", "status"); wait.setAttribute("aria-live", "polite");
    wait.innerHTML = '<span class="w">Bärner Mutz</span><div class="qp-b"><div class="qp-top"><div class="qp-hd"><span class="qp-bear">' + $("#tpl-search").innerHTML + "</span><div><b>" +
      esc(_t("Drafting the pitch")) + '</b><span class="lb">' + esc(_t("You can keep working. I open the pitch when it is written.")) +
      '</span></div><span class="qp-clock" aria-label="' + esc(_t("Time so far")) + '">0:00</span></div><span class="bar qp-bar"><i></i></span></div><ol class="qp-st"></ol></div>';
    msgs.appendChild(wait); msgs.scrollTop = msgs.scrollHeight;
    var list = wait.querySelector(".qp-st"), bar = wait.querySelector(".qp-bar i"), clk = wait.querySelector(".qp-clock");
    var tries = 0, base = 0, seen = Date.now(), last = "", ticker = setInterval(function () { clk.textContent = clock(base + (Date.now() - seen) / 1000); }, 1000);
    var peek = document.createElement("button"); peek.type = "button"; peek.className = "bubble qp-peek"; peek.addEventListener("click", function () { open(); });
    root.insertBefore(peek, btn);
    function show(st) {
      base = st.elapsed || 0; seen = Date.now(); clk.textContent = clock(base);
      bar.style.width = Math.round(100 * progress(st)) + "%";
      var h = stepsHtml(st);
      if (h !== last) {  // only redraw on news, so the list does not flicker every poll
        var stick = msgs.scrollHeight - msgs.scrollTop - msgs.clientHeight < 40;
        list.innerHTML = last = h;
        if (stick) msgs.scrollTop = msgs.scrollHeight;
      }
      var ns = st.needs || [], d = ns.filter(function (n) { return n.state === "done"; }).length;
      peek.innerHTML = "<b>" + esc(_t("Drafting the pitch")) + "</b><span>" + esc(st.step === "read" ? _t("Reading the brief") : st.step === "search" ?
        _t("Needs searched: {n} of {total}", {n: d, total: ns.length}) : _t("Writing the pitch")) + '</span><span class="bar"><i style="width:' + Math.round(100 * progress(st)) + '%"></i></span>';
    }
    show({step: "read"});
    (function poll() {
      fetch("/api/mutz/pitch/" + encodeURIComponent(lid)).then(function (r) { return r.json(); }).then(function (st) {
        if (st.step !== "done") { show(st); if (++tries < 900) setTimeout(poll, 1500); return; }
        clearInterval(ticker); peek.remove();
        try { sessionStorage.removeItem(DRAFT); } catch (x) {}
        msgs.replaceChild(addBot(st.reply), wait); msgs.scrollTop = msgs.scrollHeight;
        var a = document.activeElement, typing = a && a !== input && /^(INPUT|TEXTAREA)$/.test(a.tagName) && a.value;
        if (st.lead && !typing && location.pathname !== "/leads/" + st.lead + "/pitch") {  // the pitch is written: open it, chat and all
          try { sessionStorage.setItem("mutz-open", "1"); } catch (x) {}
          setTimeout(function () { location.href = "/leads/" + st.lead + "/pitch"; }, 2500);
        }
      }).catch(function () { if (++tries < 900) setTimeout(poll, 5000); });
    })();
  }
  function draft(q) {
    var f = file, fd = new FormData();
    busy = true;
    var me = addMe(f ? q : (q.length > 280 ? q.slice(0, 280) + "…" : q), f ? {name: f.name, size: f.size, kind: (f.name.split(".").pop() || "").toUpperCase()} : null);
    input.value = ""; setFile(null);
    fd.append("key", key); fd.append("lead", ctx.lead || ""); fd.append("text", q);
    if (f) fd.append("file", f);
    fetch("/api/mutz/pitch", {method: "POST", body: fd}).then(function (r) { return r.json(); }).then(function (r) {
      if (r.error) { addBot({text: r.error}); return; }
      if (r.file) { var c = me.querySelector(".mzdoc"); if (c) c.outerHTML = docCard(r.file); }
      try { sessionStorage.setItem(DRAFT, r.lead); } catch (x) {}
      drafting(r.lead);
    }).catch(function () {
      addBot({text: _t("The upload did not reach the server. Try again in a minute.")});
    }).finally(function () { busy = false; });
  }

  function send(q) {
    if (busy) return;
    if (file || q.trim().length >= LONG) return draft(q.trim());
    if (!q.trim()) return;
    busy = true;
    addMe(q); input.value = "";
    var wait = document.createElement("div"); wait.className = "m bot"; wait.setAttribute("role", "status");
    wait.innerHTML = '<span class="w">Bärner Mutz</span><div style="display:flex;gap:10px;align-items:center">' + $("#tpl-search").innerHTML + '<p class="lb">' + esc(_t(ctx.screen === "Pitch" && REWRITE.test(q) ? "Rewriting the pitch… this takes about half a minute." : "Bear with me… searching the sources.")) + '</p></div>';
    msgs.appendChild(wait); msgs.scrollTop = msgs.scrollHeight;
    post("/api/mutz", {key: key, message: q, ctx: Object.assign({page: pageText()}, ctx), selection: selection}).then(function (r) {
      wait.remove(); addBot(r);
      if (r.reload) {  // Mutz changed what is on screen (the pitch): show it, and keep the chat open across the reload
        try { sessionStorage.setItem("mutz-open", "1"); } catch (x) {}
        setTimeout(function () { location.reload(); }, 900);
      }
    }).catch(function () {
      wait.remove();
      addBot({text: _t("The Apertus gateway did not answer. Your question is saved here; try again in a minute.")});
    }).finally(function () { busy = false; });
  }

  if (root) {
    var bub = $("#mutzbubble");
    if (bub) setTimeout(function () { if (bub.parentNode) bub.remove(); }, 9000);  // proactive, but rarely: one bubble, then gone
    try { if (sessionStorage.getItem("mutz-open") || sessionStorage.getItem(DRAFT)) { sessionStorage.removeItem("mutz-open"); open(); } } catch (x) {}
    btn.addEventListener("click", function () { card.classList.contains("hidden") ? open() : close(); });
    input.dataset.ph = input.placeholder;
    $("#mutzclip").addEventListener("click", function () { $("#mutzfilein").click(); });
    $("#mutzfilein").addEventListener("change", function (e) { setFile(e.target.files[0]); e.target.value = ""; input.focus(); });
    $("#mutzfilex").addEventListener("click", function () { setFile(null); input.focus(); });
    card.addEventListener("dragover", function (e) { if (e.dataTransfer && [].indexOf.call(e.dataTransfer.types, "Files") >= 0) { e.preventDefault(); card.classList.add("drop"); } });
    card.addEventListener("dragleave", function (e) { if (!card.contains(e.relatedTarget)) card.classList.remove("drop"); });
    card.addEventListener("drop", function (e) {
      card.classList.remove("drop");
      if (!e.dataTransfer.files.length) return;
      e.preventDefault(); setFile(e.dataTransfer.files[0]); input.focus();
    });
    $("#mutzclose").addEventListener("click", close);
    form.addEventListener("submit", function (e) { e.preventDefault(); send(input.value); });
    document.addEventListener("keydown", function (e) {
      var t = e.target.tagName;
      if (e.key === "Escape" && !card.classList.contains("hidden")) close();
      else if ((e.key === "m" || e.key === "M") && !e.metaKey && !e.ctrlKey && t !== "INPUT" && t !== "TEXTAREA" && t !== "SELECT" && !e.target.isContentEditable) { e.preventDefault(); open(); }
    });
    // drag by the header, snap to the nearest corner, remember the corner per screen
    var hd = $("#mutzhd"), corner = null;
    try { corner = localStorage.getItem("mutz-corner-" + ctx.screen); } catch (e) {}
    if (corner) setCorner(corner);
    hd.addEventListener("pointerdown", function (e) {
      if (e.target.closest("button")) return;
      var r = card.getBoundingClientRect(), dx = e.clientX - r.left, dy = e.clientY - r.top;
      hd.setPointerCapture(e.pointerId);
      function mv(ev) { card.style.left = (ev.clientX - dx) + "px"; card.style.top = (ev.clientY - dy) + "px"; card.style.right = "auto"; card.style.bottom = "auto"; }
      function up(ev) {
        hd.removeEventListener("pointermove", mv); hd.removeEventListener("pointerup", up);
        var c = (ev.clientY < innerHeight / 2 ? "t" : "b") + (ev.clientX < innerWidth / 2 ? "l" : "r");
        setCorner(c); try { localStorage.setItem("mutz-corner-" + ctx.screen, c); } catch (e2) {}
      }
      hd.addEventListener("pointermove", mv); hd.addEventListener("pointerup", up);
    });
    function setCorner(c) {
      card.style.left = card.style.right = card.style.top = card.style.bottom = "";
      if (c[0] === "t") { card.style.top = "72px"; card.style.bottom = "auto"; }
      if (c[1] === "l") { card.style.left = "24px"; card.style.right = "auto"; } else { card.style.right = "24px"; card.style.left = "auto"; }
    }
    // links anywhere that ask Mutz something
    document.addEventListener("click", function (e) {
      var a = e.target.closest("[data-mutz-ask]");
      if (a) { e.preventDefault(); open(a.getAttribute("data-mutz-ask")); }
    });
  }
  window.mutzSelect = function (sel) {
    selection = sel;
    $("#mutzknows").textContent = _t(sel ? "Knows: {screen} · 1 selected" : "Knows: {screen}", {screen: ctx.screen ? _t(ctx.screen) : ""});
  };

  /* ------------------------------------------------------------ Brief */
  window.initBrief = function (lid, autoExtract) {
    var f = $("#briefform");
    function reading() { var r = $("#reading"); if (r) { r.classList.remove("hidden"); var n = $("#needs"); if (n) n.style.opacity = .4; var z = $("#noneeds"); if (z) z.classList.add("hidden"); } var b = $("#readbtn"); if (b) { b.disabled = true; b.textContent = _t("Reading…"); } }
    if (f) f.addEventListener("submit", reading);
    if (autoExtract && f) { reading(); f.submit(); }
    var t = $("[data-lead-title]");
    if (t) t.addEventListener("change", function () { post("/api/leads/" + lid, {title: t.value}); });
    var openNeed = null;
    try { openNeed = sessionStorage.getItem("openNeed"); sessionStorage.removeItem("openNeed"); } catch (x) {}
    $$("[data-need]").forEach(function (row) {
      var id = row.dataset.need;
      if (id === openNeed) { var dt = row.querySelector("details.nm"); if (dt) dt.open = true; }
      var tx = row.querySelector("[data-need-text]");
      if (tx) {
        tx.addEventListener("blur", function () { post("/api/needs/" + id, {text: tx.textContent}); });
        tx.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); tx.blur(); } });
      }
      row.querySelectorAll("[data-term]").forEach(function (cb) {
        cb.addEventListener("change", function () { post("/api/needs/" + id, {term: cb.dataset.term, on: cb.checked}); });
      });
      var add = row.querySelector("[data-addterm]");
      if (add) add.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); try { sessionStorage.setItem("openNeed", id); } catch (x) {} post("/api/needs/" + id, {add_term: add.value}).then(function () { location.reload(); }); }
      });
      var sh = row.querySelector("[data-share]");
      if (sh) sh.addEventListener("change", function () { post("/api/needs/" + id, {shareable: sh.checked}); });
      var del = row.querySelector("[data-del]");
      if (del) del.addEventListener("click", function () {
        post("/api/needs/" + id, {delete: true}).then(function () {
          row.innerHTML = '<div class="toast"><span style="flex:1">' + esc(_t("Need deleted")) + '</span><button class="b s" style="background:#111;color:#fff;border-color:#fff">' + esc(_t("Undo")) + '</button></div>';
          row.querySelector("button").addEventListener("click", function () { post("/api/needs/" + id, {restore: true}).then(function () { location.reload(); }); });
        });
      });
    });
    $$("[data-strike]").forEach(function (a) {
      a.addEventListener("click", function (e) {
        e.preventDefault(); a.textContent = _t("Updating…");
        post("/leads/" + lid + "/strike", {i: +a.dataset.strike}).then(function () { location.reload(); });
      });
    });
  };

  /* ------------------------------------------------------------ Matches */
  window.initMatches = function (lid, searching) {
    function setAcc(k) {
      var n = $("#nacc"); if (n) n.textContent = k;
      var m = $("#mapbtn"), h = $("#maphint"); if (!m) return;
      m.classList.toggle("off", !k);
      if (k) { m.removeAttribute("aria-disabled"); m.removeAttribute("tabindex"); } else { m.setAttribute("aria-disabled", "true"); m.setAttribute("tabindex", "-1"); }
      if (h) h.hidden = !!k;
    }
    document.addEventListener("click", function (e) { $$("details.fp[open]").forEach(function (d) { if (!d.contains(e.target)) d.open = false; }); });
    if (searching) {
      var t = setInterval(function () {
        fetch("/api/leads/" + lid + "/status").then(function (r) { return r.json(); }).then(function (d) {
          var done = d.needs.filter(function (n) { return n.done; }).length;
          if (d.state !== "running" || done === d.needs.length) { clearInterval(t); location.href = location.pathname + "?fresh=1"; }
          else if (+document.body.dataset.done !== done) { document.body.dataset.done = done; if (done > 0 && !location.search) location.reload(); }
        });
      }, 1500);
    }
    $$("article[data-entity]").forEach(function (a) {
      a.addEventListener("click", function (e) {
        var b = e.target.closest("[data-decide]");
        if (b) {
          b.disabled = true;
          post("/api/decide", {lead: lid, need: a.dataset.need, entity: a.dataset.entity, decision: b.dataset.decide || null}).then(function (r) {
            var box = a.querySelector(".dec"), d = b.dataset.decide;
            box.innerHTML = d === "accept" ? '<span style="font-size:13px;font-weight:700">' + esc(_t("Accepted")) + '</span> <button class="b s" data-decide="">' + esc(_t("Undo")) + '</button>'
              : d === "reject" ? '<span style="font-size:13px;color:#5E5E5E">' + esc(_t("Rejected")) + '</span> <button class="b s" data-decide="">' + esc(_t("Undo")) + '</button>'
              : '<button class="b s" data-decide="reject">' + esc(_t("Reject")) + '</button> <button class="b s p" data-decide="accept">' + esc(_t("Accept")) + '</button>';
            setAcc(r.accepted);
          });
          return;
        }
        if (e.target.closest("a,button,input")) return;
        $$("article.sel").forEach(function (x) { x.classList.remove("sel"); });
        a.classList.add("sel");
        window.mutzSelect({entity_id: a.dataset.entity, need: a.dataset.need, name: a.querySelector("h2").textContent});
      });
    });
    $$("[data-add-curated]").forEach(function (b) {
      b.addEventListener("click", function () {
        var m = JSON.parse(b.getAttribute("data-add-curated"));
        var need = b.dataset.need || (document.querySelector("#mutz") || {}).dataset.need;
        b.disabled = true;
        post("/api/decide", {lead: lid, need: need, entity: m.entity_id, decision: "accept", match: m, added_by: "officer"}).then(function (r) {
          b.outerHTML = '<span style="font-size:13px;font-weight:700">' + esc(_t("Added to pitch")) + '</span>';
          setAcc(r.accepted);
        });
      });
    });
    $$("[data-anyway]").forEach(function (b) {
      b.addEventListener("click", function () {
        var m = JSON.parse(b.getAttribute("data-anyway"));
        if (!m.reasons || !m.reasons.length) { b.insertAdjacentHTML("afterend", '<span class="lb"> ' + esc(_t("No quote for this need: a pitch card needs a source. Add one on the Pitch screen.")) + '</span>'); }
        post("/api/decide", {lead: lid, need: (document.querySelector("#mutz") || {}).dataset.need, entity: m.entity_id, decision: "accept", match: m, added_by: "added by you"})
          .then(function (r) { b.outerHTML = '<span style="font-size:13px;font-weight:700">' + esc(_t("Added by you")) + '</span>'; setAcc(r.accepted); });
      });
    });
    $$("[data-missed]").forEach(function (b) {
      b.addEventListener("click", function () {
        post("/api/missed", {name: b.dataset.missed, entity_id: b.dataset.eid, query: b.dataset.q})
          .then(function () { b.outerHTML = '<span class="lb">' + esc(_t("Saved as a new test case.")) + '</span>'; });
      });
    });
    $$("[data-leave]").forEach(function (b) {
      b.addEventListener("click", function () { post("/api/needs/" + b.dataset.leave, {delete: true}).then(function () { location.href = location.pathname; }); });
    });
  };
})();
