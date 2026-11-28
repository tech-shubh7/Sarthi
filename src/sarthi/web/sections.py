"""Stable route names for single-purpose dashboard pages."""

SETTINGS_SECTIONS = {
    "profile": "Profile",
    "automation": "Automation",
    "logins": "Job-site logins",
    "email": "Email",
    "telegram": "Telegram",
    "extension": "Browser extension",
}
ACTIVITY_SECTIONS = {
    "timeline": "Timeline",
    "questions": "Employer questions",
    "auto-apply": "Scheduled auto-apply",
}
SOURCE_SECTIONS = {
    "readers": "Source readers",
    "sites": "Sites with jobs",
    "companies": "Company career pages",
}

# What each kind of notice on the Timeline is called.
NOTICE_KINDS = {
    "documents": "Documents", "questions": "Questions", "preview": "Ready to submit", "result": "Application",
    "reply": "Reply", "followup": "Follow-up", "source": "Source", "auto_apply": "Auto-apply",
    "catchup": "Catch-up", "browser": "Browser",
}  # fmt: skip
TIMELINE_SORTS = {"newest": "Newest first", "oldest": "Oldest first"}


def notice_kind(kind: str) -> str:
    """A notice kind's name ('auto_apply' -> 'Auto-apply'; an unknown one in plain words)."""
    return NOTICE_KINDS.get(kind, kind.replace("_", " ").capitalize())


MATCH_SORTS = {"score": "Best match", "newest": "Newest first", "company": "Company A-Z"}
