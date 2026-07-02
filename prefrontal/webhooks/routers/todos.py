"""HTTP routes tagged "todos".

APIRouter factory for :func:`prefrontal.webhooks.app.create_app`.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter

from prefrontal.memory.store import gmail_message_url
from prefrontal.webhooks._common import (
    DEFAULT_BODY_DOUBLE_MIN_MISSES,
    DEFAULT_FIT_CAP_MINUTES,
    DEFAULT_MAX_FIRST_STEP_MINUTES,
    DEFAULT_MIN_WINDOW_MINUTES,
    MAX_CATEGORIES,
    Annotated,
    Any,
    Depends,
    HTTPException,
    ScopedRequest,
    StepDone,
    TodoCategoryUpdate,
    TodoCreate,
    TodoDeadlineUpdate,
    TodoWindowUpdate,
    _decompose_and_store,
    at_category_cap,
    augment_todo,
    available_now,
    avoided_todos,
    body_double_message,
    category_stats,
    decompose_task,
    filter_suggestible,
    fit_todos,
    format_window,
    local_datetime,
    local_hour_of,
    normalize_category,
    parse_window,
    pick_now,
    record_drop_feedback,
    record_todo_closed,
    repeat_stalled_tasks,
    resolve_user,
    status,
    timedelta,
    to_utc,
    utcnow,
    window_config_for,
    work_window_now,
)


def _validated_window(spec: str | None) -> str | None:
    """Return a normalized ``"HH:MM-HH:MM"`` window, or ``None`` for an empty spec.

    Raises 422 on a non-empty but malformed spec so a bad per-todo override is
    rejected at the edge rather than silently ignored downstream.
    """
    if spec is None or not spec.strip():
        return None
    parsed = parse_window(spec)
    if parsed is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f'Bad time_window: {spec!r} — expected "HH:MM-HH:MM".',
        )
    return format_window(*parsed)


def build_router(
    *,
    resolved_settings,
    n8n,
    ollama_client,
    summarizer_client,
    geocoder_client,
    _run_geocode,
) -> APIRouter:
    """Build the "todos" APIRouter (shared services injected by create_app)."""
    router = APIRouter()

    @router.post("/todos", status_code=status.HTTP_201_CREATED, tags=["todos"])
    def todo_create(
        payload: TodoCreate,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Add an open todo, auto-filling any fields you didn't supply.

        Missing ``estimate_minutes``/``priority``/``energy``/``deadline``/
        ``category`` are inferred (local model → keyword heuristic → default) so
        the todo is schedulable and honestly sortable. Supplied values are kept
        as-is; the response's ``augmented`` map says where each field came from.
        The category is clamped to the user's existing set once it reaches 20.
        """
        memory = ctx.store
        # A user-supplied deadline is validated strictly (422 on garbage); an
        # inferred one is best-effort (dropped if it somehow won't parse).
        user_deadline = None
        if payload.deadline:
            try:
                user_deadline = to_utc(payload.deadline)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Bad deadline: {exc}",
                ) from exc
        # A per-todo window override is validated strictly (422 on garbage).
        time_window = _validated_window(payload.time_window)

        aug = augment_todo(
            payload.title,
            estimate_minutes=payload.estimate_minutes,
            priority=payload.priority,
            energy=payload.energy,
            deadline=payload.deadline,
            category=payload.category,
            existing_categories=memory.todo_categories(),
            client=ollama_client,
        )
        deadline = user_deadline
        if user_deadline is None and aug.deadline:
            try:
                deadline = to_utc(aug.deadline)
            except ValueError:
                deadline = None

        todo_id = memory.add_todo(
            payload.title,
            notes=payload.notes,
            estimate_minutes=aug.estimate_minutes,
            priority=aug.priority,
            deadline=deadline,
            energy=aug.energy,
            category=aug.category,
            time_window=time_window,
        )
        # Big tasks stall on starting — auto-decompose into a tiny first step.
        threshold = memory.get_float("decomposition_threshold_minutes", 30.0)
        decomposition = None
        if aug.estimate_minutes >= threshold:
            decomposition = _decompose_and_store(
                memory, todo_id, payload.title, ollama_client
            )
        return {
            "todo_id": todo_id,
            "estimate_minutes": aug.estimate_minutes,
            "priority": aug.priority,
            "energy": aug.energy,
            "deadline": deadline,
            "category": aug.category,
            "augmented": aug.sources,
            "decomposition": decomposition,
        }

    @router.get("/todos", tags=["todos"])
    def todos_list(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """List open todos with decompositions, an avoidance flag, and the mail
        account each came from.

        Each todo carries an ``account`` (the mail inbox it was ingested from, or
        ``None`` for manual/impulse todos). The response also echoes the
        operator-configured ``accounts`` label map so the dashboard can render a
        friendly, colored pill (e.g. ``work`` → an orange "Vistar" pill) without
        hard-coding any account names or colors.
        """
        memory = ctx.store
        todos = memory.open_todos()
        avoided = {a["todo"]["id"]: a for a in avoided_todos(todos, utcnow())}
        sources = memory.mail_sources_for_todos([t["id"] for t in todos])
        for todo in todos:
            todo["decomposition"] = memory.get_decomposition(todo["id"])
            src = sources.get(todo["id"]) or {}
            account = src.get("account")
            todo["account"] = account
            # Deep-link back to the source email, but only for Gmail inboxes —
            # the link is a Gmail rfc822msgid search, meaningless elsewhere.
            todo["source_url"] = (
                gmail_message_url(src.get("message_id"))
                if resolved_settings.is_gmail_account(account)
                else None
            )
            hit = avoided.get(todo["id"])
            todo["avoidance"] = (
                {"days_open": hit["days_open"], "score": hit["score"]} if hit else None
            )
        return {"todos": todos, "accounts": resolved_settings.account_label_map}

    @router.get("/todos/avoided", tags=["todos"])
    def todos_avoided(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """The important todos you keep skipping, worst-avoided first.

        Honest prioritization: surfaces what's been sitting (high enough priority,
        open a while) so the fun/shiny task doesn't quietly win. Pure heuristic
        over age/priority/size/deadline — no extra tracking.
        """
        memory = ctx.store
        items = avoided_todos(memory.open_todos(), utcnow())
        return {
            "avoided": [
                {
                    "todo_id": a["todo"]["id"],
                    "title": a["todo"]["title"],
                    "days_open": a["days_open"],
                    "score": a["score"],
                    "priority": a["todo"].get("priority"),
                    "estimate_minutes": a["todo"].get("estimate_minutes"),
                    "deadline": a["todo"].get("deadline"),
                }
                for a in items
            ]
        }

    @router.get("/todos/stuck", tags=["todos"])
    def todos_stuck(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Tasks you keep bailing on, with a start-together (body-double) nudge.

        The Task Paralysis ``body_double_nudge`` intervention: repeated ``miss``
        episodes on the same task mean a solo start isn't working. Each entry
        pairs the stall count with one tiny heuristic first step and a
        start-together suggestion — surfacing what a plain reminder won't fix.
        A task that was ultimately completed drops off (see
        :func:`~prefrontal.modules.task_paralysis.repeat_stalled_tasks`).
        """
        memory = ctx.store
        try:
            min_misses = int(
                memory.get_state("body_double_min_misses")
                or DEFAULT_BODY_DOUBLE_MIN_MISSES
            )
        except (TypeError, ValueError):
            min_misses = DEFAULT_BODY_DOUBLE_MIN_MISSES
        stuck = repeat_stalled_tasks(
            memory.episodes_by_type("task", limit=200), min_misses=min_misses
        )
        max_first = memory.get_float(
            "max_first_step_minutes", DEFAULT_MAX_FIRST_STEP_MINUTES
        )
        return {
            "stuck": [
                {
                    "title": s["title"],
                    "misses": s["misses"],
                    "attempts": s["attempts"],
                    "first_step": decompose_task(
                        s["title"], max_first_minutes=max_first
                    ).first_step,
                    "suggestion": body_double_message(s["title"], s["misses"]),
                }
                for s in stuck
            ]
        }

    @router.get("/todos/categories", tags=["todos"])
    def todos_categories(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Per-category rollup + the current vocabulary, for the dashboard panel.

        ``stats`` is one entry per category (open/done/dropped counts, the typical
        estimate = "common execution length", completion rate, and summed
        avoidance) busiest-first; ``categories`` is the plain vocabulary list
        (most-common-first) for edit menus; ``cap`` is the ceiling and ``at_cap``
        says whether a new category can still be coined.
        """
        memory = ctx.store
        categories = memory.todo_categories()
        return {
            "stats": category_stats(memory.all_todos(), utcnow()),
            "categories": categories,
            "cap": MAX_CATEGORIES,
            "at_cap": len(categories) >= MAX_CATEGORIES,
        }

    @router.post("/todos/{todo_id}/decompose", tags=["todos"])
    def todo_decompose(
        todo_id: int,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Break an open todo into a tiny first step (+ remaining steps).

        On-demand counterpart to the auto-decompose on big todos — call it the
        moment you're about to start something and need a way in. Regenerates
        and replaces any existing decomposition.
        """
        memory = ctx.store
        todo = memory.get_todo(todo_id)
        if todo is None or todo["status"] != "open":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Todo {todo_id} is not open.",
            )
        decomposition = _decompose_and_store(
            memory, todo_id, todo["title"], ollama_client
        )
        return {"todo_id": todo_id, "decomposition": decomposition}

    @router.post("/todos/{todo_id}/deadline", tags=["todos"])
    def todo_set_deadline(
        todo_id: int,
        payload: TodoDeadlineUpdate,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Move (or clear) an open todo's deadline.

        Plans drift — a deadline set at creation or inferred from the title often
        needs to change. A non-empty ``deadline`` is normalized to UTC (422 on
        garbage); ``null``/empty clears it. Declared before the ``{action}`` route
        so "deadline" isn't mistaken for a done/drop action.
        """
        memory = ctx.store
        deadline = None
        if payload.deadline:
            try:
                deadline = to_utc(payload.deadline)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Bad deadline: {exc}",
                ) from exc
        if not memory.update_todo_deadline(todo_id, deadline):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Todo {todo_id} is not open.",
            )
        return {"todo_id": todo_id, "deadline": deadline}

    @router.post("/todos/{todo_id}/category", tags=["todos"])
    def todo_set_category(
        todo_id: int,
        payload: TodoCategoryUpdate,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Set (or clear) a todo's category — the UI override for the inference.

        ``null``/empty clears it (uncategorized). A value is normalized; a brand
        new category is only allowed while under the cap of
        :data:`~prefrontal.todos.MAX_CATEGORIES` — at the cap you must reuse an
        existing one (409), which is what keeps the derived set bounded. Declared
        before the ``{action}`` route so "category" isn't read as an action.
        """
        memory = ctx.store
        existing = memory.todo_categories()
        category = normalize_category(payload.category) if payload.category else None
        if category is not None and at_category_cap(category, existing):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Category limit of {MAX_CATEGORIES} reached — reuse an "
                    f"existing category instead of creating '{category}'."
                ),
            )
        if not memory.set_todo_category(todo_id, category):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No todo {todo_id}.",
            )
        return {"todo_id": todo_id, "category": category}

    @router.post("/todos/{todo_id}/window", tags=["todos"])
    def todo_set_window(
        todo_id: int,
        payload: TodoWindowUpdate,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Set (or clear) a todo's per-todo suggestion window override.

        A value is validated as ``"HH:MM-HH:MM"`` (422 otherwise) and overrides
        the category/source/default window used when deciding whether the todo is
        suggestible right now; ``null``/empty clears it so the category window
        applies. Only open todos are editable (404 otherwise). Declared before the
        ``{action}`` route so "window" isn't read as an action.
        """
        memory = ctx.store
        time_window = _validated_window(payload.time_window)
        if not memory.set_todo_window(todo_id, time_window):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Todo {todo_id} is not open.",
            )
        return {"todo_id": todo_id, "time_window": time_window}

    @router.post("/todos/{todo_id}/steps/{step_index}/done", tags=["todos"])
    def todo_step_done(
        todo_id: int,
        step_index: int,
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
        payload: StepDone | None = None,
    ) -> dict[str, Any]:
        """Tick a single decomposed step done (or clear it).

        Index ``0`` is the first step and ``1..N`` are the remaining steps.
        Checking steps off one at a time turns a stalled task into visible
        progress. Body ``{"done": false}`` un-ticks a step; the body is optional
        and defaults to marking it done. Returns the refreshed decomposition.
        """
        memory = ctx.store
        done = payload.done if payload is not None else True
        if not memory.set_step_done(todo_id, step_index, done=done):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Todo {todo_id} has no step {step_index}.",
            )
        return {
            "todo_id": todo_id,
            "step_index": step_index,
            "done": done,
            "decomposition": memory.get_decomposition(todo_id),
        }

    @router.post("/todos/{todo_id}/{action}", tags=["todos"])
    def todo_close(
        todo_id: int,
        action: Literal["done", "drop"],
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
    ) -> dict[str, Any]:
        """Mark a todo done or drop it.

        Closing logs a ``task`` episode (done ⇒ ``success``, drop ⇒ ``miss``) so
        the outcome feeds the learning pass like every other touchpoint — the
        moment an avoided todo finally resolves is captured, not discarded.

        Dropping additionally feeds the *triage* loop: if the todo came from mail
        intake, the drop is recorded as a correction (see
        :func:`prefrontal.mail.feedback.record_drop_feedback`) so a future sync's
        prompt learns not to repeat that false positive.
        """
        memory = ctx.store
        new_status = "done" if action == "done" else "dropped"
        if not memory.close_todo(todo_id, status=new_status):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Todo {todo_id} is not open.",
            )
        closed = memory.get_todo(todo_id)
        episode_id = (
            record_todo_closed(memory, closed, now=utcnow())["episode_id"]
            if closed is not None
            else None
        )
        if action == "drop" and closed is not None:
            record_drop_feedback(memory, todo_id, closed, now=utcnow())
        return {"todo_id": todo_id, "status": new_status, "episode_id": episode_id}

    @router.get("/todos/fit", tags=["todos"])
    def todos_fit(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
        minutes: float,
    ) -> dict[str, Any]:
        """Rank the open todos that fit in ``minutes`` of free time, right now.

        Applies the learned time bias, so a "10-minute" todo is judged at its
        realistic length. Great for "I have 20 minutes — what can I knock out?"
        """
        memory = ctx.store
        bias = memory.get_float("time_estimation_bias", 1.0)
        fits = fit_todos(minutes, memory.open_todos(), bias)
        return {
            "available_minutes": minutes,
            "fits": [
                {
                    "todo_id": f["todo"]["id"],
                    "title": f["todo"]["title"],
                    "estimate_minutes": f["todo"].get("estimate_minutes"),
                    "effective_minutes": f["effective_minutes"],
                    "priority": f["todo"].get("priority"),
                }
                for f in fits
            ],
        }

    @router.get("/todos/now", tags=["todos"])
    def todos_now(
        ctx: Annotated[ScopedRequest, Depends(resolve_user)],
        cap_minutes: float = DEFAULT_FIT_CAP_MINUTES,
    ) -> dict[str, Any]:
        """The single best open todo that fits your free time *right now*.

        "Right now" = the gap until your next commitment, bounded by your waking
        hours (the off-zone's complement — nothing is offered inside the off-zone,
        default 22:00–06:00 local) and capped (so a wide-open evening doesn't
        offer a multi-hour task). Candidates are further filtered to those whose
        suggestion window (per-todo → category → source → default) includes the
        current local time, so a focus-hours task isn't surfaced at 9pm. Outside
        waking hours, or with no real gap / nothing that fits *and belongs now*,
        ``suggestion`` is ``null`` (with a ``reason``). Powers the widget's "you
        have 25 min — knock this out" prompt.
        """
        memory = ctx.store
        now = utcnow()
        window_config = window_config_for(resolved_settings, memory)
        day_start, day_end = window_config.awake_band()
        within, horizon = work_window_now(
            now, resolved_settings.timezone,
            cap_minutes=cap_minutes, day_start=day_start, day_end=day_end,
        )
        upcoming = memory.upcoming_commitments(limit=1)
        result: dict[str, Any] = {
            "free_minutes": 0,
            "within_hours": within,
            "next_commitment": (
                {"title": upcoming[0]["title"], "start_at": upcoming[0]["start_at"]}
                if upcoming else None
            ),
            "suggestion": None,
            "reason": None,
        }
        if not within:
            result["reason"] = "outside waking hours"
            return result

        fmt = "%Y-%m-%d %H:%M:%S"
        # Look back far enough to catch an in-progress (or all-day) commitment
        # that started before now; free_windows clips it to the [now, horizon] band.
        commitments = memory.commitments_between(
            (now - timedelta(hours=26)).strftime(fmt), horizon.strftime(fmt)
        )
        free = available_now(commitments, now, horizon)
        result["free_minutes"] = round(free)
        if free < DEFAULT_MIN_WINDOW_MINUTES:
            result["reason"] = "no free time right now"
            return result

        # Only todos whose window includes now (off-zone already excluded above).
        now_local = local_datetime(now, resolved_settings.timezone)
        open_todos = filter_suggestible(memory.open_todos(), now_local, window_config)
        bias = memory.get_float("time_estimation_bias", 1.0)
        fits = fit_todos(free, open_todos, bias)
        if not fits:
            result["reason"] = "nothing fits this window"
            return result
        # Honest pick: surface the most-avoided todo that fits; else the best fit,
        # preferring low-energy tasks later in the day.
        avoided_ids = [a["todo"]["id"] for a in avoided_todos(open_todos, now)]
        top = pick_now(fits, avoided_ids, local_hour_of(now, resolved_settings.timezone))
        t = top["todo"]
        result["suggestion"] = {
            "todo_id": t["id"],
            "title": t["title"],
            "estimate_minutes": t.get("estimate_minutes"),
            "effective_minutes": top["effective_minutes"],
            "priority": t.get("priority"),
            "energy": t.get("energy"),
            "reason": top["reason"],  # "avoided" (been putting it off) | "fits"
        }
        return result

    # -- Admin: user provisioning (operator-only) ----------------------------

    return router
