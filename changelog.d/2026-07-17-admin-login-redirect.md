- **Fix: the admin view was unreachable via Google sign-in** — signing in from
  the `/admin` gate bounced you to `/dashboard` and never back to Admin, so the
  page just kept showing the sign-in prompt. Two causes: the OAuth callback
  *always* redirected to `/dashboard`, and the admin page only tried to load when
  a saved `X-Prefrontal-Token` was in `localStorage` — it never attempted the
  Google session cookie (which is `httponly`, so JS can't see it). Now
  `/auth/google/login` accepts a `?next=` page and carries it through the signed
  CSRF state, and the callback returns you there (`/dashboard` by default); the
  admin gate links to `?next=/admin`. `next` is clamped to a same-origin absolute
  path at both sign and redirect time (new `oauth.safe_next`), so a crafted
  `?next=//evil` / absolute URL / header-injecting value can't turn sign-in into
  an open redirect. The admin page now always attempts a load — using the session
  cookie when there's no token — and shows the gate only on a real 401/403, with
  a neutral message when no code was ever entered. Covered by new tests in
  `tests/test_oauth.py` (next round-trip, offsite-next clamp, `safe_next`).
