# The change gates are adapted from Resume-Matcher (Apache-2.0), apps/backend/app/services/improver.py
# (apply_diffs, _verify_original_matches, _bullet_order, the reorder salvage) @ 959e458. Copyright (c)
# Resume-Matcher contributors. Changed by Sarthi contributors: Sarthi's resume paths, no appends or added
# skills, and every re-worded line checked against its OWN original line (``line_problems``), not the resume.
"""The AI's resume changes, applied only when code can verify each one.

A change names its line (``experience[0].bullets[2]``) and quotes what it says now; the quote must match,
or nothing happens. A re-worded line may not add a tool, technology, number, company, team or result that
its own original line does not state: "Python" on the skills line does not let a Node.js bullet say
"Python and Node.js". The job's word for what the line already says is fine ("LLM workflows with
retrieval" -> "RAG pipeline"). Bullets and skills may be re-ordered, never dropped or added.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from sarthi.ai.documents import ResumeChange
from sarthi.documents.truth import (
    echo_problems,
    new_numbers,
    opening_problems,
    style_problems,
    title_problems,
    verb_upgrade,
)
from sarthi.jobs.skills import same_skill, skills_in
from sarthi.profile.master import MasterResume, link_key

_LINE = re.compile(r"^(experience|projects)\[(\d+)\]\.bullets\[(\d+)\]$")
_BULLETS = re.compile(r"^(experience|projects)\[(\d+)\]\.bullets$")
_SKILLS = re.compile(r"^skills\[(\d+)\]\.items$")

LONGER, LONGER_CHARS, SHORTER = 1.25, 20, 0.7  # a re-worded line keeps about its length

# Job words that name a kind of work, and what a line must already say for the word to describe it: "LLM
# workflows with retrieval" is a RAG pipeline; a LangGraph chatbot is not, by that alone, "agentic". The
# third value: whether to point the AI at lines that could use the word ("AI" -> "GenAI" adds nothing).
_TERMS = (
    (r"\brag\b|retrieval[- ]augmented", r"retriev|vector|embedding|re-?rank|semantic search", True),
    (
        r"\bllms?\b|\bgen ?ai\b|generative ai|large language models?",
        r"\bllms?\b|language model|\bgpt|langchain|langgraph|chatbot|\bai\b|openai|gemini|claude",
        False,
    ),
    (
        r"\bagentic\b|\bai agents?\b|\bllm agents?\b|\bagents?\b|\bmulti-agent\b",
        r"agent|tool[- ]calling",
        True,
    ),
    (r"\brest(?:ful)?\b|\bapis?\b", r"\bapis?\b|endpoint|\brest\b", True),
    (r"\bmicro-?services?\b", r"micro-?services?|\bservices\b", True),
    (r"\betl\b", r"pipeline|ingest|\bload", True),
    (r"vector (?:databases?|search|stores?)|\bembeddings?\b", r"pgvector|vector|embedding", True),
    (r"\bprompt", r"prompt", False),
    (r"\basync\w*|asynchronous", r"async|queue|background", True),
    (r"\bnlp\b|natural language", r"language model|\bllm|chatbot", False),
    (r"\bci/cd\b", r"ci/cd|pipeline|deploy", True),
    (r"\bmcp\b|model context protocol", r"\bmcp\b|model context protocol", False),
    (r"\bfine-?tun", r"fine-?tun", False),
)
_SAME_THING: tuple[tuple[re.Pattern[str], re.Pattern[str]], ...] = tuple(
    (re.compile(term, re.I), re.compile(evidence, re.I)) for term, evidence, _ in _TERMS
)
_HINTED = tuple(pair for pair, (*_, hinted) in zip(_SAME_THING, _TERMS, strict=True) if hinted)
MAX_LINES_PER_TERM = 2  # a job word added to more lines than this reads as keyword stuffing
# Saying "worked on" or "helped" claims a share of the work; "built" claims all of it.
_SHARE = re.compile(
    r"\b(?:worked on|worked with|helped|assisted|contributed|supported|participated|involved in"
    r"|collaborated)\b",
    re.I,
)
# Outcome words: a line with no result may not gain one.
_RESULT = re.compile(
    r"\b(?:cut|cutting|reduc\w*|improv\w*|increas\w*|boost\w*|sav(?:e|ed|es|ing)|faster|speed\w*|lower\w*|"
    r"grew|grow\w*|doubl\w*|tripl\w*|halv\w*|accelerat\w*|eliminat\w*|optimi[sz]\w*|uptime|outperform\w*)\b",
    re.I,
)
# Scope and team claims: kept only when the line already makes them.
_SCOPE = re.compile(
    r"\b(?:team|teams|cross-functional|stakeholders?|clients?|customers?|scalable|at scale|large-scale|"
    r"production(?:-grade|-ready)?|enterprise|high-performance|high-availability|real-time|mission-critical|"
    r"end-to-end|million|millions|thousands)\b",
    re.I,
)
# Words a re-worded line may bring without saying anything new; any other new word is a new detail, and a
# line may bring at most MAX_NEW_WORDS of those (moving the admin module into the fintech sentence is not).
_GLUE = frozenset(
    [
        "using",
        "used",
        "use",
        "with",
        "across",
        "through",
        "into",
        "from",
        "for",
        "based",
        "approach",
        "pipeline",
        "pipelines",
        "workflow",
        "workflows",
        "system",
        "systems",
        "service",
        "services",
        "application",
        "applications",
        "feature",
        "features",
        "solution",
        "solutions",
        "platform",
        "built",
        "build",
        "building",
        "designed",
        "design",
        "developed",
        "develop",
        "implemented",
        "implement",
        "created",
        "create",
        "delivered",
        "deliver",
        "wrote",
        "write",
        "that",
        "which",
        "while",
        "where",
        "when",
        "also",
        "both",
        "each",
        "every",
        "their",
        "them",
        "they",
        "this",
        "these",
        "those",
        "over",
        "under",
        "within",
        "without",
        "including",
        "such",
        "other",
        "more",
        "most",
    ]
)
MAX_NEW_WORDS = 2
MAX_JOB_WORDS = 1  # of those, taken from the job posting: "multidisciplinary" yes, "tracheostomy weaning" no
_WORD = re.compile(r"[a-z]{4,}")
_TOKEN = re.compile(r"[A-Za-z][\w.+#/-]*[\w+#]|[A-Za-z]")
_SENTENCE_GAP = re.compile(r"[.!?;:]\s*$")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _tidy(text: str) -> str:
    """AI text with em dashes made commas, odd hyphens plain and whitespace normalised."""
    text = re.sub(r"\s*" + chr(0x2014) + r"\s*", ", ", text)  # em dash
    text = re.sub(f"[{chr(0x2010)}-{chr(0x2012)}]", "-", text)  # hyphen, non-breaking hyphen, figure dash
    return re.sub(r"\s+", " ", text).strip()


def _evidenced(term: str, old: str) -> bool:
    """True when ``term`` is the job's word for something ``old`` already says."""
    return any(
        re.fullmatch(pattern.pattern, term.strip(), re.I) and evidence.search(old)
        for pattern, evidence in _SAME_THING
    )


