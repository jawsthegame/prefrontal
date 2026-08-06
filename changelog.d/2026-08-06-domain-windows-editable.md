- **Configurable per-domain hours** ✅ — the work/life guardrail (a todo tagged to
  a life domain is only *suggested* inside that domain's time-of-day window, via
  `resolve_window`) is now editable, not just an operator env/coaching-state knob.
  New `GET/POST /schedule/domain-windows` reads and writes a window per life domain
  (`shop`/`work`/`home`/`kids`/`personal`): `configured=true` stores a `start`–`end`
  band (as the existing `todo_window:<domain>` override), `configured=false` clears
  it back to the inherited default. Editors added to the **web** Settings page
  ("Domain hours") and the **iOS** SettingsView (`DomainWindowsSection`). The 3-way
  hand-mirrored contract (Pydantic ↔ web ↔ iOS) is pinned by a new drift guard,
  `tests/test_contract_domain_windows.py`.
