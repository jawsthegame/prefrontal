- **iOS: Mail triage inbox** ✅ — the native app now surfaces the mail-monitoring
  pillar it was missing. A new **Mail** tab reads the side-effect-free `GET /mail`
  snapshot and shows the triaged messages still **awaiting action** (their linked
  todo is open) above a **recent** feed, each row carrying sender, subject, the
  triage gist, and chips for urgency (when high/urgent), category, and who's
  waiting on you. Read-only with pull-to-refresh — resolving a message's todo in
  the Todos tab clears it from "Needs action" on the next load. New
  `MailView.swift`, `MailMessage`/`MailInbox` models, and an `APIClient.mail()`
  endpoint.