def _in_line(name: str, old: str) -> bool:
    """``name`` is written in ``old`` as a whole word, in any case, singular or plural ("APIs", "API")."""
    word = name.casefold()
    stem = word[:-1] if len(word) > 3 and word.endswith("s") else word  # noqa: PLR2004
    return re.search(rf"(?<![\w.+#]){re.escape(stem)}(?:s|es)?(?![\w+#])", old.casefold()) is not None


def _names(text: str) -> list[str]:
    """Proper names and technical terms in ``text``.

    Capitalised words not starting a sentence, and any word with a capital inside, a digit or a dot ("Kafka",
    "AWS", "Node.js", "GPT-4").
    """
    found: list[str] = []
    for match in _TOKEN.finditer(text):
        word = match.group(0).rstrip(".")
        before = text[: match.start()]
        starts_sentence = not before.strip() or _SENTENCE_GAP.search(before) is not None
        technical = any(c.isdigit() for c in word) or "." in word or any(c.isupper() for c in word[1:])
        if technical or (word[:1].isupper() and not starts_sentence and word != "I"):
            found.append(word)
    return list(dict.fromkeys(found))


def _acronym_of(name: str, old: str) -> bool:
    """``name`` is the initials of words ``old`` writes out: "NIV" for "non-invasive ventilation"."""
    letters = name.casefold()
    if not (2 <= len(letters) <= 6 and name.isupper() and letters.isalpha()):  # noqa: PLR2004
        return False
    words = [w for w in re.split(r"[\s/-]+", old.casefold()) if w and w[0].isalpha()]
    initials = "".join(w[0] for w in words)
    return letters in initials and any(
        "".join(w[0] for w in words[i : i + len(letters)]) == letters
        and all(len(w) > 1 for w in words[i : i + len(letters)])
        for i in range(len(words) - len(letters) + 1)
    )


