"""``design_decision``: Jack answers one or more questions (design §3.2).

v2 (written by every new item): ``questions`` holds 1-5 questions, each with 2-4 options, exactly one
recommended, plus Jack's own answer ("Other") when ``allow_other``. Jack answers; there is no
approve or reject. v1 (one ``question`` with 2-6 ``options``) still reads, and ``questions_of``
shows both in the v2 shape. The tool computes ``option_set_hash`` over every question, so a
re-worded question or option is a new item.

``legacy_questions`` reads a free-text ``[needs-jack:...]`` park as questions, so an old park also
shows as answerable questions instead of an Approve button.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from tools.action_item_kinds import legacy, readability as rd
from tools.action_item_kinds.core import SchemaError, prefixed_hash
from tools.action_item_kinds.spec import KindSpec, load_versions, no_external_live_check, require_hash

KIND = "design_decision"
AGENT_FIELDS = frozenset({"questions", "rec_reason"})
V1_FIELDS = ("question", "options")
OTHER_KEY = "other"


def option_set_hash(payload: dict) -> str:
    if "questions" in payload:
        return prefixed_hash({"questions": payload.get("questions")})
    return prefixed_hash({"question": payload.get("question"), "options": payload.get("options")})


def questions_of(payload: dict) -> list[dict]:
    """Every question of a v1 or v2 payload (or a ``legacy_questions`` projection) in the v2 shape,
    each question's recommended option first. Never mutates ``payload``."""
    if isinstance(payload.get("questions"), list):
        raw = payload["questions"]
    else:
        options = [{k: o[k] for k in ("key", "label", "effect", "recommended") if k in o}
                   for o in payload.get("options") or [] if isinstance(o, dict)]
        raw = [{"key": "q1", "question": payload.get("question", ""), "options": options}]
    out = []
    for q in raw:
        options = sorted((dict(o) for o in q.get("options") or []), key=lambda o: not o.get("recommended"))
        out.append({**q, "options": options, "allow_other": q.get("allow_other", True)})
    return out


def _enrich(agent: dict, sources) -> dict:
    full = dict(agent)
    if isinstance(full.get("questions"), list):
        full["questions"] = [{"allow_other": True, **q} if isinstance(q, dict) else q for q in full["questions"]]
    return {**full, "option_set_hash": option_set_hash(full)}


def _check_options(options: list, where: str) -> None:
    keys = [o["key"] for o in options]
    if len(set(keys)) != len(keys):
        raise SchemaError(f"{where}: option keys must be unique")
    if OTHER_KEY in keys:
        raise SchemaError(f'{where}: "{OTHER_KEY}" is reserved for Jack\'s own answer')
    if sum(1 for o in options if o["recommended"]) != 1:
        raise SchemaError(f"{where}: mark exactly one option recommended")


def _check(env: dict) -> None:
    p = env["payload"]
    if "questions" in p:
        keys = [q["key"] for q in p["questions"]]
        if len(set(keys)) != len(keys):
            raise SchemaError("payload.questions: question keys must be unique")
        for i, q in enumerate(p["questions"]):
            _check_options(q["options"], f"payload.questions[{i}].options")
    else:
        _check_options(p["options"], "payload.options")
    require_hash(p, "option_set_hash", option_set_hash(p))


# --- add-time readability (readability.py), with a fix in every refusal ----------------------------

