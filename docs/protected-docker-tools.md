# Credentialless Docker tool child (source only)

**Live admission remains disabled.** `production_plan` still unconditionally
refuses. Nothing here installs a profile, admits/releases a card, enables a
provider, publishes artifacts, changes macOS identities/groups or configures
Docker Desktop. This is an explicit VM alternative to the original host-native
identity contract, not a relaxation of that contract.

## Actual integrated path

A trusted owner constructs `DockerRuntime.build(...)` and `DockerLaunch(...)`,
then supplies the launch to the existing `protected_process_scope`. The existing
native acquisition/Stop registry, protected fresh conversation, selected async
provider, owner-observation ledger, transcript append, reservation/claim and
retirement scopes remain in use. `_spawn_registered_worker` acquires the fixed
Docker child instead of the old cooperative host tool child. The conversation
and provider/lifecycle Unix channels remain OUTSIDE Docker. No capability token,
owner socket, canonical DB or provider secret enters Docker.

The native reservation currently identifies the owner-held host **Docker exec
bridge PID/birth**, not a Linux guest PID. `NativeLifecycle` additionally holds
and rechecks a trusted Docker attestation (exact CID, image ID, closure hash,
generation, private lease nonce). It carries this separate typed binding in
`docker_tool_v1`. Guest hello PID/import/FD data are hostile diagnostics, not
attestation. The owner-held exec stream plus daemon inspection define identity;
no guest callback can adopt a worker, change a run, settle an admission or promote
readiness. The source-only test still retires through the existing **unknown**
admission contract, never fabricated successful Kanban settlement.

Docker bindings enable a deliberately small useful subset: `read_file` accepts a
bounded path and `terminal` accepts a bounded shell command. Empty arguments
retain the old sentinel compatibility. Commands run only in Docker through the
existing tool channel, never through the host registry, hooks, plugins, MCP or
fallback. Native regular-file primitives are reused inside the image; shell
stdio is private and bounded. The owner ledger validates the entire batch and
binds actual arguments and results to the observed provider call IDs. The host
and frozen conversation both enforce the same closed argument contract.

## Fixed runtime and resource policy

- Existing, explicitly pinned local Unix Docker endpoint; private empty Docker
  client config and allowlisted environment, no owner context/credential-helper
  discovery. System-installed Docker CLI plugins remain part of the trusted host. Daemon ID and endpoint are persisted with leases.
- Cached official Python image **ID**, never a mutable tag at launch; no pull or
  dependency install. Build context is a fresh explicit six-file source closure,
  not the repository/home. Manifest contains revision, base ID and every source
  SHA256, index tree and dirty flag; its hash labels the resulting locally inspected image ID. The build
  uses COPY/metadata only, no root guest command. The image producer, cached base
  and supplied reviewed closure remain trusted; local content addressing is not
  registry-signature verification.
- Zero host binds/volumes/devices/sockets/home/cache/skills. Read-only root, network
  none, private PID/IPC/cgroup namespaces, no caps, no-new-privileges, non-root
  execs only; default seccomp remains enabled. No Docker API/CLI in the guest.
- PID1 UID65533; tool UID65532; trusted staging/export UID65534. Builder workspace
  is a 1-MiB tmpfs owned by tool UID. Reviewer workspace is staged before tool
  exec by UID65534, then files0444/directories0555. Reviewer cannot chmod/unlink/
  rename/write/hardlink there even through shell/native operations.
- 48 PIDs, 192-MiB RAM+swap, half a CPU, 128 open FDs, 64-KiB per-file process
  fsize limit. Workspace/tmp/shm limits are 1/4/1 MiB, workspace/tmp inode limit
  128 each. They share the memory cgroup. Docker logging driver is **none**.
- Distinct-UID immutable PID1 uses CLOCK_BOOTTIME and exits after 30 seconds,
  reaping reparented children meanwhile. Exit tears down the **entire PID
  namespace**, including setsid and Docker exec descendants. This is a bounded
  lifetime, NOT immediate host-death detection or a process-group promise. PID1
  also exits after two idle seconds once it has observed tools (15-second initial
  acquisition grace); it never trusts a tool heartbeat to extend the hard bound.
  Tool bootstrap/channel lifetime is independently five seconds; shell calls
  two seconds, 16 calls, 8192-byte frames and 4096-byte outputs.

## Transfers and retirement

