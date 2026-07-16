- **iOS: durable Household entry on the Me tab** ✅ (closes an entry-point gap in
  the iOS Household UI) — the Household screen was only reachable from the **Today**
  glance, which hides itself for a user in no household (its light reads 404). That
  left the **create/join** empty state unreachable in-app for exactly the people
  who need it. The **Me** tab now carries an always-present **Household** row (next
  to Insights) that opens the Household screen regardless of membership — a member
  sees their sheet, a non-member lands on the create-a-household / join-with-a-code
  screen. The Today glance is unchanged (still the richer shortcut for members).
  Client-only (build on a Mac).
