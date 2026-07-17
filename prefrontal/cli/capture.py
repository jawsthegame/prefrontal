"""``prefrontal note`` / ``braindump`` / ``vision`` — quick capture commands.

Low-friction inbound capture: a one-line note, a free-text brain dump split into
todos, and a photo turned into todos via the vision path.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from prefrontal.cli._common import _resolve_user_store
from prefrontal.config import get_settings
from prefrontal.impact import utcnow
from prefrontal.memory.store import MemoryStore

#: File extension → image MIME type for the ``vision`` command (the Anthropic
#: vision API's supported set; see ``SUPPORTED_IMAGE_MEDIA_TYPES``).
_IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def register(sub) -> None:
    """Attach the ``note``, ``braindump`` and ``vision`` subcommands."""
    p_note = sub.add_parser(
        "note", help="Feed a free-text note to the LLM sensor (proposes, never writes)."
    )
    p_note.add_argument(
        "text",
        nargs="?",
        default="",
        help="The observation, e.g. 'I always blow off admin on Mondays'.",
    )
    p_note.add_argument(
        "--transcript",
        default=None,
        metavar="PATH",
        help="Read a conversation instead: a JSON array of {\"speaker\",\"text\"} turns.",
    )
    p_note.add_argument("--db-path", default=None, help="Override the database path.")
    p_note.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_note.set_defaults(func=_cmd_note)

    p_braindump = sub.add_parser(
        "braindump",
        help="Turn a rambling voice/free-text dump into structured items (preview).",
    )
    p_braindump.add_argument(
        "text",
        nargs="?",
        default="",
        help="The dump, e.g. 'call the dentist, out of milk, I keep skipping admin'.",
    )
    p_braindump.add_argument(
        "--file",
        default=None,
        metavar="PATH",
        help="Read the dump from a file instead (use '-' for stdin).",
    )
    p_braindump.add_argument(
        "--apply",
        action="store_true",
        help="Execute the proposed edits now (default previews them; behavioral "
        "candidates are always left pending for review).",
    )
    p_braindump.add_argument("--db-path", default=None, help="Override the database path.")
    p_braindump.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_braindump.set_defaults(func=_cmd_braindump)

    p_vision = sub.add_parser(
        "vision",
        help="Read a photo (whiteboard, list, receipt) into structured items (preview).",
    )
    p_vision.add_argument(
        "path",
        metavar="PATH",
        help="Path to the image (.jpg/.jpeg/.png/.gif/.webp).",
    )
    p_vision.add_argument(
        "--apply",
        action="store_true",
        help="Execute the proposed edits now (default previews them; behavioral "
        "candidates are always left pending for review).",
    )
    p_vision.add_argument("--db-path", default=None, help="Override the database path.")
    p_vision.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_vision.set_defaults(func=_cmd_vision)


def _cmd_note(args: argparse.Namespace) -> int:
    """Feed a free-text note to the LLM sensor; store any candidates as pending.

    Nothing is written to the profile here — the model only *proposes* structured
    updates, which land as pending proposals for review (``prefrontal proposals``).

    Args:
        args: Parsed arguments; uses ``db_path``, ``user``, ``text``.

    Returns:
        Process exit code (0 on success).
    """
    import json

    from prefrontal.integrations import ProviderResolver
    from prefrontal.sensor import (
        avoided_state_keys,
        extract_candidates,
        extract_candidates_from_transcript,
        record_candidates,
        summarize_candidate,
    )

    settings = get_settings()
    # Claude when the ``sensor`` agent is opted into Anthropic, else local.
    client = ProviderResolver.from_settings(settings).client("sensor")
    # A --transcript file (JSON array of {"speaker","text"} turns) reads a whole
    # conversation; otherwise the positional text is a single note.
    turns: list[dict[str, str]] | None = None
    if getattr(args, "transcript", None):
        try:
            loaded = json.loads(Path(args.transcript).read_text())
        except (OSError, ValueError) as exc:
            print(f"Could not read transcript {args.transcript!r}: {exc}", file=sys.stderr)
            return 1
        if not isinstance(loaded, list):
            print("Transcript file must be a JSON array of turns.", file=sys.stderr)
            return 1
        turns = [t for t in loaded if isinstance(t, dict)]
    elif not (args.text or "").strip():
        print("Provide a note, or --transcript PATH.", file=sys.stderr)
        return 1
    with MemoryStore.open(args.db_path or settings.db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)
        # Close the calibration loop: de-emphasize settings the user reliably
        # rejects (from the last `prefrontal learn` pass) in the extraction prompt.
        avoid = avoided_state_keys(store)
        if turns is not None:
            candidates = extract_candidates_from_transcript(turns, client=client, avoid_keys=avoid)
        else:
            candidates = extract_candidates(args.text, client=client, avoid_keys=avoid)
        if not candidates:
            print("No candidate updates found (or the model was unreachable).")
            return 0
        ids = record_candidates(store, candidates)
        print(f"Recorded {len(ids)} pending proposal(s) — review with `prefrontal proposals`:")
        for pid, c in zip(ids, candidates, strict=True):
            print(f"  #{pid}  {summarize_candidate(c.kind, c.payload)}")
            if c.rationale:
                print(f"        ↳ {c.rationale}")
    return 0


def _cmd_braindump(args: argparse.Namespace) -> int:
    """Turn one rambling voice/free-text dump into structured items (roadmap M1).

    Fans a single ramble out to both capture paths (see :mod:`prefrontal.braindump`):
    the editing assistant proposes actionable edits (todos, commitments, shopping,
    if-then plans, household facts), and the LLM sensor proposes behavioral asides.
    Actionable edits are a **preview** by default — nothing is written until
    ``--apply`` (or the dashboard's Apply). The behavioral candidates always land
    as *pending* proposals for review (``prefrontal proposals``), never written
    outright — the same human-in-the-loop guarantee as ``prefrontal note``.

    Args:
        args: Parsed arguments; uses ``db_path``, ``user``, ``text``, ``file``,
            ``apply``.

    Returns:
        Process exit code (0 on success, 2 on empty input).
    """
    from prefrontal.assistant import execute_actions
    from prefrontal.braindump import plan_braindump
    from prefrontal.integrations import ProviderResolver
    from prefrontal.sensor import avoided_state_keys, record_candidates, summarize_candidate

    settings = get_settings()
    # Source the dump from --file (or "-" for stdin), else the positional text.
    if getattr(args, "file", None):
        try:
            text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
        except OSError as exc:
            print(f"Could not read {args.file!r}: {exc}", file=sys.stderr)
            return 2
    else:
        text = args.text or ""
    if not text.strip():
        print(
            'Say what\'s on your mind, e.g. `braindump "call the dentist, we\'re out '
            'of milk, I keep skipping admin"` (or --file PATH, or --file - for stdin).',
            file=sys.stderr,
        )
        return 2

    resolver = ProviderResolver.from_settings(settings)
    with MemoryStore.open(args.db_path or settings.db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)
        plan = plan_braindump(
            text,
            store,
            assistant_client=resolver.client("assistant"),
            sensor_client=resolver.client("sensor"),
            now=utcnow(),
            tz=settings.timezone,
            # Close the sensor's calibration loop (see `prefrontal note`).
            avoid_keys=avoided_state_keys(store),
        )
        # Actionable half — a preview unless --apply. The reply describes *this*
        # half ("I'll add a todo…"), so print it only alongside actions:
        # assistant.plan always fills a default reply ("I didn't find anything to
        # change"), which would be noise — and misleading when the sensor half did
        # find something — if shown on its own.
        if plan.actions:
            if plan.reply:
                print(plan.reply)
            if args.apply:
                results = execute_actions(
                    store, plan.actions, timezone=settings.timezone,
                    client=resolver.client("assistant"),
                )
                applied = sum(1 for r in results if r["ok"])
                print(f"\nApplied {applied}/{len(results)} edit(s):")
                for r in results:
                    mark = "✓" if r["ok"] else "✗"
                    tail = f" — {r['detail']}" if r.get("detail") else ""
                    print(f"  {mark} {r['summary']}{tail}")
            else:
                print(
                    f"\n{len(plan.actions)} proposed edit(s) "
                    "(nothing written — re-run with --apply):"
                )
                for a in plan.actions:
                    print(f"  • {a.summary}")
        for err in plan.errors:
            print(f"  (skipped: {err})", file=sys.stderr)

        # Behavioral half — always recorded pending for review, never auto-applied.
        if plan.candidates:
            ids = record_candidates(store, plan.candidates)
            print(
                f"\nNoticed {len(ids)} thing(s) about you — recorded pending, "
                "review with `prefrontal proposals`:"
            )
            for pid, c in zip(ids, plan.candidates, strict=True):
                print(f"  #{pid}  {summarize_candidate(c.kind, c.payload)}")
                if c.rationale:
                    print(f"        ↳ {c.rationale}")

        # Nothing on either half (an empty dump, or the model was unreachable).
        if not plan.actions and not plan.candidates:
            print("Nothing to capture (or the model was unreachable).")
    return 0


def _cmd_vision(args: argparse.Namespace) -> int:
    """Read a photo into structured items (roadmap: vision capture).

    The image twin of ``prefrontal braindump``: a photo of anything already written
    down (a whiteboard, a newsletter, a scribbled list) is transcribed by the
    multimodal model and fanned out through the *same* capture paths — actionable
    edits proposed as a **preview** (nothing written until ``--apply``) and
    behavioral asides recorded *pending* for review (``prefrontal proposals``).

    Vision is local-first: it reads with the on-device multimodal model when one
    is configured (``OLLAMA_VISION_MODEL``, e.g. ``llava``) and installed, else the
    cloud Anthropic model (``ANTHROPIC_API_KEY``). With neither there's no way to
    read the image and the command exits non-zero rather than guessing.

    Args:
        args: Parsed arguments; uses ``db_path``, ``user``, ``path``, ``apply``.

    Returns:
        Process exit code (0 on success, 2 on a bad/unreadable image or no backend).
    """
    import base64

    from prefrontal.assistant import execute_actions
    from prefrontal.integrations import ProviderResolver
    from prefrontal.sensor import avoided_state_keys, record_candidates, summarize_candidate
    from prefrontal.vision import plan_vision

    settings = get_settings()
    path = Path(args.path)
    media_type = _IMAGE_MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        print(
            f"Unsupported image type {path.suffix!r}; expected one of "
            f"{', '.join(sorted(set(_IMAGE_MEDIA_TYPES)))}.",
            file=sys.stderr,
        )
        return 2
    try:
        image_base64 = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as exc:
        print(f"Could not read {args.path!r}: {exc}", file=sys.stderr)
        return 2

    resolver = ProviderResolver.from_settings(settings)
    # Local-first: prefer the on-device multimodal model, fall back to cloud.
    vision_client, vision_provider = resolver.select_vision()
    if vision_client is None:
        print(
            "Vision needs a multimodal backend: set OLLAMA_VISION_MODEL (and pull "
            "it) for on-device, or ANTHROPIC_API_KEY plus "
            "'pip install prefrontal[anthropic]' for cloud.",
            file=sys.stderr,
        )
        return 2
    print(f"(reading image with the {vision_provider} vision model…)", file=sys.stderr)

    with MemoryStore.open(args.db_path or settings.db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)
        plan = plan_vision(
            image_base64,
            media_type,
            store,
            vision_client=vision_client,
            assistant_client=resolver.client("assistant"),
            sensor_client=resolver.client("sensor"),
            now=utcnow(),
            tz=settings.timezone,
            avoid_keys=avoided_state_keys(store),
        )
        if not plan.transcript:
            print("Couldn't read anything usable from the image.", file=sys.stderr)
            return 2
        # Actionable half — a preview unless --apply (mirrors `braindump`).
        if plan.actions:
            if plan.reply:
                print(plan.reply)
            if args.apply:
                results = execute_actions(
                    store, plan.actions, timezone=settings.timezone,
                    client=resolver.client("assistant"),
                )
                applied = sum(1 for r in results if r["ok"])
                print(f"\nApplied {applied}/{len(results)} edit(s):")
                for r in results:
                    mark = "✓" if r["ok"] else "✗"
                    tail = f" — {r['detail']}" if r.get("detail") else ""
                    print(f"  {mark} {r['summary']}{tail}")
            else:
                print(
                    f"\n{len(plan.actions)} proposed edit(s) "
                    "(nothing written — re-run with --apply):"
                )
                for a in plan.actions:
                    print(f"  • {a.summary}")
        for err in plan.errors:
            print(f"  (skipped: {err})", file=sys.stderr)

        # Behavioral half — always recorded pending for review, never auto-applied.
        if plan.candidates:
            ids = record_candidates(store, plan.candidates)
            print(
                f"\nNoticed {len(ids)} thing(s) about you — recorded pending, "
                "review with `prefrontal proposals`:"
            )
            for pid, c in zip(ids, plan.candidates, strict=True):
                print(f"  #{pid}  {summarize_candidate(c.kind, c.payload)}")
                if c.rationale:
                    print(f"        ↳ {c.rationale}")

        if not plan.actions and not plan.candidates:
            print("Nothing to capture from the image.")
    return 0


