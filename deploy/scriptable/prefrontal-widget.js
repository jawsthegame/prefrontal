// Prefrontal — iOS home-screen & Lock Screen widget (Scriptable)
// ---------------------------------------------------------------------------
// A glanceable view of "right now": any active outing (with its escalation
// level), your next commitments today — with *when to leave* for the next one
// ("leave 4:15 PM · 12m") — shown separately from FYI events (where someone
// else will be; never yours, never a leave-by), conflict/todo counts, the most recent nudge
// Prefrontal sent (so a missed push is still visible), and — when you have an
// open gap — the one todo that fits it ("25m free · Reply to landlord"), a
// low-friction initiation nudge. Reads the Prefrontal API over Tailscale; tap
// the widget to open the full dashboard.
//
// One script drives every size. It auto-detects which family iOS is rendering:
//   • Home Screen — Small / Medium / Large: the full card (header + list + counts).
//   • Lock Screen — the tiny accessory slots around the clock. Each slot shows
//     ONE facet (a glyph + a short value), because iOS caps the size. Set a
//     widget's Parameter to pin it to a facet, or leave it blank for "auto":
//       focus/outing · next · alert/urgent · behind · todos/free (see PARAM below)
//     So you can place several: e.g. circular "focus" + circular "next" + an
//     inline "alert". Rectangular shows the same facet with a second context
//     line; circular shows the glyph + value; inline is one line by the clock.
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
//          Tap the added widget again to set its Parameter (focus / next /
//          alert / todos) so that slot shows just that facet. Add more slots for
//          more facets. Leave the Parameter blank to auto-pick the top one.
//   Works anywhere your phone can reach the mini over Tailscale.

// --- config ---------------------------------------------------------------
const BASE_URL = "http://mac-mini.tailnet.ts.net:8000";
const TOKEN = "PASTE_YOUR_PREFRONTAL_TOKEN"; // solo: webhook secret · multi-user: your per-user token
// Adaptive refresh cadence (minutes). iOS budgets widget reloads (~40-70/day for
// a visible widget) and treats refreshAfterDate as the *earliest* reload, not a
// promise — so a flat 15 min (96/day) overspends the budget on quiet stretches,
// and iOS throttles, spacing reloads *further* apart. Instead we ask for a tight
// interval only while something time-sensitive is live and back off when idle,
// so the budget is there for the moments that matter. See computeRefreshMinutes.
const REFRESH = {
  live: 2, // outing escalating (firm/call) or a departure is due — keep it fresh
  active: 5, // a calm active outing in progress
  soon: 10, // a commitment is bearing down (starts within SOON_WINDOW_MIN)
  idle: 30, // nothing time-sensitive — back off to bank budget for when it matters
  offline: 10, // couldn't reach the mini — retry before long, but don't hammer
};
const SOON_WINDOW_MIN = 90; // "bearing down" horizon for the `soon` cadence

// --- palette (matches the dashboard) --------------------------------------
const C = {
  bg: new Color("#0f1115"), fg: new Color("#e6e9ef"), muted: new Color("#8b93a7"),
  line: new Color("#262b36"), accent: new Color("#6ea8fe"),
  none: new Color("#6b7280"), soft: new Color("#d9a93b"),
  firm: new Color("#e07b39"), call: new Color("#e0556b"), good: new Color("#5bc97a"),
  fyi: new Color("#8ab4d8"), // "where someone will be" — matches the dashboard's --fyi
};
const LEVEL_COLOR = { none: C.none, soft: C.soft, firm: C.firm, call: C.call };

