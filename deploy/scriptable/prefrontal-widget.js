// Prefrontal — iOS home-screen widget (Scriptable)
// ---------------------------------------------------------------------------
// A glanceable view of "right now": any active outing (with its escalation
// level), your next commitments today, and conflict/todo counts. Reads the
// Prefrontal API over Tailscale; tap the widget to open the full family view.
//
// SETUP
//   1. Install Scriptable (App Store), open it, tap + to add a script, paste this.
//   2. Set TOKEN below to your Prefrontal token (kept only on your phone). On a
//      solo deploy that's the PREFRONTAL_WEBHOOK_SECRET; on a multi-user deploy
//      it's the per-user token the operator issued you — the server scopes this
//      widget to your own outings/commitments/todos, so each person's phone runs
//      the same script with their own token. If your token is rotated, update it
//      here.
//   3. Run once in-app to test. Then long-press the home screen → add a
//      Scriptable widget (Medium recommended) → choose this script.
//   Works anywhere your phone can reach the mini over Tailscale.

// --- config ---------------------------------------------------------------
const BASE_URL = "http://agent-1.tail8b0a.ts.net:8000";
const TOKEN = "PASTE_YOUR_PREFRONTAL_TOKEN"; // solo: webhook secret · multi-user: your per-user token
const REFRESH_MINUTES = 15;

// --- palette (matches the dashboard) --------------------------------------
const C = {
  bg: new Color("#0f1115"), fg: new Color("#e6e9ef"), muted: new Color("#8b93a7"),
  line: new Color("#262b36"), accent: new Color("#6ea8fe"),
  none: new Color("#6b7280"), soft: new Color("#d9a93b"),
  firm: new Color("#e07b39"), call: new Color("#e0556b"), good: new Color("#5bc97a"),
};
const LEVEL_COLOR = { none: C.none, soft: C.soft, firm: C.firm, call: C.call };

async function getJSON(path) {
  const req = new Request(BASE_URL + path);
  req.headers = { "X-Prefrontal-Token": TOKEN };
  req.timeoutInterval = 10;
  return await req.loadJSON();
}

function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(String(ts).replace(" ", "T") + "Z"); // stored UTC
  return isNaN(d) ? ts : d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}
function isToday(ts) {
  const d = new Date(String(ts).replace(" ", "T") + "Z");
  const n = new Date();
  return d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate();
}
const mins = (n) => (n == null ? "" : Math.round(n) + "m");

// --- fetch (degrade gracefully, per-call) ---------------------------------
// Each endpoint falls back to its empty shape independently, so one slow or
// failing call (say /todos) doesn't blank the whole widget — we still render
// the outing and commitments that did load. "offline" is reserved for the case
// where *nothing* came back (mini unreachable / bad token).
let outings = { active: [] }, commitments = { commitments: [] }, conflicts = { conflicts: [], possible_conflicts: [] }, todos = { todos: [] };
const settled = await Promise.allSettled([
  getJSON("/outings"), getJSON("/commitments"),
  getJSON("/commitments/conflicts"), getJSON("/todos"),
]);
const val = (i, fallback) => (settled[i].status === "fulfilled" ? settled[i].value : fallback);
outings = val(0, outings);
commitments = val(1, commitments);
conflicts = val(2, conflicts);
todos = val(3, todos);
const ok = settled.some((s) => s.status === "fulfilled");

const family = config.widgetFamily || "medium";
const small = family === "small";
const w = new ListWidget();
w.backgroundColor = C.bg;
w.setPadding(14, 16, 12, 14);
w.url = BASE_URL + "/family";
w.refreshAfterDate = new Date(Date.now() + REFRESH_MINUTES * 60 * 1000);

function text(stack, s, { color = C.fg, size = 13, bold = false, font } = {}) {
  const t = stack.addText(s);
  t.textColor = color;
  t.font = font || (bold ? Font.boldSystemFont(size) : Font.systemFont(size));
  t.lineLimit = 1;
  return t;
}

// header: title + last-updated (or offline)
const head = w.addStack();
head.centerAlignContent();
text(head, "🧠 Prefrontal", { bold: true, size: small ? 13 : 15 });
head.addSpacer();
text(head, ok ? new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "offline",
  { color: ok ? C.muted : C.call, size: 11 });
w.addSpacer(small ? 6 : 8);

if (!ok) {
  text(w, "Can't reach Prefrontal.", { color: C.muted, size: 12 });
  text(w, "Check Tailscale / token.", { color: C.muted, size: 11 });
} else {
  // Active outing takes priority (time-sensitive).
  const active = (outings.active || [])[0];
  if (active) {
    const row = w.addStack();
    row.centerAlignContent();
    const dot = text(row, "● ", { color: LEVEL_COLOR[active.level] || C.none, size: 13 });
    text(row, active.intention, { bold: true, size: small ? 13 : 14 });
    text(w, `out ${mins(active.elapsed_minutes)} of ${mins(active.time_window_minutes)} · ${active.level}`,
      { color: C.muted, size: 12 });
    w.addSpacer(6);
  }

  // Next commitments today.
  const today = (commitments.commitments || []).filter((c) => isToday(c.start_at));
  const upcoming = (today.length ? today : (commitments.commitments || [])).slice(0, small ? 1 : (family === "large" ? 6 : 3));
  if (upcoming.length) {
    if (!active) text(w, today.length ? "Today" : "Next up", { color: C.muted, size: 11, bold: true });
    for (const c of upcoming) {
      const r = w.addStack();
      r.centerAlignContent();
      text(r, fmtTime(c.start_at) + "  ", { color: C.accent, size: 12, bold: true });
      text(r, c.title, { size: small ? 12 : 13 });
    }
  } else if (!active) {
    text(w, "Nothing scheduled. 🎉", { color: C.muted, size: 13 });
  }

  // Footer counts: conflicts / possible / todos.
  if (!small) {
    w.addSpacer(8);
    const foot = w.addStack();
    foot.centerAlignContent();
    const hard = (conflicts.conflicts || []).length;
    const poss = (conflicts.possible_conflicts || []).length;
    const open = (todos.todos || []).length;
    if (hard) text(foot, `🔴 ${hard}  `, { color: C.call, size: 12 });
    if (poss) text(foot, `🟡 ${poss}  `, { color: C.soft, size: 12 });
    text(foot, `✓ ${open} todo${open === 1 ? "" : "s"}`, { color: C.muted, size: 12 });
    foot.addSpacer();
  }
}

if (!config.runsInWidget) {
  small ? await w.presentSmall() : (family === "large" ? await w.presentLarge() : await w.presentMedium());
}
Script.setWidget(w);
Script.complete();
