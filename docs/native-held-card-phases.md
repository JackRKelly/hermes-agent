# Native held-card phases (trusted Python API)

This opt-in mode separates **finite SDK execution completed** from **card workflow
completed**. `NativeAdmissionBridge(..., phase_policy=None)` retains the ordinary
completion behavior. A registered held card cannot downgrade to that default.
There is no config/env/row flag, model-facing tool, or public phase RPC.

## Bootstrap and ownership

Prepare through the canonical CLI before opening owners:

```sh
hermes kanban --board "$BOARD" create "$TITLE" --assignee "$BUILDER" \
  --initial-status blocked --max-retries 0 --json
hermes kanban --board "$BOARD" show "$TASK_ID" --json
```

Creation and readback must both show BLOCKED with `max_retries=0`, no run or
claim. Zero is accepted generally by CLI creation as a zero-failure threshold,
not unlimited retries: existing recomputation/reapers keep an initially blocked
zero-retry card blocked even before a failure. Negative, boolean and noninteger
values remain invalid; omitted and positive thresholds retain their behavior.
The signed held claim below is the only activation seam for this workflow. Do
not replace zero with one or expose a READY window.

```python
from gateway.session_card_owner import card_owner_runtime
from gateway.session_protected_phase import (
    HeldPhasePolicy, install_held_card, native_phase_bootstrap_expected,
    native_phase_expected, native_phase_commit, native_phase_readback,
)

# All these bindings come from the existing signed CampaignStore graph and
# trusted native mapping, NOT a model response. Paths are operator parameters.
policy = HeldPhasePolicy(
    graph_digest=graph_digest, campaign_key=campaign_key, card_id=card_id,
    board=board, task_id=task_id, role="builder",
    implementer_profile=implementer_profile, reviewer_profile=reviewer_profile,
    input_head=baseline_head,
)
app = {"app_id": verified_app_id, "login": verified_app_bot_login}

def install(authority):
    expected = native_phase_bootstrap_expected(
        authority, policy=policy, controller_started_at=original_started_at,
        publication_identity=app,
    )
    # Existing OperatorAuthority signer/verifier; no alternate signing scheme.
    install_held_card(
        authority, policy=policy, bootstrap_envelope=operator.sign(expected),
        verify=operator.verify, ledger=original_request_ledger,
        controller_started_at=original_started_at, publication_identity=app,
    )
    # Install the existing NativeAdmissionBridgeFactory here, BEFORE recovery.
    # Its build(ref, canonical_started_row) constructs/signs a real bridge with
    # the same ledger, verifier, original start and exact current input head.
    install_signed_native_bridge_factory(authority)

async with card_owner_runtime(
    profile_home=profile_home, kanban_home=kanban_home, install=install,
) as owner:
    # owner.connection is a real AuthorityConnection backed by a redeemed
    # native TicketStore grant; not a fabricated Principal/namespace.
    identity = owner.identify()
```

`install_held_card` takes three dependencies beyond the originally proposed
minimal signature: **ledger, controller_started_at, publication_identity**.
The first canonical claim precedes any bridge/admission; it cannot safely invent
a budget or infer an App actor from worker JSON. Both owner bootstraps sign these
same original bindings. The ledger is the existing broker ledger (native check
protocol or existing snapshot adapter), not a new core store.

The installer runs synchronously after the actual epoch exists and before native
recovery/adoption. An installer exception prevents publication. Named profiles
must be absent from two stable parent-control `identify` served-set snapshots,
and their real `ProfileOwnership` reservation must be released. Startup never
forces an unserve, changes profile flags, replaces a running owner, or reads a
root messaging token. Only the selected home is reserved and advertised; no
messaging adapters, cron service, hooks, global dispatcher, or multiplexer starts.
Producer admission deliberately skips viewer `resume` inventory/provider probes.

Cross-role claims use actual in-process owned authority handles, not a serialized
owner DB path or a new RPC relay. The predecessor must still be owned at its
signed epoch, on the same board, graph, original controller clock and pinned App.
Run both selected owners in the trusted controller process for a two-role loop.

Context exit performs native Stop/join, protected process/provider/receipt
retirement, scoped control-socket removal, DB close and reservation release.
Cleanup is cancellation-drained. If physical retirement fails, the exact handles
and flock remain retained. Only `await owner.retry_retirement()` retries those
same handles; it cannot replace them or start another owner.

## Claim, execute, observe, publish

```python
scope = native_phase_expected(
    owner.authority, board=board, task_id=task_id, admission_id=None,
    action="claim_builder", published_head=None, publication=None,
)
context = native_phase_commit(owner.authority, envelope=operator.sign(scope))
# context: {board, task_id, run_id, claim_lock, db}
from gateway.session_kanban import run_task
admission = await run_task(owner.connection, context)
# Observe/join the canonical drain; do not synthesize a started row.
readback = native_phase_readback(owner.authority, admission_id=admission_id)
```

