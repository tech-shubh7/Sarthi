"""Hand-labelled fictional postings for the profiles in ``world.py``: what a careful recruiter would say.

Labels: ``strong`` (apply now), ``possible`` (worth a look) and ``no`` (not for this person). Each case says
why. Companies, places and numbers are invented. Postings are run through ``complete`` first, as ingestion
does, so places and levels are read from the text exactly as for a real job.

``ai_only`` marks cases only a reader of the whole posting can label (a Singapore "Account Executive" that is
really bookkeeping): the rule layer is not expected to get those right.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sarthi.jobs.models import JobPosting
from sarthi.jobs.normalize import complete

Label = Literal["strong", "possible", "no"]
POSTED = datetime(2026, 11, 19, 9, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Case:
    """One posting, the profile it is judged for, and the label."""

    id: str
    profile: str
    label: Label
    why: str
    title: str
    location: str
    description: str
    company: str = "Fictional Employer Ltd"
    extra: dict[str, Any] = field(default_factory=dict)
    ai_only: bool = False

    def job(self) -> JobPosting:
        """The posting, completed the way ingestion completes it."""
        data: dict[str, Any] = {
            "source": "greenhouse",
            "url": f"https://jobs.example.org/{self.id}",
            "title": self.title,
            "company": self.company,
            "location_text": self.location,
            "employment_type": "full_time",
            "description": self.description,
            "posted_at": POSTED,
        }
        return complete(JobPosting.model_validate(data | self.extra))


CASES: tuple[Case, ...] = (
    # ---- India, AI engineer, 3 years, INR 18 lakh floor ----
    Case("in-ai-strong", "in-ai-engineer", "strong", "same role and stack, right level",
         "AI Engineer", "Bengaluru, Karnataka",
         "Build LLM agents and RAG pipelines in Python and FastAPI on AWS. 2-4 years of experience. "
         "Hybrid, three days a week in our Bengaluru office.", extra={"remote_type": "hybrid"}),
    Case("in-ai-staff", "in-ai-engineer", "no", "staff level, 8+ years asked",
         "Staff AI Engineer", "Bengaluru, Karnataka",
         "Lead the architecture of our LLM platform. 8+ years of experience building ML systems in Python."),
    Case("in-ai-low-pay", "in-ai-engineer", "no", "pays below the floor",
         "Backend Engineer", "Pune, Maharashtra",
         "Python and FastAPI services for our lending app. 2+ years of experience.",
         extra={"salary": {"currency": "INR", "minimum": 600_000, "maximum": 900_000}}),
    Case("in-ai-java", "in-ai-engineer", "no", "a Java/Spring job, not their stack",
         "Java Backend Developer", "Hyderabad, Telangana",
         "Spring Boot microservices, Kafka, Oracle. 3+ years of Java experience."),
    Case("in-ai-remote-india", "in-ai-engineer", "possible", "backend role open to India, partly their stack",
         "Backend Engineer", "Remote - India",
         "Go and Python services, PostgreSQL, AWS. 3+ years of backend experience.",
         extra={"remote_type": "remote"}),
    # ---- USA, data scientist, 6 years, USD 140k floor ----
    Case("us-ds-strong", "us-data-scientist", "strong", "same role, level and skills, pays well",
         "Senior Data Scientist", "New York, NY",
         "Design experiments and causal A/B test analysis; forecasting models in Python and scikit-learn; "
         "SQL. 5+ years of experience.",
         extra={"salary": {"currency": "USD", "minimum": 160_000, "maximum": 190_000}}),
    Case("us-ds-toronto", "us-data-scientist", "no", "on-site in Canada, not one of their countries",
         "Data Scientist", "Toronto, ON",
         "Python, SQL and statistics for our insurance pricing team. On-site.",
         extra={"remote_type": "onsite"}),
    Case("us-ds-low-pay", "us-data-scientist", "no", "pays below the floor",
         "Data Scientist", "Columbus, OH",
         "Python, SQL, statistics. 3+ years of experience.",
         extra={"salary": {"currency": "USD", "minimum": 90_000, "maximum": 110_000}}),
    Case("us-ds-data-entry", "us-data-scientist", "no", "data entry is not data science",
         "Data Entry Clerk", "Dallas, TX",
         "Enter customer records into our CRM. Accuracy and typing speed of 50 wpm."),
    Case("us-ds-mle-remote", "us-data-scientist", "possible", "adjacent role, remote in the US",
         "Machine Learning Engineer", "Remote - US",
         "Train and ship recommendation models in Python; feature engineering, statistics. 4+ years.",
         extra={"remote_type": "remote"}),
    # ---- UK, product designer, 4 years, GBP 55k floor ----
    Case("gb-pd-strong", "gb-product-designer", "strong", "same role, Figma and design systems, pays well",
         "Senior Product Designer", "London, UK",
         "Own end-to-end product design in Figma, user research and our design system. 4+ years.",
         extra={"salary": {"currency": "GBP", "minimum": 65_000, "maximum": 80_000}}),
    Case("gb-pd-head", "gb-product-designer", "no", "head of design, 10+ years of leadership",
         "Head of Design", "London, UK",
         "Lead a team of 20 designers. 10+ years of experience, 5 managing design teams."),
    Case("gb-pd-interior", "gb-product-designer", "no", "interior design is a different profession",
         "Interior Designer", "Manchester, UK",
         "Space planning and furniture specification for hotel refurbishments. AutoCAD and SketchUp."),
    Case("gb-pd-us-remote", "gb-product-designer", "no", "remote, but only for people in the US",
         "Product Designer", "Remote - US only",
         "Figma, prototyping and user research for our fintech app.", extra={"remote_type": "remote"}),
    # ---- Germany, marketing manager, 7 years, German, EUR 65k floor ----
    Case("de-mm-german", "de-marketing-manager", "strong", "their role in Berlin, German ad, their tools",
         "Marketing Manager (m/w/d)", "Berlin",
         "Du verantwortest unsere Kampagnen in Google Ads und Meta, SEO und HubSpot. Mindestens 5 Jahre "
         "Berufserfahrung im Performance Marketing. Fließende Deutschkenntnisse."),
    Case("de-mm-munich", "de-marketing-manager", "strong", "their role, pays above the floor",
         "Performance Marketing Manager", "Munich, Germany",
         "Run paid search and paid social budgets; Google Ads, SEO, HubSpot. 5+ years of experience.",
         extra={"salary": {"currency": "EUR", "minimum": 70_000, "maximum": 85_000}}),
    Case("de-mm-intern", "de-marketing-manager", "no", "an internship",
         "Marketing Intern", "Berlin", "Support our campaigns for six months. Google Ads basics.",
         extra={"employment_type": "internship"}),
    Case("de-mm-sales", "de-marketing-manager", "no", "a sales job",
         "Sales Manager DACH", "Hamburg, Germany", "Close enterprise deals across the DACH region."),
    Case("de-mm-french", "de-marketing-manager", "no", "needs French, which they do not speak",
         "Marketing Manager", "Frankfurt, Germany",
         "Run our campaigns for the French market. Fluent French required. Google Ads and SEO."),
    # ---- Canada, accountant, 3 years, CAD 65k floor ----
    Case("ca-acc-staff", "ca-accountant", "strong", "a staff accountant is their level and work",
         "Staff Accountant", "Toronto, ON",
         "Month-end close, reconciliations and IFRS reporting. CPA designation. 2+ years of experience.",
         extra={"salary": {"currency": "CAD", "minimum": 70_000, "maximum": 80_000}}),
    Case("ca-acc-senior-fa", "ca-accountant", "no", "asks for 7+ years",
         "Senior Financial Analyst", "Vancouver, BC",
         "FP&A, budgeting and board reporting. 7+ years of experience in corporate finance."),
    Case("ca-acc-sydney", "ca-accountant", "no", "in Australia",
         "Accountant", "Sydney NSW", "Month-end close and BAS lodgements. CPA or CA."),
    Case("ca-acc-low-pay", "ca-accountant", "no", "pays below the floor",
         "Accountant", "Mississauga, ON", "Accounts payable and month-end close. Excel.",
         extra={"salary": {"currency": "CAD", "minimum": 45_000, "maximum": 50_000}}),
    # ---- Australia, registered nurse, 6 years, AUD 85k floor ----
    Case("au-rn-ed", "au-registered-nurse", "strong", "their role and ward, in their city",
         "Registered Nurse - Emergency Department", "Brisbane QLD",
         "Current AHPRA registration as a Registered Nurse. Triage, patient care and medication "
         "administration in a busy emergency department.", extra={"employment_type": "part_time"}),
    Case("au-rn-software", "au-registered-nurse", "no", "software engineering",
         "Senior Software Engineer", "Sydney NSW", "Build Go services on AWS. 5+ years of experience."),
    Case("au-rn-remote-dev", "au-registered-nurse", "no", "a remote-tech feed job, not nursing",
         "Frontend Developer", "Anywhere in the World", "React and TypeScript for our SaaS dashboard.",
         extra={"remote_type": "remote", "source": "remoteok"}),
    Case("au-rn-unit-manager", "au-registered-nurse", "possible", "a step up, but within reach",
         "Nurse Unit Manager", "Gold Coast QLD",
         "Lead the nursing team of a medical ward. AHPRA registration; 5+ years of experience as a "
         "Registered Nurse; patient care."),
    Case("au-rn-dubai", "au-registered-nurse", "no", "in the UAE, not one of their countries",
         "Registered Nurse", "Dubai - United Arab Emirates", "DHA licence. ICU experience."),
    # ---- Singapore, account executive, 5 years ----
    Case("sg-ae-saas", "sg-account-executive", "strong", "same role in SaaS sales",
         "Account Executive, Mid-Market", "Singapore",
         "Own a mid-market B2B SaaS book across South-East Asia; Salesforce; 3+ years of experience."),
    Case("sg-ae-bookkeeping", "sg-account-executive", "no", "in Singapore this 'account executive' keeps books",
         "Account Executive", "Singapore",
         "Handle full set of accounts, GST filing, bank reconciliation and payroll for SME clients. "
         "Diploma in Accountancy. Xero.", ai_only=True),
    Case("sg-ae-scam", "sg-account-executive", "no", "a pay-to-start scam",
         "Sales Executive - URGENT HIRING", "Singapore",
         "Urgent hiring! Earn up to SGD 500 per day. Pay a training fee of SGD 200 to confirm your "
         "offer. WhatsApp hr.fastjobs@gmail.com"),
    # ---- UAE, mechanical engineer, 9 years, AE + SA + QA ----
    Case("ae-me-hvac", "ae-mechanical-engineer", "strong", "their role, level and skills in Dubai",
         "Senior Mechanical Engineer - HVAC", "Dubai, UAE",
         "HVAC and MEP design for high-rise towers; AutoCAD and Revit; 8+ years of experience."),
    Case("ae-me-saudi-only", "ae-mechanical-engineer", "no", "for Saudi nationals only",
         "Mechanical Engineer", "Riyadh, Saudi Arabia",
         "Saudi nationals only. HVAC maintenance planning for hospitals; 5+ years of experience."),
    Case("ae-me-mep-doha", "ae-mechanical-engineer", "possible", "MEP role in Qatar with a visa",
         "MEP Engineer", "Doha, Qatar",
         "HVAC, plumbing and fire-fighting design review; AutoCAD, Revit; MEP coordination. "
         "Visa sponsorship provided."),
    Case("ae-me-germany", "ae-mechanical-engineer", "no", "in Germany, not one of their countries",
         "Mechanical Engineer", "Stuttgart, Germany", "Design gearbox housings in CATIA."),
    # ---- Netherlands, teacher, 10 years ----
    Case("nl-t-ib", "nl-teacher", "strong", "IB English teacher at an international school",
         "English Teacher (IB Diploma)", "Amsterdam, Netherlands",
         "Teach IB Diploma English Literature; lesson planning; classroom management; a qualified "
         "teacher with 5+ years of experience."),
    Case("nl-t-us", "nl-teacher", "no", "in the US",
         "High School English Teacher", "Austin, TX", "State teaching certificate required."),
    Case("nl-t-sales-trainer", "nl-teacher", "no", "corporate sales trainer, a sales job",
         "Sales Trainer", "Rotterdam, Netherlands", "Train our sales team on the pitch and quotas."),
    # ---- Brazil, customer support, 2 years, Portuguese ----
    Case("br-cs-remote-br", "br-customer-support", "strong", "their role, remote from Brazil",
         "Customer Support Specialist", "Remote - Brazil",
         "Answer tickets in Portuguese and English in Zendesk; 1+ years of customer service experience.",
         extra={"remote_type": "remote"}),
    Case("br-cs-us-only", "br-customer-support", "no", "remote for the US only",
         "Customer Support Specialist", "Remote (US only)", "Zendesk, phone and chat support.",
         extra={"remote_type": "remote"}),
    Case("br-cs-lead", "br-customer-support", "no", "a team lead, 5+ years asked",
         "Customer Support Team Lead", "São Paulo, Brazil",
         "Lead a team of 12 agents. 5+ years of customer support experience."),
    Case("br-cs-portuguese", "br-customer-support", "strong", "their role, advertised in Portuguese",
         "Analista de Suporte ao Cliente", "São Paulo, SP",
         "Atendimento ao cliente por chat e e-mail no Zendesk. Experiência com CRM. Português e inglês."),
    # ---- remote anywhere, frontend engineer, 5 years, lives in Kenya ----
    Case("rf-worldwide", "remote-frontend-engineer", "strong", "their role and stack, remote worldwide",
         "Senior Frontend Engineer", "Remote - Worldwide",
         "React, TypeScript and Next.js for our design tool. 4+ years of experience.",
         extra={"remote_type": "remote"}),
    Case("rf-berlin-hybrid", "remote-frontend-engineer", "no", "hybrid in Berlin; they want remote only",
         "Frontend Engineer", "Berlin, Germany", "React and TypeScript.", extra={"remote_type": "hybrid"}),
    Case("rf-europe-only", "remote-frontend-engineer", "no", "remote, but Europe only",
         "Frontend Engineer (React)", "Remote - Europe", "React, TypeScript, GraphQL.",
         extra={"remote_type": "remote"}),
    Case("rf-title-only", "remote-frontend-engineer", "possible", "only a title to go on",
         "Frontend Engineer", "Remote", "", extra={"remote_type": "remote", "description_complete": False}),
    Case("rf-angular", "remote-frontend-engineer", "no", "an Angular job, not their stack",
         "Senior Angular Developer", "Remote - Worldwide", "Angular 17, RxJS, NgRx. 5+ years.",
         extra={"remote_type": "remote"}),
    # ---- India, data analyst fresher ----
    Case("in-da-junior", "in-data-analyst-fresher", "strong", "a junior analyst role for freshers",
         "Junior Data Analyst", "Jaipur, Rajasthan",
         "SQL, Excel and Power BI dashboards for our sales team. 0-2 years of experience. Freshers welcome."),
    Case("in-da-3-5", "in-data-analyst-fresher", "no", "asks for 3-5 years",
         "Data Analyst", "Bengaluru, Karnataka", "SQL and Power BI. Experience: 3-5 Years."),
    Case("in-da-senior", "in-data-analyst-fresher", "no", "a senior role",
         "Senior Data Analyst", "Gurugram, Haryana", "Own the analytics roadmap. SQL, Python."),
    Case("in-da-intern", "in-data-analyst-fresher", "possible", "an internship, which they accept",
         "Data Analyst Intern", "Remote - India", "SQL and Excel reporting for six months.",
         extra={"remote_type": "remote", "employment_type": "internship"}),
    Case("in-da-scam", "in-data-analyst-fresher", "no", "a work-from-home fee scam",
         "Data Analyst - Work From Home", "Delhi, India",
         "Work from home and earn Rs 2000 per day. Pay a registration fee of Rs 999 and a security deposit "
         "to confirm your job. Contact jobs.hr.desk@gmail.com"),
    Case("in-da-agency-spam", "in-data-analyst-fresher", "no", "a staffing agency's catch-all post",
         "Multiple Openings - IT / Non-IT - Freshers & Experienced", "Hyderabad, Telangana",
         "Openings in BPO, voice process, telecalling and back office. Send CV on WhatsApp."),
    # ---- Germany, mechanical engineer, 4 years, German ----
    Case("de-me-konstrukteur", "de-mechanical-engineer", "strong", "their role, advertised in German",
         "Konstrukteur Maschinenbau (m/w/d)", "Stuttgart",
         "Konstruktion von Baugruppen in CATIA V5, FEM-Berechnungen. Mindestens 3 Jahre Berufserfahrung "
         "in der Konstruktion. Sehr gute Deutschkenntnisse."),
    Case("de-me-texas", "de-mechanical-engineer", "no", "in the US",
         "Mechanical Engineer", "Houston, TX", "SolidWorks design of pumps. On-site.",
         extra={"remote_type": "onsite"}),
    Case("de-me-10-jahre", "de-mechanical-engineer", "no", "asks for 10 years (written in German)",
         "Maschinenbauingenieur (m/w/d)", "München",
         "Leitung der Konstruktion. Mindestens 10 Jahre Berufserfahrung im Maschinenbau. CATIA, FEM."),
    # ---- UK, staff nurse, 2 years ----
    Case("gb-sn-band5", "gb-staff-nurse", "strong", "a Band 5 staff nurse post is their exact job",
         "Staff Nurse - Surgical Ward", "Leeds, UK",
         "NMC registered nurse for our adult surgical ward; patient care and medication rounds. "
         "Band 5, £29,970 - £36,483 a year.",
         extra={"salary": {"currency": "GBP", "minimum": 29_970, "maximum": 36_483}}),
    Case("gb-sn-charge", "gb-staff-nurse", "no", "a senior charge nurse, 5+ years",
         "Senior Charge Nurse", "Glasgow, UK",
         "Band 7 ward leadership. 5+ years of experience as a registered nurse."),
    Case("gb-sn-sydney", "gb-staff-nurse", "no", "in Australia",
         "Registered Nurse", "Sydney NSW", "AHPRA registration required."),
)  # fmt: skip
