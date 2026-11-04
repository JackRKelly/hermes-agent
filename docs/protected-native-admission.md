# Native protected async admission

This additive bridge is **disabled unless the native owner constructs and installs an exact capability**. A row/config flag cannot turn it on. The default `NativeSDKPolicy.accounting='strict'` refuses the currently unproven subscription token/USD guarantees before resolving credentials or constructing an SDK.

## Owner integration entrypoint

After native session creation and admission claim, the existing owner/broker constructs:

```python
from gateway.session_protected_admission import NativeAdmissionBridge
from gateway.session_protected_native_sdk import NativeSDKPolicy

sdk_policy = NativeSDKPolicy(
    provider=selected_native_provider,  # 'openai-codex' or 'anthropic'
    model=exact_model,
    session_id=ref.session_id,
    base_url=exact_native_endpoint,
    accounting='subscription_request_bound',
)
bridge = NativeAdmissionBridge(
    authority=authority, ref=ref, row=claimed_native_row,
    launch=trusted_docker_launch, sdk_policy=sdk_policy,
    envelope=signed_admission_envelope,
    verify=operator_authority.verify,  # existing pinned broker verifier
    ledger=shared_broker_request_ledger,
    resolve=resolve_selected_native_runtime_snapshot,
    artifact_names=approved_export_paths,
    controller_started_at=original_controller_started_at,
)
authority._protected_admission_bridge = bridge
# Native execute_managed(...) now selects this exact owner capability.
```

For canonical FIFO claims whose admission IDs/generations are not known at
bootstrap, install `NativeAdmissionBridgeFactory(authority=authority,
build=build_signed_bridge)` on that same owner attribute instead. The trusted
`build_signed_bridge(ref, canonical_row)` callback constructs the exact bridge
above using the persisted card mapping and existing signer/broker. The factory
reads the actual started row in the current owner epoch before calling it,
rejects input/generation/principal/session tamper, caches at most twelve exact
capabilities and never retries a failed build. The callback is trusted bootstrap
Python, not a model-supplied callable/JSON flag, plugin path, environment selector
or command. No generic dispatch or new public owner endpoint is introduced.
`native_protected_readback` works unchanged with either installation form; the
factory readback selects only an already-created exact capability and never
constructs a bridge in response to a readback request.

`bridge.expected_authorization()` is the non-secret exact scope the existing operator signer must approve. It binds native profile/principal/session/admission/generation/epoch/owner instance, current board/task/run/claim, actual input digest, workspace/role/allowed inputs/exports, source commit/tree, immutable runtime image/closure and SDK policy. The signed payload must additionally contain a nonempty `approval_id` and a future timezone-aware `expires_at`. A digest, boolean, unsigned JSON, expired approval or mismatched scope is not authority. The scope explicitly fixes `max_requests=12`, `max_elapsed_seconds=600`, `controller_started_at` and `hard_token_usd_guarantee=False`. This is pilot-only, not a global waiver. The actual owner approval and executable graph signing remain private deployment responsibilities; source fixtures do not manufacture them.

Install this capability only on the explicitly selected candidate profile authority. Once installed, **every managed admission on that authority must resolve the exact capability or refuse**: a fresh session/retry cannot fall through to ordinary host bootstrap. The actual native card must have `max_retries=0`. A card still carrying the former proposal's value 1 is refused before auth/requests.

The production source identity reader requires the current source checkout to be clean and binds its immutable Git commit/tree. Before credentials/SDK construction, the bridge verifies the actual owner-created Docker closure manifest, six source-file hashes, source revision/tree/clean status, immutable image ID and trusted daemon image inspection. Container startup and native lifecycle retain the existing actual daemon/CID/image/generation attestation. `source_identity` injection exists only for explicitly labelled controlled dirty-tree fixtures/deployment manifest readers; it is never accepted through a request row or guest IPC. Normal production integration uses the default reader.

Source identity is strongly rechecked at construction, provider acquisition and
immediately before successful settlement. Within that run, lifecycle/inference/
tool fences use the pinned identity rather than spawning Git processes on each
check. The trusted owner must keep host source immutable; the credentialless
guest has no host-source mount/write access. An immutable Docker test manifest
requires the real clean commit/tree reader and cannot silently fall back to a
dirty-source fixture.

## Native auth and SDK construction