def line_problems(  # noqa: PLR0912  (one check per kind of claim)
    new: str, old: str, vocabulary: Iterable[str] = (), *, known: str = "", job_text: str = ""
) -> list[str]:
    """What ``new`` claims that its own original line ``old`` does not (empty when it adds nothing).

    Numbers, skills and tools, names (companies, products, technologies), results, team and scope must all
    come from ``old``; the job's term for something ``old`` already says is allowed, and so is an acronym
    of words ``old`` writes out. ``known`` is more text whose facts the line may use (the whole resume, for
    the summary); what ``old`` itself says (its numbers, a share of the work) must still be kept. With
    ``job_text``, at most ``MAX_JOB_WORDS`` new words may come from the posting.
    """
    facts = f"{old}\n{known}" if known else old
    problems = [f'adds the number "{n}"' for n in new_numbers(new, facts)]
    problems += [f'drops the number "{n}"' for n in new_numbers(old, new)]
    vocabulary = list(vocabulary)
    for skill in skills_in(new, vocabulary):
        if not same_skill(skill, facts) and not _in_line(skill, facts) and not _evidenced(skill, facts):
            problems.append(f'adds "{skill}", which its original line does not name')
    named = {p.split('"')[1].casefold() for p in problems if '"' in p}
    for name in _names(new):
        if name.casefold() in named or _in_line(name, facts) or _evidenced(name, facts):
            continue
        if _acronym_of(name, facts) or any(same_skill(skill, name) for skill in skills_in(facts, vocabulary)):
            continue
        problems.append(f'adds "{name}", which its original line does not name')
        named.add(name.casefold())
    for term, evidence in _SAME_THING:
        found = term.search(new)
        if (
            found
            and not term.search(facts)
            and not evidence.search(facts)
            and found.group(0).casefold() not in named
        ):
            problems.append(f'calls it "{found.group(0)}", which its original line does not describe')
            named.add(found.group(0).casefold())
    problems += [  # "Ran A/B tests" -> "Ran landing page tests" loses the keyword a recruiter searches for
        f'drops "{name}", which the original line names'
        for name in _names(old)
        if name.casefold() not in new.casefold() and not same_skill(name, new)  # "AI" lives on in "GenAI"
    ]
    share = _SHARE.search(old)
    if share and not _SHARE.search(new):
        problems.append(
            f'drops "{share.group(0).lower()}": the original line claims a share of the work, not all of it'
        )
    result = _RESULT.search(new)
    if result and not _RESULT.search(facts):
        problems.append(f'adds a result ("{result.group(0)}") the original line does not state')
    for claim in dict.fromkeys(m.group(0).casefold() for m in _SCOPE.finditer(new)):
        if not re.search(rf"\b{re.escape(claim)}\b", facts, re.I):
            problems.append(f'adds "{claim}", a claim the original line does not make')
    problems += verb_upgrade(new, facts)
    problems += tense_problems(new, old)
    problems += count_problems(new, old)
    fresh = _new_words(new, facts, named)
    if len(fresh) > MAX_NEW_WORDS:
        problems.append(f"brings details its original line does not state ({', '.join(fresh[:5])})")
    elif job_text:  # the posting's own nouns are where untrue details come from: "tracheostomy weaning"
        asked = {_stem(w) for w in _WORD.findall(job_text.casefold())}
        borrowed = [w for w in fresh if _stem(w) in asked]
        if len(borrowed) > MAX_JOB_WORDS:
            problems.append(
                f"takes details from the job posting its original line does not state ({', '.join(borrowed)})"
            )
    return problems


