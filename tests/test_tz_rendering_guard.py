"""Static guard against a recurring bug class: rendering a stored *naive-UTC*
timestamp as a wall clock without converting to the local zone.

Prefrontal stores every timestamp as naive UTC (see :mod:`prefrontal.clock`).
Showing one to the user as ``HH:MM`` therefore requires
:func:`prefrontal.clock.local_datetime` first — skipping it prints times in
UTC (e.g. 4h ahead for an Eastern user). This has bitten the morning briefing,
the rough-day recovery plan, and the household sheet (PR #307), each time by one
of two code shapes. These two AST checks catch a fresh occurrence in CI instead
of on someone's phone:

1. **No timestamp string slices.** ``ts[11:16]`` / ``ts[11:19]`` pulls ``HH:MM``
   straight out of the stored UTC string. There is never a good reason to — go
   through ``local_datetime``. No exceptions allowed.

2. **Clock ``strftime`` must be local.** A ``.strftime()`` whose format carries a
   wall-clock token (``%H``/``%I``/``%p``/``%-I``/``%-H``) must render an
   already-local datetime — its receiver names a local source (``local_datetime``,
   ``now_local``, ``local_start`` …) — or be marked ``# tz-ok: <why>`` for the
   rare legitimate case: a stored *local* wall-clock **schedule** string, or an
   intentional UTC **wire** format (iCal). Date-only formats (``%Y-%m-%d``) carry
   no clock token and are ignored.

If you're adding a genuinely-local or wire-format case, append ``# tz-ok: <why>``
on the offending line. If you're rendering a stored timestamp, convert it with
``local_datetime(parse_ts(ts), tz)`` instead.
"""

from __future__ import annotations

import ast
import pathlib

SRC_ROOT = pathlib.Path(__file__).resolve().parent.parent / "prefrontal"

#: strftime format tokens that denote a wall clock (hour/half-day marker). Minute
#: (%M) and second (%S) never appear without one of these in this codebase, so
#: anchoring on the hour keeps date-only formats (%Y-%m-%d) out of scope.
CLOCK_TOKENS = ("%H", "%I", "%p", "%-I", "%-H")

#: Marker a maintainer puts on a line to assert a clock strftime is legitimate.
ALLOW_MARKER = "# tz-ok"

#: A receiver is treated as already-local when the word ``local`` appears in its
#: source — the codebase's convention for a converted value (``local_datetime(…)``,
#: ``now_local``, ``start_local``/``leave_local``). No explicit marker is then
#: needed; this is the common, correct path.
LOCAL_HINT = "local"


def _py_files() -> list[pathlib.Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


def _slice_offenders(path: pathlib.Path, src: str, tree: ast.AST) -> list[str]:
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Slice):
            lo = n.slice.lower.value if isinstance(n.slice.lower, ast.Constant) else None
            hi = n.slice.upper.value if isinstance(n.slice.upper, ast.Constant) else None
            if lo == 11 and hi in (13, 16, 19):
                seg = ast.get_source_segment(src, n) or "<slice>"
                out.append(f"{path}:{n.lineno}: `{seg}` — slices a wall clock out of a UTC string")
    return out


def _clock_strftime_offenders(path: pathlib.Path, src: str, tree: ast.AST) -> list[str]:
    lines = src.splitlines()
    out = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
            continue
        if n.func.attr != "strftime" or not n.args:
            continue
        fmt = n.args[0]
        if not (isinstance(fmt, ast.Constant) and isinstance(fmt.value, str)):
            continue
        if not any(tok in fmt.value for tok in CLOCK_TOKENS):
            continue
        recv = (ast.get_source_segment(src, n.func.value) or "").lower()
        if LOCAL_HINT in recv:
            continue
        end = getattr(n, "end_lineno", n.lineno) or n.lineno
        # Accept the marker on the call's own line(s) or the line directly above it,
        # where an explanatory comment most naturally sits.
        spanned = "\n".join(lines[max(0, n.lineno - 2) : end])
        if ALLOW_MARKER in spanned:
            continue
        seg = ast.get_source_segment(src, n) or "<strftime>"
        out.append(
            f"{path}:{n.lineno}: `{seg}` — clock strftime on a non-local receiver; "
            f"convert via local_datetime or mark `{ALLOW_MARKER}: <why>`"
        )
    return out


def test_no_utc_timestamp_slices() -> None:
    offenders: list[str] = []
    for path in _py_files():
        src = path.read_text()
        offenders += _slice_offenders(path, src, ast.parse(src))
    assert not offenders, (
        "Timestamp string slices found (render via local_datetime):\n" + "\n".join(offenders)
    )


def test_clock_strftime_is_local_or_marked() -> None:
    offenders: list[str] = []
    for path in _py_files():
        src = path.read_text()
        offenders += _clock_strftime_offenders(path, src, ast.parse(src))
    assert not offenders, "Wall-clock strftime not proven local:\n" + "\n".join(offenders)


# --- the detectors themselves (lock the behavior these guards rely on) --------

FAKE = pathlib.Path("prefrontal/fake.py")


def _clock_hits(src: str) -> int:
    return len(_clock_strftime_offenders(FAKE, src, ast.parse(src)))


def _slice_hits(src: str) -> int:
    return len(_slice_offenders(FAKE, src, ast.parse(src)))


def test_slice_detector_flags_hhmm_slices_only() -> None:
    assert _slice_hits('x = ts[11:16]\n') == 1          # HH:MM
    assert _slice_hits('x = ts[11:19]\n') == 1          # HH:MM:SS
    assert _slice_hits('x = ts[:10]\n') == 0            # date slice — not a wall clock


def test_clock_strftime_detector_local_and_markers() -> None:
    assert _clock_hits('y = start.strftime("%H:%M")\n') == 1          # non-local receiver → flagged
    assert _clock_hits('y = local_dt.strftime("%H:%M")\n') == 0       # names "local" → skipped
    assert _clock_hits('y = start_local.strftime("%H:%M")\n') == 0    # ..._local suffix too
    assert _clock_hits('y = due.strftime("%H:%M")  # tz-ok: schedule\n') == 0   # same-line marker
    assert _clock_hits('# tz-ok: schedule\ny = due.strftime("%H:%M")\n') == 0   # above-line marker
    assert _clock_hits('y = d.strftime("%Y-%m-%d")\n') == 0           # date-only → out of scope
