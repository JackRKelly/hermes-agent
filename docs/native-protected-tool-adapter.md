# Protected-owner native adapter: source integration and privileged gate

**SOURCE ONLY. Production protected admission remains unconditionally disabled.**
Baseline: `75d15c1a53d74ecce6f3cc094cb0bd9bf4238fb4`.
No installation, live provider request, credential access, profile/state migration,
card release, root/account/group/ACL/network change or scheduler activation is
part of this artifact. The controller/CI branch is a separately owned artifact.

## Implemented conversation path

`protected_process_scope` in `gateway/session_protected_process.py` now composes
one actual path, rather than two disconnected routing/import fixtures:

1. Read the canonical owner's durable `local_receipt` and current admission.
   Require exact profile, principal, target session, generation, current epoch,
   persisted Kanban context, workspace and explicitly selected model/provider.
   Missing policy or a substituted owner context refuses **before any spawn**.
2. Reuse the native pre-spawn Stop latch, cancellation-safe process acquisition,
   managed worker registry and `SessionAuthority.adopt_agent` Stop delivery.
   Bind/reserve the real fixed tool child through `reserve_admission_worker`,
   native producer claim/adoption and the existing transcript receipt contract.
3. Launch the fixed source `hermes_protected_entry.py` in a NEW `-I -S -B`
   interpreter with a locale-only environment, closed inherited descriptors and
   a private process group. It calls `hermes_protected.freeze()` BEFORE importing
   the conversation facade. It never imports ordinary AIAgent/bootstrap,
   `model_tools`, hooks/plugins, auxiliary models, memory, MCP, browser/computer,
   provider selection or canonical state.
4. The import-pure `ProtectedConversation.run_conversation` consumes the existing
   `ProtectedTurn`, `NormalizedResponse` and `ToolCall` contracts. It has no
   AIAgent constructor, subclass/instance dispatch override, ordinary inline tool
   route or host fallback. It exposes the frozen two zero-argument sentinels only.
5. The fresh conversation opens three independent authenticated Unix channels:
   inference `complete`, lifecycle `check`/`append`, and fixed tool `call`.
   Both ends verify kernel peer PID and UID on each connected socket
   (`SO_PEERCRED` on Linux; `LOCAL_PEERPID`/`LOCAL_PEERCRED` on macOS), not a
   caller-supplied hello. A channel-specific secret, exact native binding,
   immutable-policy digest, owner PID, reserved tool PID/birth, strictly typed
   monotonic sequence, 64-request cap, five-second endpoint deadline and 64-KiB
   length-prefixed JSON bound are enforced. Duplicate keys/nonfinite constants
   and oversized/truncated frames refuse. One socket may claim each channel.
6. Owner-side inference uses `SelectedAsyncProvider`: ONE explicitly supplied
   async SDK create method, close method and Hermes `ProviderTransport`, with the
   model/provider pinned to persisted policy. There is no SDK/auth/discovery or
   fallback import in the protected child, and no model-supplied provider/URL/key
   or generic method selection. The owner ALSO checks the exact frozen tool
   schema and native/policy fences before and after inference. The owner can use
   the existing `ChatCompletionsTransport`; the regression exercises that real
   transport and SDK response type with a controlled NO-NETWORK async creator.
7. The lifecycle owner remains the only holder of canonical DB/context, claim
   lock, adoption secret and native mutation authority. None goes to the tool
   child. Its board/task/run must match durable `kanban_card_v1`; current task/run,
   unexpired claim lock, process birth, assignment and epoch are checked around
   every inference and tool. Policy is re-read and digest-checked. There is no
   worker registration/adoption, card completion, lease renewal, session targeting
   or arbitrary owner RPC on these channels.
8. The fresh process's owner-private `OwnerObservations` ledger is the transcript
   source of truth: the owner seeds the exact prompt, accepts only its committed
   history and fixed tool schema for inference, records the actual normalized
   provider response, and authorizes only that response's next ordered tool call.
   Observed tool results are bound to that exact call ID/name. Lifecycle append
   must supply the next observation correlation and byte-equivalent JSON rows;
   forged/replayed/out-of-order or unobserved rows refuse before native mutation.
   The owner constructs the final result from committed evidence, not child stdout.
   A capability token authenticates a channel, never the truth of child claims.
   Legacy in-process fixtures below retain their explicitly trusted content path;
   they must not be used as an untrusted-child boundary.

