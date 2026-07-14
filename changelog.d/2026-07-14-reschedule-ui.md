- **Double-booking reschedule UI (web + iOS)** ✅ — the `POST
  /commitments/conflicts/reschedule` endpoint (draft/send a polite "please move
  one" note to the other party) had no front-end; now both surfaces expose it.
  On the **web dashboard**, a firm conflict row gains a **Reschedule…** button
  that opens a popover to preview the drafted note (recipient, optional name +
  cover note) and then **Send** it (confirm-first — nothing leaves the box until
  you send; a successful send dismisses the conflict). The **iOS Calendar** tab
  gains a **Schedule conflicts** card (double-bookings + soft possibles, each
  dismissable) with the same preview-then-send **Reschedule** sheet. No server
  changes — both drive the existing endpoints (`/commitments/conflicts`,
  `.../reschedule`, `.../dismiss`).