The SDK closes, tools are revoked, allowlisted bytes are exported and the real
finite native receipt/transcript are committed. The card is **BLOCKED**, with its
exact current run/claim retained. A safety-hold commit precedes the cross-WAL
native receipt projection: an ambiguous native/board write fault stays held,
without refund, automatic retry or inference replay. `run_task` retry returns the
existing admission, never another model request. Missing projection requires
explicit operator reconciliation; it is not treated as success.

Readback is `protected_phase_readback_v1`: exact native/card/claim/source/typed
policy, finite receipt, result/transcript/export digests, bounded artifact bytes,
and actual owned namespace-retirement proof. `sdk_policy` reports the actual
owner-selected SDK provider/model/base URL/accounting mode after successful
finite execution; it is not the ordinary agent's billing-route projection.
Consumers must check it against the signed role and source binding, not accept
a requested provider or worker transcript field as executed provenance.
It rechecks the canonical owner,
board, native terminal row, transcript, pinned CID/image/daemon and real process
teardown. Finite completion is never presented as card DONE.

## Signed phase actions

Use the same `native_phase_expected` / `native_phase_commit` pair for every
operation. `publication` is **data only**; the existing deterministic publisher
must independently verify the real GitHub App actor, review/delivery identity,
state and exact commit before signing. Model prose/JSON/verdict alone authorizes
neither publication nor transition.

Publication data has `head`, `state`, `actor={type:"Bot",app_id,login}`,
`publication_id` (PUBLISHED) or `review_id` (review decisions), and
`delivery_signature`. IDs are positive integers. App identity must match the bootstrap pin.
The operator envelope binds the whole publication, original policy/clock,
current owner/epoch, exact current/last run/claim, sequence, predecessor digest,
native execution/receipt/transcript/result/export digests, next role and head.
Nonce/expiry verification remains the existing pinned verifier's responsibility;
core also rejects expired signed scopes and exhausted/unknown original budgets.
Both owners must share the same ledger/callback object. A pinned monotonic deadline
is carried across owners and bridges: clock rollback or a new role cannot renew it.

| Action | Required local state | Canonical effect / committed lane |
|---|---|---|
| `claim_builder` | Prepared BLOCKED, no live claim | Canonical new builder run, atomically running+claimed |
| `request_review` | Builder/repair finite receipt + `PUBLISHED` exact-head delivery | Same builder run ends `review_requested`, authentic implementer/reviewer event, BLOCKED `review_held` |
| `claim_reviewer` | `review_held`, actual predecessor owner | New reviewer run, claimed event `source_status=review` |
| `request_changes` | Owner-observed exact JSON `{verdict:"REQUEST_CHANGES",head}` + matching `CHANGES_REQUESTED` bot delivery | Same reviewer run ends, implementer restored, BLOCKED `repair_held` |
| `claim_repair` | Idle `repair_held`, actual predecessor owner | New builder run on the SAME card |
| `approve` | Owner-observed exact JSON `{verdict:"APPROVE",head}` + matching `APPROVED` bot delivery | Canonical completion of the SAME current reviewer run, DONE once |

A model's structured verdict is only an observed prerequisite, never authority.
COMMENT, ambiguous output, wrong head or mismatched delivery stays held.
Canonical review/completion functions own their transactions; private guarded
projection seams accept the held claim and land lanes only inside that single transaction. No
READY/REVIEW idle commit is visible even to pre-protection dispatcher binaries.
Exact duplicate signed actions return their recorded context/receipt; a new
signature over stale scope cannot create a new run. Twelve requests deny another
claim, while approved terminal finalization/duplicate cleanup requires no 13th
request. An authenticated native Stop revokes the held campaign capability in
the existing owner store; stale epoch, owner loss or expiry fails closed.

## Controlled acceptance, not live deployment

`tests/gateway/test_protected_phase_loop.py` exercises two actual card owners,
canonical `run_task`/drains, real Anthropic and OpenAI Codex SDKs against local HTTP
fixtures, actual Docker tool round-trips, finite held readback, reject/repair/
re-review/approve, and DONE once at exactly twelve genuine SDK requests. Every
reservation reads the actual native owner. `test_protected_phase_faults.py` runs
cross-WAL and Stop cases in a separate independently bounded file; neither file
weakens or skips the ordinary 180-second Docker watchdog.
Unsigned/wrong-scope/App/head/CID/epoch, retired predecessor, native Stop,
12-request finalization and cross-WAL projection failures are negative controls.

Every idle hold/handoff runs the actual dispatcher/reapers from immutable
`75d15c1a53d74ecce6f3cc094cb0bd9bf4238fb4` against the SAME controlled WAL and
asserts unchanged claims/runs and no new model POST. Old-source import/runtime
failure fails the probe; it is never counted as fence proof. The fixture requires
that Git object locally; it does not fetch the network. Keep that object in the
source-bound diagnostic checkout. Docker tests use the existing explicit
boundary-test executable/endpoint/base-image parameters, not ambient Docker auth.

These fixtures are not public GitHub reviews, owner budget approvals, hosted
verification, a profile cutover, live credentialed acceptance, merge authority or
installation permission. Whole-candidate independent review and fresh
source-bound hosted diagnostics remain separate gates.
