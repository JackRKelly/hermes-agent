"""What Jack reads on the face of an Action Item: the field limits and the banned content, in one place.

These numbers and this banned list are the ones in JackRKelly/assistant
``docs/action-items-card-guide.md`` (sections 3 and 6). When one changes, change the other in the
same pair of PRs so they match again.

They are add-time rules: ``build_envelope`` (so ``action_item_add`` and ``action_item_update``)
enforces them on every new item. An item stored before they existed still loads, so the read-time
schemas only carry the hard length caps. Every refusal names the field, the rule, the offending
token or length, a conforming example and the guide, so the calling agent can fix it in one retry.

BANNED (mirrored by the app's adapter) refuses every local path in the title, summary, questions and
option labels. The other texts Jack reads (ask, why, risk.note, an ``other`` body, steps, option
effects, rec_reason) refuse a local path too, with one exception the app needs: a file inside the
card's own workspace or LinkedIn preview dir, named by its path, which the app embeds. That
exception depends on the card, so ``check_item_paths`` takes the card id the tool resolved.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from tools.action_item_kinds.core import SchemaError

GUIDE = "docs/action-items-card-guide.md"

TITLE_MAX_WORDS = 8
TITLE_MAX_CHARS = 60
SUMMARY_MAX_SENTENCES = 2
SUMMARY_MAX_CHARS = 240
RISK_NOTE_MAX_CHARS = 120
MAX_QUESTIONS = 5
QUESTION_MAX_CHARS = 120
MIN_OPTIONS = 2
MAX_OPTIONS = 4
OPTION_LABEL_MAX_CHARS = 40
OPTION_EFFECT_MAX_CHARS = 120
REC_REASON_MAX_CHARS = 240

EXAMPLE_TITLE = "Railway: when to roll out staging and prod?"
EXAMPLE_SUMMARY = "Dev now uses the new deploy settings. Staging and prod need the same change before Dec 1."
EXAMPLE_RISK_NOTE = "Waiting past Dec 1 breaks the next prod deploy."
EXAMPLE_QUESTION = "Convoy: keep or discard?"
EXAMPLE_LABEL = "Staging this week, then prod"
EXAMPLE_EFFECT = "Convoy isn't running anywhere, so nothing stops."
EXAMPLE_REC_REASON = "Staging first catches a bad setting before prod."
EXAMPLE_QUESTIONS = [
    {"key": "q1", "question": "When should staging and prod get it?", "options": [
        {"key": "staging-first", "label": "Staging this week, then prod", "recommended": True},
        {"key": "both-now", "label": "Both this week", "recommended": False}]},
    {"key": "q2", "question": "Convoy: keep or discard?", "options": [
        {"key": "discard", "label": "Discard it", "recommended": True},
        {"key": "keep", "label": "Keep it", "recommended": False}]},
]

# Jack, Nov 12: how text he reads is written. Every banned-content and local-path refusal repeats it,
# and the action_item_add description carries the same rules (the guide's section 7).
STYLE_HINT = ("Name PRs and Linear issues inline as #n, owner/repo#n or CYB-n (the app links them); wrap "
              "commands, env var names, Redis/SQL keywords, file names and identifiers in backticks")


def _literals(kind: str, *tokens: str) -> list[tuple[str, re.Pattern]]:
    return [(kind, re.compile(re.escape(t), re.IGNORECASE)) for t in tokens]


# (what it is, pattern): searched case-insensitively in the title, the summary, every question and
# every option label.
BANNED: tuple[tuple[str, re.Pattern], ...] = (
    ("a raw tag", re.compile(r"\[[a-z-]+[:\]]", re.IGNORECASE)),
    *_literals("a park field", "then=", "head=", "draft=", "thread=", "account="),
    ("a card id", re.compile(r"\bt_[0-9a-f]{8}\b", re.IGNORECASE)),
    *_literals("a local path", "/Users/", "~/", "/private/", ".hermes/"),
    *_literals("an em dash", "\u2014"),
    *_literals("board jargon", "needs-jack", "awaiting-approval", "legacy park", "unblock",
               "hands the card back"),
)
# "(1) ... (2) ..." or "1. ... 2. ...": several questions written into one.
NUMBERED_RE = re.compile(r"(?:^|(?<=\s))(?:\(([1-9])\)|([1-9])[.)](?=\s))")
SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+(?=\S)")


def fix(where: str, problem: str, example: Any) -> SchemaError:
    """The refusal: field, rule and offending value, then a conforming example and the guide."""
    shown = f'"{example}"' if isinstance(example, str) else json.dumps(example, ensure_ascii=False)
    return SchemaError(f"{where}: {problem}. Write e.g. {shown} (see {GUIDE})")


def banned_token(text: str) -> Optional[tuple[str, str]]:
    """``(what, token)`` of the first banned thing in ``text``, else None."""
    for what, pattern in BANNED:
        found = pattern.search(text)
        if found:
            return what, found.group(0)
    return None


def numbered_markers(text: str) -> list[re.Match]:
    """The "(1)", "(2)", ... (or "1.", "2.", ...) markers of a numbered list in ``text``, in order;
    empty unless there are at least two."""
    run: list[re.Match] = []
    for marker in NUMBERED_RE.finditer(text):
        if int(marker.group(1) or marker.group(2)) == len(run) + 1:
            run.append(marker)
    return run if len(run) >= 2 else []


def sentences(text: str) -> int:
    return len([s for s in SENTENCE_END_RE.split(text.strip()) if s])


def check_banned(text: str, where: str, example: str) -> None:
    hit = banned_token(text)
    if hit:
        raise fix(where, f'contains {hit[0]} ({hit[1]!r}); raw tags, park fields, card ids, local '
                         "paths, em dashes and board jargon are not shown to Jack, say it in plain words. "
                         + STYLE_HINT, example)


# A local path as the Action Items app finds one in item text (JackRKelly/assistant
# ``scripts/action_items_face.py`` PATH_RE): what it embeds when the path is inside the card's own
# workspace, and otherwise blanks.
PATH_RE = re.compile(r"(?:~|/Users|/private|/var|/tmp|/home|/opt)/\S+|\S*\.hermes/\S*")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_WORD_START_RE = re.compile(r"[\s(\[{\"'`=<*]")
_ALWAYS_LOCAL_RE = re.compile(r"/Users/|~/|/private/|\.hermes/", re.IGNORECASE)
_PATH_EDGE = ".,;:()[]{}<>\"'`*"
EXAMPLE_PLAIN_FILE = "the rollout report is attached to the card"


def card_file_dirs(card: str) -> tuple[Path, Path]:
    """The two places a card may name a file it shows (the app embeds an image named by its path):
    its own kanban workspace and its LinkedIn preview dir, under the shared board root."""
    from hermes_cli.kanban_db import kanban_home

    root = kanban_home()
    return root / "kanban" / "workspaces" / card, root / "state" / "linkedin" / "previews" / card


def _inside(raw: str, dirs: tuple[Path, ...]) -> bool:
    """Lexical: ``raw`` (``~`` expanded, ``..`` folded) names something below one of ``dirs``. The
    agent's path is never touched on disk; the app resolves symlinks itself before it serves a file."""
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        return False
    path = Path(os.path.normpath(expanded))
    roots = {Path(os.path.normpath(d)) for d in dirs} | {d.resolve() for d in dirs}
    return any(path != d and path.is_relative_to(d) for d in roots)


