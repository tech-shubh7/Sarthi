"""Follow-up drafts for applications that went quiet (idea from JobRadar's follow-ups, MIT).

Written from the user's own data only (the role, the company, the day they applied, their name), never by
an AI, so the draft cannot claim anything the user has not said. Sarthi never sends it: the user does.
"""

from __future__ import annotations

from datetime import date


def followup_draft(name: str, role: str, company: str, applied_on: date) -> str:
    """A short, polite follow-up the user can send to the recruiter."""
    when = applied_on.strftime("%d %B %Y").lstrip("0")
    sign = f"\n\nBest regards,\n{name}" if name else "\n\nBest regards"
    return (
        f"Subject: Following up on my application for {role}\n\n"
        f"Hello {company} team,\n\n"
        f"I applied for the {role} role on {when} and wanted to check whether there is any update on my "
        "application. I'm still very interested in the role and happy to share anything else you need."
        f"{sign}"
    )
