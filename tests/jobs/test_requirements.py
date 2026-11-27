import pytest

from sarthi.jobs.requirements import min_years_required


@pytest.mark.parametrize(
    ("text", "years"),
    [
        ("5+ years of experience in Python", 5),
        # a range from zero welcomes freshers: no minimum (seen live: "0-2" was read as "2+")
        ("With 0-2 years of backend experience and knowledge of Node.js or Python", None),
        ("0-2 years of backend development experience.", None),
        ("Experience: 0 to 3 years", None),
        ("With 2-4 years of backend experience", 2),
        ("3-5 years experience", 3),
        ("3–5 years of experience", 3),
        ("Minimum 2 yrs of backend development experience", 2),
        ("4 plus years of hands-on experience", 4),
        ("10+ years of leadership experience preferred. 3+ years of Go experience", 3),
        ("10 years of company history. 3+ years of Go experience", 3),
        ("a 2 year contract", None),
        ("0 years experience", None),
        ("40 years of experience", None),
        ("fresher", None),
        ("", None),
        # Shapes from 614 real postings (2026-11-22) that were read as "no requirement":
        ("**Experience:** 3–4 Years", 3),
        ("Experience: 8-10 Years", 8),
        ("Experience\\: 6-12 Years", 6),
        ("Experience: 3+ yrs", 3),
        ("Experience Required: 3 - 6 years", 3),
        ("Experience Required: 9+ years", 9),
        ("💼 Experience: 5–9 Years", 5),
        ("Experience: Min 7+ Years", 7),
        ("Role: Product Owner Experience: 8+ years Location: Bangalore", 8),
        ("Overall Testing: 9+ years", 9),
        ("Automation Engineering: 4+ years", 4),
        ("Cucumber BDD - 4+ yrs", 4),
        ("* Back-end development: 3 years (Required)", 3),
        ("Node.js (4+ years): Express, Fastify. React (3+ years): Hooks", 3),
        ("6-9 Years", 6),
        ("Needs 7+ years", 7),
        ("* **5+ years** of experience in cloud operations", 5),
        ("4–6 years in data engineering or ML roles", 4),
        ("an AI Engineer with 3–4 years of relevant experience", 3),
        ("Ideal candidates have 3+ years in full stack development, cloud experience", 3),
        ("* 8+ years of hands-on Python development", 8),
        ("3+ years", 3),
        ("3-6 years hands-on ML engineer", 3),
        ("5+ years in AI/ML and Python", 5),
        ("4+ years for Mid-level, 7+ years for Senior", 4),
        ("PayPal has been revolutionizing commerce for more than 25 years", None),
        ("manufacturer of scooters for the last **25 years**, Hero is", None),
        # The overall requirement is the floor: one skill's years cannot lower it.
        ("Overall Testing: 9+ years. Automation Engineering: 4+ years. Playwright: 2+ years", 9),
        ("7 years of overall experience in IT. 3 years of hands-on experience with Azure", 7),
        ("* 4 to 6 years of total experience in ML engineering. 1 year of LangChain experience", 4),
        ("2-5 years overall experience. 2+ years full stack web development experience", 2),
        # A feed dropped the dash: "3-7" arrived as "37".
        ("Experience: 37 Years (preferred)", 3),
        ("Experience: 24 years", 2),
        ("Experience: 58 Years", 5),
        ("Experience: 12 Years (or a strong portfolio of personal projects)", 1),
        ("Experience: 20 years", 20),  # not two rising digits: really twenty years
        ("Experience: 8 years", 8),
        # Lengths of time that are not experience asked for.
        ("Contract: 2 years", None),
        ("Contract duration - 3 years", None),
        ("Notice period: 3 years", None),
        ("We have 25 years of operation and an average employee tenure of 15 years", None),
        ("Experience: 0-2 years", None),  # freshers welcome: nothing to hold against the user
        ("Experience: 1-3 years", 1),
        # Postings in other languages.
        ("Mindestens 10 Jahre Berufserfahrung im Maschinenbau.", 10),
        ("Sie bringen 3-5 Jahre Erfahrung in der Konstruktion mit.", 3),
        ("Al menos 4 años de experiencia en ventas B2B.", 4),
        ("Mínimo de 2 anos de experiência com atendimento.", 2),
        ("Vous avez 5 ans d'expérience en comptabilité.", 5),
        ("Je hebt minimaal 3 jaar werkervaring in het onderwijs.", 3),
        ("Unser Unternehmen besteht seit 25 Jahren.", None),  # company age, not experience
        # Hostile input never breaks it.
        ("Experience: " + "9" * 5000 + " years", None),
        ("years " * 2000, None),
        ("\x00Experience:\x00 3 years", None),
    ],
)
def test_min_years_required(text: str, years: int | None) -> None:
    assert min_years_required(text) == years
