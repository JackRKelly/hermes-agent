"""What Jack reads on the face of an Action Item: the field limits and the banned content, in one place.

These numbers and this banned list are the ones in JackRKelly/assistant
``docs/action-items-card-guide.md`` (sections 3 and 6). When one changes, change the other in the
same pair of PRs so they match again.

They are add-time rules: ``build_envelope`` (so ``action_item_add`` and ``action_item_update``)
enforces them on every new item. An item stored before they existed still loads, so the read-time
schemas only carry the hard length caps. Every refusal names the field, the rule, the offending
token or length, a conforming example and the guide, so the calling agent can fix it in one retry.

Jack reads Action Items from another machine (Nov 24: "stop sharing paths of files on the action items
cards ... i dont have access to view from web on another machine"), so no text he reads may name a
local path. BANNED (mirrored by the app's adapter) refuses the commonest ones in the title, summary,
questions and option labels, and ``check_item_paths`` refuses every local path in every text Jack reads,
with one exception: a file the app itself serves (an image, video, audio clip or md/txt/log/json report)
inside the card's own workspace or LinkedIn preview dir, named in plain text, which the app replaces with
the file (that one depends on the card, so ``check_item_paths`` takes the card id the tool resolved).
SQL or a command Jack must run goes in the text itself, never in a file he is pointed to.
"""

from __future__ import annotations

import itertools
import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from tools.action_item_kinds.core import MARKDOWN_RE, SchemaError

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
STYLE_HINT = ("Name PRs and Linear issues inline as #n, owner/repo#n or CYB-n (the app links them); put any "
              "other URL Jack should open in links[] with a short label; wrap "
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
# Beyond the app's PATH_RE: a home directory spelled as a variable (``$HOME/x``, ``${HERMES_HOME}``) names
# this Mac just as much, so the refusal finds it too.
_HOME_VAR = r"\$\{?(?:HOME|HERMES_HOME|TMPDIR)\b\}?\S*"
LOCAL_PATH_RE = re.compile(rf"{PATH_RE.pattern}|{_HOME_VAR}")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_WORD_START_RE = re.compile(r"[\s(\[{\"'`=<*]")
_ALWAYS_LOCAL_RE = re.compile(r"/Users/|~/|/private/|\.hermes/|\$\{?(?:HOME|HERMES_HOME|TMPDIR)\b", re.IGNORECASE)
_PATH_EDGE = ".,;:()[]{}<>\"'`*"
# Inline `code` and ```fenced``` blocks: the app shows them verbatim and never swaps a path in them for the file.
_CODE_RE = re.compile(r"```.*?(?:```|\Z)|`[^`\n]+`", re.S)
# The file types the app serves from a card's own directories (JackRKelly/assistant
# ``scripts/action_items_app.py`` ``item_file``: IMAGE_TYPES, VIDEO_TYPES, AUDIO_TYPES, TEXT_FILE_RE). Any other
# file there (a ``.sql`` script, Nov 24) shows as a bare "file" Jack cannot open, so naming it is refused.
SERVED_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm", ".m4a", ".mp3", ".wav",
                             ".md", ".txt", ".log", ".json"})
EXAMPLE_PLAIN_FILE = "the rollout report is attached to the card"
# What a task with SQL looks like with no file: each statement inline (the app gives a step's one code span its
# own Copy box), or the whole script as one fenced block in one step (the app renders it with Copy).
EXAMPLE_SQL_STEPS = [
    "Open a prod Postgres session with write access",
    "Preview, expect 12 rows: `SELECT id, committed FROM inventory_level WHERE committed < 0;`",
    "Run the repair:\n```sql\nBEGIN;\nUPDATE inventory_level SET committed = 0 WHERE committed < 0;\nCOMMIT;\n```",
    "Verify, expect 0: `SELECT count(*) FROM inventory_level WHERE committed < 0;`",
]
# What a task about a file on the Hermes Mac looks like with no path (narration takes, a Full Disk Access
# binary): Jack sends the file and the agent moves it, or the agent first runs `open -R <path>` itself so
# Finder shows the file selected and the step only says what to do with it.
EXAMPLE_FILE_STEPS = [
    "AirDrop the 7 narration takes to the Hermes Mac (they land in Downloads; the agent moves them)",
    "Mark this Done",
]
EXAMPLE_PICK_STEPS = [
    "On the Hermes Mac, open System Settings > Privacy & Security > Full Disk Access",
    "Drag the file selected in the Finder window the agent opened into the list and turn it on",
]
LOCAL_PATH_FIX = (
    "Jack reads Action Items on another machine and cannot open a file on this Mac. Put the SQL or "
    "command itself in the text: one statement per user_task step as its code span, or a whole script as "
    "one ```sql fenced block in one step (or in an other body); the app shows both with a Copy button. A "
    "file Jack sends or picks on the Hermes Mac is named in plain words, never by path: he AirDrops it to "
    "the Hermes Mac (it lands in Downloads) and the agent moves it, or for a pick the agent first runs "
    "`open -R <path>` itself so Finder shows the file selected. A "
    "picture, video or md/txt/log/json report the card shows is saved in this card's own workspace and named "
    "by its path in plain text (never in backticks), and the app embeds it")
