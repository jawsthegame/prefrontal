// Prefrontal — iOS home-screen & Lock Screen widget (Scriptable)
// ---------------------------------------------------------------------------
// A glanceable view of "right now": any active outing (with its escalation
// level), your next commitments today, conflict/todo counts, and the most
// recent nudge Prefrontal sent (so a missed push is still visible). Reads the
// Prefrontal API over Tailscale; tap the widget to open the full family view.
//
// One script drives every size. It auto-detects which family iOS is rendering:
//   • Home Screen — Small / Medium / Large: the full card (header + list + counts).
//   • Lock Screen — the accessory slots around the clock:
//       – Rectangular: active outing (or next commitment) + a counts line.
//       – Circular:    a single glyph + number (elapsed mins / next time / todos).
//       – Inline:      one line beside the clock (the single most urgent thing).
//   Lock Screen widgets are rendered monochrome by iOS, so these lean on SF
//   Symbols + text rather than the dashboard colors.
//
// SETUP
//   1. Install Scriptable (App Store), open it, tap + to add a script, paste this.
//   2. Set TOKEN below to your Prefrontal token (kept only on your phone). On a
//      solo deploy that's the PREFRONTAL_WEBHOOK_SECRET; on a multi-user deploy
//      it's the per-user token the operator issued you — the server scopes this
//      widget to your own outings/commitments/todos, so each person's phone runs
//      the same script with their own token. If your token is rotated, update it
//      here.
//   3. Run once in-app to test (previews the Medium card). Then add it where you
//      want it:
//        • Home Screen: long-press → add a Scriptable widget → choose this script.
//        • Lock Screen: edit the Lock Screen → tap a widget slot → Scriptable →
//          choose this script (pick the circular, rectangular, or inline slot).
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
let outings = { active: [] }, commitments = { commitments: [] }, conflicts = { conflicts: [], possible_conflicts: [] }, todos = { todos: [] }, nudges = { nudges: [] };
const settled = await Promise.allSettled([
  getJSON("/outings"), getJSON("/commitments"),
  getJSON("/commitments/conflicts"), getJSON("/todos"), getJSON("/nudges"),
]);
const val = (i, fallback) => (settled[i].status === "fulfilled" ? settled[i].value : fallback);
outings = val(0, outings);
commitments = val(1, commitments);
conflicts = val(2, conflicts);
todos = val(3, todos);
nudges = val(4, nudges);
const ok = settled.some((s) => s.status === "fulfilled");

const family = config.widgetFamily || "medium";
const small = family === "small";
const w = new ListWidget();
w.url = BASE_URL + "/family"; // tap opens the family view (after unlock on the Lock Screen)
w.refreshAfterDate = new Date(Date.now() + REFRESH_MINUTES * 60 * 1000);

function text(stack, s, { color = C.fg, size = 13, bold = false, font } = {}) {
  const t = stack.addText(s);
  t.textColor = color;
  t.font = font || (bold ? Font.boldSystemFont(size) : Font.systemFont(size));
  t.lineLimit = 1;
  return t;
}

// An SF Symbol as an image — the right primitive for the monochrome Lock Screen
// (iOS tints it to match the clock). Returns null-safely if the symbol is absent.
function symbol(stack, name, size, color = C.fg) {
  const sym = SFSymbol.named(name);
  if (!sym) return null;
  sym.applyFont(Font.systemFont(size));
  const img = stack.addImage(sym.image);
  img.imageSize = new Size(size + 3, size + 3);
  img.tintColor = color;
  return img;
}

// --- shared "right now" summary (used by every family) --------------------
const active = (outings.active || [])[0];
const todayCommitments = (commitments.commitments || []).filter((c) => isToday(c.start_at));
const upcomingList = todayCommitments.length ? todayCommitments : (commitments.commitments || []);
const nextCommitment = upcomingList[0];
const hard = (conflicts.conflicts || []).length;
const poss = (conflicts.possible_conflicts || []).length;
const open = (todos.todos || []).length;

// Most recent nudge the system sent — shown only while still "recent" (last 8h),
// so a nudge you already acted on doesn't linger on the widget for days.
const NUDGE_MAX_AGE_MIN = 8 * 60;
function ageMinutes(ts) {
  if (!ts) return Infinity;
  const d = new Date(String(ts).replace(" ", "T") + "Z"); // stored UTC
  return isNaN(d) ? Infinity : (Date.now() - d.getTime()) / 60000;
}
const latestNudge = (nudges.nudges || [])[0];
const recentNudge =
  latestNudge && ageMinutes(latestNudge.created_at) <= NUDGE_MAX_AGE_MIN ? latestNudge : null;

// ===========================================================================
// Lock Screen (accessory) families — monochrome, tiny; SF Symbols + text.
// ===========================================================================
function renderInline() {
  // A single line beside the clock: the one most urgent thing.
  let sym, label;
  if (!ok) { sym = "wifi.slash"; label = "Prefrontal offline"; }
  else if (active) { sym = "figure.walk"; label = `${active.intention} · ${active.level}`; }
  else if (nextCommitment) { sym = "calendar"; label = `${fmtTime(nextCommitment.start_at)} ${nextCommitment.title}`; }
  else if (hard) { sym = "exclamationmark.triangle"; label = `${hard} conflict${hard === 1 ? "" : "s"}`; }
  else { sym = "checklist"; label = `${open} todo${open === 1 ? "" : "s"}`; }
  symbol(w, sym, 12);
  text(w, label, { size: 13 });
}

