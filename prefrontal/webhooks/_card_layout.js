// Shared card-column layout — user-arranged, draggable, collapsible columns.
//
// A generalized version of the dashboard's column system, injected (see
// _common.py) into any page that opts in with:
//
//     <main class="masonry" data-layout-key="prefrontal_<view>_layout"> …cards… </main>
//     <select id="col-count"> … </select>   (the Columns picker, anywhere on the page)
//
// Cards are <section class="card" id="…"> with an <h2> OR <p class="label">
// heading. Placement (which column, order) and collapse state persist per device
// under the given localStorage key. Call window.initCardLayout() once the
// container is visible (measuring width needs a laid-out .masonry).
//
// Scope note: this is the reusable core only — the dashboard's per-card item
// caps, collapsed-preview summaries, and unseen badges stay in dashboard.html.
(function () {
  const MAX_COLS = 3;
  const MIN_CARD_W = 340;  // narrowest a card should get before dropping a column
  const HEAD = ":scope > h2, :scope > .label";  // a card's heading, either markup

  let main = null, LAYOUT_KEY = null, layout = null;

  const save = () => { try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout)); } catch (_) {} };
  const cards = () => [...main.querySelectorAll(".card")];  // direct before render, nested in .col after

  function fitCols() {
    const w = (main ? main.clientWidth : window.innerWidth) - 32;  // minus L/R padding
    return Math.max(1, Math.min(MAX_COLS, Math.floor((w + 16) / (MIN_CARD_W + 16))));
  }
  function prefCols() {
    return layout.colCount != null ? Math.max(1, Math.min(MAX_COLS, layout.colCount)) : fitCols();
  }
  function effectiveCols() { return Math.max(1, Math.min(prefCols(), fitCols())); }

  // Saved order first (existing cards), then any new cards in DOM order.
  function orderedIds() {
    const present = new Set(cards().map((c) => c.id));
    const seen = new Set(), out = [];
    (layout.order || []).forEach((id) => { if (present.has(id) && !seen.has(id)) { out.push(id); seen.add(id); } });
    cards().forEach((c) => { if (!seen.has(c.id)) { out.push(c.id); seen.add(c.id); } });
    return out;
  }

  // Rebuild the .col containers and distribute cards into them.
  function render() {
    const E = effectiveCols();
    const full = E === prefCols();  // not clamped by the viewport → editing is safe
    main.classList.toggle("compact", !full);
    const cc = document.getElementById("col-count");
    if (cc) cc.value = String(prefCols());

    const byId = {}; cards().forEach((c) => { byId[c.id] = c; });
    const cols = [];
    for (let i = 0; i < E; i++) { const d = document.createElement("div"); d.className = "col"; cols.push(d); }
    const load = new Array(E).fill(0);
    orderedIds().forEach((id) => {
      const node = byId[id]; if (!node) return;
      let ci = layout.colIndex[id];
      if (ci == null || ci < 0) { ci = load.indexOf(Math.min(...load)); if (full) layout.colIndex[id] = ci; }
      const t = Math.min(ci, E - 1);
      cols[t].appendChild(node); load[t]++;
    });
    main.innerHTML = "";
    cols.forEach((c) => main.appendChild(c));
    refreshButtons();
    applyCollapsed();
  }

  // Persist the on-screen arrangement (no-op while clamped/merged).
  function serialize() {
    if (main.classList.contains("compact")) return;
    const order = [];
    [...main.querySelectorAll(":scope > .col")].forEach((col, ci) => {
      [...col.querySelectorAll(":scope > .card")].forEach((card) => { layout.colIndex[card.id] = ci; order.push(card.id); });
    });
    layout.order = order;
    save();
  }

  function refreshButtons() {
    [...main.querySelectorAll(":scope > .col")].forEach((col, ci, cols) => {
      const cs = [...col.querySelectorAll(":scope > .card")];
      cs.forEach((card, i) => {
        const set = (k, d) => {
          const b = card.querySelector(`:scope > .card-ctl [data-arr='${k}'], :scope > .card-ctl [data-col='${k}']`);
          if (b) b.disabled = d;
        };
        set("up", i === 0); set("down", i === cs.length - 1);
        set("left", ci === 0); set("right", ci === cols.length - 1);
      });
    });
  }

  function moveWithin(card, dir) {
    const col = card.parentElement; if (!col) return;
    const sib = dir < 0 ? card.previousElementSibling : card.nextElementSibling;
    if (!sib || !sib.classList.contains("card")) return;
    if (dir < 0) col.insertBefore(card, sib); else col.insertBefore(sib, card);
    serialize(); refreshButtons();
  }
  function moveCol(card, dir) {
    const cols = [...main.querySelectorAll(":scope > .col")];
    const col = card.parentElement;
    const tj = cols.indexOf(col) + dir;
    if (tj < 0 || tj >= cols.length) return;
    const idx = [...col.querySelectorAll(":scope > .card")].indexOf(card);
    const tc = [...cols[tj].querySelectorAll(":scope > .card")];
    cols[tj].insertBefore(card, tc[idx] || null);
    serialize(); refreshButtons();
  }

  function injectControls() {
    cards().forEach((card) => {
      if (card.querySelector(":scope > .card-ctl")) return;
      const ctl = document.createElement("div");
      ctl.className = "card-ctl";
      ctl.innerHTML =
        `<span class="drag-handle" title="Drag to move this card (or its heading)" aria-hidden="true">⠿</span>`
        + `<button class="cardbtn" data-col="left" title="Move to previous column" aria-label="Move card to previous column">◀</button>`
        + `<button class="cardbtn" data-arr="up" title="Move up" aria-label="Move card up">▲</button>`
        + `<button class="cardbtn" data-arr="down" title="Move down" aria-label="Move card down">▼</button>`
        + `<button class="cardbtn" data-col="right" title="Move to next column" aria-label="Move card to next column">▶</button>`;
      card.insertBefore(ctl, card.firstChild);
    });
    refreshButtons();
  }

  function applyCollapsed() {
    cards().forEach((card) => { if (card.id) card.classList.toggle("collapsed", !!layout.collapsed[card.id]); });
  }
  function toggleCollapsed(card) {
    const on = card.classList.toggle("collapsed");
    if (card.id) { if (on) layout.collapsed[card.id] = true; else delete layout.collapsed[card.id]; save(); }
  }

  // Drag via Pointer Events (mouse + touch). Grab a card's ⠿ handle or heading.
  let drag = null;
  function initDrag() {
    if (main.dataset.dndWired) return;
    main.dataset.dndWired = "1";
    const THRESH = 5;
    const clearT = () => main.querySelectorAll(".col.drop-target").forEach((c) => c.classList.remove("drop-target"));
    const colFromX = (x) => {
      const cols = [...main.querySelectorAll(":scope > .col")];
      let best = null, bd = Infinity;
      for (const c of cols) {
        const r = c.getBoundingClientRect();
        if (x >= r.left && x <= r.right) return c;
        const d = Math.min(Math.abs(x - r.left), Math.abs(x - r.right));
        if (d < bd) { bd = d; best = c; }
      }
      return best;
    };
    const cardAfterY = (col, y) => {
      const cs = [...col.querySelectorAll(":scope > .card:not(.dragging)")];
      for (const c of cs) { const r = c.getBoundingClientRect(); if (y < r.top + r.height / 2) return c; }
      return null;
    };
    main.addEventListener("pointerdown", (e) => {
      if (e.button != null && e.button !== 0) return;
      if (main.classList.contains("compact")) return;
      if (e.target.closest(".cardbtn, input, textarea, select, button, a, [contenteditable]")) return;
      let grip = e.target.closest(".drag-handle"), fromHeading = false;
      if (!grip) {
        const h = e.target.closest("h2, .label");
        if (h && h.parentElement && h.parentElement.classList.contains("card") && main.contains(h)) { grip = h; fromHeading = true; }
      }
      if (!grip) return;
      const card = grip.closest(".card");
      if (!card || !main.contains(card)) return;
      drag = { card, grip, origin: { parent: card.parentElement, next: card.nextElementSibling },
               pointerId: e.pointerId, startX: e.clientX, startY: e.clientY, active: false, fromHeading };
      try { grip.setPointerCapture(e.pointerId); } catch (_) {}
      if (!fromHeading) e.preventDefault();
    });
    main.addEventListener("pointermove", (e) => {
      if (!drag || e.pointerId !== drag.pointerId) return;
      if (!drag.active) {
        if (Math.abs(e.clientX - drag.startX) < THRESH && Math.abs(e.clientY - drag.startY) < THRESH) return;
        drag.active = true;
        drag.card.classList.add("dragging");
        main.classList.add("dragging-active");
      }
      e.preventDefault();
      const col = colFromX(e.clientX); if (!col) return;
      clearT(); col.classList.add("drop-target");
      const after = cardAfterY(col, e.clientY);
      if (after) col.insertBefore(drag.card, after); else col.appendChild(drag.card);
    });
    const finish = (e) => {
      if (!drag || (e && e.pointerId !== drag.pointerId)) return;
      try { drag.grip.releasePointerCapture(drag.pointerId); } catch (_) {}
      if (drag.active) {
        drag.card.classList.remove("dragging");
        main.classList.remove("dragging-active");
        clearT();
        serialize(); refreshButtons();
        if (drag.fromHeading) {  // a heading drag would fire a click that toggles collapse — eat it
          const kill = (ev) => { ev.stopPropagation(); ev.preventDefault(); };
          document.addEventListener("click", kill, { capture: true, once: true });
          setTimeout(() => document.removeEventListener("click", kill, true), 350);
        }
      }
      drag = null;
    };
    main.addEventListener("pointerup", finish);
    main.addEventListener("pointercancel", finish);
  }

  function initColPicker() {
    const cc = document.getElementById("col-count");
    if (!cc || cc.dataset.wired) return;
    cc.dataset.wired = "1";
    cc.value = String(prefCols());
    cc.addEventListener("change", () => {
      layout.colCount = Math.max(1, Math.min(MAX_COLS, Number(cc.value) || fitCols()));
      save(); render();
    });
  }

  // Delegated clicks (bound once): reorder buttons + collapse-on-heading-click.
  function initClicks() {
    if (document.body.dataset.cardLayoutClicks) return;
    document.body.dataset.cardLayoutClicks = "1";
    document.addEventListener("click", (e) => {
      const m = e.target.closest(".masonry[data-layout-key]");
      if (!m) return;
      const arr = e.target.closest("[data-arr]");
      if (arr) { const c = arr.closest(".card"); if (c) moveWithin(c, arr.dataset.arr === "up" ? -1 : 1); return; }
      const colb = e.target.closest("[data-col]");
      if (colb) { const c = colb.closest(".card"); if (c) moveCol(c, colb.dataset.col === "left" ? -1 : 1); return; }
      const h = e.target.closest("h2, .label");
      if (h && h.parentElement && h.parentElement.classList.contains("card") && m.contains(h)) toggleCollapsed(h.parentElement);
    });
  }

  window.initCardLayout = function () {
    main = document.querySelector(".masonry[data-layout-key]");
    if (!main || main.dataset.cardLayoutReady) return;
    main.dataset.cardLayoutReady = "1";
    LAYOUT_KEY = main.dataset.layoutKey;
    try { layout = JSON.parse(localStorage.getItem(LAYOUT_KEY)) || {}; } catch (_) { layout = {}; }
    layout.order = layout.order || [];
    layout.colIndex = layout.colIndex || {};
    layout.collapsed = layout.collapsed || {};
    injectControls();
    render();
    initDrag();
    initColPicker();
    initClicks();
    if (!window.__cardLayoutResize) {
      window.__cardLayoutResize = true;
      let rt = null;
      window.addEventListener("resize", () => {
        clearTimeout(rt);
        rt = setTimeout(() => {
          if (main && main.querySelectorAll(":scope > .col").length !== effectiveCols()) render();
        }, 150);
      });
    }
  };

  // Collapse-only mode — for pages that want collapsible, persisted cards but
  // no column layout (a single-column form/list page). Opt in with a container
  // <div … data-collapse-key="prefrontal_<view>_layout">; each <section class="card">
  // inside it that has an id becomes collapsible by clicking its heading.
  window.initCardCollapse = function () {
    const box = document.querySelector("[data-collapse-key]");
    if (!box || box.dataset.collapseReady) return;
    box.dataset.collapseReady = "1";
    const KEY = box.dataset.collapseKey;
    let state; try { state = JSON.parse(localStorage.getItem(KEY)) || {}; } catch (_) { state = {}; }
    state.collapsed = state.collapsed || {};
    const save = () => { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (_) {} };
    box.querySelectorAll(".card").forEach((card) => {
      if (!card.id) return;
      const h = card.querySelector(":scope > h2, :scope > .label");
      if (h && !h.querySelector(".collapse-caret")) {
        h.classList.add("collapsible-head");
        const car = document.createElement("span");
        car.className = "collapse-caret"; car.setAttribute("aria-hidden", "true");
        h.insertBefore(car, h.firstChild);
      }
      card.classList.toggle("collapsed", !!state.collapsed[card.id]);
    });
    box.addEventListener("click", (e) => {
      const h = e.target.closest("h2, .label");
      if (!h || !h.parentElement || !h.parentElement.classList.contains("card") || !h.parentElement.id) return;
      const card = h.parentElement;
      const on = card.classList.toggle("collapsed");
      if (on) state.collapsed[card.id] = true; else delete state.collapsed[card.id];
      save();
    });
  };
})();