def foreign_local_path(text: str, card: Optional[str]) -> Optional[str]:
    """The first local path in ``text`` that is not a file inside ``card``'s own workspace or
    LinkedIn preview dir, else None. With no card every local path is foreign. A path inside a URL
    is part of the URL, not a local path."""
    urls = [m.span() for m in _URL_RE.finditer(text)]
    dirs = card_file_dirs(card) if card else ()
    for match in PATH_RE.finditer(text):
        if any(start <= match.start() < end for start, end in urls):
            continue
        # "src/home/page.tsx" is a repo path, not /home; a home or Hermes path is local wherever it sits.
        mid_word = match.start() > 0 and not _WORD_START_RE.match(text[match.start() - 1])
        if mid_word and not _ALWAYS_LOCAL_RE.search(match.group(0)):
            continue
        raw = match.group(0).strip(_PATH_EDGE)
        if not raw or not (dirs and _inside(raw, dirs)):
            return raw or match.group(0)
    return None


def check_local_paths(text: Any, where: str, card: Optional[str]) -> None:
    """Refuse a local path Jack would see printed; a file inside the card's own workspace or LinkedIn
    preview dir is the one exception, because the app replaces that path with the file itself."""
    if not isinstance(text, str):
        return
    raw = foreign_local_path(text, card)
    if raw is None:
        return
    if card:
        workspace, previews = card_file_dirs(card)
        allowed = (f"only a file inside this card's own workspace ({workspace}/) or LinkedIn preview dir "
                   f"({previews}/) may be named by its path, and the app embeds it")
        example: str = f"Image: {workspace}/post-image.png"
    else:
        allowed = "only a file inside the card's own workspace may be named, and this call names no card"
        example = EXAMPLE_PLAIN_FILE
    raise fix(where, f"contains a local path ({raw!r}) Jack would see printed; {allowed}. Otherwise say "
                     f"it in plain words. {STYLE_HINT}", example)


