- **Fix: deleting a star chart with awarded stars 500'd** ✅ — removing a
  household behaviour plan (`POST /household/agreements/{id}/remove`, the dashboard
  delete button) raised `FOREIGN KEY constraint failed` whenever the plan was a
  star chart with any grants: `household_stars.agreement_id` FK-references the
  agreement with no `ON DELETE CASCADE`, so the ledger rows blocked the delete
  (surfacing as a 500). `remove_agreement` now clears the chart's star ledger in
  the same transaction before deleting the agreement, so a chart and its grants
  drop together atomically. A plain plan (no stars) was unaffected. Regression test
  in `tests/test_household.py`.
