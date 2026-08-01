- **Settings: configure mail accounts & iCal feeds in the web UI** ✅ — the two
  inbound sources you used to add only from the CLI (`prefrontal mail add-source` /
  `prefrontal calendar add-source`) now have Settings cards, so a household member
  can wire up their own inboxes and calendars without a terminal. A new **Mail
  accounts (IMAP)** card manages each mailbox (host, username, app password,
  folder, Important-only, and full/signals retention); a new **Calendar feeds
  (iCal)** card manages each private `.ics` feed (URL, label, and your own
  addresses for declined-event filtering). Both are the same one-card-per-source
  pattern as the SMTP card: the secret (IMAP password, feed URL) is sealed at rest
  and **never** returned to the browser — an edit that leaves the field blank keeps
  the stored secret — and the form explains itself (and disables saving) when no
  encryption key is configured. Backed by `GET`/`POST`/`DELETE /mail-sources` and
  `/calendar-sources` on the same per-user source registry the CLI writes, so the
  two stay in sync. Covered by `tests/test_source_endpoints.py`.
