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

## Consequences

- The dashboard plugin and the assistant scripts read v1 and v2 envelopes with the same code path
  and decide "has a human title" by the field's presence.
- `action_item_update` on a v1 item must pass `title`, `summary` and, for a v1 `design_decision`,
  `questions`: the refusal says so with an example.
- If a future change needs a hard envelope marker, it adds a field under a new KINDS_VERSION; the
  prefix still never changes.
