import io
import zipfile

import pytest

from sarthi.jobs.geo import first_country
from sarthi.jobs.models import SalaryPeriod
from sarthi.profile.answers import (
    edit_list,
    find_salary,
    is_simple_answer,
    parse_countries,
    parse_remote,
    parse_remote_text,
    parse_salary,
    parse_years,
    reconcile,
    split_list,
)
from sarthi.profile.models import RemotePreference
from sarthi.profile.resume import MAX_UPLOAD_BYTES, ResumeError, ResumeKind, detect, extract_text

RESUME = "Asha Rao\nBackend engineer with 4 years of Python, Go and PostgreSQL. Built payment APIs on AWS."


def test_split_list() -> None:
    assert split_list("Backend Engineer, SRE / Platform Engineer and backend engineer") == (
        "Backend Engineer",
        "SRE",
        "Platform Engineer",
    )
    assert split_list("none") == ()
    assert split_list(" ") == ()


def test_parse_countries() -> None:
    parsed = parse_countries("UK, UAE and Atlantis")
    assert parsed.codes == {"GB", "AE"}
    assert parsed.unknown == ("Atlantis",)
    assert "DE" in parse_countries("Europe").codes
    assert parse_countries("none").codes == frozenset()
    assert parse_countries("anywhere").unknown == ()


@pytest.mark.parametrize(
    ("text", "value"), [("ok", RemotePreference.OK), (" ONLY ", RemotePreference.ONLY), ("maybe", None)]
)
def test_parse_remote(text: str, value: RemotePreference | None) -> None:
    assert parse_remote(text) == value


@pytest.mark.parametrize(
    ("text", "years"), [("4", 4.0), ("3.5 years", 3.5), ("fresher", 0.0), ("about ten", None), ("99", None)]
)
def test_parse_years(text: str, years: float | None) -> None:
    assert parse_years(text) == years


@pytest.mark.parametrize(
    ("text", "home", "currency", "amount", "period"),
    [
        ("12 LPA", "IN", "INR", 1_200_000, SalaryPeriod.YEAR),
        ("15 lakh", "IN", "INR", 1_500_000, SalaryPeriod.YEAR),
        ("₹18,00,000", "IN", "INR", 1_800_000, SalaryPeriod.YEAR),
        ("80k GBP", "IN", "GBP", 80_000, SalaryPeriod.YEAR),
        ("USD 120000", "IN", "USD", 120_000, SalaryPeriod.YEAR),
        ("$150k", "GB", "USD", 150_000, SalaryPeriod.YEAR),
        ("60000", "GB", "GBP", 60_000, SalaryPeriod.YEAR),
        ("50000 / month", "IN", "INR", 50_000, SalaryPeriod.MONTH),
        ("12 LPA", "IN", "INR", 1_200_000, SalaryPeriod.YEAR),
    ],
)
def test_parse_salary(text: str, home: str, currency: str, amount: float, period: SalaryPeriod) -> None:
    salary = parse_salary(text, home)
    assert salary is not None
    assert (salary.currency, salary.minimum, salary.period) == (currency, amount, period)


@pytest.mark.parametrize("text", ["lots", "0", "12 apples per day", "5000"])
def test_unclear_salaries_are_rejected(text: str) -> None:
    assert parse_salary(text, "ZZ") is None


