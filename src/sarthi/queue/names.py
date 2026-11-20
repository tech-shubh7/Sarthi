"""Task type names, shared by every pipeline step (steps never import each other, so names live here)."""

from __future__ import annotations

SOURCE_CHECK = "source_check"  # read one job source
MATCH = "match"  # run the match funnel for one job
ALERT = "alert"  # send one strong match to the user now
DIGEST = "digest"  # send the day's best unseen matches
APPLY = "apply"  # the user tapped Apply
CATCHUP = "catchup"  # search every source over a past window (catch-up mode)
CATCHUP_REPORT = "catchup_report"  # send the catch-up results as one batch