# A step about SQL gets the inline SQL example; any other step the file example.
_SQL_HINT_RE = re.compile(r"\b(?:SELECT|INSERT|UPDATE|DELETE|BEGIN|COMMIT|SQL|psql)\b|\.sql\b")

# Jack, Nov 17: a park put a Drive URL in its summary with ``links: []``, so the card face read as a
# raw URL instead of a sentence. A URL in text stays accepted (the app links it in place), so this is
# a warning on the tool result, never a refusal: a summary whose URLs are more than this share of its
# characters gets one.
URL_HEAVY_SHARE = 0.5
EXAMPLE_LINK = {"label": "Rules video v4", "url": "https://drive.google.com/file/d/<id>/view"}


def url_heavy_warnings(summary: Any) -> list[str]:
    """Non-blocking warnings for a summary that is mostly a raw URL (empty when it is not)."""
    if not isinstance(summary, str) or not summary.strip():
        return []
    url_chars = sum(len(m.group(0)) for m in _URL_RE.finditer(summary))
    if url_chars <= URL_HEAVY_SHARE * len(summary.strip()):
        return []
    return [f"summary: {url_chars} of its {len(summary.strip())} characters are a raw URL. Put the URL in "
            f"links[] with a short label, e.g. {json.dumps(EXAMPLE_LINK)}, and say in words what it is "
            f'(e.g. "The rules video v4 is on Drive, ready to post."). The item was still parked (see {GUIDE})']


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


def _spans(pattern: re.Pattern, text: str) -> list[tuple[int, int]]:
    """The ``(start, end)`` of every match of ``pattern`` in ``text``."""
    return [m.span() for m in pattern.finditer(text)]


def _within(pos: int, spans: list[tuple[int, int]]) -> bool:
    """``pos`` falls inside one of ``spans``."""
    return any(start <= pos < end for start, end in spans)


def _served_file(raw: str, dirs: tuple[Path, ...]) -> bool:
    """``raw`` names a file the app serves (``SERVED_SUFFIXES``) inside one of ``dirs``, so it shows the file."""
    return Path(raw).suffix.lower() in SERVED_SUFFIXES and _inside(raw, dirs)


def foreign_local_path(text: str, card: Optional[str]) -> Optional[str]:
    """The first local path in ``text`` that the app does not replace with the file itself, else None.
    The one path it replaces is a served file type inside ``card``'s own workspace or LinkedIn preview
    dir, written in plain text: in `code` or a fenced block the app prints it as text. With no card every
    local path is foreign. A path inside a URL is part of the URL, not a local path."""
    urls = _spans(_URL_RE, text)
    code = _spans(_CODE_RE, text)
    dirs = card_file_dirs(card) if card else ()
    for match in LOCAL_PATH_RE.finditer(text):
        if _within(match.start(), urls):
            continue
        # "src/home/page.tsx" is a repo path, not /home; a home or Hermes path is local wherever it sits.
        mid_word = match.start() > 0 and not _WORD_START_RE.match(text[match.start() - 1])
        if mid_word and not _ALWAYS_LOCAL_RE.search(match.group(0)):
            continue
        raw = match.group(0).strip(_PATH_EDGE)
        if not raw or _within(match.start(), code) or not (dirs and _served_file(raw, dirs)):
            return raw or match.group(0)
    return None


def check_local_paths(text: Any, where: str, card: Optional[str]) -> None:
    """Refuse a local path Jack would see printed (``foreign_local_path``). The example for a task's steps and
    where is inline SQL steps when the text is about SQL, else the file Jack sends to the Hermes Mac; for any
    other text it is the one file form the app embeds."""
    if not isinstance(text, str):
        return
    raw = foreign_local_path(text, card)
    if raw is None:
        return
    example: Any
    if where.startswith(("payload.steps[", "payload.where")):
        example = EXAMPLE_SQL_STEPS if _SQL_HINT_RE.search(text) else EXAMPLE_FILE_STEPS
    elif card:
        example = f"Image: {card_file_dirs(card)[0]}/post-image.png"
    else:
        example = EXAMPLE_PLAIN_FILE
    raise fix(where, f"contains a local path ({raw!r}). {LOCAL_PATH_FIX}. {STYLE_HINT}", example)


