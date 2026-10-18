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
    card.classList.remove("hidden");
    btn.setAttribute("aria-expanded", "true");
    var b = $("#mutzbubble"); if (b) b.remove();
    if (!loaded) loadThread();
    if (q) { input.value = q; }
    input.focus();
    if (q && q.indexOf("[name]") < 0) send(q);
    else if (q) { input.setSelectionRange(q.indexOf("[name]"), q.indexOf("[name]") + 6); }
  }
  function close() { card.classList.add("hidden"); btn.setAttribute("aria-expanded", "false"); btn.focus(); }
  window.mutzOpen = open;

  function loadThread() {
    loaded = true;
    fetch("/api/mutz/thread?key=" + encodeURIComponent(key)).then(function (r) { return r.json(); }).then(function (d) {
      msgs.innerHTML = "";
      if (!d.messages.length) {
        addBot({text: "Grüessech! Bärner si gmüetlech. I bi d Usnahm.", note: "Hello! Bernese people take it slow. I'm the exception."});
      }
      d.messages.forEach(function (m) { if (m.role === "user") addMe(m.text); else addBot(m); });
    }).catch(function () {});
  }

  function addMe(t) {
    var d = document.createElement("div"); d.className = "m me";
    d.innerHTML = '<span class="w">You</span><p>' + esc(t) + "</p>";
    msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight;
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
    var h = '<span class="w">Mutz</span><p>' + cite(m.text, m.sources) + "</p>";
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
    if (m.actions && m.actions.length) {
      h += '<div style="display:flex;gap:6px;flex-wrap:wrap">' + m.actions.map(function (a, i) {
        return '<button type="button" class="b s ' + (a.primary === false ? "" : "p") + '" data-act="' + i + '">' + esc(a.label) + "</button>"; }).join("") +
        (m.actions.some(function (a) { return a.kind !== "link"; }) ? '<button type="button" class="b s" data-cancel>Cancel</button>' : "") + "</div>";
    }
    if (m.after) h += '<span class="lb">' + esc(m.after) + "</span>";
    d.innerHTML = h;
    (m.actions || []).forEach(function (a, i) {
      var b = d.querySelector('[data-act="' + i + '"]');
      b.addEventListener("click", function () {
        if (a.kind === "link") { location.href = a.href; return; }
        b.disabled = true;
        post("/api/mutz/confirm", {key: key, action: a, ctx: ctx}).then(function (r) {
          b.parentNode.innerHTML = '<span class="lb">' + esc(r.message || "Done.") + "</span>";
          if (r.reload) setTimeout(function () { location.reload(); }, 700);
        }).catch(function () { b.disabled = false; b.parentNode.insertAdjacentHTML("beforeend", '<span class="lb">That did not work. Try again.</span>'); });
      });
    });
    var c = d.querySelector("[data-cancel]");
    if (c) c.addEventListener("click", function () { c.parentNode.innerHTML = '<span class="lb">Cancelled. Nothing changed.</span>'; });
    msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight;
  }

  function send(q) {
    if (busy || !q.trim()) return;
    busy = true;
    addMe(q); input.value = "";
    var wait = document.createElement("div"); wait.className = "m bot"; wait.setAttribute("role", "status");
    wait.innerHTML = '<span class="w">Mutz</span><div style="display:flex;gap:10px;align-items:center">' + $("#tpl-search").innerHTML + '<p class="lb">Bear with me… searching the archives.</p></div>';
    msgs.appendChild(wait); msgs.scrollTop = msgs.scrollHeight;
    post("/api/mutz", {key: key, message: q, ctx: ctx, selection: selection}).then(function (r) {
      wait.remove(); addBot(r);
    }).catch(function () {
      wait.remove();
      addBot({text: "The Apertus gateway did not answer. Your question is saved here; try again in a minute."});
    }).finally(function () { busy = false; });
  }

  if (root) {
    var bub = $("#mutzbubble");
    if (bub) setTimeout(function () { if (bub.parentNode) bub.remove(); }, 9000);  // proactive, but rarely: one bubble, then gone
    btn.addEventListener("click", function () { card.classList.contains("hidden") ? open() : close(); });
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
    $("#mutzknows").textContent = "Knows: " + (ctx.screen || "") + (sel ? " · 1 selected" : "");
  };

  /* ------------------------------------------------------------ Brief */
  window.initBrief = function (lid, autoExtract) {
    var f = $("#briefform");
    function reading() { var r = $("#reading"); if (r) { r.classList.remove("hidden"); var n = $("#needs"); if (n) n.style.opacity = .4; var z = $("#noneeds"); if (z) z.classList.add("hidden"); } var b = $("#readbtn"); if (b) { b.disabled = true; b.textContent = "Reading…"; } }
    if (f) f.addEventListener("submit", reading);
    if (autoExtract && f) { reading(); f.submit(); }
    var t = $("[data-lead-title]");
    if (t) t.addEventListener("change", function () { post("/api/leads/" + lid, {title: t.value}); });
    $$("[data-need]").forEach(function (row) {
      var id = row.dataset.need;
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
        if (e.key === "Enter") { e.preventDefault(); post("/api/needs/" + id, {add_term: add.value}).then(function () { location.reload(); }); }
      });
      var sh = row.querySelector("[data-share]");
      if (sh) sh.addEventListener("change", function () { post("/api/needs/" + id, {shareable: sh.checked}); });
      var del = row.querySelector("[data-del]");
      if (del) del.addEventListener("click", function () {
        post("/api/needs/" + id, {delete: true}).then(function () {
          row.innerHTML = '<div class="toast"><span style="flex:1">Need deleted</span><button class="b s" style="background:#111;color:#fff;border-color:#fff">Undo</button></div>';
          row.querySelector("button").addEventListener("click", function () { post("/api/needs/" + id, {restore: true}).then(function () { location.reload(); }); });
        });
      });
    });
    $$("[data-strike]").forEach(function (a) {
      a.addEventListener("click", function (e) {
        e.preventDefault(); a.textContent = "Updating…";
        post("/leads/" + lid + "/strike", {i: +a.dataset.strike}).then(function () { location.reload(); });
      });
    });
  };

  /* ------------------------------------------------------------ Matches */
  window.initMatches = function (lid, searching) {
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
            box.innerHTML = d === "accept" ? '<span style="font-size:13px;font-weight:700">Accepted</span> <button class="b s" data-decide="">Undo</button>'
              : d === "reject" ? '<span style="font-size:13px;color:#5E5E5E">Rejected</span> <button class="b s" data-decide="">Undo</button>'
              : '<button class="b s" data-decide="reject">Reject</button> <button class="b s p" data-decide="accept">Accept</button>';
            var n = $("#nacc"); if (n) n.textContent = r.accepted;
          });
          return;
        }
        if (e.target.closest("a,button")) return;
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
          b.outerHTML = '<span style="font-size:13px;font-weight:700">Added to pitch</span>';
          var n = $("#nacc"); if (n) n.textContent = r.accepted;
        });
      });
    });
    $$("[data-leave]").forEach(function (b) {
      b.addEventListener("click", function () { post("/api/needs/" + b.dataset.leave, {delete: true}).then(function () { location.href = location.pathname; }); });
    });
  };
})();