_COUNTED = re.compile(r"[\w%$+/'.-]+")
_COUNT_WINDOW = 12
_YEAR = re.compile(r"(?:19|20)\d\d")


def _pairs(text: str) -> list[tuple[str, str]]:
    """Each word as written (with its punctuation) and bare."""
    return [(raw, raw.strip(".,;:'")) for raw in _COUNTED.findall(text.casefold()) if raw.strip(".,;:'")]


def _tokens(text: str) -> list[str]:
    return [bare for _, bare in _pairs(text)]


def count_problems(new: str, old: str) -> list[str]:
    """A number now counting something else (empty when every number counts what it did).

    "Won 23 new accounts, including two hotel groups" must not become "Won 23 new hotel accounts" (not all
    23 were hotels). The two words after each number in ``new`` must be words ``old`` uses after that
    number, up to its next number or the end of its sentence.
    """
    old_raw = [raw for raw, _ in _pairs(old)]
    old_words, new_words = _tokens(old), _tokens(new)
    problems: list[str] = []
    for i, word in enumerate(new_words):
        if not any(c.isdigit() for c in word) or word not in old_words or _YEAR.fullmatch(word):
            continue  # a year dates the work; it counts nothing
        start = old_words.index(word) + 1
        window: set[str] = set()
        for raw, token in zip(old_raw[start:], old_words[start:], strict=False):
            if any(c.isdigit() for c in token) or len(window) >= _COUNT_WINDOW:
                break
            window.add(_stem(token))
            if raw.endswith((".", ";")):
                break
        after = [w for w in new_words[i + 1 : i + 3] if w.isalpha() and len(w) > 2 and w not in _GLUE]  # noqa: PLR2004
        added = [w for w in after if _stem(w) not in window and w not in {"and", "the", "per", "of"}]
        if added and window:
            problems.append(f'changes what "{word}" counts ("{word} {" ".join(new_words[i + 1 : i + 3])}")')
    return problems


# A line about current duties is written in the present ("Run discharge planning"); a tailored copy that
# turns it into the past ("Ran discharge planning") mixes tenses within one role.
_PRESENT_VERB = re.compile(
    r"^(?:act|manage|lead|run|own|mentor|coach|teach|coordinate|supervise|review|administer|handle|"
    r"negotiate|track|design|build|maintain|support|work|plan|write|deliver|oversee|train|sell|prepare|"
    r"develop|create|analy[sz]e|differentiate|assess|monitor|report|organi[sz]e|direct|advise|serve|care)s?$",
    re.I,
)


def tense_problems(new: str, old: str) -> list[str]:
    """A present-tense opening verb turned into the past (or the other way round)."""
    old_first = (re.findall(r"[A-Za-z]+", old) or [""])[0]
    new_first = (re.findall(r"[A-Za-z]+", new) or [""])[0]
    if (
        _PRESENT_VERB.match(old_first)
        and not _PRESENT_VERB.match(new_first)
        and new_first.lower().endswith(("ed", "an", "ote", "ught", "ilt", "ld"))
    ):
        return [f'turns "{old_first}" (present tense, a current duty) into "{new_first}": keep the tense']
    return []


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:  # noqa: PLR2004
            word = word[: -len(suffix)]
            break
    return word[:6]


def _new_words(new: str, old: str, reported: set[str]) -> list[str]:
    """Content words of ``new`` that ``old`` never uses (beyond connective words and named problems)."""
    had = {_stem(w) for w in _WORD.findall(old.casefold())}
    found: dict[str, None] = {}
    for word in _WORD.findall(new.casefold()):
        if word in _GLUE or word in reported or _stem(word) in had:
            continue
        if any(term.fullmatch(word) or term.search(word) for term, _ in _SAME_THING):
            continue  # a job word for the line's own work, checked above
        found[word] = None
    return list(found)


