# Protected-owner native adapter: source implementation and remaining gate

**SOURCE ONLY. Production protected admission is unconditionally disabled.**
Baseline: `75d15c1a53d74ecce6f3cc094cb0bd9bf4238fb4`.
No deployment, canonical profile/state, credentials, accounts, root commands,
card release, admission or scheduler activation is authorized by this artifact.

## Accepted direction versus implemented scope

The conversation loop may live in a protected inference process ONLY if that
process cannot execute untrusted local code. Provider capability, lifecycle
owner and untrusted tool child remain separate authorities. This direction is
accepted for source design, **not isolation or deployment acceptance**.

This source adds a **restricted conversation subset** at the actual
public `AIAgent.run_conversation`, outer and inner conversation-loop entries,
and `AIAgent._execute_tool_calls` boundaries. It is not a replacement scheduler or
control plane. `ProtectedTurn` consumes Hermes' actual `NormalizedResponse` /
`ToolCall` provider interface. Its frozen two-tool schema exposes only the
existing zero-argument nonsecret `read_file` and `terminal` source sentinels.
It does not provide arbitrary file/terminal support or the full AIAgent feature
set. Unknown tools/args, extra conversation options and incomplete responses
fail closed, with no ordinary registry/inline fallback. Sequential, concurrent,
segmented and inline-resolver entry points explicitly refuse protected agents.

The restricted route runs BEFORE the ordinary turn's credential refresh,
transforms, hooks, context construction, auxiliary calls and prelude. It never
invokes memory, delegation, MCP, browser, skill or provider plugins. Ordinary
managed workers do not select this source route; their existing path is retained.

## Bootstrap/import freeze

`hermes_protected.freeze()` is an irreversible cooperative source latch, usable
only in a fresh `-I -S -B` interpreter before bootstrap/config/agent imports. It
rejects prior third-party/source discovery and unexpected import machinery.
A closed source-module list admits only the restricted conversation and shared
provider types. Other modules (including `run_agent`, `model_tools`,
`agent.tool_executor`, plugins/hooks, auxiliary/memory/delegation/MCP/browser,
canonical state and owner RPC) are denied BEFORE execution. The agent package
skips its normal optional parser preload under this latch. Stdlib acceptance
uses the resolved spec origin, not a name allowlist; a workspace stdlib shadow
is rejected before its sentinel side effect executes.

**This is not a Python sandbox.** Same-UID writable source/interpreter/stdlib,
explicit loaders, native calls, debugging and arbitrary already-trusted callback
code are not confined by an import finder. Production needs immutable runtime
inputs and kernel-enforced identities. No env/profile/client switch enables
this latch or promotes readiness.

## Native lifecycle and inference interfaces

`NativeLifecycle.reserve` uses the REAL existing
`reserve_admission_worker`, producer claim digest and native adoption contract.
The live direct Popen child, PID/birth, principal, admission, session,
generation and owner epoch are checked. A real isolated Kanban policy/claim
supplies a trusted owner-side DB/context; its board/task/run must equal the
admission's durable `kanban_card_v1`. The card and run must still be running,
current, unexpired and carry the exact claim lock.

Canonical DB/context, claim lock, native adoption secret and general worker RPC
remain lifecycle-owner-private. They NEVER enter the tool child's fixed
stdin/stdout protocol. A different random token authenticates each inference
and lifecycle endpoint; channel, complete binding, strictly typed monotonic
sequence, payload size, operation count and deadline are enforced. Inference
has only `complete`, with a preselected trusted callable; no provider name,
credential, URL, tool execution or configuration operation exists. Lifecycle
has only `check` and bounded `append`, mapped to native `transcript.append`
receipts. No child reserve/adopt, card complete, lease renewal, session targeting
or general RPC crosses the interface. The owner-owned `protected_turn_scope`
performs blocking bind/reservation and teardown off-loop, shields acquisition
through cancellation, and always attempts native retirement and reaps the
worker. A turn error eagerly closes its child; the enclosing owner scope still
retires its reservation. Cleanup uses native loss/revocation, never a fabricated
completed admission. Failed storage cleanup denies work and is retryable through
the direct lifecycle handle. The scope currently propagates a retirement storage
failure and drops that handle; same-epoch recovery/retry delivery is not yet
provided. This source scope is fixture-only until that residual is addressed.

The source test uses authenticated **in-process callable interfaces**, NOT
separately protected service identities or a production transport. The inference
callable is explicitly a **controlled NO-REAL-PROVIDER fixture**, returning the
actual provider response type. No live provider SDK/auth success is claimed.
These interfaces validate/recheck deadlines; a synchronous trusted callable can
still block while executing. Hard transport I/O deadlines, peer identity,
FD transfer, cancellation and per-service credential placement remain required.
Cancelling an awaiting coroutine does not stop an already running `to_thread`
inference callable; the fixture checks reject its late result but do not cancel
that call or close the inference endpoint. Concurrent eager/scope child teardown
also remains a hardening gap. No non-fixture caller or stronger cancellation
acceptance is authorized by these interfaces.

Before tools, the lifecycle owner durably appends the exact assistant call row;
results/final text use the same scoped native receipts. Native/card fencing is
rechecked before and after inference and each tool. This rejects a result after
card replacement, generation/epoch loss, admission revocation or Stop. It does
not atomically couple the two SQLite databases, eliminate the last-check/tool
race or prove cancellation of an already executing arbitrary tool descendant.
A successful restricted turn does NOT settle its admission or complete/release
a card. Closing its experimental reservation records the native loss outcome
(unknown admission / terminal worker), not success. On a replaced generation or
epoch the old owner does not overwrite state; ordinary new-owner recovery owns
that disposition. The subset starts fresh, accepts no prior history, and a failed
tool round can retain a call row without a result until unknown/discard recovery.