def _pdf(text: str) -> bytes:
    """A minimal one-page PDF with ``text`` in Helvetica (hand-built: no PDF-writing dependency)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref))
    return out.getvalue()


def _docx(paragraphs: list[str], extra_body_bytes: int = 0) -> bytes:
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>' for p in paragraphs)
    xml = f'<?xml version="1.0"?><w:document><w:body>{body}{" " * extra_body_bytes}</w:body></w:document>'
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return out.getvalue()


def test_reads_pdf_docx_and_text() -> None:
    assert "4 years of Python" in extract_text(_pdf(RESUME.replace("\n", " ")))
    docx = extract_text(
        _docx(["Asha Rao", "Backend engineer: Python &amp; Go, 4 years.", "Built payment APIs on AWS."])
    )
    assert docx.splitlines()[:2] == ["Asha Rao", "Backend engineer: Python & Go, 4 years."]
    assert extract_text(RESUME.encode()) == RESUME


def test_type_comes_from_the_bytes_not_the_name() -> None:
    assert detect(b"%PDF-1.7 ...") is ResumeKind.PDF
    assert detect(b"PK\x03\x04...") is ResumeKind.DOCX
    with pytest.raises(ResumeError, match="PDF, a Word"):
        extract_text(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"x" * (MAX_UPLOAD_BYTES + 1), "over 5 MB"),
        (b"%PDF-1.4 broken", "couldn't read this PDF"),
        (b"PK\x03\x04 not really a zip", "couldn't read this Word"),
        (b"Asha Rao", "almost no text"),
    ],
)
def test_unreadable_uploads_explain_themselves(data: bytes, message: str) -> None:
    with pytest.raises(ResumeError, match=message):
        extract_text(data)


def test_oversized_docx_body_is_refused_before_reading() -> None:
    with pytest.raises(ResumeError, match="too large"):
        extract_text(_docx(["Asha"], extra_body_bytes=6 * 1024 * 1024))


def test_docx_entities_are_not_expanded() -> None:
    bomb = _docx(["&lol9;" * 10 + " Backend engineer with Python, Go and AWS experience for four years."])
    text = extract_text(bomb)
    assert "&lol9;" in text  # unknown entities stay as text: nothing expands
    assert len(text) < 200


SUGGESTED = [
    "Backend Engineer",
    "Software Developer",
    "Web Developer Trainee",
    "Frontend Developer Intern",
    "AI Engineer",
]


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        # the real answer that was once saved as two job titles (2026-11-20)
        (
            "remove frontend developer intern, other okay",
            ["Backend Engineer", "Software Developer", "Web Developer Trainee", "AI Engineer"],
        ),
        ("ok", SUGGESTED),
        ("remove trainee and intern, rest ok", ["Backend Engineer", "Software Developer", "AI Engineer"]),
        ("ok, add Data Engineer, without AI Engineer", [*SUGGESTED[:4], "Data Engineer"]),
        ("keep the rest, remove Frontend Developer Intern", [*SUGGESTED[:3], "AI Engineer"]),
        ("remove all, add Data Engineer", ["Data Engineer"]),
        ("remove everything", []),
        ("Python, Go", ["Python", "Go"]),
    ],
)
def test_edit_list(answer: str, expected: list[str]) -> None:
    result = edit_list(answer, SUGGESTED)
    assert result.error is None
    assert list(result.items) == expected


def test_instructions_are_never_saved_as_items() -> None:
    assert edit_list("okay okay", SUGGESTED).items == tuple(SUGGESTED)  # read as "ok"
    assert edit_list("please keep backend", SUGGESTED).error is not None
    assert edit_list("other okay", []).items == ()


ROLES_2 = [
    "Backend Engineer",
    "AI Engineer",
    "Node.js Developer",
    "Fintech Application Developer",
    "Microservices Developer",
]


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        # real answers from the second setup session (2026-11-20)
        (
            "ok, backend AI engineer, software developer, associate software developer also, "
            "Expect Fintech, microservices one",
            [
                "Backend Engineer",
                "AI Engineer",
                "Node.js Developer",
                "backend AI engineer",
                "software developer",
                "associate software developer",
            ],
        ),
        ("Use skills", ROLES_2),
        ("use these", ROLES_2),
        ("ok add Data Engineer too", [*ROLES_2, "Data Engineer"]),
        (
            "okay remove fintech",
            ["Backend Engineer", "AI Engineer", "Node.js Developer", "Microservices Developer"],
        ),
        (
            "except the fintech one",
            ["Backend Engineer", "AI Engineer", "Node.js Developer", "Microservices Developer"],
        ),
        ("yes please", ROLES_2),
        ("Okta Engineer", ["Okta Engineer"]),
    ],
)
def test_edit_list_real_phrasings(answer: str, expected: list[str]) -> None:
    result = edit_list(answer, ROLES_2)
    assert result.error is None
    assert list(result.items) == expected


@pytest.mark.parametrize(
    ("answer", "simple"),
    [
        ("ok", True),
        ("Backend Engineer, Data Engineer", True),
        ("use these", True),
        ("remove fintech", False),
        ("keep only first two", False),
        ("same but replace AI Engineer with GenAI", False),
    ],
)
def test_simple_answers_skip_the_ai(answer: str, simple: bool) -> None:
    assert is_simple_answer(answer) is simple


@pytest.mark.parametrize(
    ("answer", "qwen_said", "expected"),
    [
        # outputs from a local qwen2.5:3b run (2026-11-20), checked by rules
        (
            "remove fintech and node one, add ML engineer and python developer instead",
            ["Backend Engineer", "AI Engineer", "ML Engineer", "Python Developer"],
            ["Backend Engineer", "AI Engineer", "ML Engineer", "Python Developer", "Microservices Developer"],
        ),
        (
            "same but replace AI Engineer with GenAI Engineer",
            ["Backend Engineer", "GenAI Engineer", "Node.js Developer", "Microservices Developer"],
            [
                "Backend Engineer",
                "GenAI Engineer",
                "Node.js Developer",
                "Fintech Application Developer",
                "Microservices Developer",
            ],
        ),
        (
            "ok, backend AI engineer, software developer associate software developer also, expect fintech, "
            "microservices one",
            ["Backend Engineer", "AI Engineer", "Software Developer Associate", "Microservices Developer"],
            ["Backend Engineer", "AI Engineer", "Node.js Developer", "Software Developer Associate"],
        ),
        ("keep only first two", ["Backend Engineer", "AI Engineer"], ["Backend Engineer", "AI Engineer"]),
        (
            "i dont want microservices and fintech, rest is fine and also add full stack developer",
            ["Backend Engineer", "AI Engineer", "Node.js Developer", "Full Stack Developer"],
            ["Backend Engineer", "AI Engineer", "Node.js Developer", "Full Stack Developer"],
        ),
    ],
)
def test_rules_correct_what_a_small_model_gets_wrong(
    answer: str, qwen_said: list[str], expected: list[str]
) -> None:
    assert reconcile(ROLES_2, qwen_said, answer) == expected


def test_values_found_inside_sentences() -> None:
    salary = find_salary("make my minimum 15 LPA please", "IN")
    assert salary is not None
    assert (salary.currency, salary.minimum) == ("INR", 1_500_000)
    pay = find_salary("change pay to USD 120000", "IN")
    assert pay is not None
    assert pay.currency == "USD"  # "pay" is not read as a currency
    assert find_salary("no idea yet", "IN") is None
    assert parse_remote_text("actually remote only") is RemotePreference.ONLY
    assert parse_remote_text("office or hybrid please") is RemotePreference.NO
    assert parse_remote_text("remote or office, both fine") is RemotePreference.OK
    assert parse_remote_text("hmm") is None
    assert first_country("I moved to Dubai") == "AE"
    assert first_country("nowhere") is None