# Every text of an item Jack reads, as ``(field, text)``. A send's draft is the message itself, never text
# about the card, so it is not here.
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
            texts.append((f"payload.questions[{i}].question", q.get("question")))
            for j, o in enumerate(o for o in options if isinstance(o, dict)):
                texts += [(f"payload.questions[{i}].options[{j}].label", o.get("label")),
                          (f"payload.questions[{i}].options[{j}].effect", o.get("effect"))]
        return texts
    return []


def item_texts(kind: str, payload: dict, *, title: Any, summary: Any, ask: Any, why: Any,
               risk: Any) -> list[tuple[str, Any]]:
    """Every ``(field, text)`` of an item that Jack reads on its card."""
    note = risk.get("note") if isinstance(risk, dict) else None
    return [("title", title), ("summary", summary), ("ask", ask), ("why", why), ("risk.note", note),
            *_payload_texts(kind, payload)]


def item_path_problems(kind: str, payload: dict, *, title: Any, summary: Any, ask: Any, why: Any, risk: Any,
                       card: Optional[str]) -> list[str]:
    """``"<field> names a local path (<path>)"`` for every text of a stored item that shows Jack a local
    path (``foreign_local_path``), empty when none does. The read-time twin of ``check_item_paths``: the
    Action Items readiness check sends such an open item back to its owner (JackRKelly/assistant
    ``scripts/action_items_readiness.py``), so an item parked before this rule is fixed too."""
    out = []
    for where, text in item_texts(kind, payload if isinstance(payload, dict) else {}, title=title,
                                  summary=summary, ask=ask, why=why, risk=risk):
        raw = foreign_local_path(text, card) if isinstance(text, str) else None
        if raw is not None:
            out.append(f"{where} names a local path ({raw})")
    return out


def check_item_paths(kind: str, payload: dict, *, title: Any, summary: Any, ask: Any, why: Any, risk: Any,
                     card: Optional[str]) -> None:
    """The add-time local-path rule on every human-facing text of a new item (Jack, Nov 12 and Nov 24),
    plus a ``user_task``'s step rules (``check_task_steps``)."""
    if kind == "user_task":
        check_task_steps(payload)
    for where, text in item_texts(kind, payload, title=title, summary=summary, ask=ask, why=why, risk=risk):
        check_local_paths(text, where, card)


# Jack, Nov 14: a Full Disk Access task named its path folder by folder ("Open `.hermes`, then `tools`,
# then ... `bin`") and he could not follow it. Since Nov 24 no step names a local path at all, written whole
# or folder by folder: Jack works from another machine.
_NAME_SPAN_RE = re.compile(r"`([^`/\s]+)`")
_CHAIN_GAP_RE = re.compile(r"\s*(?:[,>]\s*)?(?:(?:and\s+)?then\s+)?(?:(?:open|go\s+(?:in)?to|double-click|click)\s+)?",
                           re.IGNORECASE)
_CHAIN_MIN = 3
# A step is short (the app shows it as one numbered line) unless it carries a fenced block: a SQL script too
# long for one line goes in ONE step as ```sql ... ``` (the app renders it with Copy), up to the schema's cap.
STEP_MAX_CHARS = 300
STEP_BLOCK_MAX_CHARS = 4000
_FENCE_LINE_RE = re.compile(r"^\s*```", re.M)
# A SQL file named in a step: Jack, Nov 24, a repair task said "Open the repair script at <path>.sql".
_SQL_FILE_RE = re.compile(r"[\w.-]+\.sql\b", re.IGNORECASE)


def check_folder_chain(text: Any, where: str) -> None:
    """Refuse a step that walks a path folder by folder: 3+ backticked names joined by "then", ","
    or ">"."""
    if not isinstance(text, str):
        return
    run = 1
    spans = list(_NAME_SPAN_RE.finditer(text))
    for prev, span in itertools.pairwise(spans):
        gap = text[prev.end():span.start()]
        joined = bool(gap.strip()) and _CHAIN_GAP_RE.fullmatch(gap) is not None
        run = run + 1 if joined else 1
        if run >= _CHAIN_MIN:
            raise fix(where, f"names a local path folder by folder. {LOCAL_PATH_FIX}", EXAMPLE_PICK_STEPS)