def _added_terms(new: str, old: str, vocabulary: list[str]) -> set[str]:
    """Job words and skills ``new`` says that ``old`` did not (lower case)."""
    added = {
        found.group(0).casefold()
        for term, _ in _SAME_THING
        if (found := term.search(new)) and not term.search(old)
    }
    return added | {s.casefold() for s in skills_in(new, vocabulary) if not same_skill(s, old)}


def term_hints(master: MasterResume, job_text: str) -> str:
    """Lines that already describe work the job names with its own word, as "path: word" lines.

    The same table the checks use, so a hint the AI follows passes them: a line about "retrieval and
    re-ranking" may say "RAG" for a job that asks for RAG.
    """
    lines = [("summary", master.summary)] if master.summary else []
    for kind, entries in (
        ("experience", [job.bullets for job in master.experience]),
        ("projects", [project.bullets for project in master.projects]),
    ):
        for i, bullets in enumerate(entries):
            lines += [(f"{kind}[{i}].bullets[{k}]", b) for k, b in enumerate(bullets)]
    hints = []
    for path, text in lines:
        words = []
        for term, evidence in _HINTED:
            asked = term.search(job_text)
            if asked and not term.search(text) and evidence.search(text):
                words.append(asked.group(0))
        if words:
            hints.append(f"{path}: {', '.join(dict.fromkeys(words))}")
    return "\n".join(hints)


def length_problem(new: str, old: str) -> str:
    """Why ``new`` is too long or too short to replace ``old`` ("" when it keeps about its length)."""
    if len(new) > max(len(old) * LONGER, len(old) + LONGER_CHARS):
        return f"is {len(new)} characters for an original of {len(old)}: keep its length"
    if len(new) < len(old) * SHORTER:
        return f"is {len(new)} characters for an original of {len(old)}: say all it said"
    return ""


@dataclass(frozen=True, slots=True)
class Changed:
    """One line as it was and as it is now."""

    where: str
    before: str
    after: str


@dataclass
class Applied:
    """The tailored resume, what changed and which changes were thrown away (with why)."""

    resume: MasterResume
    changed: list[Changed] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)  # "path: why", for the log and a second try
    retry: list[str] = field(default_factory=list)  # rejected re-wordings, to ask the AI to fix


@dataclass
class _Context:
    vocabulary: list[str]
    job_text: str
    resume_text: str
    facts: str = ""  # everything the resume states (no contact details), for the summary


def resume_facts(master: MasterResume) -> str:
    """The person's qualifications the summary may name: titles held, degrees, registrations, licences.

    A recruiter looks for "NMC registered" or "CA" in the first lines. Bullet details and skills are not
    included: "Python" on the skills list does not make the summary's Node.js services Python ones.
    """
    parts = [master.headline]
    parts += [f"{job.title}, {job.company}" for job in master.experience]
    parts += [f"{s.degree}, {s.school}, {', '.join(s.details)}" for s in master.education]
    parts += [item for extra in master.extras for item in (extra.heading, *extra.items)]
    return "\n".join(p for p in parts if p)


def _reword(new: str, old: str, where: str, ctx: _Context, *, summary: bool = False) -> list[str]:
    """Why ``new`` may not replace ``old`` (empty when it may).

    The summary speaks for the whole resume, so it may name what other lines state (a registration, a
    certification, a tool or result): every such fact must be written on the resume. A bullet may use only
    its own facts.
    """
    problems = line_problems(
        new, old, ctx.vocabulary, known=ctx.facts if summary else "", job_text=ctx.job_text
    )
    if summary:
        problems += title_problems(new, ctx.resume_text)  # never the job's title
    problems += [p for p in style_problems(new) if p not in style_problems(old)]
    if not echo_problems(old, ctx.job_text):
        problems += echo_problems(new, ctx.job_text, own=old)
    problems += [p for p in [length_problem(new, old)] if p]
    if not summary:
        problems += opening_problems(new, old)
    return [f"{where} {p}" for p in problems]