def _precheck_question(q: dict, where: str) -> None:
    text = q.get("question")
    if isinstance(text, str):
        rd.check_banned(text, f"{where}.question", rd.EXAMPLE_QUESTION)
        if rd.numbered_markers(text):
            raise rd.fix(f"{where}.question", f"holds {len(rd.numbered_markers(text))} numbered questions; "
                         "split into questions[], one entry per question", rd.EXAMPLE_QUESTIONS)
        rd.check_chars(text, rd.QUESTION_MAX_CHARS, f"{where}.question", rd.EXAMPLE_QUESTION)
    options = q.get("options")
    if not isinstance(options, list):
        return
    if not rd.MIN_OPTIONS <= len(options) <= rd.MAX_OPTIONS:
        raise rd.fix(f"{where}.options", f"{len(options)} options, needs {rd.MIN_OPTIONS}-{rd.MAX_OPTIONS} "
                     "(Jack can always answer Other)", rd.EXAMPLE_QUESTIONS[0]["options"])
    for j, option in enumerate(o for o in options if isinstance(o, dict)):
        label, effect = option.get("label"), option.get("effect")
        if isinstance(label, str):
            rd.check_banned(label, f"{where}.options[{j}].label", rd.EXAMPLE_LABEL)
            rd.check_chars(label, rd.OPTION_LABEL_MAX_CHARS, f"{where}.options[{j}].label", rd.EXAMPLE_LABEL)
        if isinstance(effect, str):
            rd.check_chars(effect, rd.OPTION_EFFECT_MAX_CHARS, f"{where}.options[{j}].effect", rd.EXAMPLE_EFFECT)


def _precheck(payload: dict) -> None:
    """Refuse the v1 shape and every readability limit before the schema, each with its fix."""
    if any(field in payload for field in V1_FIELDS):
        old = payload.get("question")
        split = (f" (your question holds {len(rd.numbered_markers(old))} numbered questions: split into "
                 "questions[])") if isinstance(old, str) and rd.numbered_markers(old) else ""
        raise rd.fix("payload.questions", "required; design_decision asks its questions as questions[] "
                     f"(1-{rd.MAX_QUESTIONS}, one entry per question), not question/options{split}",
                     {"questions": rd.EXAMPLE_QUESTIONS})
    questions = payload.get("questions")
    if questions is None:
        raise rd.fix("payload.questions", f"required, 1-{rd.MAX_QUESTIONS} questions",
                     {"questions": rd.EXAMPLE_QUESTIONS})
    if isinstance(questions, list):
        if not 1 <= len(questions) <= rd.MAX_QUESTIONS:
            raise rd.fix("payload.questions", f"{len(questions)} questions, needs 1-{rd.MAX_QUESTIONS}; "
                         "ask the rest in a later item", {"questions": rd.EXAMPLE_QUESTIONS})
        for i, q in enumerate(questions):
            if isinstance(q, dict):
                _precheck_question(q, f"payload.questions[{i}]")
    reason = payload.get("rec_reason")
    if isinstance(reason, str):
        rd.check_chars(reason, rd.REC_REASON_MAX_CHARS, "payload.rec_reason", rd.EXAMPLE_REC_REASON)


# --- legacy free-text parks ----------------------------------------------------------------------

_TAG_RE = re.compile(r"\[[a-z-]+(?::[^\]\s]*)?\]", re.IGNORECASE)
_REC_RE = re.compile(r"(?:^|(?<=\s))Rec(?:ommendation|ommended)?\s*:\s*(?P<rec>.*)$", re.IGNORECASE)
# "keep or discard?", "UPS or FedEx for label buys?": the two words around an "or" of the question.
_ALTERNATIVES_RE = re.compile(r"(?:^|(?<=[\s:,(]))([\w-]+),?\s+or\s+([\w-]+)(?=[\s?.,;:)]|$)", re.IGNORECASE)
_APPROVE_WORDS = frozenset({"approve", "approved", "approval", "reject", "rejected"})


def _clean(reason: str) -> str:
    """The park text without its tags and ``key=value`` fields, on one line."""
    return " ".join(legacy.KV_RE.sub(" ", _TAG_RE.sub(" ", reason)).split())