The existing real public `AIAgent.run_conversation`, outer/inner loop and batch
interceptions are retained as compatibility/routing controls. They bypass normal
credential refresh, transforms, prelude, hooks, context and auxiliary calls, and
all ordinary executor bypass entries deny protected agents. **Ordinary managed
workers do not select the protected source route and retain their existing path.**

## Cancellation and retirement

The new process scope does not run provider calls in `to_thread`. The owner
schedules the selected SDK coroutine on its actual event loop. Cancellation or
native Stop revokes endpoints before cancelling/draining the actual call and
closing the selected client, shuts down IPC, terminates/reaps the fresh child,
and retires/reaps the native tool worker. The stdout reader is owned/drained,
server threads are joined, and concurrent eager/scope close shares one close
operation. Close waits for an in-flight acquisition, including a late unpublished
Popen handle, before reaping. Running after close cannot acquire anything. Cleanup
attempts every resource even after a fault, retains exact failed handles, and
retries only unfinished resources on a later `close()` or an owner-only
`retry_protected_process_closes` sweep.
The fresh child is signalled through its owned process group, not PID alone.
Provider close errors remain visible while child cleanup still runs.
Provider ownership includes failed preflight/acquisition, not just a yielded
scope. A provider implementation must actually support async cancellation and
client closure; this source fixture does not establish that contract for every
SDK or an uncooperative arbitrary coroutine. Provider revocation wakes IPC future
waiters immediately; owned children/IPC are reaped before a bounded one-second SDK
close/drain. An uncooperative task remains retained for explicit owner retry and
raises `provider_close_timeout`; it cannot block native retirement. A teardown
fault (including that bounded timeout) takes precedence over `CancelledError`;
a successful teardown propagates cancellation. This priority reports failed
cleanup rather than silently claiming it completed. An SDK `aclose` that itself
raises cancellation is reported as the fault `provider_close_cancelled`, never
as cancellation of the owner; explicit retry can replace that cancelled SDK
close task. The outer scope's provider-close fault takes priority if both it and
a process-cleanup fault occur; retained resource handles still require retry. No caller-cancel retry can
turn the provider-drain bound into an unbounded gather.

`SentinelWorker.close` is serialized and idempotent across threads; a stop-request
flag is not mistaken for completed pipe teardown/reap. The native registry entry
is removed only after the child is reaped, and only if it still names this worker.

Native retirement storage failure retains the exact lifecycle owner in the
canonical authority's owner-private pending-retirement registry. It stays
revoked (`_closing`) and reachable. `retry_protected_retirements(authority)`
retries the SAME native assignment/delivery, retains failures, and removes a
handle only after success or a recognized stale-generation/epoch disposition.
Call this owner-only retry before shutting down the authority DB after a failed
scope; it is never exposed to model/tool frames. Real full-path and direct scope
regressions cover retirement failure/retry, repeated cancellation, actual
in-flight call cancellation, close faults and concurrent eager/scope close.

The older `protected_turn_scope(..., complete=callable)` and `ScopedInference`
remain **legacy controlled in-process fixtures only** to preserve the original
routing controls. A synchronous arbitrary callback cannot be forcibly cancelled
by Python; these are not the new cancellable process entry and must not be used
as a production provider adapter. The process scope is the integrated source
entry for the selected async adapter. No production caller is enabled for either.

## Persisted outcomes are deliberately not promoted

A restricted conversation durably appends its user/call/tool/final rows through
native `transcript.append` receipts. It starts fresh, accepts no prior history,
and uses bounded single-turn/tool budgets with whole-batch validation and
turn-wide call-ID replay refusal. Unknown capabilities disable before discovery.

A successful fixture conversation does **not** settle its admission or release a
card. Its experimental reservation closes through the existing native loss
contract: unknown admission / terminal worker, not fabricated success. No
`owner_admitted`, `worker_bound` or privileged-ready event is synthesized. The
real dispatcher/drain settlement and protected admission cutover remain gated;
this source conversation is not an activated working Kanban deployment.
Generation/epoch replacement is not overwritten by an old owner. The two SQLite
stores are not atomically coupled, and last-check/tool races or arbitrary already
executing detached tool descendants are not claimed solved.

## Original subprocess and FD controls retained