# Each kind's payload texts Jack reads, beside ask, why and risk.note (title, summary, questions and
# option labels already refuse every local path through BANNED). A send's draft is the message itself.
def _payload_texts(kind: str, payload: dict) -> list[tuple[str, Any]]:
    if kind == "other":
        return [("payload.title", payload.get("title")), ("payload.body", payload.get("body"))]
    if kind == "user_task":
        steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
        return [(f"payload.steps[{i}]", s) for i, s in enumerate(steps)] + [("payload.where", payload.get("where"))]
    if kind == "design_decision":
        texts: list[tuple[str, Any]] = [("payload.rec_reason", payload.get("rec_reason"))]
        questions = payload.get("questions") if isinstance(payload.get("questions"), list) else []
        for i, q in enumerate(x for x in questions if isinstance(x, dict)):
            options = q.get("options") if isinstance(q.get("options"), list) else []
            texts += [(f"payload.questions[{i}].options[{j}].effect", o.get("effect"))
                      for j, o in enumerate(o for o in options if isinstance(o, dict))]
        return texts
    return []


def check_item_paths(kind: str, payload: dict, *, ask: Any, why: Any, risk: Any, card: Optional[str]) -> None:
    """The add-time local-path rule on every human-facing text of a new item (Jack, Nov 12)."""
    note = risk.get("note") if isinstance(risk, dict) else None
    for where, text in [("ask", ask), ("why", why), ("risk.note", note), *_payload_texts(kind, payload)]:
        check_local_paths(text, where, card)


# Jack, Nov 13: "Run this SQL to pause two webhooks" was parked as ``other``, so the app offered Approve
# for something Jack does himself. A task Jack performs is a ``user_task`` (Done / Can't do it); ``other``
# and ``design_decision`` are for what an agent does once Jack approves or answers. Jack's own action
# reads as an imperative at the start of the ask or title ("Run ...", "Please paste ...", "Can you sign
# in ...") or as "mark this done"; "OK for me to run ...?" and "Should we run ...?" stay approvals.
# Mirrored by JackRKelly/assistant ``scripts/action_items_face.py`` SELF_TASK_RE (the app's fallback).
_SELF_VERBS = (r"run|paste|execute|click|upload|call|phone|pay|install|enter|"
               r"sign(?:\s+(?:in|into|up|on))?|log\s*(?:in|into|on)")
SELF_TASK_RE = re.compile(r"^\W*(?:(?:jack|please)\b[,:]?\s+)*(?:(?:can|could|will|would)\s+you\s+(?:please\s+)?)?"
                          rf"(?P<verb>{_SELF_VERBS})\b", re.IGNORECASE)
MARK_DONE_RE = re.compile(r"\bmark\s+(?:this|it)?\s*(?:as\s+)?done\b", re.IGNORECASE)
# In an ``other`` body: an instruction to Jack ("... read-only. Paste the block below ...", "you need to
# run ..."). "Paste" opens a sentence or a list item; nobody but Jack pastes.
_BODY_SELF_RE = re.compile(rf"(?:^|(?<=[\n.!?:]))\s*(?:\d{{1,2}}[.)]\s+)?paste\b|"
                           rf"\byou(?:'ll)?\s+(?:need\s+to|have\s+to|must|should)\s+(?:{_SELF_VERBS})\b",
                           re.IGNORECASE)
EXAMPLE_USER_TASK = {"kind": "user_task", "ask": "Run the pause SQL in prod, then mark done?", "payload": {
    "steps": ["Open a prod Postgres session with write access",
              "Run `UPDATE outbound_webhook_endpoint SET paused = true WHERE id IN (...) RETURNING id;`",
              "Check it returns 2 rows"],
    "where": "Prod Postgres write session"}}


def self_task_hit(text: Any, *, lead: bool = True, body: bool = False) -> Optional[str]:
    """The words in ``text`` that ask Jack to do something himself, else None: a leading imperative
    (``lead``), "mark this done", and in a body an instruction to Jack (``body``)."""
    if not isinstance(text, str):
        return None
    found = (lead and SELF_TASK_RE.search(text)) or MARK_DONE_RE.search(text) or (body and _BODY_SELF_RE.search(text))
    return " ".join(found.group(0).split()).lstrip("0123456789.) ") if found else None


def check_self_task(kind: str, payload: dict, *, ask: Any, title: Any) -> None:
    """Refuse an ``other`` or ``design_decision`` that asks Jack to perform the action himself: it is a
    ``user_task``, so the app shows Done / Can't do it, never Approve (Jack, Nov 13)."""
    if kind not in ("other", "design_decision"):
        return
    texts = [("ask", ask, False), ("title", title, False)]
    if kind == "other":
        texts += [("payload.title", payload.get("title"), False), ("payload.body", payload.get("body"), True)]
    for where, text, body in texts:
        hit = self_task_hit(text, lead=not body, body=body)
        if hit:
            raise fix(where, f"asks Jack to do it himself ({hit!r}); a task Jack performs (run or paste SQL or a "
                             "command, sign in, click, upload, call, sign, pay, install) is kind=\"user_task\" "
                             f"with steps[] and where, so the app shows Done, never kind=\"{kind}\" with "
                             "Approve. Keep other for what an agent does after Jack approves, asked as "
                             "\"OK for me to ...?\"", EXAMPLE_USER_TASK)


