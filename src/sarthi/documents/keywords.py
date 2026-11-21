"""The ATS keyword report for one job: which skills the posting asks for, and where the resume stands.

Applicant tracking systems and recruiters scan for the job's own words. For each skill the posting names:
- **on the resume**: the tailored resume says it;
- **you have, not on the resume**: the person's profile or uploaded resume has it, but the tailored resume
  does not say it (worth adding to the resume by hand if true);
- **missing**: nothing the person gave mentions it. Never added automatically: only the person can say
  whether it is true.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sarthi.jobs.skills import same_skill, skills_in

MAX_SKILLS = 25


@dataclass(frozen=True, slots=True)
class KeywordReport:
    """Where the resume stands against the job's skills."""

    on_resume: list[str] = field(default_factory=list)
    have_not_shown: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def score(self) -> int | None:
        """Share of the job's skills the resume shows, 0-100; None when the posting names no skills."""
        total = len(self.on_resume) + len(self.have_not_shown) + len(self.missing)
        return None if total == 0 else round(100 * len(self.on_resume) / total)


def keyword_report(
    job_text: str, resume_text: str, profile_text: str, own_skills: tuple[str, ...]
) -> KeywordReport:
    """Compare the job's skills with the tailored ``resume_text`` and everything the person gave."""
    wanted = skills_in(job_text, own_skills)[:MAX_SKILLS]
    report = KeywordReport()
    for skill in wanted:
        if same_skill(skill, resume_text):
            report.on_resume.append(skill)
        elif same_skill(skill, profile_text) or any(skill.lower() == s.lower() for s in own_skills):
            report.have_not_shown.append(skill)
        else:
            report.missing.append(skill)
    return report