def _check_sql_file(text: Any, where: str) -> None:
    if not isinstance(text, str):
        return
    urls = _spans(_URL_RE, text)
    for found in _SQL_FILE_RE.finditer(text):
        if not _within(found.start(), urls):
            raise fix(where, f"points Jack to the SQL file {found.group(0)!r}, which he cannot open from his "
                             "machine; put its SQL in the steps instead: the preview `SELECT` with the row count "
                             "to expect, the repair as one ```sql block from `BEGIN;` to `COMMIT;`, and the "
                             "verify query", EXAMPLE_SQL_STEPS)


def _check_step_form(text: Any, where: str) -> None:
    """A step is plain text (the schema stores steps as markdown only so one can hold a fenced block)."""
    if not isinstance(text, str):
        return
    fences = len(_FENCE_LINE_RE.findall(text))
    if fences % 2:
        raise fix(where, "has a ``` fence that is never closed; end the block with a ``` line", EXAMPLE_SQL_STEPS[2])
    prose = _CODE_RE.sub(" ", text)
    if MARKDOWN_RE.search(prose):
        raise fix(where, "plain text only outside a ```sql fenced block (no links, headings or bold; the app "
                         "numbers the steps itself)", EXAMPLE_SQL_STEPS[1])
    limit = STEP_BLOCK_MAX_CHARS if fences else STEP_MAX_CHARS
    if len(text) > limit:
        more = "" if fences else "; a longer script goes in this step as one ```sql fenced block"
        raise fix(where, f"{len(text)} chars, max {limit}{more}", EXAMPLE_SQL_STEPS[2])


def check_task_steps(payload: dict) -> None:
    """A ``user_task``'s step rules: no folder-by-folder path, no SQL file named instead of its SQL, a plain
    step within ``STEP_MAX_CHARS`` and a step with a closed fenced block within ``STEP_BLOCK_MAX_CHARS``."""
    steps = payload.get("steps")
    for i, step in enumerate(steps if isinstance(steps, list) else []):
        where = f"payload.steps[{i}]"
        check_folder_chain(step, where)
        _check_sql_file(step, where)
        _check_step_form(step, where)
    _check_sql_file(payload.get("where"), "payload.where")


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
# rule is conservative: a text is a no-op only when EVERY sentence is a no-op phrase or a closed generic
# ask, with no other words; a label, a colon, a scope or any extra word makes it a real ask. It is the
# shared fixture ``tests/fixtures/no_op_rule.v3.json``, committed byte-identical in JackRKelly/assistant
# (``scripts/kanban_gates.py``); its test pins these five patterns and every case.
NO_OP_RE = re.compile(r"\bnothing (?:here |else )?(?:needs?|for) (?:jack|you)\b|\bnothing (?:to (?:fix|do|decide)|needs? (?:fixing|doing))\b|\bno (?:decision|action|input) (?:is )?(?:needed|required|for jack)\b|\bnot (?:a decision|an action item) for jack\b", re.IGNORECASE)
# A whole sentence of no-op phrases ("Nothing to fix and nothing needs Jack").
NO_OP_SENTENCE_RE = re.compile(rf"(?:{NO_OP_RE.pattern})(?:\s*(?:,\s*and|,|and)\s*(?:{NO_OP_RE.pattern}))*",
                               re.IGNORECASE)
# A whole sentence that is a generic ask from this closed list ("What next?", "Anything else?").
GENERIC_ASK_RE = re.compile(r"what(?: should (?:happen|we do)| do we do)? next|what now|anything else", re.IGNORECASE)
# Words that are not part of the message: the autopilot's "Consult Jack:" lead and "(from t_x)" provenance.
NO_OP_NOISE_RE = re.compile(r"^\s*consult jack\s*:|\(from (?:t_\w+|the card)\)", re.IGNORECASE)
SENTENCE_SPLIT_RE = re.compile(r"[.;!?]+(?=\s|$)|\s/\s", re.IGNORECASE)


def _no_op_sentences(text: str) -> list[str]:
    parts = SENTENCE_SPLIT_RE.split(NO_OP_NOISE_RE.sub(" ", text))
    return [s for s in (" ".join(p.split()).strip(" ,") for p in parts) if s]


