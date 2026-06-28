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
│   ├── cli.py                 # `prefrontal` console script: init-db | serve | profile
│   ├── memory/                # the SQLite behavioral memory layer (the core)
│   │   ├── schema.sql         # canonical schema: episodes, patterns, coaching_state
│   │   ├── db.py              # connection management + init_db()
│   │   ├── store.py           # MemoryStore: the read/write API over the tables
│   │   └── summarizer.py      # build_profile(): tables -> profile.md (heuristic stub)
│   ├── webhooks/              # FastAPI listener for iOS Shortcut / n8n triggers
│   │   └── app.py             # routes: /health, /webhooks/shortcut, /webhooks/n8n
│   ├── modules/              # challenge-area modules (one per EF difficulty)
│   │   ├── base.py            # Module ABC + Intervention dataclass
│   │   ├── registry.py        # register / available / enabled_modules
│   │   └── *.py               # time_blindness, task_paralysis, hyperfocus, impulsivity
│   └── integrations/          # external systems
│       └── n8n.py             # bidirectional n8n stub (outbound client + inbound parser)
├── docs/schema.md             # human-readable companion to schema.sql
├── tests/                     # pytest suite (memory + webhooks)
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
- **Stubs are labeled.** Incomplete pieces (the n8n inbound handlers, the
  LLM-backed summarizer) carry a `.. todo::` note in their docstring so the gap
  between "scaffolded" and "finished" is always visible.
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
