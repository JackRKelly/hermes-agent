# Native tool-child adapter: controlled source slice

Status: **source review only; production admission is unconditionally disabled**.
Baseline: `75d15c1a53d74ecce6f3cc094cb0bd9bf4238fb4`. No installed source,
profile, account, credential, canonical board or dispatcher is changed.

## Actual integrated boundary

`gateway.session_managed_worker._spawn_registered_worker` has an explicit
source-test-only `SentinelLaunch` input. It uses the **real** cancellation-safe
`acquire_process`, native worker registry and `ManagedWorker` lifetime. The
per-worker Stop event/writer is reused; canonical authorization, admission
settlement and the pre-spawn generation latch are not exercised. An owner-owned
`sentinel_scope` closes/reaps and deregisters on every exit. A cancelled sentinel
spawn propagates cancellation without fabricating an admission settlement.
There is no environment variable, profile option, plugin, launcher setting or
admission registry that enables it. A row carrying `protected_tools_v1` is rejected
before the profile environment or ordinary bootstrap is constructed, including
when a sentinel object is also supplied. The ordinary managed path is intended to retain its existing behavior, covered
by the enclosing regression suite, not a proof of unchanged behavior everywhere.
This patch does NOT retroactively isolate normal managed sessions.

The actual `agent/managed_worker.py` executable recognizes exactly one fixed
sentinel argv before PM bootstrap and any profile, provider-client, AIAgent, hook or MCP
import. The agent package itself still imports `agent.jiter_preload`, which
attempts the optional parser import; `-S` excludes installed dependencies, and
the actual child fails if any third-party module is present. The exact
non-stdlib module list is checked at hello and after every tool call. Both package
initializers and that preload module are part of the source review closure.
This is an assertion about this pinned test source, not protection from mutable
source code. The same fixed argv also selects this source-test entry with `-m`;
it is not an isolation permission. `-I -S -B`, fixed source-owned entry, fresh locale-only environment,
`close_fds=True`, empty `pass_fds`, private process group and explicit stdio are
used. The sentinel entry refuses root. The test workspace is same-UID and
user-writable: it is **not** a protected production runtime input.

The parent uses a proxy with a frozen two-name allowlist. The child independently
checks its closed bootstrap and every request. Only `read_file({})` and
`terminal({})` exist; both read the fixed non-secret `allowed.txt`. No caller can
choose a path, executable, environment, command, UID, provider or RPC method.
The file operation reuses the built-in native regular-file primitive with an
explicit final-component no-follow mode and a 4097-byte bounded read. It does not
fall back to the shell. The terminal consumer runs real `/bin/cat` on those
bounded bytes, not arbitrary shell text or a reopened caller-selected path.

Frames have an 8192-byte pre-buffer bound, duplicate-key rejection, exact schemas,
monotonic request sequence, at most 16 tool requests and a five-second channel
lifetime. Bootstrap PID is checked against the actual Popen child. Stop uses the
native latch/writer and the tool channel's sole cancellation operation; the proxy
refuses later tools. No provider/lifecycle channel exists in this slice; there is
no general owner RPC, canonical DB access or fake `owner_admitted`/`worker_bound`
event. The native reservation/claim transaction is **not** exercised or replaced.

## Comparison with the accepted authority split

This is the executable tool-side tracer, not an alternate admission foundation.
The proposed refinement keeps inference and lifecycle in distinct protected
services and places the native tool proxy between inference and a credentialless
tool child. It preserves the accepted separation of provider, canonical owner
and execution identities. It changes the location of the conversation loop:
the accepted ADR describes a credentialless inference/tool child using a separate
inference service; the refinement would retain the conversation loop with the
protected inference owner and proxy all tools. That refinement still requires
explicit architectural acceptance before any production adapter is implemented.

Inspection found that `AIAgent` cannot simply receive a replacement
`model_tools.handle_function_call`: `model_tools` performs discovery at import;
`agent/tool_executor.py` has inline tool routes before registry dispatch; agent
construction, hooks, auxiliary models, memory and provider plugins have their
own local execution paths. **This patch does not instantiate AIAgent or pretend
that replacing one callback disables those paths.** The sentinel owner is a
controlled non-provider caller of the real managed-child seam, not inference.

## What the tests prove (and do not)

Real owner/test process -> real registered managed child -> native file primitive
and real `/bin/cat`. Real inherited file, high-numbered FD, directory and Unix
listener are present; the file's deliberate exec-inheritance control succeeds.
The actual child enumerates only stdio and receives no profile environment. These are cooperative same-UID reports of
Popen sanitation, not kernel attestation or an isolation boundary.
Symlink and oversized sentinel reads fail. Unknown tools and arbitrary terminal
arguments are rejected with no host fallback. Stop acknowledges and the actual
managed child is reaped; subsequent proxy work is refused.

This does **not** prove OS filesystem confinement, a read-only reviewer identity,
service/network/debug denial, root-owned immutable code/interpreter/dependencies,
protected caller/registry admission, saved-ID/group/memberd properties, or
cancellation of arbitrary/detached tool descendants. `/bin/cat` is bounded and
has no children; no broader descendant claim follows. The previously recorded
positive inherited-FD bypass and broad scratch-policy negatives remain valid.

## Exact next gate

Do not request an administrator command from this artifact. First decide the
conversation-loop placement above. If accepted, the next implementation must
supply a protected inference entry with import-time discovery/inline/auxiliary
execution disabled, bind the proxy to native owner reservation/card provenance,
and route only reviewed provider/lifecycle adapters through separate bounded
channels. Then implement/review the single-purpose root-owned native entry and
protected registry with immutable full runtime input checks. Only that frozen
candidate can define the privileged sentinel protocol. Nonempty/nonzero groups,
memberd opt-out, irreversible IDs and all host/service/descendant negatives stay
mandatory and unproven. There is no safe administrator handoff or
privileged acceptance protocol yet. No Python setuid helper, account/group change, live
admission, deployment or pilot release is supplied.

## Reproduction

Use the canonical `scripts/run_tests.sh tests/gateway/test_protected_tool_adapter.py`
with an isolated test interpreter and scratch HOME/HERMES_HOME/TMPDIR. No real
provider is invoked. Native process tests run on POSIX; platform skips are not
privileged acceptance. Run the enclosing managed-worker and file-operation
regressions separately and record exact head, test counts and skips.