The owner explicitly approves at most 32 relative regular-file paths, 4096 bytes
per file and 65536 total. Held no-follow directory descriptors, nonblocking
regular-file fstat, link-count checks, bounded reads and metadata-change checks
reject symlinks, hardlinks, FIFOs/devices/sockets and traversal. Casefold/NFC
collisions and hidden/control-character names refuse. Inputs use individual
bounded JSON/base64 frames, never tar extraction or a shared host workspace.

Owner-only `worker.export(paths)` FIRST revokes tools, stops all tool-UID
processes, and independently verifies quiescence from PID1's distinct UID. The
image-baked exporter UID reads only explicitly allowlisted regular no-follow,
nonblocking, single-link bounded files. Owner validates schema/path/count/size/
hash and returns bytes in memory. It never writes guest paths into the owner's
workspace or executes artifacts. Hashes are transport integrity, not trustworthy
content or publication approval. Failure to quiesce/read/export fails closed;
there is no root export or permission bypass. Retirement always kills/removes
the exact CID and verifies stopped/Pid0 then absence; bridge kill alone is not
retirement.

A private random name/nonce/generation/source/image lease is fsynced as
PENDING_CREATE, then CREATING is fsynced immediately before issuing create. Create is inspected before start; labels/name/CID/image are matched
before teardown. Acquisition cancellation latches before tool exec and waits for
late acquisition cleanup despite repeated cancellation. `DockerRuntime.reconcile`
attempts every matching private lease on the SAME daemon/image/source, never
kills by broad prefix/label. Retired leases are skipped before runtime comparison;
foreign runtime leases are reported/skipped (reopen their exact approved runtime
for a separate restart sweep). Per-lease faults are collected and reported AFTER
other leases are attempted. Never run this restart sweep against active sessions.
Inputs are validated before leasing. PENDING_CREATE without a CID is known never
issued and can retire. CREATING with an absent/unknown object reissues the exact
same fixed named create, acquires the resulting object or original name-conflict
object, verifies all immutable identity and removes it WITHOUT EVER STARTING it.
Failed/unreachable recovery stays retained/retryable, never declared absent on a
transport error. Failed lease writes retain exact in-memory handles and durable
prior lease for retry. A late unstarted container cannot execute tools. Recovery
is owner-only, never model RPC; it is not a daemon transaction or a guarantee
against arbitrarily delayed requests beyond the daemon's own request semantics.

## Verification and remaining gates

Private opt-in tests: `HERMES_DOCKER_BOUNDARY_TEST=1` runs
`tests/gateway/test_protected_docker.py` against the already running local Desktop
endpoint and cached pinned base in the test. No registry/network/provider access.
Ordinary CI skips these deployment-specific tests. `HERMES_DOCKER_EVIDENCE` may
point to a private test-owned directory for image/lease/attestation/reaping
receipts; use isolated HOME/HERMES_HOME, never the live profile. Run the canonical
10 protected test files separately through `scripts/run_tests.sh` (96 existing
controls, no skips). Keep broad-suite RED classifications and hosted diagnostics
separate; previous-head CI does not validate these additions.

Docker admins sharing the daemon, the host owner, Docker/VM/hypervisor/kernel,
base runtime and immutable producer are trusted. This is NOT a dedicated tenant
boundary. Existing Desktop file sharing is unchanged; a VM/kernel escape may
reach shared host paths. A hostile tool can replace/ptrace its SAME-UID worker
and forge tool output: frame validation limits authority and resource use, not
the honesty of guest content. Stdlib Python is not a sandbox. The protected HOST
conversation's prior cooperative same-UID limitations also remain.

Before live admission: independent exact-final-tree review, deployment-specific
hardening/coverage acceptance (including all daemon crash points), canonical owner
approval, actual provider SDK/credential-placement and cancellation proof, native
successful settlement/card contract, publisher
approval and opposite-vendor crew acceptance. No installation, live-provider,
publisher or autonomous-crew acceptance is claimed by controlled fixtures.

Nonblocking source-review residuals: quiescence currently inspects thread-group
leaders, not every thread; same-UID `comm`/proc exit races can cause fail-safe early
PID1 exit; daemon-default attestation is not an exhaustive OCI configuration
validator; the existing asyncio Stop event has thread wake-up limitations. These need deployment hardening/coverage acceptance, not
new authority or an admission exception. Absolute/idle timing depends on the
running VM's clock/scheduler. Artifact content remains untrusted regardless.