def _shorten(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:") + "\u2026"


def _segments(body: str) -> list[str]:
    markers = rd.numbered_markers(body)
    if not markers:
        return [body]
    ends = [m.start() for m in markers[1:]] + [len(body)]
    return [body[m.end():end].strip() for m, end in zip(markers, ends)]


def _question_text(segment: str) -> str:
    """The segment's first sentence ending in "?", else its first sentence."""
    parts = [s for s in rd.SENTENCE_END_RE.split(segment.strip()) if s]
    asked = next((s for s in parts if s.endswith("?")), parts[0] if parts else "")
    return _shorten(asked, rd.QUESTION_MAX_CHARS)


def _alternatives(question: str) -> list[str]:
    found = _ALTERNATIVES_RE.findall(question)
    words = list(found[-1]) if found else []
    return [] if any(w.lower() in _APPROVE_WORDS for w in words) else words


def _mentions(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text, re.IGNORECASE) is not None


def _rec_for(index: int, count: int, parts: list[str], alternatives: list[str]) -> Optional[str]:
    """The Rec part answering question ``index``: positional when the parts match the questions."""
    if not parts:
        return None
    if count == 1:
        return "; ".join(parts)
    if len(parts) == count:
        return parts[index]
    return next((p for p in parts if any(_mentions(p, a) for a in alternatives)), None)


def _key(label: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:28].strip("-") or "option"
    if base == OTHER_KEY:
        base = "other-answer"
    key, n = base, 2
    while key in taken:
        key, n = f"{base}-{n}", n + 1
    taken.add(key)
    return key


def _options(rec: Optional[str], alternatives: list[str]) -> list[dict]:
    """The recommended answer first (an alternative it names, else the Rec text), then the rest."""
    picked = next((a for a in alternatives if rec and _mentions(rec, a)), None)
    labels: list[tuple[str, bool]] = []
    if rec and picked is None and rec.split()[0].lower() not in _APPROVE_WORDS:
        labels.append((rec, True))
    labels += sorted(((a, a is picked) for a in alternatives), key=lambda pair: not pair[1])
    taken: set[str] = set()
    out = []
    for text, recommended in labels:
        label = _shorten(text[:1].upper() + text[1:], rd.OPTION_LABEL_MAX_CHARS)
        out.append({"key": _key(label, taken), "label": label, "recommended": recommended})
    return out


def legacy_questions(reason: str) -> Optional[dict]:
    """A free-text ``[needs-jack:...]`` park read as v2 questions, else None.

    ``{questions: [{key, question, options, allow_other: True}], source_text}``: numbered
    questions ("(1) ... (2) ..." / "1. ... 2. ...") become one question each; a "Rec:" line split
    on ";" gives each question its recommended option, and an "A or B?" question gives the
    alternatives. A question with no derivable option has ``options: []`` (a free-text answer).
    Never an approve-style option. ``source_text`` is the raw park for the Details view. This is a
    read-only projection, not an agent payload: it is never stored or hashed.
    """
    if not reason or not legacy.NEEDS_JACK_RE.search(reason):
        return None
    text = _clean(reason)
    rec_match = _REC_RE.search(text)
    body = text[:rec_match.start()].strip() if rec_match else text
    rec_parts = [p.strip(" .") for p in rec_match["rec"].split(";")] if rec_match else []
    rec_parts = [p for p in rec_parts if p]
    segments = [s for s in _segments(body) if s][:rd.MAX_QUESTIONS]
    questions = []
    for i, segment in enumerate(segments):
        question = _question_text(segment)
        alternatives = _alternatives(question)
        rec = _rec_for(i, len(segments), rec_parts, alternatives)
        questions.append({"key": f"q{i + 1}", "question": question, "options": _options(rec, alternatives),
                          "allow_other": True})
    if not questions:
        return None
    return {"questions": questions, "source_text": reason}


SPEC = KindSpec(
    kind=KIND, park_tag="needs-jack", gate="decision",
    schema_versions=load_versions(KIND, 1, 2), agent_fields=AGENT_FIELDS,
    # Unchanged from v1 so a stored v1 item keeps its binding hash; v2 binds through option_set_hash.
    binding_fields=("question", "option_set_hash"),
    allowed_actions=("answer", "snooze", "later"),
    executor=None, enrich=_enrich,
    live_check=no_external_live_check("option_set_hash", option_set_hash),
    legacy_parse=legacy_questions, check=_check, precheck=_precheck,
)