`resolve` is a **trusted owner callback**, not guest JSON. It returns the native owner's selected resolved runtime snapshot: `provider`, `api_mode`, `base_url`, `api_key`, and optional `default_headers`. It must preserve native profile/auth/pool/OAuth/endpoint resolution and must not switch provider/account or fall back. The returned provider, protocol and endpoint must equal the signed SDK policy. Keep this snapshot in owner memory only. This bridge does not introduce a parallel auth store or copy secrets. Native owner bootstrap/account readiness remains separate from controlled tests.

The native owner's ordinary profile runtime/secret scope must already be bound. The bridge checks `current_secret_scope_home()` against its canonical persisted authority profile before invoking the resolver; unbound or sibling-profile auth is refused rather than borrowing ambient credentials.

The async factory does no plugin/tool discovery, never calls the auxiliary sync-to-thread client, reuses native Codex account/identity headers and native Anthropic OAuth/API-key/beta identity rules, and constructs actual `AsyncOpenAI.responses.create` / `AsyncAnthropic.messages.create`. It pins canonical session/cache affinity, native transports and owner-selected request policy. The Responses transport has additive explicit endpoint/issuer inputs so this selected first-party path does not query provider registries; ordinary callers retain their previous defaults. Fresh-process tests fence provider/tool/auth-router imports across actual loopback SDK requests and separately witness ordinary `run_agent`, `tools`, `cron`, `gateway` and `agent` imports.

SDK retries are zero; there is no provider/account fallback, redirects or environment proxy use. Default total request timeout is at most 5 seconds (the separately signed pilot profile below permits at most 10 seconds). The HTTP stream has a cumulative 65,536-byte hard read bound, rejects content encodings other than identity and closes on cancellation. Codex consumes the actual asynchronous SSE Responses stream. Reported model and integer usage must be present and match the selected model before normalized text/tools can reach owner observations. Provider replay/reasoning metadata is deliberately excluded from the narrower protected wire. SDK request/close exceptions are normalized so arbitrary provider response bodies cannot leak through diagnostics.

Reported model matching is **strict string equality**, not alias/date-prefix
matching: the deployment must verify the actual backend returns the exact
selected `gpt-6.1-sol` / `claude-opus-5-5` ID before a live pilot. A different or
missing reported ID freezes after the charged request, without fallback.
Anthropic signed/interleaved thinking blocks are retained only in the owner SDK
closure and replayed unchanged with the exact previously observed tool-call
turn. They never enter the guest response, transcript or export and are erased
on SDK close. OAuth inbound `mcp__` names are mapped only against the closed
protected tool names, never `tools.registry`. Native fixed-endpoint conversion
bypasses unrelated auth/provider discovery; ordinary Anthropic conversion keeps
its previous route policy. Ambient OpenAI organization/project headers are
explicitly omitted. Ledger/readback callbacks are synchronous owner work: a
single-profile pilot must account for that event-loop latency, not advertise
multi-profile throughput guarantees.

`max_output_tokens=1024` is a request preference. Native Codex omits that unsupported wire limit; neither it nor subscription prices prove a strict token/USD cap. The pilot accounting mode explicitly acknowledges that limit. Unknown/strict accounting still denies before credentials/network.

## Shared durable broker ledger contract

### Explicit signed pilot timing (v1)

The five-second default **total conversation** is a sentinel budget, not a
real-provider performance guarantee. Two approximately 2.6-second SDK responses
already exceed it before native tools/readbacks/export. The bounded private pilot
therefore requires a distinct trusted-owner policy, not a worker/config flag:

```python
from gateway.session_protected_timing import PilotTiming

sdk_policy = NativeSDKPolicy(
    provider=selected_native_provider, model=exact_model, session_id=ref.session_id,
    base_url=exact_native_endpoint, accounting='subscription_request_bound',
    request_seconds=10, pilot_timing=PilotTiming(),
)
```

`PilotTiming` is frozen and accepts exactly the reviewed profile, not arbitrary
limits: request 10s, turn 25s, tool 5s, shell 2s, namespace 30s, SDK close/drain 1s.
`request_seconds` may be shorter, never longer. Strict accounting still refuses.
`asdict(sdk_policy)` now includes `pilot_timing` (null for the original default).
The complete nested object is covered by `binding.sdk_policy` in the existing
per-attempt signature and broker expected binding; dropping/forging those fields
invalidates authorization. JSON is never converted into a timing constructor.

