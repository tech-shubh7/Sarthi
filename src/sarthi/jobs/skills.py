"""Skill names in job text, for the ATS keyword report ("the job asks for Docker; the resume omits it").

A fixed vocabulary of common skills plus the person's own skills, each matched as a whole term. Anything not
in the vocabulary is left alone rather than guessed, so the report never lists random words as "keywords".
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from sarthi.jobs.stack import TECHS

# canonical name -> extra spellings (the name itself always matches)
VOCABULARY: dict[str, tuple[str, ...]] = {
    "AWS": ("amazon web services",),
    "Azure": (),
    "GCP": ("google cloud",),
    "Docker": (),
    "Kubernetes": ("k8s",),
    "Terraform": (),
    "CI/CD": ("ci cd", "continuous integration"),
    "Git": (),
    "GitHub Actions": (),
    "Linux": (),
    "SQL": (),
    "PostgreSQL": ("postgres",),
    "MySQL": (),
    "MongoDB": ("mongo",),
    "Redis": (),
    "Elasticsearch": (),
    "Kafka": (),
    "RabbitMQ": (),
    "GraphQL": (),
    "REST": ("rest api", "restful"),
    "gRPC": (),
    "Microservices": ("micro-services",),
    "WebSockets": ("websocket",),
    "Serverless": (),
    "Nginx": (),
    "OAuth": (),
    "JWT": (),
    "HTML": (),
    "CSS": (),
    "Tailwind": ("tailwindcss",),
    "Redux": (),
    "Express.js": ("express",),
    "FastAPI": (),
    "Django": (),
    "Flask": (),
    "Spring": ("spring boot",),
    "Next.js": ("nextjs",),
    "LLM": ("llms", "large language model", "large language models"),
    "RAG": ("retrieval augmented generation", "retrieval-augmented generation"),
    "LangChain": (),
    "LangGraph": (),
    "LlamaIndex": (),
    "Vector databases": (
        "vector database",
        "vector db",
        "pinecone",
        "weaviate",
        "qdrant",
        "chroma",
        "pgvector",
    ),
    "Prompt engineering": (),
    "Fine-tuning": ("fine tuning", "finetuning"),
    "OpenAI API": ("openai",),
    "Hugging Face": ("huggingface", "transformers"),
    "PyTorch": (),
    "TensorFlow": (),
    "scikit-learn": ("sklearn",),
    "Pandas": (),
    "NumPy": (),
    "NLP": ("natural language processing",),
    "Computer vision": ("opencv",),
    "MLOps": (),
    "Airflow": (),
    "Spark": ("pyspark",),
    "Snowflake": (),
    "dbt": (),
    "Excel": (),
    "Jira": (),
    "Agile": ("scrum",),
    "TDD": ("test driven development", "test-driven development"),
    "Unit testing": ("unit tests",),
    "pytest": (),
    "Jest": (),
    "Cypress": (),
    "Playwright": (),
    "Selenium": (),
    "System design": (),
    "Data structures": ("data structures and algorithms", "dsa"),
}


def _pattern(spellings: Iterable[str]) -> re.Pattern[str]:
    alternatives = "|".join(sorted((re.escape(s) for s in spellings if s), key=len, reverse=True))
    return re.compile(rf"(?<![\w.#+])(?:{alternatives})(?![\w#+])", re.I)


# Skills whose names are everyday English words ("the rest of their time", "a swift reply", "excel at"),
# or whose stack spelling is broader than the skill ("iOS" is not Swift): matched only as written.
_AS_WRITTEN: dict[str, re.Pattern[str]] = {
    "REST": re.compile(r"(?<![\w.#+])(?:REST(?:ful)?|(?i:rest(?:ful)?\s+apis?|restful))(?![\w#+])"),
    "Swift": re.compile(r"(?<![\w.#+])Swift(?![\w#+])"),
    "Rust": re.compile(r"(?<![\w.#+])Rust(?![\w#+])"),
    "Spark": re.compile(r"(?<![\w.#+])(?:Spark|(?i:pyspark|apache spark))(?![\w#+])"),
    "Excel": re.compile(r"(?<![\w.#+])(?:Excel|(?i:ms excel|microsoft excel))(?![\w#+])"),
    "Express.js": re.compile(r"(?<![\w.#+])(?:Express(?:\.js)?|(?i:express\.js|expressjs))(?![\w#+])"),
}
_KNOWN: dict[str, re.Pattern[str]] = {
    **{name: _pattern((name, *extra)) for name, extra in VOCABULARY.items()},
    **{
        name: re.compile(rf"(?<![\w.#+])(?:{spellings})(?![\w#+])", re.I) for name, spellings in TECHS.items()
    },
    **_AS_WRITTEN,
}


def skills_in(text: str, own_skills: Iterable[str] = ()) -> list[str]:
    """Skills the text names, in the order they first appear (vocabulary plus the person's own skills)."""
    patterns = dict(_KNOWN)
    for skill in own_skills:
        name = skill.strip()
        if name and not any(p.fullmatch(name) for p in patterns.values()):
            patterns[name] = _pattern((name,))
    found: dict[str, int] = {}
    for name, pattern in patterns.items():
        if match := pattern.search(text or ""):
            found[name] = match.start()
    return sorted(found, key=found.__getitem__)


def same_skill(name: str, text: str) -> bool:
    """True when ``text`` names the skill ``name`` (any known spelling)."""
    pattern = _KNOWN.get(name) or _pattern((name,))
    return pattern.search(text or "") is not None
