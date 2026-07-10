/* Shared keyboard-shortcut engine, inlined into every web surface by
   prefrontal.webhooks._common._with_shortcuts (alongside its CSS + the help /
   quick-add markup). It provides:

     - Global shortcuts on every tab: quick-add a todo from anywhere ("a"),
       search ("/"), theme toggle ("t"), the "?" cheatsheet, j/k card movement,
       and "g"+letter navigation between tabs.
     - A registry each page pushes context bindings to before this loads:
         (window.PF_SHORTCUTS = window.PF_SHORTCUTS || []).push({key,label,group,run})
       Those show up in the cheatsheet and fire like the globals.

   Kept dependency-free — it touches only the DOM it's handed and localStorage,
   never a page's own globals (except the optional window.load refresh hook). */
(function () {
  "use strict";
  var TOKEN_KEY = "prefrontal_token";
  var CHORD_MS = 1200;  // window to press the second key of a "g <x>" chord

  // "g" then one of these jumps to that tab. Chorded, so the single-key context
  // shortcuts (e.g. dashboard "p" = panic) never collide with them.
  var NAV = {
    d: ["/dashboard", "Dashboard"], c: ["/calendar", "Calendar"],
    h: ["/household", "Household"], k: ["/kids", "Kids"], p: ["/pets", "Pets"],
    i: ["/stats", "Insights"], v: ["/review", "Review"],
    s: ["/settings", "Settings"], b: ["/projects/board", "Projects"]
  };

  function $(sel) { return document.querySelector(sel); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function typingInField() {
    var el = document.activeElement;
    return !!el && (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.isContentEditable);
  }
  // Any visible modal (a page's own or ours) suppresses single-key shortcuts.
  function dialogOpen() {
    return [].some.call(document.querySelectorAll('[role="dialog"]'), function (el) {
      return el.offsetParent !== null;
    });
  }
  function authHeaders(extra) {
    var h = extra || {}, t = null;
    try { t = localStorage.getItem(TOKEN_KEY); } catch (e) { /* private mode */ }
    if (t) h["X-Prefrontal-Token"] = t;  // else the session cookie authenticates
    return h;
  }

  // ── j/k card movement (any page that has .card elements) ──────────────────
  function visibleCards() {
    return [].filter.call(document.querySelectorAll(".card"), function (c) {
      return c.offsetParent !== null && !c.closest('[role="dialog"]');
    });
  }
  function flash(el) {
    el.classList.remove("kbd-flash");
    void el.offsetWidth;  // reflow so the animation restarts on repeat presses
    el.classList.add("kbd-flash");
    setTimeout(function () { el.classList.remove("kbd-flash"); }, 1400);
  }
  function moveCard(dir) {
    var cards = visibleCards();
    if (!cards.length) return;
    // Anchor to whichever card is nearest the top of the viewport, then step —
    // recomputed each press, so holding j walks straight down.
    var cur = 0, best = Infinity;
    cards.forEach(function (c, i) {
      var d = Math.abs(c.getBoundingClientRect().top - 76);
      if (d < best) { best = d; cur = i; }
    });
    var el = cards[Math.min(cards.length - 1, Math.max(0, cur + dir))];
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    flash(el);
  }

  function focusSearch() {
    var s = $("#global-search");
    if (s) s.focus(); else location.href = "/dashboard";  // search lives on the dashboard
  }
  function toggleTheme() { var t = $("#theme-toggle"); if (t) t.click(); }

  // ── Quick-add todo (create from any tab) ──────────────────────────────────
  function openQuickAdd() {
    var m = $("#kbd-quickadd");
    if (!m) return;
    $("#kbd-qa-err").textContent = "";
    var inp = $("#kbd-qa-title");
    inp.value = "";
    m.style.display = "flex";
    setTimeout(function () { inp.focus(); }, 0);
  }
  function closeQuickAdd() { var m = $("#kbd-quickadd"); if (m) m.style.display = "none"; }
  function submitQuickAdd() {
    var inp = $("#kbd-qa-title");
    var title = (inp.value || "").trim();
    if (!title) { inp.focus(); return; }
    var pri = $("#kbd-qa-pri");
    var body = { title: title };
    if (pri && pri.value && pri.value !== "auto") body.priority = Number(pri.value);
    var btn = $("#kbd-qa-add");
    btn.disabled = true;
    fetch("/todos", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body)
    }).then(function (r) {
      if (r.status === 401) { toast("Sign in to add todos"); closeQuickAdd(); return null; }
      if (!r.ok) throw new Error("HTTP " + r.status);
      closeQuickAdd();
      toast("Added: " + title);
      // Refresh the current surface if it exposes a reload (the dashboard does).
      if (typeof window.load === "function") { try { window.load(); } catch (e) { /* noop */ } }
      return null;
    }).catch(function (err) {
      $("#kbd-qa-err").textContent = "Couldn't add — " + err.message;
    }).then(function () { btn.disabled = false; });
  }

  var toastTimer = null;
  function toast(msg) {
    var t = $("#kbd-toast");
    if (!t) return;
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove("show"); }, 2200);
  }

  // ── Cheatsheet ("?") ──────────────────────────────────────────────────────
  function globalBindings() {
    return [
      { key: "a", label: "Add a todo", group: "Everywhere", run: openQuickAdd },
      { key: "/", label: "Search", group: "Everywhere", run: focusSearch },
      { key: "t", label: "Toggle light / dark", group: "Everywhere", run: toggleTheme },
      { key: "?", label: "Keyboard help", group: "Everywhere", run: toggleHelp },
      { key: "j", label: "Next card", group: "Move", run: function () { moveCard(1); } },
      { key: "k", label: "Previous card", group: "Move", run: function () { moveCard(-1); } }
    ];
  }
  function pageBindings() { return (window.PF_SHORTCUTS || []).slice(); }
  function findBinding(key) {
    var all = globalBindings().concat(pageBindings());
    for (var i = 0; i < all.length; i++) if (all[i].key === key) return all[i];
    return null;
  }
  function renderKeys(keys) {
    return String(keys).split(" ").map(function (k) { return "<kbd>" + esc(k) + "</kbd>"; }).join(" ");
  }
  function renderHelp() {
    var groups = {};
    groups["Go to a tab"] = Object.keys(NAV).map(function (k) {
      return { keys: "g " + k, label: NAV[k][1] };
    });
    globalBindings().concat(pageBindings()).forEach(function (b) {
      (groups[b.group] = groups[b.group] || []).push({ keys: b.key, label: b.label });
    });
    (groups["Dialogs"] = groups["Dialogs"] || []).push({ keys: "Esc", label: "Close any dialog" });
    var rank = function (n) {
      return ({ "Everywhere": 0, "Move": 1, "Go to a tab": 2 })[n] !== undefined
        ? ({ "Everywhere": 0, "Move": 1, "Go to a tab": 2 })[n]
        : (n === "Dialogs" ? 99 : 50);  // page groups sit between nav and Dialogs
    };
    return Object.keys(groups).sort(function (a, b) { return rank(a) - rank(b); }).map(function (g) {
      return '<div class="kbd-group"><h3>' + esc(g) + "</h3>" + groups[g].map(function (r) {
        return '<div class="kbd-row">' + renderKeys(r.keys) + "<span>" + esc(r.label) + "</span></div>";
      }).join("") + "</div>";
    }).join("");
  }
  function openHelp() { $("#kbd-help-body").innerHTML = renderHelp(); $("#kbd-help").style.display = "flex"; }
  function closeHelp() { var h = $("#kbd-help"); if (h) h.style.display = "none"; }
  function toggleHelp() {
    var h = $("#kbd-help");
    if (h && getComputedStyle(h).display !== "none") closeHelp(); else openHelp();
  }

  // ── Key handling ──────────────────────────────────────────────────────────
  var chord = false, chordTimer = null;
  function clearChord() { chord = false; clearTimeout(chordTimer); }
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { closeHelp(); closeQuickAdd(); clearChord(); return; }
    if (e.metaKey || e.ctrlKey || e.altKey) return;  // leave browser/OS combos alone
    if (typingInField()) return;                      // never hijack typing
    if (chord) {                                      // second key of a "g <x>" chord
      clearChord();
      var dest = NAV[e.key];
      if (dest) { e.preventDefault(); location.href = dest[0]; }
      return;
    }
    if (e.key === "g" && !dialogOpen()) {
      chord = true;
      chordTimer = setTimeout(clearChord, CHORD_MS);
      e.preventDefault();
      return;
    }
    var b = findBinding(e.key);
    if (!b) return;
    if (dialogOpen() && e.key !== "?") return;  // with a dialog open, only "?" still fires
    e.preventDefault();
    b.run();
  });

  function wire() {
    var add = $("#kbd-qa-add"); if (add) add.onclick = submitQuickAdd;
    var cancel = $("#kbd-qa-cancel"); if (cancel) cancel.onclick = closeQuickAdd;
    var title = $("#kbd-qa-title");
    if (title) title.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); submitQuickAdd(); }
    });
    var hc = $("#kbd-help-close"); if (hc) hc.onclick = closeHelp;
    var help = $("#kbd-help"); if (help) help.addEventListener("click", function (e) { if (e.target === help) closeHelp(); });
    var qa = $("#kbd-quickadd"); if (qa) qa.addEventListener("click", function (e) { if (e.target === qa) closeQuickAdd(); });
    // A page can add its own help affordance with id/data-kbd-help.
    var btn = document.getElementById("kbd-help-btn"); if (btn) btn.onclick = toggleHelp;
    [].forEach.call(document.querySelectorAll("[data-kbd-help]"), function (el) { el.onclick = toggleHelp; });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();

  // Exposed so a page's own button (e.g. the dashboard "⌨︎") can open these.
  window.Shortcuts = { openHelp: openHelp, openQuickAdd: openQuickAdd };
})();