One monotonic controller deadline is pinned from the **original** started-at
plus 600s. At single-use execution, one frozen absolute turn deadline is created
before provider/guest acquisition, at `min(now + 25s, controller_deadline)`.
SDK waits, owner capability expiry, proxy lifetime, IPC idle/frame waits and the
fresh conversation all use that same deadline; channels/calls do not renew it.
Each SDK POST is additionally limited to `min(request_seconds, remaining)`;
each proxy tool, including daemon attestation, to `min(5s, remaining)`. Shells
retain the unchanged 2s guest watchdog and output bound. Operational Docker
stage/seal/export calls also clamp their existing CLI timeout to remaining time.

Only after signature validation does the owner select the pilot guest entries.
The default guest still has its 5s tool watchdog, 15s initial staging grace and
2s post-tool idle exit. Pilot tools have a 25s watchdog, and pilot PID1 removes
those early idle exits while retaining an independent **at-most-30s** boot-clock
namespace limit. Its exact attested command carries the owner-derived absolute
wall expiry, `min(turn_deadline + 5s, original_controller_deadline)`, converted
once to the VM boot clock on entry. This assumes the existing trusted VM/daemon
wall clock is synchronized with the trusted host; clock skew can cause early
denial. No mounts, credentials, resource caps, UIDs, shell authority or runtime
image producer trust change. Rebuild the same six-file image closure from the
final clean source revision. The namespace ceiling is not a renewable heartbeat.

Revocation/expiry never allows a late response, append or success settlement.
SDK cancellation and close/drain retain the 1s bound and exact failed-task
retention. Exact-CID teardown/retirement uses its existing separately finite
cleanup bounds **even after operational budget expiry**; expiry must not skip
cleanup. Failed/cancelled reservations remain charged; request 13 is refused
before POST. None of this guarantees remote backend billing cancellation.

Controlled tests use the actual SDKs, TCP peers delaying **each** of two replies
by 2.6s, real Docker/native file tools, native lifecycle readbacks and artifact
settlement. The default fails without a completion receipt; the signed pilot
completes, including opaque signed Opus 5.5 thinking replay on the Anthropic path.
They separately prove request timeout/Stop/original-controller expiry close the
SDK socket and guest with no refund/late append; PID1 self-exit is measured with
four seconds remaining and no owner teardown until after exit. This demonstrates
the approved operational behavior, **not live-provider throughput or latency**.

### Ledger methods

The owner passes the existing broker's shared ledger, **not** a per-run in-memory counter:

The merged `PilotRequestLedger` exposes `snapshot/reserve/reconcile/freeze`. It can be passed directly: `BrokerRequestLedgerAdapter` automatically consumes its real `snapshot()` transaction (`mode`, `request_count`, `elapsed_seconds`, `usd_guarantee`) and independently enforces the signed original controller start. Its successful snapshot already verifies the active/non-suspended signed graph. No new store or fake accounting record is introduced. The internal `check` interface below also supports controlled fixtures and explicitly trusted owner adapters.

- `check(native_binding)` returns exactly enough trusted state to validate `{'requests': int, 'started_at': number, 'frozen': False}`. Requests are cumulative across builder/reviewer/repair/client instances; `started_at` must equal the signed original controller start, never a fresh per-run clock.
- `reserve(request_binding)` atomically commits an attempted-request reservation **before the first POST** and returns a non-None opaque receipt. It must enforce the shared 12-attempt ceiling and 600-second window, including failed/cancelled requests. No reservation refunds. The bridge independently checks that the durable count increased by one and remains within the signed ceiling; ambiguous reserve results freeze rather than send.
- `reconcile(receipt, {'model', 'input_tokens', 'output_tokens'})` durably records actual reported usage without refunding a request.
- `freeze(receipt_or_none, reason)` durably suspends the attempt/campaign on unknown accounting, cancellation or close failure. `None` denotes a provider-close/ambiguous-reservation fault not tied to one known receipt.

The merged broker itself accepts only a known charged receipt. The adapter maps a post-response close failure's `None` to the exact last returned receipt before invoking it. If no receipt is known after an ambiguous reservation, core refuses with an explicit recovery requirement instead of inventing one; the broker's transaction must retain its own suspension/unknown state.

Each request revalidates persisted policy/admission/card claim, signed scope and the shared ledger. The same guard runs on native lifecycle/tool checks. Unknown, stale, suspended or reset ledgers deny. The original source limits remain: inference/tool 5s, shell 2s, namespace 30s, provider close/drain 1s. No timeout is presented as a backend billing cap.