## Actual subprocess path retained

The original seven tests remain. Real
`gateway.session_managed_worker._spawn_registered_worker` -> fixed
`agent/managed_worker.py --sentinel-tools-v1` -> native regular-file primitive
and real `/bin/cat` still use cancellation-safe acquisition, native registry,
owner scope, per-worker Stop event/writer and reap/deregistration. `-I -S -B`,
locale-only environment, stdio-only descriptors, empty pass-fds and a private
process group are enforced. Frames are strict/bounded/replay-resistant; only
fixed `allowed.txt` is read, with no-follow/oversize refusal. File, high-FD,
directory and Unix-listener negative controls prove cooperative exec sanitation,
NOT protected kernel admission.

The original tool child's name-level module assertion still does NOT detect a
stdlib-named repository shadow; the new origin-based freeze is a separate
inference-side negative control, not a fix to that carried tool-child limitation.
Writable bytecode caches also remain runtime inputs: `-B` suppresses writes,
not reads. Neither source assertion is immutable-runtime attestation.

## Exact technical gap (no administrator command is ready)

The full `run_agent` / `conversation_loop` import closure still loads ordinary
bootstrap, tool discovery and broad phase helpers. Therefore a genuinely frozen
fresh interpreter **refuses importing the ordinary AIAgent implementation**.
The controlled integration fixture imports the existing ordinary modules first,
constructs an uninitialized AIAgent carrying only the restricted route, and
exercises the real public facade and loop/batch methods plus an actual
SessionAuthority, card/claim, reservation/adoption, child and DB contracts. That is
routing evidence, **not proof that the full AIAgent constructor can safely run
with provider capability**. The fresh-process import-freeze test is a separate
negative control and must not be represented as the same end-to-end process.

Before production, select one bounded source completion:
1. Move the restricted loop/batch entry behind an import-pure public facade,
   with no ordinary bootstrap/import closure; keep optional AIAgent semantics
   explicitly unsupported rather than restoring fail-open hooks.
2. Supply separate authenticated, bounded inference and lifecycle IPC adapters
   with pinned peer identity; move real provider client construction behind the
   reviewed inference service interface. Do not forward generic owner RPC.
3. Connect that facade to the existing native owner's protected reservation,
   pre-spawn Stop latch and immutable launch plan; bind the trusted policy from
   the owner's persisted admission, never inference/tool-authored context.
4. Implement/review the single-purpose root-owned fixed entry and protected
   registry with immutable interpreter/stdlib/source/dependencies/policy.

The reused native adoption claim currently checks same-UID process ownership;
it cannot be reused unchanged for a separate-UID tool child. Replace that proof
with protected launcher/registry attestation before an identity split. Trusted
callables and agent-class dispatch must be pinned, with no subclass or instance
override accepted by the future protected facade.

Only that candidate can define the privileged sentinel gate. Real/effective/
saved IDs, nonempty nonzero supplementary groups, memberd opt-out, privilege-
regain negatives, ACL/read-only reviewer identity, filesystem/service/network/
debug denial and arbitrary/detached-descendant cancellation remain unimplemented
and unproven. No sudo command, account/group change or virtual readiness marker
is supplied. Effective leaf limit remains **250**, not an asserted 10.

## Reproduction and CI

Run the canonical wrapper with an isolated developer interpreter and scratch
HOME/HERMES_HOME/TMPDIR:

```
scripts/run_tests.sh tests/gateway/test_protected_conversation.py tests/gateway/test_protected_turn_scope.py tests/gateway/test_protected_turn_validation.py tests/gateway/test_protected_tool_adapter.py
```

The separate `ci/native-protected-controller-20261031` branch carries the
registered `linux-contract-probe.yml` path as a narrowly scoped manual
no-provider diagnostic: one Linux and one macOS job, exact source-SHA checkout,
source ancestry/expected-tree/hash evidence, 51 focused native and ordinary-agent
regression files (including public turn, tool persistence/inline, memory profile,
guardrail, and unchanged managed-worker cases)
and `scripts/check`. No diagnostic-controller workflow is present in the source
PR's final tree, preventing an accidental default-branch workflow replacement.
The diagnostic
controller/ref and tested source SHA are separate evidence fields. A diagnostic
pass is not an assertion that an empty PR check rollup is green. No default-
branch, full-ci, Windows, publish, deployment or secret workflow change is needed.

Current external CI blocker: GitHub's permissions API reports Actions enabled,
but dispatching the individually enabled registered workflow returned HTTP 422,
`Actions has been disabled for this repository.` The authenticated Actions UI
shows a fork-wide disabled-workflows gate. Its global re-enable button would
also expose unrelated inherited workflows/schedules and was NOT clicked. The
single diagnostic workflow was restored to its original disabled state. Zero
new source-bound runs/checks exist; Linux evidence is absent and CI is NOT green.
Fork-wide activation is a separate owner authorization/safe-containment gate,
not permission to change main or run the full inherited CI/publish graph.

A separate broader local agent/gateway diagnostic is not green: compaction
snapshot failures and the absent optional ACP dependency also reproduce on the
pinned baseline (in a checkout differing only by the controller workflow).
Those are not hidden as passes or silently repaired in this source slice. The
focused diagnostic is deliberately bounded to the changed and adjacent contracts;
it does not represent the full suite or production-provider coverage.
