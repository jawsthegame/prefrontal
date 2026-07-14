- **Per-user Context pack on/off — surfaces (web + iOS)** ✅ — the follow-up to
  per-user modules: a person can now turn an enabled Context pack off for
  themselves. This is the **surfaces** overlay (P1) — a `pack_enabled:<key>`
  coaching-state `"off"` hides that pack's **situation tools**
  (`/packs/situations` list + run) and its **`/care`** lens for that user, while
  its vocabulary and domain classification stay deployment-wide (so "off" is
  cosmetic, not structural). Mirrors the module overlay: new registry resolvers
  (`user_disabled_pack_keys` / `user_enabled_packs` / `user_enabled_situations` /
  `user_get_situation` / `user_pack_enabled`), and `GET`/`POST /settings/features`
  now lists and toggles **packs** alongside modules. The **Features** section on
  the web Settings page and the iOS Settings screen groups packs and modules.
  Covered by `tests/test_user_pack_overlay.py`.