def _order(source: Sequence[str], current: Sequence[str], proposed: Sequence[str]) -> list[int] | None:
    """Positions of the bullets in the order ``proposed`` lists them, when it lists each exactly once.

    A bullet may be quoted as it was (``source``) or as re-worded in the same answer (``current``).
    """
    if len(proposed) != len(source):
        return None
    order: list[int] = []
    for item in proposed:
        key = _norm(item)
        found = [
            k for k in range(len(source)) if k not in order and key in (_norm(source[k]), _norm(current[k]))
        ]
        if not found:
            return None
        order.append(found[0])
    return order


def _short(text: str, words: int = 7) -> str:
    cut = text.split()
    return " ".join(cut[:words]) + (" ..." if len(cut) > words else "")


def _skill_order(items: Sequence[str], proposed: Sequence[str]) -> tuple[str, ...]:
    """``items`` in the order ``proposed`` names them; ones it leaves out follow, ones it adds are dropped."""
    remaining = {_norm(i): i for i in items}
    ordered = [remaining.pop(_norm(p)) for p in proposed if _norm(p) in remaining]
    return (*ordered, *(i for i in items if _norm(i) in remaining))


def apply_changes(  # noqa: PLR0912, PLR0915  (one gate per kind of change, as in Resume-Matcher)
    master: MasterResume,
    changes: Iterable[ResumeChange],
    *,
    job_text: str = "",
    resume_text: str = "",
    own_skills: Iterable[str] = (),
) -> Applied:
    """``master`` with every verifiable change made; re-orders are applied after re-wordings, by quote."""
    ctx = _Context(
        [*master.all_skills(), *own_skills],
        job_text,
        resume_text or _known_text(master),
        resume_facts(master),
    )
    sections: dict[str, list[list[str]]] = {
        "experience": [list(job.bullets) for job in master.experience],
        "projects": [list(p.bullets) for p in master.projects],
    }
    summary = master.summary
    skills = [tuple(g.items) for g in master.skills]
    result = Applied(master)
    done: set[str] = set()
    orders: list[tuple[str, str, int, list[str]]] = []
    terms_used: Counter[str] = Counter()

    def reject(path: str, why: str, *, retry: bool = False) -> None:
        result.rejected.append(f"{path}: {why}")
        if retry:
            result.retry.append(f"{path}: {why}")

    for change in changes:
        path, action, value = change.path.replace(" ", ""), change.action, change.value
        if path in done:
            reject(path, "changed twice; the first change is kept")
            continue
        line, bullets, skill_line = _LINE.match(path), _BULLETS.match(path), _SKILLS.match(path)
        if action == "replace" and (path == "summary" or line):
            if path == "summary":
                old = master.summary
            else:
                assert line is not None  # noqa: S101  (matched above)
                rows = sections[line.group(1)]
                i, k = int(line.group(2)), int(line.group(3))
                if i >= len(rows) or k >= len(rows[i]):
                    reject(path, "no such line")
                    continue
                old = rows[i][k]
            if not old or not isinstance(change.original, str) or _norm(change.original) != _norm(old):
                reject(path, "its quote of the current text does not match")
                continue
            if not isinstance(value, str) or not value.strip():
                reject(path, "a re-worded line must be one piece of text")
                continue
            new = _tidy(value)
            if _norm(new) == _norm(old):
                continue
            problems = _reword(new, old, path, ctx, summary=path == "summary")
            added = _added_terms(new, old, ctx.vocabulary)
            problems += [
                f'{path} adds "{t}" to more than {MAX_LINES_PER_TERM} lines: once or twice is enough'
                for t in added
                if terms_used[t] >= MAX_LINES_PER_TERM
            ]
            if problems:
                reject(
                    path,
                    f'"{new[:90]}": ' + "; ".join(p.removeprefix(path + " ") for p in problems),
                    retry=True,
                )
                continue
            if path == "summary":
                summary = new
            else:
                rows[i][k] = new
            terms_used.update(added)
            result.changed.append(Changed(path, old, new))
            done.add(path)
        elif action == "reorder" and bullets:
            kind, i = bullets.group(1), int(bullets.group(2))
            if i >= len(sections[kind]) or not isinstance(value, list):
                reject(path, "a re-order must list every original bullet of that entry exactly once")
                continue
            orders.append((path, kind, i, value))  # checked once every re-wording is in
            done.add(path)
        elif action == "reorder" and skill_line:
            i = int(skill_line.group(1))
            if i >= len(skills) or not isinstance(value, list):
                reject(path, "no such skills line")
                continue
            ordered = _skill_order(skills[i], value)
            if ordered != skills[i]:
                result.changed.append(Changed(path, ", ".join(skills[i]), ", ".join(ordered)))
                skills[i] = ordered
            done.add(path)
        else:
            reject(path, f'"{action}" is not a change that can be made there')

    for path, kind, i, proposed in orders:  # by the bullets' positions, after any re-wording
        rows = sections[kind]
        source = (master.experience if kind == "experience" else master.projects)[i].bullets
        order = _order(source, rows[i], proposed)
        if order is None:
            reject(path, "a re-order must list every original bullet of that entry exactly once")
            continue
        if order == sorted(order):
            continue
        before = rows[i]
        rows[i] = [before[k] for k in order]
        result.changed.append(
            Changed(
                path,
                " / ".join(f"{k + 1}. {_short(b)}" for k, b in enumerate(before)),
                " / ".join(f"{k + 1}. {_short(before[k])}" for k in order),
            )
        )

    tailored = master.model_copy(
        update={
            "summary": summary,
            "experience": tuple(
                job.model_copy(update={"bullets": tuple(rows)})
                for job, rows in zip(master.experience, sections["experience"], strict=True)
            ),
            "projects": tuple(
                p.model_copy(update={"bullets": tuple(rows)})
                for p, rows in zip(master.projects, sections["projects"], strict=True)
            ),
            "skills": tuple(
                g.model_copy(update={"items": items}) for g, items in zip(master.skills, skills, strict=True)
            ),
        }
    )
    lost = dropped(master, tailored)
    if lost:  # never reached by the gates above; a last guard so nothing is ever lost
        return Applied(master, rejected=[f"resume: the changes would drop {', '.join(lost)}"])
    result.resume = tailored
    return result


