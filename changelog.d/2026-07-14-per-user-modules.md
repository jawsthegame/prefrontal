- **Per-user module on/off (web + iOS)** ✅ — modules were all-or-nothing per
  *deployment* (`PREFRONTAL_MODULES`); now each person can turn individual support
  behaviors off for themselves, since not everyone has the same symptoms and too
  many nudges overwhelm. A per-user overlay (`module_enabled:<key>` coaching state,
  the enable twin of the usage-loop mute) is applied in the coaching tick right
  beside the mute filter — a module a user turns off offers no cues and no
  protection for them, without touching deployment config or anyone else. New
  read/write endpoints `GET`/`POST /settings/features`; a **Features** section on
  the web **Settings** page and in the iOS **Settings** screen lists each
  deployment-enabled module (title + what it addresses) with a toggle. Unset =
  the deployment default, so nothing changes until a user opts out. Covered by
  `tests/test_user_module_overlay.py`. (Per-user *packs* — a bigger change since a
  pack also seeds vocabulary/classification — are a planned follow-up.)