// Outing escalation → a phase you can read at a glance. The API's `level`
// (none/soft/firm/call) is the *current* elapsed-vs-window phase, so this ramps
// the glyph + words as you push past your stated time:
//   none  beginning, under 50%      → on track  (you're good)
//   soft  past 50%, first nudge      → heads up  (pushing it)
//   firm  past 100%, firm nudge      → wrap up   (really pushing it)
//   call  past 150%, after the call  → you're late  (stop — head home now)
// Lock Screen slots are monochrome, so the signal leans on the escalating glyph
// and the words, not the color (which only shows on the Home Screen card).
const OUTING_PHASE = {
  none: { glyph: "figure.walk",                   word: "on track",   phrase: "you're good" },
  soft: { glyph: "exclamationmark.circle",         word: "heads up",   phrase: "pushing it" },
  firm: { glyph: "exclamationmark.triangle.fill",  word: "wrap up",    phrase: "really pushing it" },
  call: { glyph: "exclamationmark.octagon.fill",   word: "you're late", phrase: "you're late — head home now" },
};
// Resolve an active outing to its phase, plus how deep into the window it is
// (a percentage: 40% early, 120% past the window, 180% way over — a number that
// reads as "which phase" even in monochrome).
function outingPhase(o) {
  const p = OUTING_PHASE[o.level] || OUTING_PHASE.none;
  const pct = o.time_window_minutes > 0
    ? Math.round((o.elapsed_minutes / o.time_window_minutes) * 100)
    : null;
  return { ...p, pct };
}

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
// Whole calendar days from today to `d` (local): 0 = today, 1 = tomorrow, etc.
function daysFromToday(d) {
  const startOfDay = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate());
  return Math.round((startOfDay(d) - startOfDay(new Date())) / 86400000);
}
// A day label for a commitment that isn't today, so "Next up" can say *when* — a
// 9am two days out reads "Wed 9:00 AM", not a bare "9:00 AM" that looks like this
// morning. Empty for today (the time alone is unambiguous). "Tomorrow" for the
// next day, a weekday name within the coming week, else a short date.
function dayLabel(ts) {
  if (!ts || isToday(ts)) return "";
  const d = new Date(String(ts).replace(" ", "T") + "Z");
  if (isNaN(d)) return "";
  const dayDiff = daysFromToday(d);
  if (dayDiff === 1) return "Tomorrow";
  if (dayDiff > 1 && dayDiff < 7) return d.toLocaleDateString([], { weekday: "short" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}
// Time for a list row, prefixed with a day label when the commitment isn't today.
function fmtWhen(ts) {
  const day = dayLabel(ts);
  return day ? `${day} ${fmtTime(ts)}` : fmtTime(ts);
}
const mins = (n) => (n == null ? "" : Math.round(n) + "m");
// Elapsed as a compact "45m" under an hour, "1h" / "1h 5m" once past it. Used as
// the *static* (non-ticking) elapsed once an outing crosses an hour: the live
// timer style ticks seconds every second (and shows H:MM:SS past an hour), which
// is just noise on a glance, so we stop ticking and show whole minutes instead.
const hm = (n) => {
  if (n == null) return "";
  const t = Math.round(n);
  if (t < 60) return t + "m";
  const h = Math.floor(t / 60), m = t % 60;
  return m ? `${h}h ${m}m` : `${h}h`;
};
// An outing that's been out at least an hour: past this the elapsed is rendered
// static (see `hm`) rather than as a live, seconds-ticking timer.
const OVER_AN_HOUR_MIN = 60;
const isLongOuting = (o) => o != null && o.elapsed_minutes != null && o.elapsed_minutes >= OVER_AN_HOUR_MIN;

// --- fetch (degrade gracefully, per-call) ---------------------------------
// Each endpoint falls back to its empty shape independently, so one slow or
// failing call (say /todos) doesn't blank the whole widget — we still render
// the outing and commitments that did load. "offline" is reserved for the case
// where *nothing* came back (mini unreachable / bad token).
let outings = { active: [] }, commitments = { commitments: [] }, conflicts = { conflicts: [], possible_conflicts: [] }, todos = { todos: [] }, nudges = { nudges: [] }, fitNow = { free_minutes: 0, suggestion: null }, departure = { departure: null }, cascade = { at_risk: [], hard_conflict: false, source: "now" };
const settled = await Promise.allSettled([
  getJSON("/outings"), getJSON("/commitments"),
  getJSON("/commitments/conflicts"), getJSON("/todos"), getJSON("/nudges"),
  getJSON("/todos/now"), getJSON("/departure/next"), getJSON("/impact/cascade"),
]);
const val = (i, fallback) => (settled[i].status === "fulfilled" ? settled[i].value : fallback);
outings = val(0, outings);
commitments = val(1, commitments);
conflicts = val(2, conflicts);
todos = val(3, todos);
nudges = val(4, nudges);
fitNow = val(5, fitNow);
departure = val(6, departure);
cascade = val(7, cascade);
const ok = settled.some((s) => s.status === "fulfilled");

const family = config.widgetFamily || "medium";
const small = family === "small";
const w = new ListWidget();
w.url = BASE_URL + "/dashboard"; // tap opens the full dashboard (after unlock on the Lock Screen)
// refreshAfterDate is set adaptively near the end, once we know whether anything
// time-sensitive is live (active outing, due departure, imminent commitment).

function text(stack, s, { color = C.fg, size = 13, bold = false, font, minScale } = {}) {
  const t = stack.addText(s);
  t.textColor = color;
  t.font = font || (bold ? Font.boldSystemFont(size) : Font.systemFont(size));
  t.lineLimit = 1;
  // Let iOS shrink the text to fit its slot instead of clipping/overflowing —
  // essential in the tiny circular accessory, where e.g. a time runs off the edge.
  if (minScale) t.minimumScaleFactor = minScale;
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

// A live, self-updating elapsed timer (counts up from a past date). iOS ticks a
// WidgetDate in timer style every second on the Lock Screen *without* a widget
// reload — so an outing's clock stays current between iOS's throttled refreshes
// (the phase word/color still only change on reload). Returns the WidgetDate.
function timer(stack, date, { color = C.fg, size = 13, bold = false, minScale } = {}) {
  const d = stack.addDate(date);
  d.applyTimerStyle();
  d.textColor = color;
  d.font = bold ? Font.boldSystemFont(size) : Font.systemFont(size);
  if (minScale) d.minimumScaleFactor = minScale;
  return d;
}

// The outing's departure as a Date (stored UTC), or null if unparseable — the
// anchor for the live elapsed timer.
function outingStart(o) {
  if (!o || !o.departure_at) return null;
  const d = new Date(String(o.departure_at).replace(" ", "T") + "Z");
  return isNaN(d) ? null : d;
}

// --- shared "right now" summary (used by every family) --------------------
const active = (outings.active || [])[0];

// Split commitments into the two streams the user cares about separately:
//   • self ("your commitments") — things that actually need YOU. These drive
//     "next up", the leave-by, and the refresh cadence.
//   • fyi  ("where someone will be") — informational; never a conflict, never
//     your leave-by. Shown in its own muted section so it can't be mistaken for
//     something you have to do. (Matches the dashboard's self/FYI split.)
const allCommitments = commitments.commitments || [];
const isFyi = (c) => c.kind === "fyi";
const selfCommitments = allCommitments.filter((c) => !isFyi(c));
const fyiCommitments = allCommitments.filter(isFyi);
// Lead each stream with today's items, falling back to the next upcoming when
// today is clear — computed per stream so "yours" and "FYI" each behave that way.
const pickShown = (list) => {
  const today = list.filter((c) => isToday(c.start_at));
  return today.length ? today : list;
};
const todayCommitments = selfCommitments.filter((c) => isToday(c.start_at));
const upcomingList = pickShown(selfCommitments); // YOUR commitments (never FYI)
const fyiList = pickShown(fyiCommitments);       // FYI (where someone else will be)
const nextCommitment = upcomingList[0];          // the next one that's actually yours

// Leave-by for the next commitment — *when to leave*, not just when it starts —
// from the read-only /departure/next (no nudge side effects). Surfaced only for a
// *travel* commitment that's today and whose leave-by we're the next for: a
// leave-by days out, or for a meeting you attend from your desk (mode "attend"),
// is noise. Colored by the departure level (heads_up → soon → go).
const dep = departure.departure;
const DEP_LEVEL_COLOR = { none: C.accent, heads_up: C.soft, soon: C.firm, go: C.call };
const nextDeparture =
  dep && dep.mode === "travel" && nextCommitment &&
  dep.commitment_id === nextCommitment.id && isToday(dep.start_at)
    ? dep
    : null;
// "leave 8:35" (+ travel estimate when known), or "leave now" once it's due.
function leaveByText(d) {
  const travel = d.travel_minutes != null ? ` · ${Math.round(d.travel_minutes)}m` : "";
  if (d.level === "go") return d.minutes_until_leave < 0 ? "leave now — late" : "leave now";
  return `leave ${fmtTime(d.leave_by)}${travel}`;
}
const hard = (conflicts.conflicts || []).length;
const poss = (conflicts.possible_conflicts || []).length;
const open = (todos.todos || []).length;

// Cascade knock-on: when you're running behind (an active outing over its window,
// or already too late to leave for something), which upcoming commitments topple.
// Surfaced only for a genuine domino (>=2 at risk) — a single at-risk item is
// already covered by the departure/next facets, so this earns its slot when it
// reveals a chain those don't. `/impact/cascade` is scoped to today's own
// commitments (never FYI, never tomorrow), so this reflects a real same-day
// domino — not a tight back-to-back on a future day (see the endpoint).
const cascadeRisky = (cascade.at_risk || []);
const cascadeChain = cascadeRisky.length >= 2 ? cascadeRisky : [];
const cascadeColor = cascade.hard_conflict ? C.call : C.firm;
// Worst (last, most-delayed) link's lateness, for the compact context line.
const cascadeWorst = cascadeChain.length
  ? Math.round(-cascadeChain[cascadeChain.length - 1].slack_minutes) : 0;

// "You have time for one thing" — the single open todo that fits the gap until
// your next commitment (server-computed, bounded by working hours + a cap).
const fitSug = fitNow.suggestion; // { title, estimate_minutes, effective_minutes, reason, ... } or null
const fitFree = Math.round(fitNow.free_minutes || 0);
// When the pick is something you've been avoiding, frame it as "catch up" (amber)
// rather than a breezy "free time" (green) — the honest-prioritization nudge.
const fitAvoided = !!(fitSug && fitSug.reason === "avoided");
const fitColor = fitAvoided ? C.soft : C.good;
const fitGlyph = fitAvoided ? "hourglass" : "bolt.fill";
const fitLead = fitAvoided ? "catch up" : `${fitFree}m free`;

// Most recent nudge the system sent — shown only while still "recent", so one
// you already acted on doesn't linger. The server now expires nudges (an outing
// nudge defaults to ~2h, a departure to its meeting start), so /nudges already
// drops stale ones; this client cap is a secondary bound for any legacy row that
// predates the server default and so came back with a NULL expiry.
const NUDGE_MAX_AGE_MIN = 2 * 60;
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
// ---------------------------------------------------------------------------
// A Lock Screen accessory slot is minuscule and iOS caps its size, so each one
// shows exactly ONE facet: a glyph + a short value. One script backs every
// slot — set a widget's Parameter (edit the Lock Screen → tap the slot →
// Scriptable → Parameter) to pin it to a facet, or leave it blank for "auto"
// (the most pressing facet right now). This lets you place several dedicated
// widgets: e.g. a circular "focus" beside a circular "next", an inline "alert".
//   focus / outing  → active outing: which escalation phase you're in
//                     (on track → heads up → wrap up → you're late), with a
//                     live elapsed timer that ticks between iOS's refreshes
//   next            → next commitment (start time)
//   alert / urgent  → conflicts or a due departure ("leave now")
//   behind / cascade → the knock-on chain when you're running late (>=2 topple)
//   todos / free    → the todo that fits your free window, else open count
// ===========================================================================

// A short clock label that fits a circular slot: "2:30", no AM/PM and no 24h
// zero-padding, so it stays narrow. Ambiguity is fine on a Lock Screen glance —
// the next commitment is nearly always within the coming few hours.
function fmtTimeShort(ts) {
  if (!ts) return "";
  const d = new Date(String(ts).replace(" ", "T") + "Z");
  if (isNaN(d)) return ts;
  let h = d.getHours() % 12;
  if (h === 0) h = 12;
  return `${h}:${String(d.getMinutes()).padStart(2, "0")}`;
}

// A compact day token for a date that isn't today: a weekday within the coming
// week ("Wed"), else a numeric date ("7/14"). Empty for today / an unparseable ts.
function dayTokShort(ts) {
  if (!ts || isToday(ts)) return "";
  const d = new Date(String(ts).replace(" ", "T") + "Z");
  if (isNaN(d)) return "";
  const dayDiff = daysFromToday(d);
  return dayDiff > 0 && dayDiff < 7
    ? d.toLocaleDateString([], { weekday: "short" })
    : `${d.getMonth() + 1}/${d.getDate()}`;
}

// The circular-slot value for the next commitment: the short time, prefixed with
// a compact day token when it isn't today, so a commitment days out doesn't read
// as a time this morning. Kept terse ("Wed 9:00", "7/14 9:00"); the circular
// renderer stacks the token over the time (see facetNext.lines) so a two-word
// value doesn't truncate ("Wed 11:…") in the tiny slot.
function fmtWhenShort(ts) {
  const tok = dayTokShort(ts);
  return tok ? `${tok} ${fmtTimeShort(ts)}` : fmtTimeShort(ts);
}

// A due departure ("leave now") arrives as the most recent *departure* nudge;
// it already self-expires at the meeting's start, so a live one is genuinely due.
const dueDeparture = recentNudge && recentNudge.kind === "departure" ? recentNudge : null;

// Each facet resolves to { glyph, value (circular), label (inline/rect), sub
// (rect 2nd line), color }, or null when it has nothing to show right now. The
// outing facet also carries `timerDate` (its departure): a live elapsed timer the
// renderers ticks in real time, plus `intention`/`word`/`windowLabel` so they can
// compose "out <timer> / 15m" around it. Renderers fall back to the static
// `value`/`sub` when there's no timerDate (e.g. an outing with no departure time).
function facetInProgress() {
  if (!active) return null;
  const ph = outingPhase(active);
  // Circular value = how deep into the window you are ("40%" → "120%" → "180%"),
  // so the phase reads as a number too; the escalating glyph carries it in mono.
  const value = ph.pct != null ? `${ph.pct}%` : mins(active.elapsed_minutes);
  return {
    glyph: ph.glyph, value,
    // Headline names the outing and the phase in words ("Coffee · wrap up").
    label: `${active.intention} · ${ph.word}`,
    // Rectangular's second line spells out both the numbers and the phase feeling.
    sub: `out ${mins(active.elapsed_minutes)}/${mins(active.time_window_minutes)} · ${ph.phrase}`,
    color: LEVEL_COLOR[active.level] || C.fg,
    timerDate: outingStart(active),
    // Once the outing passes an hour, renderers show `elapsedStatic` (whole
    // minutes, refreshed on reload) instead of ticking `timerDate`'s seconds.
    elapsedIsLong: isLongOuting(active),
    elapsedStatic: hm(active.elapsed_minutes),
    intention: active.intention,
    word: ph.word,
    windowLabel: mins(active.time_window_minutes),
  };
}
function facetNext() {
  if (!nextCommitment) return null;
  const ts = nextCommitment.start_at;
  const tok = dayTokShort(ts);
  return {
    glyph: "calendar", value: fmtWhenShort(ts),
    // Circular slot only: stack the day token over the time so a two-word value
    // ("Wed 11:00") doesn't truncate to "Wed 11:…" in the narrow circle. Today
    // (no token) stays a single, larger line.
    lines: tok
      ? [{ text: tok, size: 12 }, { text: fmtTimeShort(ts), size: 15, bold: true }]
      : [{ text: fmtTimeShort(ts), size: 17, bold: true }],
    label: `${fmtWhen(ts)} ${nextCommitment.title}`,
    // Rectangular's second line carries the leave-by when we have one, so the
    // Lock Screen "next" slot says when to leave, not only the title.
    sub: nextDeparture ? `${nextCommitment.title} · ${leaveByText(nextDeparture)}` : nextCommitment.title,
    color: C.fg,
  };
}
function facetUrgent() {
  if (hard) return {
    glyph: "exclamationmark.triangle.fill", value: String(hard),
    label: dueDeparture ? dueDeparture.message : `${hard} conflict${hard === 1 ? "" : "s"}`,
    sub: dueDeparture ? dueDeparture.message : `${hard} calendar conflict${hard === 1 ? "" : "s"}`,
    color: C.call,
  };
  if (dueDeparture) return {
    // Circular shows just glyph + value, so a walking figure + "now" reads as
    // "leave now"; the full "leave now for …" text is the label (inline/rect).
    glyph: "figure.walk", value: "now", label: dueDeparture.message,
    sub: dueDeparture.message, color: C.call,
  };
  return null;
}
function facetCascade() {
  if (!cascadeChain.length) return null;
  const titles = cascadeChain.map((c) => c.title);
  return {
    // A clock-with-warning reads as "you're behind on the clock"; falls back to
    // text on the rare device without the symbol.
    glyph: "clock.badge.exclamationmark", value: String(cascadeChain.length),
    label: `running behind: ${titles.slice(0, 2).join(" → ")}`,
    sub: `${cascadeChain.length} topple · ~${cascadeWorst}m worst`,
    color: cascadeColor,
  };
}
function facetTodos() {
  if (fitSug) return {
    glyph: fitGlyph, value: `${fitFree}m`, label: `${fitLead} · ${fitSug.title}`,
    sub: fitSug.title, color: fitColor,
  };
  return {
    glyph: "checklist", value: String(open), label: `${open} todo${open === 1 ? "" : "s"}`,
    sub: `${open} open`, color: C.fg,
  };
}

// A calm "nothing here" facet when a *pinned* slot has no live data.
function emptyFacet(kind) {
  const clear = { color: C.muted };
  if (kind === "focus" || kind === "outing") return { glyph: "figure.walk", value: "—", label: "No outing", sub: "not out", ...clear };
  if (kind === "next") return { glyph: "calendar", value: "—", label: "Nothing scheduled", sub: "clear", ...clear };
  if (kind === "alert" || kind === "urgent") return { glyph: "checkmark.circle", value: "0", label: "All clear", sub: "nothing urgent", ...clear };
  if (kind === "cascade" || kind === "behind") return { glyph: "clock.badge.checkmark", value: "0", label: "On track", sub: "nothing toppling", ...clear };
  return { glyph: "checklist", value: String(open), label: `${open} todo${open === 1 ? "" : "s"}`, sub: `${open} open`, ...clear };
}

const FACET_BY_PARAM = {
  focus: facetInProgress, outing: facetInProgress, "in-progress": facetInProgress,
  next: facetNext, commitment: facetNext,
  alert: facetUrgent, urgent: facetUrgent, conflicts: facetUrgent,
  cascade: facetCascade, behind: facetCascade, knockon: facetCascade,
  todos: facetTodos, todo: facetTodos, free: facetTodos,
};

// Set once at the top: the widget's Parameter, lowercased ("" = auto).
const PARAM = ((typeof args !== "undefined" && args.widgetParameter) || "").trim().toLowerCase();

// Resolve the facet to show. A pinned slot always shows its facet (falling back
// to a calm empty state); an unpinned slot shows the most pressing one, with
// `urgentFirst` so the single-line inline slot leads with what needs attention.
function pickFacet(urgentFirst = false) {
  if (PARAM && FACET_BY_PARAM[PARAM]) return FACET_BY_PARAM[PARAM]() || emptyFacet(PARAM);
  const order = urgentFirst
    ? [facetUrgent, facetCascade, facetInProgress, facetNext, facetTodos]
    : [facetInProgress, facetUrgent, facetCascade, facetNext, facetTodos];
  for (const f of order) { const r = f(); if (r) return r; }
  return facetTodos();
}

function renderInline() {
  // One line beside the clock: the single thing that most needs a glance.
  if (!ok) { symbol(w, "wifi.slash", 12); text(w, "Prefrontal offline", { size: 13 }); return; }
  const f = pickFacet(true);
  symbol(w, f.glyph, 12);
  // Outing: name + a live elapsed timer ("Coffee 12:34" ticking); the glyph
  // carries the phase. Past an hour the seconds are noise, so show static
  // minutes ("Coffee 1h 5m") instead. Everything else is a static label.
  if (f.timerDate) {
    text(w, " " + f.intention + " ", { size: 13 });
    if (f.elapsedIsLong) text(w, f.elapsedStatic, { size: 13 });
    else timer(w, f.timerDate, { size: 13 });
  } else {
    text(w, f.label, { size: 13 });
  }
}

function renderCircular() {
  // A glyph over its value — big enough to read at arm's length. Rows are laid
  // out vertically and each is horizontally centered (spacer on both sides).
  w.addAccessoryWidgetBackground = true;
  w.setPadding(0, 0, 0, 0);
  const col = w.addStack();
  col.layoutVertically();
  col.centerAlignContent();
  const row = () => { const r = col.addStack(); r.addSpacer(); return r; };
  const centerText = (s, opts) => { const r = row(); text(r, s, opts); r.addSpacer(); };
  const top = row();
  if (!ok) {
    symbol(top, "wifi.slash", 15); top.addSpacer();
    centerText("—", { size: 15 });
    return;
  }
  const f = pickFacet();
  symbol(top, f.glyph, 15); top.addSpacer();
  // Outing: a live elapsed timer under the phase glyph, so the clock keeps
  // ticking between reloads — until it passes an hour, when the ticking seconds
  // are dropped for static minutes. A multi-line facet (the "next" slot stacks
  // its day token over the time) renders one centered row per line so neither
  // truncates in the narrow circle. Otherwise the single value, auto-shrunk hard
  // so a wider value (a time like "12:30") fits instead of clipping.
  if (f.timerDate && !f.elapsedIsLong) {
    const r = row(); timer(r, f.timerDate, { size: 16, bold: true, minScale: 0.5 }); r.addSpacer();
  } else if (f.lines) {
    for (const ln of f.lines) centerText(ln.text, { size: ln.size || 16, bold: !!ln.bold, minScale: 0.45 });
  } else {
    centerText(f.timerDate ? f.elapsedStatic : f.value, { size: 16, bold: true, minScale: 0.45 });
  }
}

function renderRectangular() {
  // Icon-forward and minimal: a bold headline (glyph + label) over one muted
  // context line. No counts clutter — this slot shows one facet, like the others.
  w.addAccessoryWidgetBackground = true;
  // Reclaim the slot: iOS gives the rectangular accessory a fixed, short height
  // and draws our background across all of it, but Scriptable's default
  // ListWidget insets otherwise cluster the content into a small central band
  // (the "only fills half the slot" look). Zero the padding, then a full-height
  // vertical stack lays the lines out from the top.
  w.setPadding(0, 1, 0, 1);
  const col = w.addStack();
  col.layoutVertically();
  col.spacing = 1;
  const rowLine = (glyph, s, opts) => {
    const r = col.addStack();
    r.centerAlignContent();
    symbol(r, glyph, 13, opts && opts.color);
    text(r, " " + s, opts);
    r.addSpacer();
  };

  if (!ok) {
    rowLine("wifi.slash", "Prefrontal offline", { size: 14, bold: true });
    col.addSpacer();
    return;
  }
  const f = pickFacet();
  rowLine(f.glyph, f.label, { size: 14, bold: true, color: f.color });
  if (f.timerDate) {
    // Outing: a live "out <timer> / 15m" second line — the elapsed ticks, until
    // it passes an hour, when the seconds give way to static "out 1h 5m / 15m".
    const sub = col.addStack(); sub.centerAlignContent();
    text(sub, "out ", { size: 12, color: C.muted });
    if (f.elapsedIsLong) text(sub, f.elapsedStatic, { size: 12, color: C.muted });
    else timer(sub, f.timerDate, { size: 12, color: C.muted });
    text(sub, " / " + f.windowLabel, { size: 12, color: C.muted });
    sub.addSpacer();
  } else if (f.sub) {
    const sub = col.addStack(); sub.centerAlignContent();
    text(sub, f.sub, { size: 12, color: C.muted });
    sub.addSpacer();
  }
  // Push content to the top edge so it fills the slot from the top down.
  col.addSpacer();
}

// ===========================================================================
// Home Screen (Small / Medium / Large) — the full card.
// ===========================================================================
function renderHomeScreen() {
  w.backgroundColor = C.bg;
  w.setPadding(14, 16, 12, 14);
  // Medium is kept deliberately spare — just the header, your commitments, and a
  // single most-pressing alert line. FYI, the footer counts, and the last-nudge
  // line are large-only (large has the room; medium reads as crowded with them).
  const large = family === "large";

  // header: title only (no clock — a home-screen widget sits under the phone's
  // own clock, so its own timestamp is just noise). Offline is the one thing the
  // right side still calls out, since a stale card should say so.
  const head = w.addStack();
  head.centerAlignContent();
  text(head, "🧠 Prefrontal", { bold: true, size: small ? 13 : 15 });
  head.addSpacer();
  if (!ok) text(head, "offline", { color: C.call, size: 11 });
  w.addSpacer(small ? 6 : 8);

  if (!ok) {
    text(w, "Can't reach Prefrontal.", { color: C.muted, size: 12 });
    text(w, "Check Tailscale / token.", { color: C.muted, size: 11 });
    return;
  }

  // Active outing takes priority (time-sensitive). The dot + phase word ramp
  // none→call (here in color), and the second line spells out the phase.
  if (active) {
    const ph = outingPhase(active);
    const lvlColor = LEVEL_COLOR[active.level] || C.none;
    const start = outingStart(active);
    const row = w.addStack();
    row.centerAlignContent();
    text(row, "● ", { color: lvlColor, size: 13 });
    text(row, active.intention, { bold: true, size: small ? 13 : 14 });
    text(row, `  ${ph.word}`, { color: lvlColor, size: 11, bold: true });
    // Second line ticks live: "out <timer> of 15m · phrase". Once past an hour
    // the ticking seconds are dropped for static minutes ("out 1h 5m of …"); also
    // falls back to static when there's no parseable departure time.
    const line = w.addStack();
    line.centerAlignContent();
    text(line, "out ", { color: C.muted, size: 12 });
    if (start && !isLongOuting(active)) timer(line, start, { color: C.muted, size: 12 });
    else text(line, hm(active.elapsed_minutes), { color: C.muted, size: 12 });
    text(line, ` of ${mins(active.time_window_minutes)} · ${ph.phrase}`, { color: C.muted, size: 12 });
    w.addSpacer(6);
  }

  // Next commitments today. Medium stays lean (2) so the card reads at a glance;
  // large has the room for the fuller list.
  const upcoming = upcomingList.slice(0, small ? 1 : (large ? 6 : 2));
  if (upcoming.length) {
    if (!active) text(w, todayCommitments.length ? "Today" : "Next up", { color: C.muted, size: 11, bold: true });
    for (const c of upcoming) {
      const r = w.addStack();
      r.centerAlignContent();
      text(r, fmtWhen(c.start_at) + "  ", { color: C.accent, size: 12, bold: true });
      text(r, c.title, { size: small ? 12 : 13 });
      // When to *leave* for this one (travel commitment, today) — the actionable
      // number the start time alone doesn't give. A muted second line, colored up
      // to red as the leave-by bears down.
      if (nextDeparture && c.id === nextDeparture.commitment_id) {
        const lr = w.addStack();
        lr.centerAlignContent();
        const col = DEP_LEVEL_COLOR[nextDeparture.level] || C.accent;
        if (!symbol(lr, "figure.walk", 10, col)) text(lr, "🚶", { size: 10, color: col });
        text(lr, " " + leaveByText(nextDeparture),
          { color: col, size: 11, bold: nextDeparture.level === "go" });
      }
    }
  } else if (!active && !(fyiList.length && large)) {
    // Nothing that needs you, and nothing else will be shown (FYI is large-only)
    // — a genuinely clear glance rather than a near-empty card.
    text(w, "Nothing scheduled. 🎉", { color: C.muted, size: 13 });
  }

  // FYI — where someone else will be. Informational only (never yours to do), so
  // it's large-only: on medium it's the first thing to cut for a glanceable card.
  if (fyiList.length && large) {
    w.addSpacer(6);
    text(w, "FYI", { color: C.fyi, size: 11, bold: true });
    for (const c of fyiList.slice(0, 4)) {
      const r = w.addStack();
      r.centerAlignContent();
      text(r, fmtWhen(c.start_at) + "  ", { color: C.fyi, size: 12 });
      text(r, c.title, { color: C.muted, size: 13 });
    }
  }

  // A single most-pressing alert line. Medium shows ONLY the top one so the card
  // stays glanceable; large may show both. Priority: running-behind cascade (a
  // genuine >=2-item domino) outranks the softer "time for one thing" nudge.
  let alertShown = false;
  if (cascadeChain.length && !small) {
    w.addSpacer(6);
    const cr = w.addStack();
    cr.centerAlignContent();
    if (!symbol(cr, "clock.badge.exclamationmark", 11, cascadeColor)) {
      text(cr, "⚠️", { size: 11, color: cascadeColor });
    }
    const titles = cascadeChain.map((c) => c.title).slice(0, 2).join(" → ");
    text(cr, ` behind · ${titles}`, { color: cascadeColor, size: 11, bold: true });
    alertShown = true;
  }

  // "Time for one thing" — the todo that fits your free window right now (the
  // initiation nudge). On medium it yields when the behind line already took the
  // one alert slot; large can show it alongside.
  if (fitSug && !small && (large || !alertShown)) {
    w.addSpacer(6);
    const fr = w.addStack();
    fr.centerAlignContent();
    if (!symbol(fr, fitGlyph, 11, fitColor)) text(fr, fitAvoided ? "⏳" : "⚡", { size: 11, color: fitColor });
    text(fr, ` ${fitLead}`, { color: fitColor, size: 11, bold: true });
    text(fr, ` · ${fitSug.title}`, { size: 12 });
  }

  // Footer counts and the last-nudge line are large-only — on medium they're the
  // clutter the "keep it glanceable" pass removes. Large has room for both.
  if (large) {
    w.addSpacer(8);
    const foot = w.addStack();
    foot.centerAlignContent();
    if (hard) text(foot, `🔴 ${hard}  `, { color: C.call, size: 12 });
    if (poss) text(foot, `🟡 ${poss}  `, { color: C.soft, size: 12 });
    text(foot, `✓ ${open} todo${open === 1 ? "" : "s"}`, { color: C.muted, size: 12 });
    foot.addSpacer();
  }

  // Most recent nudge — what Prefrontal last told you, so a missed push is still
  // visible. Large-only (up to two lines).
  if (recentNudge && large) {
    w.addSpacer(6);
    const nrow = w.addStack();
    nrow.centerAlignContent();
    if (!symbol(nrow, "bell.badge", 11, C.accent)) text(nrow, "🔔", { size: 11 });
    const nt = text(nrow, " " + recentNudge.message, { size: 11, color: C.muted });
    nt.lineLimit = 2;
  }
}

// --- adaptive refresh hint -------------------------------------------------
// Ask iOS to reload sooner only when there's live, time-sensitive state; back
// off when idle so the daily reload budget is banked for those moments. All of
// `active` / `dueDeparture` / `nextCommitment` are resolved above.
function computeRefreshMinutes() {
  if (!ok) return REFRESH.offline; // mini unreachable — retry before long
  if (active) return active.level === "firm" || active.level === "call" ? REFRESH.live : REFRESH.active;
  if (dueDeparture) return REFRESH.live; // "leave now" is time-critical
  // A bearing-down leave-by is time-sensitive too — keep the countdown fresh as
  // it escalates, even before the departure nudge itself fires.
  if (nextDeparture) {
    if (nextDeparture.level === "soon" || nextDeparture.level === "go") return REFRESH.live;
    if (nextDeparture.level === "heads_up") return REFRESH.soon;
  }
  if (cascadeChain.length) return REFRESH.soon; // behind & toppling — keep it fresh
  if (nextCommitment) {
    const startMs = new Date(String(nextCommitment.start_at).replace(" ", "T") + "Z").getTime();
    const minsUntil = (startMs - Date.now()) / 60000;
    if (!isNaN(minsUntil) && minsUntil >= 0 && minsUntil <= SOON_WINDOW_MIN) return REFRESH.soon;
  }
  return REFRESH.idle;
}
w.refreshAfterDate = new Date(Date.now() + computeRefreshMinutes() * 60 * 1000);

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
