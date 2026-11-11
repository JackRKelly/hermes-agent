# 0001. Action item envelope v2: how the version is marked, and v1 compatibility

## Context

KINDS_VERSION 2 (card t_bfb520b6) adds a required human `title` and `summary` to every new Action
Item envelope and a multi-question `design_decision` payload (schema v2). The card fixes the rules
(limits, banned list, required at add/update, optional on read, "bump the envelope schema version
and keep v1 readable") but leaves open how the envelope version is marked and how stored phase-1a
items keep working. On Nov 11 the live board already holds v1 `design_decision` items, some
decided through the web app, whose audit rows carry their `binding_hash`.

## Decision

1. The envelope schema file becomes `envelope.v2.schema.json` (`ENVELOPE_VERSION = 2` in
   `registry.py`). It is a superset of v1: `title` and `summary` are optional properties, so a v1
   envelope still validates. No version field is added to the stored envelope and the comment
   prefix stays `[action-item:v1] `: the prefix is part of the hash16 byte contract shared with the
   assistant repo, and changing it would orphan every stored item. A v2 envelope is recognised by
   carrying `title`. The payload keeps its own `schema` number (design_decision is now 2).
2. The card-guide rules (word and sentence counts, the 120-char risk note, the banned list) are
   add-time only (`readability.py`, called by `build_envelope`). Read-time validation keeps only
   the schema caps, so nothing stored earlier stops loading.
3. `design_decision.binding_fields` stays `("question", "option_set_hash")`. For v2, `question` is
   absent (null in the binding) and `option_set_hash` covers every question. Changing the fields
   would move the binding hash of every stored v1 item and void its recorded decisions.
4. `legacy_questions` returns a read-only projection (`questions` with possibly empty `options`,
   plus `source_text`), not an agent payload: it is never stored or hashed, so it need not satisfy
   the v2 schema's 2-4 options rule.
5. Actions follow the stored payload schema (`KindSpec.schema_actions`). A v2 `design_decision`
   takes `answer`, `snooze` and `later`. A stored v1 one keeps phase 1a's `pick_option` and `other`
   (the app that is live today decides it that way) and also takes `answer`, so the v2 app can
   answer it through `questions_of`. Neither takes `reject`.
6. An answer is its own decision comment, verified like an approval in `action_item_decision.py`:
   `[jack-answer:<id>] "<readable answer>" answers=q1:<option>,q2:other source=webapp:<audit_id>
   binding=<hash>`. `answers=` must name exactly the item's question keys, each with one of that
   question's option keys or `other` when the question allows it. The audit row carries
   `verdict: "answer"`, the same `answers` map and the same `note`. An Other answer's words live
   only in the note, so binding the note binds them. `action_item_get_decision` returns `answers`.

## Consequences

- The dashboard plugin and the assistant scripts read v1 and v2 envelopes with the same code path
  and decide "has a human title" by the field's presence.
- `action_item_update` on a v1 item must pass `title`, `summary` and, for a v1 `design_decision`,
  `questions`: the refusal says so with an example.
- The assistant app writes the answer comment and its audit row in exactly the form of point 6;
  its gate check (`kanban_gates.py`) treats `[jack-answer` like `[jack-approved`.
- If a future change needs a hard envelope marker, it adds a field under a new KINDS_VERSION; the
  prefix still never changes.
