# n8n workflow sync

**Update the running n8n directly — no manual editor import.**

Prefrontal's orchestration workflows live in the repo as importable templates
under [`../deploy/n8n/`](../deploy/n8n/). Historically each change meant opening
the n8n editor and re-importing the JSON by hand. The **workflow sync** closes
that loop: it pushes every template into the running n8n over its REST API, so
a repo change becomes live workflows on the next update.

It's wired into the update path, so it happens as part of the **Update** button:

```
Update button  →  POST /admin/update  ─┐
prefrontal update ─────────────────────┼─► deploy/update.sh
                                        │      git pull
                                        │      pip install -e .
                                        │      init-db (schema)
                                        │      prefrontal n8n push  ◄── this
                                        └─► launchd kickstart (restart)
```

`deploy/update.sh` calls `prefrontal n8n push` right after the schema step. You
can also run it by hand any time:

```bash
prefrontal n8n push               # upsert deploy/n8n/*.json into n8n
prefrontal n8n push --no-activate # upsert definitions only; don't touch active state
prefrontal n8n push --dir path/   # a different template directory
```

---

## Setup

Two environment variables turn the sync on (both required — set them in
Prefrontal's `.env`, see [`../.env.example`](../.env.example)):

| Variable | Example | What it is |
|---|---|---|
| `N8N_API_URL` | `http://127.0.0.1:5678/api/v1` | Base URL of n8n's **Public REST API**. Use `127.0.0.1`, **not** `localhost` (see the [networking gotcha](deployment.md#7-n8n-orchestration--delivery)). |
| `N8N_API_KEY` | `n8n_api_…` | An n8n API key. Create it in the n8n editor: **Settings → n8n API → Create an API key**. |

Leave either blank and the sync is a **clean no-op** — nothing is pushed and an
update never fails for lack of n8n. This is the same local-first stance as the
outbound webhook client: absent config means the box stays quiet.

---

## How it behaves

**Idempotent upsert, matched by name.** Each template carries a unique
`"Prefrontal — …"` workflow name. The sync lists the workflows already in n8n,
then for each template `PUT`s the one whose name matches (update in place) or
`POST`s a new one. Re-running an update therefore **updates, never
duplicates** — no template needs a pinned id, and a fresh n8n gets them all
created on the first run.

**Declarative activation.** After the upsert, the sync converges each
workflow's **Active** state to its template's own `active` flag
(`/activate` vs `/deactivate`). Because every shipped template is
`active: false`, a sync **never surprise-enables an opt-in workflow** (the
weekly check-in, encouragement, digest, …). To have a workflow auto-activate on
update, set `"active": true` in its `deploy/n8n/*.json` file and commit it —
the next update lights it up. `--no-activate` skips this entirely and only
upserts definitions.

**Best-effort, never fatal.** In `deploy/update.sh` the push is guarded with
`|| echo …`, so a down or misconfigured n8n logs a line and the update still
finishes and restarts the service. A broken n8n must not bounce a working
Prefrontal — the same principle as "a down n8n never breaks a capture path."
Run by hand, `prefrontal n8n push` exits `0` on success (or a clean skip) and
`1` if a configured n8n rejected or dropped one or more workflows.

---

## What the sync does *not* own

Some things can't be pushed through the API and stay a one-time manual setup —
the sync references them by name but never creates them:

- **Credentials.** n8n never exports credential secrets, so the shared
  `Prefrontal Token` (Header Auth) credential and any ntfy / Twilio credentials
  must already exist in n8n, referenced by name. Set them up once per the
  [deployment runbook](deployment.md#configure-once-env-vars--the-token-credential);
  a pushed workflow links to them on import. If a node ever shows a credential
  unset, pick it from the dropdown once.
- **`$env` variables.** The templates read `PREFRONTAL_BASE_URL`, `NTFY_SERVER`,
  `NTFY_TOPIC`, etc. from **n8n's own** process environment. Those live with n8n
  (its launchd plist / shell), not in Prefrontal's `.env`.
- **Node `typeVersion` drift.** A template authored for one n8n release can use
  a node version a different n8n flags. The API push can't reconcile that; if
  n8n rejects a workflow after an upgrade, re-export it from a working editor.
- **Installing n8n / Node itself.** Out of scope — that's
  [deployment §7](deployment.md#7-n8n-orchestration--delivery).

---

## Under the hood

- Code: [`../prefrontal/integrations/n8n.py`](../prefrontal/integrations/n8n.py)
  — `N8nWorkflowSyncer` (the `N8nClient` beside it is the unrelated outbound
  webhook trigger).
- Only the API-writable keys (`name`, `nodes`, `connections`, `settings`,
  `staticData`) are sent; read-only fields (`id`, `active`, `meta`, `pinData`,
  `tags`, …) are stripped, since the REST API rejects unknown properties and
  manages `active` through its own endpoints.
- The listing follows n8n's `nextCursor` pagination, so it matches names across
  any number of existing workflows.
- Tests: [`../tests/test_n8n.py`](../tests/test_n8n.py) drive create / update /
  activate / failure / pagination through an httpx mock transport (no live n8n).
