# Original Windows x64 timing diagnostic (not native acceptance)

Branch-only push workflow: `.github/workflows/windows-marker-timing.yml`.
No dispatch, schedules, releases, publishing, commits or remote writes are part of
this preparation. Parent must freeze/review these bytes before pushing. Confirm
that the separate final native workflow remains disabled before execution.

## Immutable boundary

- Subject: `2b682fffaa6c06fb700df0d32e21c0b4ac7370e6`.
- Original CIM module SHA256:
  `2cb44bc565731abd75eea33376eb250065ce97471aee7b5acf83274145e6c36f`.
- Controller and five diagnostics are copied outside the checkout **before**
  switching to that subject. No candidate patch or prepared replacement module is
  copied/applied. `helper_candidate.py` is an isolated comparison import only.
- Subject guard checks HEAD, tracked diff, 55 pinned Git blobs and disk content
  using the subject's Git clean/eol contract. The original helper additionally
  requires its exact raw-byte SHA. Guard runs before and after, and a final guard
  failure overrides an otherwise successful exit.
- One `windows-2025` x64 VM, no matrix; 25-minute job, two file workers maximum,
  zero retries and no accepted skips. Each of the three sequential invocations
  has a 300-second total envelope: 270 seconds work plus up to 30 seconds owned
  process cleanup. Canonical per-file timeout stays 300. No full-suite budgets
  or acceptance inventories are changed.

## Sequence and evidence

1. Canonical `scripts/run_tests.sh` runs only the unchanged `crlf-bom-v2` marker
   body parameter, wrapping the original `_creation_time` and `_run`. Exact JUnit
   identity, no failures/errors/skips, durable plugin collection, matching helper
   starts/ends and setup/call/teardown records are required.
2. Real original-helper no-shell RED: exit 1 is accepted only with a returned
   identity and actual PowerShell `subprocess.Popen` audit event in JSONL.
   Import/query/timeouts and missing audit evidence fail closed.
3. Corrected `native_controls.py` compares actual CIM and kernel calls
   for two live children and the original limited-only protected selector, then
   checks invalid/dead (exit 0 and 259)/inaccessible PIDs and handle counts.
   PID 4 may be accessible on a hosted machine: that is an **unavailable control
   and failed diagnostic**, not a skip or fabricated pass. It emits
   `control_blocked` with a reason, runs the remaining handle measurements, emits
   completion with `blocked_controls`, and returns distinct exit 3 (never accepted).
   Raw JSONL/log remains.

A worker joins a Windows kill-on-close job before it starts diagnostic children.
Worker exit closes its non-inherited job handle and terminates remaining owned
children, including detached ones. Timeout terminates the exact worker process
handle, not an image name or recycled numeric PID. Failure to establish job
containment prevents the diagnostic command from running. These native job
semantics remain subject to the first actual Windows run. Portable controller
checks use POSIX sessions, without spoofing the OS.

Each child receives an explicit location-only environment and disposable home,
appdata, temp and Hermes home. No provider/credential/activation/runtime directory,
caller plugins, PYTHONPATH or Git configuration environment is forwarded. Uploads
select only JSONL, JUnit, timing/process/source/harness receipts and diagnostic
logs, never sandbox homes, environment dumps, source archives or worker specs.
Artifacts include the same run ID/attempt. A successful summary means **these
three diagnostics only**, never full seven-case, 60-case or ARM64 acceptance.

## Canonical plugin loader and prepared-script corrections

The original wrapper clears caller `PYTEST_PLUGINS` with `env -i`; its only
internal plugin exception is the unrelated live-home guard. Passing `-p` also
collides with Hermes CLI profile parsing during test imports. Neither is used.
A scratch executable Bash interpreter adapter selected with the wrapper's
supported `HERMES_PYTHON` mechanism sets **only**
`PYTEST_PLUGINS=marker_timing_plugin` immediately before exec of the locked
pytest-capable interpreter. Per-file children inherit that value. The controller
copies only the diagnostic plugin into checkout root, verifies tracked source
before/after, and removes the plugin in `finally`. No tracked wrapper/conftest is
edited, and no real home guard is reused.

The five prepared inputs were verified against the upstream scratch
`SHA256SUMS.json`; original hashes are retained in `prepared-sha256.json`.
Two files (`helper_candidate.py` and `summarize.py`) retain their original bytes.
The plugin correction is preserved: a real portable pytest reproduction
showed that `pytest_collection_modifyitems` recorded both chosen and deselected
items before the built-in `-k` filter, even though only the chosen item ran.
The prepared-source delta in `marker_timing_plugin.py` remains:

```diff
-def pytest_collection_modifyitems(session, config, items):
+def pytest_collection_finish(session):
+    # Final selected items only: modifyitems runs before pytest's -k deselection.
+    config, items = session.config, session.items
```

