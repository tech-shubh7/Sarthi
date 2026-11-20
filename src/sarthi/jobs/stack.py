"""The technology a job title is built around ("Java Developer", ".NET Full Stack", "SAP ABAP Consultant").

A title that names a main technology the person does not have is a different job, however many of their
other skills the description mentions: a Python and Node.js developer is not a Java developer. Only well-known
names are recognised, each with its common spellings; anything else is left to the skills score and the AI.
"""

from __future__ import annotations

import re

# canonical name -> regex of its spellings in titles (case-insensitive, word-bounded below)
TECHS: dict[str, str] = {
    "Java": r"java(?!\s*script)|j2ee|spring\s*boot",
    ".NET": r"\.net|dot\s*net|dotnet|asp\.net|c#|vb\.net",
    "PHP": r"php|laravel|codeigniter|symfony",
    "Ruby": r"ruby|rails|ror",
    "Go": r"golang",
    "Scala": r"scala",
    "Kotlin": r"kotlin",
    "Swift": r"swift|ios",
    "Android": r"android",
    "Flutter": r"flutter|dart",
    "Angular": r"angular(?:js)?",
    "Vue": r"vue(?:\.?js)?|nuxt",
    "React": r"react(?:\.?js)?|next\.?js",
    "React Native": r"react\s*native",
    "Node.js": r"node(?:\.?js)?|mern|mean\s+stack",
    "Python": r"python|django|flask|fastapi|pyspark",
    "JavaScript": r"javascript|js",
    "TypeScript": r"typescript",
    "C++": r"c\+\+|cpp",
    "C": r"embedded\s+c",
    "Rust": r"rust",
    "Elixir": r"elixir",
    "Perl": r"perl",
    "COBOL": r"cobol|mainframe",
    "SAP": r"sap|abap|hana|s/4",
    "Salesforce": r"salesforce|sfdc|apex",
    "ServiceNow": r"servicenow",
    "Oracle": r"oracle|pl/sql|plsql|oic",
    "Siebel": r"siebel",
    "Magento": r"magento",
    "WordPress": r"wordpress",
    "Drupal": r"drupal",
    "SharePoint": r"sharepoint",
    "Power BI": r"power\s*bi",
    "Tableau": r"tableau",
    "Informatica": r"informatica",
    "Ab Initio": r"ab\s*initio",
    "Unity": r"unity",
    "Unreal": r"unreal",
    "Verilog": r"verilog|vhdl|fpga|rtl",
    "Mulesoft": r"mule\s*soft|mulesoft",
    "Pega": r"pega",
    "Appian": r"appian",
    "Workday": r"workday",
    "Guidewire": r"guidewire",
    "Murex": r"murex",
    "T24": r"t24|temenos",
}
# Skills that cover another technology's title ("Next.js" in the title, "React" on the resume).
_ALSO_COVERS: dict[str, frozenset[str]] = {
    "TypeScript": frozenset({"JavaScript"}),
    "JavaScript": frozenset({"TypeScript"}),
    "Node.js": frozenset({"JavaScript"}),
    "React": frozenset({"JavaScript"}),
}

_PATTERNS = {
    name: re.compile(rf"(?<![\w.#+])(?:{spellings})(?![\w#+])", re.I) for name, spellings in TECHS.items()
}


def techs_in(text: str) -> set[str]:
    """Canonical names of the known technologies ``text`` mentions."""
    found = {name for name, pattern in _PATTERNS.items() if pattern.search(text or "")}
    if "React Native" in found:
        found.discard("React")
    return found


def known_techs(skills: tuple[str, ...] | list[str]) -> set[str]:
    """The technologies a person's skills cover (their own and the ones those imply)."""
    known = techs_in(" , ".join(skills))
    for tech in list(known):
        known |= _ALSO_COVERS.get(tech, frozenset())
    return known


def missing_stack(title: str, skills: tuple[str, ...] | list[str]) -> set[str]:
    """The title's main technologies when the person has none of them; empty if they fit or none are named."""
    named = techs_in(title)
    if not named or named & known_techs(skills):
        return set()
    return named