function renderCircular() {
  w.addAccessoryWidgetBackground = true;
  const col = w.addStack();
  col.layoutVertically();
  col.centerAlignContent();
  const top = col.addStack(); top.addSpacer();
  const bot = col.addStack(); bot.addSpacer();
  if (!ok) {
    symbol(top, "wifi.slash", 14); top.addSpacer();
    text(bot, "—", { size: 13 }); bot.addSpacer();
  } else if (active) {
    symbol(top, "figure.walk", 13); top.addSpacer();
    text(bot, mins(active.elapsed_minutes), { size: 15, bold: true }); bot.addSpacer();
  } else if (nextCommitment) {
    text(top, "Next", { size: 9, color: C.muted }); top.addSpacer();
    text(bot, fmtTime(nextCommitment.start_at), { size: 14, bold: true }); bot.addSpacer();
  } else {
    symbol(top, "checklist", 13); top.addSpacer();
    text(bot, String(open), { size: 15, bold: true }); bot.addSpacer();
  }
}

function renderRectangular() {
  w.addAccessoryWidgetBackground = true;
  if (!ok) {
    const r = w.addStack(); r.centerAlignContent();
    symbol(r, "wifi.slash", 12);
    text(r, " Prefrontal offline", { size: 13 });
    return;
  }
  if (active) {
    const r = w.addStack(); r.centerAlignContent();
    symbol(r, "figure.walk", 12);
    text(r, " " + active.intention, { size: 13, bold: true });
    text(w, `out ${mins(active.elapsed_minutes)}/${mins(active.time_window_minutes)} · ${active.level}`,
      { size: 12, color: C.muted });
  } else if (nextCommitment) {
    const r = w.addStack(); r.centerAlignContent();
    symbol(r, "calendar", 12);
    text(r, " " + fmtTime(nextCommitment.start_at) + "  " + nextCommitment.title, { size: 13, bold: true });
  } else if (recentNudge) {
    // Nothing time-sensitive right now — surface the last thing Prefrontal said.
    const r = w.addStack(); r.centerAlignContent();
    symbol(r, "bell", 12);
    text(r, " " + recentNudge.message, { size: 13 });
  } else {
    const r = w.addStack(); r.centerAlignContent();
    symbol(r, "checkmark.circle", 12);
    text(r, " Nothing scheduled", { size: 13 });
  }
  // Compact counts line (conflicts surface first when present).
  const bits = [];
  if (hard) bits.push(`⚠ ${hard}`);
  if (poss) bits.push(`~ ${poss}`);
  bits.push(`✓ ${open}`);
  text(w, bits.join("   "), { size: 11, color: C.muted });
}

// ===========================================================================
// Home Screen (Small / Medium / Large) — the full card.
// ===========================================================================
function renderHomeScreen() {
  w.backgroundColor = C.bg;
  w.setPadding(14, 16, 12, 14);

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
    return;
  }

  // Active outing takes priority (time-sensitive).
  if (active) {
    const row = w.addStack();
    row.centerAlignContent();
    text(row, "● ", { color: LEVEL_COLOR[active.level] || C.none, size: 13 });
    text(row, active.intention, { bold: true, size: small ? 13 : 14 });
    text(w, `out ${mins(active.elapsed_minutes)} of ${mins(active.time_window_minutes)} · ${active.level}`,
      { color: C.muted, size: 12 });
    w.addSpacer(6);
  }

  // Next commitments today.
  const upcoming = upcomingList.slice(0, small ? 1 : (family === "large" ? 6 : 3));
  if (upcoming.length) {
    if (!active) text(w, todayCommitments.length ? "Today" : "Next up", { color: C.muted, size: 11, bold: true });
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
    if (hard) text(foot, `🔴 ${hard}  `, { color: C.call, size: 12 });
    if (poss) text(foot, `🟡 ${poss}  `, { color: C.soft, size: 12 });
    text(foot, `✓ ${open} todo${open === 1 ? "" : "s"}`, { color: C.muted, size: 12 });
    foot.addSpacer();
  }

  // Most recent nudge — what Prefrontal last told you, so a missed push is still
  // visible. Small has no room; medium gets one line, large gets up to two.
  if (recentNudge && !small) {
    w.addSpacer(6);
    const nrow = w.addStack();
    nrow.centerAlignContent();
    if (!symbol(nrow, "bell.badge", 11, C.accent)) text(nrow, "🔔", { size: 11 });
    const nt = text(nrow, " " + recentNudge.message, { size: 11, color: C.muted });
    nt.lineLimit = family === "large" ? 2 : 1;
  }
}

// --- dispatch by family ----------------------------------------------------
if (family === "accessoryInline") renderInline();
else if (family === "accessoryCircular") renderCircular();
else if (family === "accessoryRectangular") renderRectangular();
else renderHomeScreen();

if (!config.runsInWidget) {
  if (family === "accessoryInline") await w.presentAccessoryInline();
  else if (family === "accessoryCircular") await w.presentAccessoryCircular();
  else if (family === "accessoryRectangular") await w.presentAccessoryRectangular();
  else if (small) await w.presentSmall();
  else if (family === "large") await w.presentLarge();
  else await w.presentMedium();
}
Script.setWidget(w);
Script.complete();