def is_no_op_text(text: Any, *, has_real_question: bool = False) -> bool:
    """True when every sentence of ``text`` is a no-op phrase (``NO_OP_SENTENCE_RE``) or a closed generic
    ask (``GENERIC_ASK_RE``), with no other words, and at least one is a no-op phrase. Anything else, a
    label ("Prod: what now?"), a scope ("on staging") or one extra word, is a real ask: a wrong "real"
    only lets an item through, a wrong "no-op" hides a question from Jack. ``has_real_question`` (another
    question of the item is real) also clears a text whose no-op phrases all say "nothing else ...":
    "else" means something did. Shared fixture: ``tests/fixtures/no_op_rule.v3.json``."""
    if not isinstance(text, str):
        return False
    sentences = _no_op_sentences(text)
    no_op = [s for s in sentences if NO_OP_SENTENCE_RE.fullmatch(s)]
    if not no_op or any(s not in no_op and not GENERIC_ASK_RE.fullmatch(s) for s in sentences):
        return False
    return not (has_real_question
                and all(m.group(0).lower().startswith("nothing else") for s in no_op for m in NO_OP_RE.finditer(s)))


NO_OP_PROBLEM = ("This says nothing needs Jack: complete the card with a one-line summary instead, or tag it "
                 "[captain:<topic>] for Captain triage")


def check_no_op(kind: str, payload: dict, *, title: Any, summary: Any, ask: Any, why: Any) -> None:
    """Refuse a ``design_decision`` whose own text says nothing needs Jack and asks nothing real
    (``is_no_op_text``): there is nothing to answer."""
    if kind != "design_decision":
        return
    questions = payload.get("questions")
    asked = [q.get("question") for q in (questions if isinstance(questions, list) else []) if isinstance(q, dict)]
    real = any(isinstance(q, str) and q.strip() and not is_no_op_text(q) for q in asked)
    for i, question in enumerate(asked):
        if is_no_op_text(question):
            raise fix(f"payload.questions[{i}].question", NO_OP_PROBLEM, EXAMPLE_QUESTION)
    texts = [("title", title, EXAMPLE_TITLE), ("summary", summary, EXAMPLE_SUMMARY), ("ask", ask, EXAMPLE_QUESTION),
             ("why", why, EXAMPLE_SUMMARY), ("payload.rec_reason", payload.get("rec_reason"), EXAMPLE_REC_REASON)]
    for where, text, example in texts:
        if is_no_op_text(text, has_real_question=real):
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


# Words a risk note can be made of and still say nothing beyond its level (Jack, Nov 17: "You should be adding an
# explanation on the risk level for every Action Items card"): "Medium risk.", "low", "Risk: high", "Low risk
# change". Any other word ("Low risk: docs only") explains something. The same list as JackRKelly/assistant
# ``scripts/action_item.py`` ``LEVEL_ONLY_WORDS``, whose app derives a reason for an older item with such a note.
LEVEL_ONLY_WORDS = frozenset({"low", "medium", "med", "high", "moderate", "minimal", "minor", "risk", "risky",
                              "level", "rated", "rating", "is", "it", "its", "this", "the", "a", "an", "overall",
                              "very", "fairly", "quite", "pretty", "relatively", "change", "item"})
_WORD_RE = re.compile(r"[a-z]+")


def restates_level(note: str) -> bool:
    """True when ``note`` is empty or only restates the risk level (``LEVEL_ONLY_WORDS``)."""
    return all(word in LEVEL_ONLY_WORDS for word in _WORD_RE.findall(note.lower()))


def check_risk_note(risk: Any, where: str = "risk.note") -> None:
    note = risk.get("note") if isinstance(risk, dict) else None
    if not isinstance(note, str):
        return  # the envelope schema reports the missing or mistyped note
    # The app shows the note beside the level on every card, so it must say why: never empty, never the level again.
    # These two refusals are word for word the JackRKelly/assistant CLI's (``action_item.risk_note_refusal``), so an
    # agent gets one answer whichever way it parks; both repos' tests assert the full strings.
    if not note.strip():
        raise fix(where, "required, one line saying why this level", EXAMPLE_RISK_NOTE)
    if restates_level(note):
        raise fix(where, f"{note.strip()!r} only restates the level; say why in a few words", EXAMPLE_RISK_NOTE)
    if "\n" in note or "\r" in note:
        raise fix(where, "must be one line", EXAMPLE_RISK_NOTE)
    check_chars(note, RISK_NOTE_MAX_CHARS, where, EXAMPLE_RISK_NOTE)


def check_new_envelope(*, title: Any, summary: Any, risk: Any) -> None:
    """The add-time rules on the envelope fields every kind shares."""
    check_title(title)
    check_summary(summary)
    check_risk_note(risk)