# A decision that says there is nothing to decide (a stuck-work check that found nothing, Nov 13). The
# same pattern as JackRKelly/assistant's; change both in the same pair of PRs.
NO_OP_RE = re.compile(r"\bnothing (?:here |else )?(?:needs?|for) (?:jack|you)\b|\bnothing (?:to (?:fix|do|decide)|needs? (?:fixing|doing))\b|\bno (?:decision|action|input) (?:is )?(?:needed|required|for jack)\b|\bnot (?:a decision|an action item) for jack\b", re.I)  # noqa: E501
NO_OP_PROBLEM = ("This says nothing needs Jack: complete the card with a one-line summary instead, or tag it "
                 "[captain:<topic>] for Captain triage")


def check_no_op(kind: str, payload: dict, *, title: Any, summary: Any, ask: Any, why: Any) -> None:
    """Refuse a ``design_decision`` whose own text says nothing needs Jack: there is nothing to answer."""
    if kind != "design_decision":
        return
    texts = [("title", title, EXAMPLE_TITLE), ("summary", summary, EXAMPLE_SUMMARY), ("ask", ask, EXAMPLE_QUESTION),
             ("why", why, EXAMPLE_SUMMARY), ("payload.rec_reason", payload.get("rec_reason"), EXAMPLE_REC_REASON)]
    questions = payload.get("questions")
    for i, q in enumerate(questions if isinstance(questions, list) else []):
        if isinstance(q, dict):
            texts.append((f"payload.questions[{i}].question", q.get("question"), EXAMPLE_QUESTION))
    for where, text, example in texts:
        if isinstance(text, str) and NO_OP_RE.search(text):
            raise fix(where, NO_OP_PROBLEM, example)


def check_chars(text: str, limit: int, where: str, example: str) -> None:
    if len(text) > limit:
        raise fix(where, f"{len(text)} chars, max {limit}", example)


TOP_LEVEL = "a top-level parameter beside kind and payload, never a payload field"


def _required_line(value: Any, where: str, rule: str, example: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise fix(where, f"required ({TOP_LEVEL}), {rule}", example)
    if "\n" in value or "\r" in value:
        raise fix(where, f"must be one line ({rule})", example)
    return value


def check_title(value: Any, where: str = "title") -> None:
    rule = f"1-{TITLE_MAX_WORDS} words and at most {TITLE_MAX_CHARS} chars"
    title = _required_line(value, where, rule, EXAMPLE_TITLE)
    check_banned(title, where, EXAMPLE_TITLE)
    words = len(title.split())
    if words > TITLE_MAX_WORDS:
        raise fix(where, f"{words} words, max {TITLE_MAX_WORDS}", EXAMPLE_TITLE)
    check_chars(title, TITLE_MAX_CHARS, where, EXAMPLE_TITLE)


def check_summary(value: Any, where: str = "summary") -> None:
    rule = f"1-{SUMMARY_MAX_SENTENCES} sentences and at most {SUMMARY_MAX_CHARS} chars"
    if not isinstance(value, str) or not value.strip():
        raise fix(where, f"required ({TOP_LEVEL}), {rule}", EXAMPLE_SUMMARY)
    check_banned(value, where, EXAMPLE_SUMMARY)
    count = sentences(value)
    if count > SUMMARY_MAX_SENTENCES:
        raise fix(where, f"{count} sentences, max {SUMMARY_MAX_SENTENCES}", EXAMPLE_SUMMARY)
    check_chars(value, SUMMARY_MAX_CHARS, where, EXAMPLE_SUMMARY)


def check_risk_note(risk: Any, where: str = "risk.note") -> None:
    note = risk.get("note") if isinstance(risk, dict) else None
    if not isinstance(note, str):
        return  # the envelope schema reports the missing or mistyped note
    if "\n" in note or "\r" in note:
        raise fix(where, "must be one line", EXAMPLE_RISK_NOTE)
    check_chars(note, RISK_NOTE_MAX_CHARS, where, EXAMPLE_RISK_NOTE)


def check_new_envelope(*, title: Any, summary: Any, risk: Any) -> None:
    """The add-time rules on the envelope fields every kind shares."""
    check_title(title)
    check_summary(summary)
    check_risk_note(risk)