def _known_text(master: MasterResume) -> str:
    """Everything the resume says, for the title check (titles it gives the person)."""
    parts = [master.headline, master.summary, *(f"{j.title} {j.company}" for j in master.experience)]
    return "\n".join(parts)


def dropped(master: MasterResume, tailored: MasterResume) -> list[str]:
    """What ``tailored`` lost from ``master`` (empty when nothing; re-worded or re-ordered is fine).

    A role, bullet, project, skill, school, certification, date, link, the name or the headline.
    """
    lost: list[str] = []
    for kind, before, after in (
        ("role", master.experience, tailored.experience),
        ("project", master.projects, tailored.projects),
        ("school", master.education, tailored.education),
        ("section", master.extras, tailored.extras),
    ):
        if len(after) < len(before):
            lost.append(f"a {kind}")
    for old_job, new_job in zip(master.experience, tailored.experience, strict=False):
        if (old_job.company, old_job.title, old_job.start, old_job.end) != (
            new_job.company,
            new_job.title,
            new_job.start,
            new_job.end,
        ):
            lost.append(f"the {old_job.company} title or dates")
        if len(new_job.bullets) < len(old_job.bullets):
            lost.append(f"a {old_job.company} bullet")
    for old_project, new_project in zip(master.projects, tailored.projects, strict=False):
        if len(new_project.bullets) < len(old_project.bullets):
            lost.append(f"a {old_project.name} bullet")
    if sorted(map(_norm, master.all_skills())) != sorted(map(_norm, tailored.all_skills())):
        lost.append("a skill")
    if [e.model_dump() for e in master.extras] != [e.model_dump() for e in tailored.extras]:
        lost.append("a certification or other section line")
    if [s.model_dump() for s in master.education] != [s.model_dump() for s in tailored.education]:
        lost.append("an education detail")
    if {link_key(x) for x in master.contact.links} - {link_key(x) for x in tailored.contact.links}:
        lost.append("a link")
    if master.headline != tailored.headline or master.name != tailored.name:
        lost.append("the name or headline")
    return lost