The original seven tests still exercise `_spawn_registered_worker` -> fixed
`agent/managed_worker.py --sentinel-tools-v1` -> native regular-file open and real
`/bin/cat`. `-I -S -B`, locale-only environment, stdio-only inherited descriptors,
empty pass-fds, private process group, strict bounded/replay-resistant frames,
fixed `allowed.txt`, no-follow and oversize refusal are retained. File, high-FD,
directory and Unix-listener negatives establish cooperative exec sanitation,
**not kernel containment**. The conversation child's newly connected sockets
are not inherited by this tool child.

The old tool child's name-level import assertion still does not prove immunity
to a stdlib-named source shadow. The new conversation's origin-based import guard
is not a repair of that tool-child limitation. Writable bytecode is also a
runtime input: `-B` suppresses writes, not reads.

## Exact irreducible privileged boundary (no administrator command ready)

The fresh-process facade, real bounded IPC and selected-provider source shape are
now present. That does NOT make the cooperative import latch a Python sandbox.
Same-UID writable source/interpreter/stdlib/dependencies/policy, explicit loaders,
native calls, debugging and arbitrary trusted provider implementation remain
outside its enforcement. Runtime inputs are not immutable across identities.

The existing native adoption verifier assumes same-UID ownership. A root-owned
fixed launcher/registry must attest the exact reviewed entry, immutable runtime
and policy, caller, native reservation/generation/epoch, and spawned process
identity before a cross-UID tool child can be adopted. Reusing the current
same-UID claim unchanged, accepting a role-selected workspace/entry, installing a
setuid Python helper, or recording a synthetic admitted event is NOT a substitute.
No such launcher is installed or run here. Its source/protocol still requires
independent implementation/review; this is a technical boundary, not merely an
administrator toggle.

Privileged acceptance additionally requires real/effective/saved UID/GID and
nonempty nonzero supplementary-group proof, memberd opt-out, privilege-regain
negatives, immutable/read-only reviewer inputs, filesystem/network/service/debug
negative controls and arbitrary detached-descendant cancellation under the actual
kernel identities. Reviewed provider construction/credential placement and real
SDK cancellation remain separately scoped gates. No root/account/group/ACL or
service/network change is authorized. Effective leaf limit remains **250**, not 10.

## Reproduction and evidence

Use an isolated developer interpreter, disposable HOME/HERMES_HOME and scratch
TMPDIR, and retain the canonical wrapper's real platform skips and fixed budgets:

```
HERMES_TEST_FILE_RETRIES=0 HERMES_TEST_FILE_TIMEOUT=120 scripts/run_tests.sh -j 4 tests/gateway/test_protected_conversation.py tests/gateway/test_protected_turn_scope.py tests/gateway/test_protected_turn_validation.py tests/gateway/test_protected_tool_adapter.py tests/gateway/test_protected_fresh_process.py tests/gateway/test_protected_ipc.py tests/gateway/test_protected_retirement_retry.py tests/gateway/test_protected_process_close.py tests/gateway/test_protected_observations.py tests/gateway/test_protected_provider_close_fault.py
```

The fresh-process full-path tests are explicitly controlled **NO-REAL-PROVIDER**
fixtures, including the existing transport/SDK-response control. A real-listener
helper test rejects deliberately mismatched expected PID/UID; it is NOT a
non-child process presenting a valid channel token. Actual channel negatives
cover cross-channel token/binding substitution,
replay, oversize, revocation, generic RPC, substituted inference tool schemas,
forged history/append, unobserved tools and type-only owner fault diagnostics.
No Linux SO_PEERCRED execution is claimed without separate Linux evidence. The
macOS LOCAL_PEERPID check is not fd-transfer-resistant audit-token attestation.
They are not credential, SDK-network-success or privileged containment evidence.

Regressions for all existing named roots (`tests/agent`, `tests/gateway`,
`tests/tools`, `tests/cron`) must be bound to the final immutable head. The
reviewer's named `tests/run_agent/` root does not exist in this checkout; the real
facade suite is `tests/agent/test_run_agent*.py` and is included by `tests/agent`. A
failed broad run is not a pass: missing optional dependencies, real baseline
failures and introduced failures must be classified with actual reruns/baseline
witnesses, not exclusions or changed assertions. Focused passes are not the full
repository suite. CI/controller state is owned and reported separately; neither
an empty PR check rollup nor an old head's passing checks is current-head CI.
