- **iOS: operator admin actions + connect QR** ✅ — the web `/admin` page's
  operator surface, native. An **Admin** entry appears on the Me tab only when
  `GET /admin/whoami` reports the signed-in user is an operator, opening a screen
  that mirrors `prefrontal/webhooks/admin.html`: **add a user** (handle, display
  name, optional Google sign-in email, operator toggle) which provisions them via
  `POST /admin/users` and reveals their minted **token once**, alongside a
  **scannable connect QR** — the same `prefrontal://connect?…` link the CLI's
  `prefrontal user connect-link --qr` prints on the paper setup sheet, so a new
  phone's camera opens straight into the app fully connected (base URL + token +
  handle/name). The user list supports **rotate token** (with a confirm; the
  rotated code is shown once too), **disable / re-enable**, **set/clear the Google
  email**, and **add to a household**; a **Households** card creates households
  (`POST /admin/households`) and shows their members. New `Models/Admin.swift`,
  `Onboarding/QRCode.swift`, `Views/AdminView.swift`, the `admin*` calls in
  `Networking/Endpoints.swift`, and a memberwise `ConnectPayload` init; covered by
  `PrefrontalTests/AdminTests.swift`. Client-only (build on a Mac); the endpoints
  are the existing operator-only admin router.
