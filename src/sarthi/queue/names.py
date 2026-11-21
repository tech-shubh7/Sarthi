"""Task type names, shared by every pipeline step (steps never import each other, so names live here)."""

from __future__ import annotations

SOURCE_CHECK = "source_check"  # read one job source
MATCH = "match"  # run the match funnel for one job
FILL_TEXT = "fill_text"  # read the full description from a job's own page, then match it
ALERT = "alert"  # send one strong match to the user now
DIGEST = "digest"  # send the day's best unseen matches
APPLY = "apply"  # the user tapped Apply
CATCHUP = "catchup"  # search every source over a past window (catch-up mode)
CATCHUP_REPORT = "catchup_report"  # send the catch-up results as one batch
DOCUMENTS = "documents"  # make the tailored resume and cover letter for one job
DOCUMENTS_READY = "documents_ready"  # send the documents (or why they could not be made) to the user
FORM_CHECK = "form_check"  # read a form's questions before applying; ask the user what only they can answer
ASK_QUESTIONS = "ask_questions"  # ask the user the next question a form needs
APPLY_PREVIEW = "apply_preview"  # show the filled form and ask for Submit / Cancel
SUBMIT = "submit"  # the user approved one filled form: submit it
APPLY_RESULT = "apply_result"  # tell the user how an application ended
MAIL_CHECK = "mail_check"  # read new emails and find replies about applications
REPLY_NOTICE = "reply_notice"  # tell the user an employer replied
QUIET_CHECK = "quiet_check"  # find applications with no reply for a while
FOLLOWUP_NOTICE = "followup_notice"  # send the user a follow-up draft
