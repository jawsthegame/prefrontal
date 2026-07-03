# Contributing to Prefrontal

Thanks for your interest. Prefrontal is open source (MIT) and contributions,
feedback, and lived experience with executive function challenges are all
welcome. This document explains how the project is laid out and the conventions
that keep it transparent and easy to work on — for new contributors and for
future-us alike.

## Project layout

```
prefrontal/
├── prefrontal/                # the Python package
│   ├── config.py              # environment-driven Settings (see .env.example)
│   ├── cli.py                 # `prefrontal` console script: init-db | serve | user | learn | summarize |
│   │                          #   profile | briefing | todo | fit | mail | coach | encourage | panic |
│   │                          #   crunch | note | proposals | household | modules | migrate-multi-tenant
│   ├── memory/                # the SQLite behavioral memory layer (the core)
│   │   ├── schema.sql         # canonical schema (per-user + household tables)
│   │   ├── db.py              # connection management + init_db()
│   │   ├── migrate.py         # multi-tenant rebuild + added-column back-fill ladder
│   │   ├── store.py           # MemoryStore: the read/write API (composed from repos/)
│   │   ├── repos/             # per-concern repo mixins (episodes, todos, schedule, household, proposals, …)
│   │   ├── patterns.py        # learn pass: episodes -> derived patterns + time-estimation bias
│   │   └── summarizer.py      # build_profile() + summarize_profile() (Ollama, heuristic fallback)
│   ├── webhooks/              # FastAPI listener for iOS Shortcut / n8n triggers
│   │   ├── app.py             # create_app(): assembles the routers below
│   │   ├── routers/           # one APIRouter per tag: admin, anchor, assistant, coaching, focus,
│   │   │                      #   household, impulsivity, ingestion, memory, schedule, system, todos
│   │   ├── _common.py         # shared deps/schemas · notify.py · oauth.py
│   ├── modules/              # challenge-area modules
│   │   ├── base.py            # Module ABC + Intervention dataclass
│   │   ├── registry.py        # register / available / enabled_modules
│   │   └── *.py               # time_blindness, task_paralysis, hyperfocus, impulsivity, location_anchor, self_care
│   ├── mail/                  # mail ingestion: normalize -> triage -> surface as todos (incl. imap.py)
│   ├── integrations/          # external systems: n8n, ollama, anthropic, nominatim, delivery (ntfy/Pushover/TTS)
│   ├── coaching.py            # the coaching tick engine (fans over modules' evaluate())
│   ├── encouragement.py       # rough-day tone shift + recovery plan
│   ├── panic.py               # overwhelm triage: what's on fire + one first step
│   ├── household.py           # shared co-parent sheet render + star/checkin/digest logic
│   ├── assistant.py           # natural-language edits (validate -> preview -> confirm)
│   ├── sensor.py              # LLM-as-sensor: free text -> pending proposals
│   ├── briefing.py            # morning digest (build + optional Ollama prose)
│   ├── commitments.py         # calendar sync + double-booking detection
│   ├── scheduling.py          # free-window + todo time-fitting (windows, domains, guardrails)
│   ├── todos.py               # open loops, augmentation, tiny-first-step decomposition
│   ├── impact.py              # at-risk-commitment projection from the learned bias
│   ├── departure.py           # travel-aware "when to leave" reminders
│   └── geocode.py             # places -> cache -> opt-in Nominatim resolution
├── docs/                      # schema.md, whitepaper.md, household-sheet.md + design specs; brand/
├── deploy/                    # launchd plist, n8n workflows, iOS Shortcuts, Scriptable widget
├── tests/                     # pytest suite (memory, webhooks, modules, ...)
├── pyproject.toml             # build, dependencies, tooling config
└── .env.example               # all configuration, with safe defaults
```

The **memory layer is the core**. If you're new, read in this order:
`docs/schema.md` → `prefrontal/memory/store.py` → `prefrontal/webhooks/app.py`.

## Local setup

Requires Python 3.10+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"      # installs the package plus pytest and ruff
cp .env.example .env         # then edit as needed
prefrontal init-db           # create and seed the SQLite database
```

## Running the checks

```bash
pytest          # the test suite (uses in-memory SQLite; touches no files)
ruff check .    # lint
ruff format .   # auto-format
```

Please make sure `pytest` and `ruff check .` pass before opening a pull request.

## Conventions

- **Docstrings everywhere.** Every module, public class, and public function has
  a docstring written for `pydoc` (PEP 257 / Google-style sections). This is a
  deliberate transparency choice — `python -m pydoc prefrontal.memory.store` (or
  any module) should explain the layer without reading the source. Keep new code
  to the same standard.
- **Standard library first.** The memory layer uses stdlib `sqlite3` rather than
  an ORM, on purpose — it keeps the dependency surface small and the behavior
  inspectable. Add a dependency only when it clearly earns its place.
- **`schema.sql` is the source of truth** for the database. Update it and
  `docs/schema.md` together; the SQL wins if they disagree.
- **Local first.** Behavioral data must not leave the host unless the user
  explicitly configures it (e.g. an `N8N_WEBHOOK_URL`). Outbound integrations
  default to a no-op.
- **Stubs are labeled.** Incomplete pieces (e.g. the n8n inbound event router)
  carry a `.. todo::` note in their docstring, and not-yet-wired module behaviors
  stay `Intervention(status="planned")`, so the gap between "scaffolded" and
  "finished" is always visible.
- **Tests for behavior you add.** New endpoints or store methods come with a
  test in `tests/`.

## Adding a challenge-area module

ADHD presents differently for everyone, so support behaviors live in independent modules
(`prefrontal/modules/`). To add one:

1. Create `prefrontal/modules/your_module.py` and subclass
   `prefrontal.modules.base.Module`. Set `key`, `title`, `challenge`, and an optional
   `default_state` dict (coaching-state defaults seeded when the module is enabled).
2. Implement `profile_section(store)` — return a Markdown fragment built from the memory
   layer (or `None` when there isn't enough signal yet). Declare your `interventions()`.
3. Call `register(YourModule())` at the bottom of the file.
4. Add it to the side-effect import block in `prefrontal/modules/__init__.py`.
5. Add a test in `tests/test_modules.py`.

Keep real, data-derived behavior in `profile_section` and mark not-yet-wired behaviors as
`Intervention(status="planned")` so the gap between scaffolded and finished stays visible.

## Commit and PR notes

- Keep commits focused and messages descriptive.
- Describe what changed and why in the PR body; link any relevant discussion.
- By contributing you agree your contributions are licensed under the project's
  MIT License.
