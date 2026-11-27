"""Fictional job seekers across countries and industries, shared by the matching and source tests.

Every person here is invented: names, employers and numbers are made up. The profiles carry what matching
reads (roles, skills, places, languages, years, pay floor) and a short resume, like a real saved profile.
"""

from __future__ import annotations

from typing import Any

from sarthi.profile.models import Preferences, Profile


def _profile(skills: tuple[str, ...], resume: str, **preferences: Any) -> Profile:
    return Profile(
        preferences=Preferences.model_validate(preferences), skills=skills, resume_text=resume.strip()
    )


PROFILES: dict[str, Profile] = {
    "in-ai-engineer": _profile(
        ("Python", "FastAPI", "PostgreSQL", "LLM", "RAG", "AWS"),
        """
        AI Engineer, 3 years. Built retrieval-augmented chat assistants with Python, FastAPI and pgvector at
        a fictional fintech in Pune. Deployed LLM agents on AWS Lambda; wrote evaluation harnesses.
        """,
        roles=["AI Engineer", "Backend Engineer"],
        keywords=["python", "llm", "rag", "fastapi"],
        home_country="IN",
        years_experience=3,
        languages=["Hindi", "English"],
        min_salary={"currency": "INR", "minimum": 1_800_000},
    ),
    "us-data-scientist": _profile(
        ("Python", "SQL", "pandas", "scikit-learn", "A/B testing", "Statistics"),
        """
        Data Scientist, 6 years. Forecasting and experimentation for a fictional retailer in Ohio: demand
        models in scikit-learn, causal A/B test analysis, SQL pipelines in Snowflake, dashboards in Looker.
        """,
        roles=["Data Scientist", "Machine Learning Engineer"],
        keywords=["python", "sql", "statistics", "machine learning"],
        home_country="US",
        years_experience=6,
        min_salary={"currency": "USD", "minimum": 140_000},
    ),
    "gb-product-designer": _profile(
        ("Figma", "User research", "Prototyping", "Design systems", "Accessibility"),
        """
        Product Designer, 4 years. Designed the checkout and onboarding of a fictional London travel app in
        Figma; ran usability tests and owned the design system and its WCAG accessibility audits.
        """,
        roles=["Product Designer", "UX Designer"],
        keywords=["figma", "user research", "prototyping"],
        home_country="GB",
        years_experience=4,
        min_salary={"currency": "GBP", "minimum": 55_000},
    ),
    "de-marketing-manager": _profile(
        ("Google Ads", "SEO", "HubSpot", "Campaign management", "Content strategy"),
        """
        Marketing Manager, 7 years. Led performance marketing for a fictional Berlin e-commerce brand: Google
        Ads and Meta budgets of EUR 2 M a year, SEO, HubSpot automation, a team of four.
        """,
        roles=["Marketing Manager", "Performance Marketing Manager"],
        keywords=["google ads", "seo", "hubspot", "campaign"],
        home_country="DE",
        languages=["German", "English"],
        years_experience=7,
        min_salary={"currency": "EUR", "minimum": 65_000},
    ),
    "ca-accountant": _profile(
        ("CPA", "Excel", "IFRS", "QuickBooks", "Month-end close", "Financial reporting"),
        """
        Accountant (CPA), 3 years. Month-end close, reconciliations and IFRS reporting for a fictional
        Toronto logistics firm; QuickBooks and Excel models; supported the annual audit.
        """,
        roles=["Accountant", "Financial Analyst"],
        keywords=["cpa", "ifrs", "month-end close", "excel"],
        home_country="CA",
        years_experience=3,
        languages=["English", "French B1"],
        min_salary={"currency": "CAD", "minimum": 65_000},
    ),
    "au-registered-nurse": _profile(
        ("AHPRA registration", "Patient care", "Medication administration", "Emergency nursing", "ICU"),
        """
        Registered Nurse (AHPRA), 6 years. Emergency department and ICU nursing at a fictional Brisbane
        hospital: triage, medication administration, patient assessment, mentoring graduate nurses.
        """,
        roles=["Registered Nurse", "Emergency Nurse"],
        keywords=["registered nurse", "ahpra", "patient care", "emergency"],
        home_country="AU",
        years_experience=6,
        employment_types=["full_time", "part_time"],
        min_salary={"currency": "AUD", "minimum": 85_000},
    ),
    "sg-account-executive": _profile(
        ("B2B sales", "Salesforce", "SaaS", "Negotiation", "Pipeline management"),
        """
        Account Executive, 5 years. Closed mid-market SaaS deals across South-East Asia for a fictional
        Singapore software vendor; 120% of quota two years running; Salesforce pipeline hygiene.
        """,
        roles=["Account Executive", "Sales Manager"],
        keywords=["b2b sales", "saas", "salesforce"],
        home_country="SG",
        years_experience=5,
    ),
    "ae-mechanical-engineer": _profile(
        ("AutoCAD", "HVAC", "SolidWorks", "MEP", "Revit"),
        """
        Mechanical Engineer, 9 years. HVAC and MEP design for towers and malls in Dubai for a fictional
        consultancy: load calculations, AutoCAD and Revit drawings, site supervision.
        """,
        roles=["Mechanical Engineer", "HVAC Engineer"],
        keywords=["hvac", "mep", "autocad"],
        home_country="AE",
        countries=["AE", "SA", "QA"],
        years_experience=9,
        languages=["English", "Arabic"],
    ),
    "nl-teacher": _profile(
        ("Lesson planning", "Classroom management", "IB curriculum", "English literature"),
        """
        Secondary school English teacher, 10 years, at a fictional international school in Utrecht. IB
        Diploma English Literature, lesson planning, classroom management, head of year.
        """,
        roles=["Teacher", "English Teacher"],
        keywords=["teacher", "ib", "lesson planning"],
        home_country="NL",
        languages=["English", "Dutch B2"],
        years_experience=10,
    ),
    "br-customer-support": _profile(
        ("Zendesk", "Customer service", "CRM", "Ticketing", "Spanish"),
        """
        Customer Support Specialist, 2 years. Answered email and chat tickets in Portuguese, English and
        Spanish for a fictional São Paulo fintech in Zendesk; wrote help-centre articles.
        """,
        roles=["Customer Support Specialist", "Customer Success Specialist"],
        keywords=["zendesk", "customer support", "customer service"],
        home_country="BR",
        languages=["Portuguese", "English", "Spanish"],
        years_experience=2,
    ),
    "remote-frontend-engineer": _profile(
        ("React", "TypeScript", "Next.js", "CSS", "GraphQL"),
        """
        Frontend Engineer, 5 years, fully remote for fictional US and EU start-ups: React and Next.js apps in
        TypeScript, a component library, GraphQL clients, web performance work.
        """,
        roles=["Frontend Engineer", "React Developer"],
        keywords=["react", "typescript", "next.js"],
        home_country="KE",
        remote="only",
        years_experience=5,
    ),
    "in-data-analyst-fresher": _profile(
        ("SQL", "Excel", "Power BI", "Python"),
        """
        B.Com graduate, 2026. Internship: Power BI sales dashboards and SQL reports for a fictional Jaipur
        distributor. Coursework in statistics and Python.
        """,
        roles=["Data Analyst", "Business Analyst"],
        keywords=["sql", "excel", "power bi"],
        home_country="IN",
        years_experience=0,
        employment_types=["full_time", "internship"],
    ),
    "de-mechanical-engineer": _profile(
        ("SolidWorks", "CATIA", "FEM", "Konstruktion"),
        """
        Maschinenbauingenieur, 4 Jahre. Konstruktion von Getriebegehäusen in CATIA und SolidWorks bei einem
        fiktiven Zulieferer in Stuttgart, FEM-Berechnungen, Zeichnungserstellung.
        """,
        roles=["Mechanical Engineer", "Konstrukteur"],
        keywords=["catia", "solidworks", "konstruktion"],
        home_country="DE",
        languages=["German", "English"],
        years_experience=4,
    ),
    "gb-staff-nurse": _profile(
        ("NMC registration", "Patient care", "Ward nursing", "Medication administration"),
        """
        Staff Nurse (NMC), 2 years, adult surgical ward at a fictional NHS trust in Leeds: post-operative
        care, medication rounds, discharge planning.
        """,
        roles=["Staff Nurse", "Registered Nurse"],
        keywords=["nurse", "nmc", "patient care"],
        home_country="GB",
        years_experience=2,
        employment_types=["full_time", "part_time"],
    ),
}