No wrapped helper, arguments, return, exception or test body changes. The same
canonical wrapper + adapter portable reproduction then recorded exactly one
selected item. `diagnostic-sha256.json` pins the five transferred runtime files,
including all corrected copied scripts. `harness.json` records every controller/support
file actually transferred. Parent review should bind the workflow/controller
hashes as well as these pinned diagnostics.

## Independent-review corrections (local only)

- Held-dead exit 0 and 259 controls now require **exactly** `ProcessLookupError`
  containing `has exited`. Generic `OSError`, subclasses and missing-identity
  errors cannot masquerade as the post-query exited branch.
- Both dead children are retained alongside the live children until final cleanup;
  their real `wait()` results must equal 0 and 259 respectively. One real
  `GetProcessHandleCount` before/after envelope covers 64 live-success queries
  **plus 64 held-dead exception queries for each exit code**, with identical counts
  required. Only then is `close-handle` emitted, with explicit query counts.
- Accessible PID 4 is blocked, not passed; closure still runs before exit 3.
  A failed closure remains a real failure, not a blocked-control success.
- `native_no_shell.py` imports only the actual test source module at the exact
  checkout path. It verifies the function's module/code path, hashes the imported
  module bytes, emits the receipt and asserts the exact hash. Original RED is bound
  to `2cb44bc565731abd75eea33376eb250065ce97471aee7b5acf83274145e6c36f`;
  future `--implementation patched-source` GREEN is bound to
  `9d19bbd5320e6dcdafa2b6fdd96c54401848e72c1e92ee5459ace3a18e972331`.
  The old `candidate` selector is rejected: isolated helper equality is **not**
  patched-fixture GREEN. This workflow runs only original RED and isolated equality;
  it does not apply a patch or invoke patched-source GREEN.
- Both native scripts fail closed before native work under `-O` or
  `PYTHONOPTIMIZE=1`; assertions cannot be silently disabled.
- Timing classification additionally requires the marker-bodies module's exact
  `_creation_time` **and** `_run` entries in `collection.wrapped_aliases`.
  Generic helper start/end records from some other import are insufficient.
  The first native run missed the test's execution namespace; local collection
  reproduced pytest importing this test by its bare module name.
  Instrumentation therefore also binds the selected function's actual globals,
  verifies their source path against its node ID, and records canonical aliases
  derived from that verified path. Original callables and test bodies remain
  unchanged; a passing test without these bindings is not timing evidence.
- The canonical command selects an absolute resolved `ProgramFiles/Git/bin/bash.exe`,
  rejects missing/relative/escaped/System32/WSL paths, and never falls back to bare
  `bash` or PATH lookup. The existing supported interpreter adapter is unchanged.

Changes from prepared originals are restricted to the existing plugin collection
hook correction and the above two native-script corrections. Controller changes
are hash-bound RED classification, exact alias attribution and Git Bash selection.
The workflow, source manifest, prepared-origin manifest, helper candidate, summary
helper, job/worker containment, caps, cleanup, sandbox and PM setup are unchanged.
The optional launch-time process receipt was not added; `process.json` remains a
post-wait receipt, while per-event JSONL and containment receipts remain durable.

Portable `test_diagnostics.py` exercises extracted decision/measurement helpers
with explicit callbacks and classifier fixtures, real local source hashing and
actual optimized Python subprocesses. These are **not** native API simulations or
native proof. `test_controller.py` also checks actual local POSIX ownership cleanup.
Nine recorded RED→GREEN slices precede final verification. Evidence is retained
outside the repo in `corrections-*.log` and `CORRECTIONS-REPORT.md`; the previous
`verification.json`/`FROZEN-SHA256.json` describe the superseded preparation, not
this correction. `CORRECTIONS-SHA256.json` binds the corrected review surface.

## Local verification / remaining boundary

```bash
python3 -m unittest discover -s .github/scripts/windows-marker-timing -p 'test_*.py'
actionlint .github/workflows/windows-marker-timing.yml
ruff check .github/scripts/windows-marker-timing
git diff --check
```

Portable tests cover intended RED/wrong-reason rejection, success/nonzero/timeout
process results, partial/wrong-node/skipped JUnit and helper classification,
credential/runtime exclusions, initial and final source-guard exit propagation.
Real immutable-subject `--guard-only` and a changed-source rejection were also
exercised locally. They do not execute Windows tests.

Local evidence is outside the repository at
`$LOCAL_EVIDENCE_ROOT/ci-windows-native-timing-controller/`.
No native attribution, kernel/CIM equality, hosted protected/inaccessible control,
Windows job cleanup or full-native pass is claimed. Opposite-vendor review and
native execution remain parent-owned gates.