## Concrete authoritative broker readback

`gateway.session_protected_admission.native_protected_readback(authority, request_binding)` resolves **only the exact owner-installed bridge** and invokes its actual native/process/daemon readback. It is a trusted in-owner callback, not a public unauthenticated JSON endpoint. The broker adapter must bind this callback to the canonical owner through an explicitly reviewed deployment/transport integration; its previous unconditional denial must not be replaced with acceptance of worker/config/receipt JSON.

The exact request binding is `{provider, model, session_id, base_url, accounting, native_binding}`; `native_binding` is the bridge's signed native binding, not a caller-created capability. The version-pinned response contains:

- `version='protected_native_readback_v1'`, canonical `profile`, selected `provider/model`, actual source identity and `first_provider_activity_at`;
- `native`: profile/session/admission/principal/execution/generation/epoch/owner-instance and the actual reserved host bridge PID/birth; no adoption secret/token;
- `card`: actual board/task/run/claim plus read-back native retry/runtime/completion policy;
- `container`: actual daemon identity/endpoint, CID, image, source closure, generation, lease, name, role, fixed protocol, mounts and the inspected security/resource fields.

The readback independently verifies canonical reservation/adoption/generation/claim, the current trusted Docker daemon identity and the full existing no-bind/network-none/cap/resource/image/namespace attestation. It refuses before a real namespace/native reservation exists, on caller binding tamper, after Stop/close/terminal retirement or if the daemon/claim changes. It does **not** re-enter the broker ledger: `protected_attempt` is called inside the broker's reserve transaction, so recursive accounting would deadlock. Normal worker-facing lifecycle/tool operations still execute both native verification and the signed budget guard.

The personal broker can validate this actual response against its signed campaign's native source/image/profile/generation and its card mapping, then form its existing five-field `protected_attempt` observation. Core does not copy an unverified campaign binding into an alleged VM proof. Profile ownership transition, real graph signing, a bound cross-process transport if used, publisher identity and actual live acceptance remain separate gates.

## Completion and recovery

The real native `execute_managed` path routes a matching owner capability before ordinary reference expansion, agent bootstrap, provider/tool discovery or credential-to-child transfer. It executes the fresh conversation plus existing credentialless Docker tool namespace. The SDK must close/drain successfully **before** durable native success settlement. Owner-observed transcript and the exact exported artifact receipt are required; prose/EOF/close are not completion evidence.

The bridge is single-use even when auth, SDK, source, cancellation or cross-WAL projection fails. It never starts another inference attempt or hands off automatically. `_runner` retains the exact failed owner handle for explicit diagnostics/recovery; existing protected process/retirement registries own cleanup retries. A canonical-owner receipt followed by a failed board projection remains an explicit recovery case, not permission to double-claim or rerun. The in-memory `_protected_completion_receipts` cache is only a convenience after the durable owner and board readbacks pass.

## Verification scope

`test_protected_native_sdk.py` uses actual SDK packages and a real local TCP HTTP fixture with clearly labelled dummy credentials and synthetic responses. It covers success, delayed headers/body, in-flight cancellation, total timeout, HTTP failure/retry-zero, response byte limits, missing usage/model mismatch, freeze and post-failure refusal. These are not external-provider responses.

`test_protected_native_admission.py` uses a scratch canonical SessionAuthority/native board and RSA-signed **controlled fixtures**, the actual native execute route, real Docker processes/tools and those SDKs. It covers two-request SDK → real Docker file tool → SDK follow-up, owner transcript/export/durable completion readbacks, cancellation and close-fault refusal, actual peer disconnect, task drain, exact namespace removal, signed/persisted binding tamper and a shared durable 12-request counter reopened across SDK instances. It does not claim an actual Jack signature, live account accounting, profile cutover, live card or publication acceptance.

The canonical drain test additionally starts with an unclaimed persisted input,
uses the bootstrap factory through the actual FIFO claim/execute/settlement/
publication path, asserts exactly one completed event and durable success
receipt, and proves an unapproved queued follower reaches a terminal refusal
without ordinary bootstrap or a FIFO pause. Protected settlement stores the
native `{result, usage}` envelope so canonical result replay is compatible.
Fresh-process SDK tests include real tools and OAuth signed-thinking follow-up;
they record attempted forbidden imports even when ordinary helpers swallow the
raised import error, rather than merely checking `sys.modules` afterwards.
