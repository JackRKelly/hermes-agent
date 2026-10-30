# Windows x64 marker diagnostic (not full native acceptance)

Branch-only push workflow: `.github/workflows/windows-marker-timing.yml`.
Preparation does not dispatch native jobs, commit, push or write remote state.
Parent independently reviews the final bytes before publication; the separate
60-case native acceptance gate remains parent-owned.

## Immutable boundaries

- Original CIM oracle: `2b682fffaa6c06fb700df0d32e21c0b4ac7370e6`.
- Original module SHA256:
  `2cb44bc565731abd75eea33376eb250065ce97471aee7b5acf83274145e6c36f`.
- Reviewed final GREEN subject: `81f29fbf2ac018f41ec4325a229d2ee3e913222b`.
- Final subject tree: `f153e596650057455256432f44efd02920b1864f`.
- Actual patched module SHA256:
  `9d19bbd5320e6dcdafa2b6fdd96c54401848e72c1e92ee5459ace3a18e972331`.

The controller/support scripts are copied outside the checkout before the
original-subject checkout. A separate pinned official checkout fetches the final
subject from the same fork, with `persist-credentials: false`. The workflow
validates its literal `GREEN_SUBJECT` as lowercase 40-hex before using it. There is
no arbitrary shell-input ref expansion or unreviewed branch fallback.

The original subject guard checks HEAD, tracked diff, all pinned Git blobs and
disk content using Git's clean/eol contract, plus the helper's exact raw SHA256.
It runs before and after; final guard failure overrides any successful result.
No tracked original source, original assertion or test parameter is edited.

`git archive` exports the exact final commit into temporary source **outside both
checkouts and the upload inventory**. A data-filtered tar extraction prevents
archive path escape. No checkout patch is applied. The module's raw hash must
match the approved final helper hash before execution and after diagnostics.
`green-source.json` binds the final commit, module path, before/after hashes and
actual-source GREEN status. The full source export is local-only and removed.

## Four bounded diagnostics

One `windows-2025` x64 VM, no matrix; 25-minute job, maximum two file workers,
zero retries and no accepted skips. Each invocation retains the same 300-second
total envelope (270 work + 30 owned-tree cleanup); no full-suite budget changes.

1. Canonical `scripts/run_tests.sh` runs only unchanged `crlf-bom-v2`, wrapping
   actual `_creation_time` and `_run` globals. Exact JUnit identity, no failures,
   errors or skips, selected-node collection, matching helper calls and all three
   pytest phases are required.
2. Actual original no-shell RED requires exit 1, returned creation identity,
   original module hash and real PowerShell `subprocess.Popen` audit evidence.
   The Windows executable=None/command-line audit extraction remains unchanged.
3. Isolated candidate equality uses the unchanged original CIM oracle for two live
   children and the original protected/limited-rights selector. Invalid PIDs,
   retained dead children with actual exit codes 0 and 259, exact exited exceptions
   and kernel handle-count closure controls remain unchanged.
4. Actual final-subject no-shell GREEN runs `native_no_shell.py --implementation
   patched-source --repo <archived-final-source>`. Imported module/function/code
   origins and exact approved module hash must match. Classification requires
   source/start/observed/complete receipts, returned creation identity, **zero
   Popen launches**, correct archived path and exit 0. Detached helper equality
   cannot qualify as actual-source GREEN.

### Owned-thread access-denied precondition, not a waived negative contract

The denied precondition no longer depends on a protected/foreign process being
unreadable on a particular runner. The controller passes the **already-created
pinned final source archive** into native equality with `--denial-repo`. Only this
negative diagnostic uses its actual unwrapped `_creation_time`; the original CIM
oracle, detached-candidate equality and all other controls are unchanged. The
archived module is imported under `marker_owned_denial_source` without replacing
the original oracle's module. Its path, function/code origin and exact approved
hash are checked before use; no source is rewritten or AST-transformed.

The fixture retains an existing owned sleeping child's real Popen HANDLE. Before
impersonation it loads all modules, inspects the exact failed-OpenProcess raise
line, prebinds pointer-correct kernel32/advapi32 APIs with `use_last_error=True`,
requires `OpenThreadToken` to fail with **ERROR_NO_TOKEN (1008)**, verifies the child
is live and obtains its baseline identity from the actual source helper. It never
replaces an existing thread token. It calls
`ImpersonateAnonymousToken(GetCurrentThread())` on the diagnostic process's own
current thread, then independently calls `OpenProcess(0x1000, False, owned_pid)`.
Only a real NULL HANDLE with immediately captured WinError **5** qualifies. The
same actual source helper must then raise WinError 5, returning no identity.

No reports, file/console I/O, child polling, subprocesses or thread creation occur
in the restricted orchestration interval; only the warmed, unchanged helper and
prebound native calls run. The timing wrapper is deliberately not used here.
`RevertToSelf` is mandatory in `finally`, including entry/helper/native failures.
A failed/throwing revert immediately terminates the **owned diagnostic process**
via `os._exit(86)`, without flushing, reporting or ordinary cleanup. Failure to
verify ERROR_NO_TOKEN after a successful revert also failfast-terminates before
postfailure I/O. The controller's unchanged job containment cleans descendants.

After restoring and verifying ERROR_NO_TOKEN, the child must still be live with
the same actual-source identity. Only then is the exception inspected: its
innermost traceback must be the exact actual helper's failed-OpenProcess raise,
with a false `handle` local and the same owned PID. Loader errors, errors from
GetProcessTimes/other branches, wrong WinErrors, early child exit, identity drift
and unexpected helper success cannot pass. A pass receipt binds source hash,
module/path, raise location, limited-query mask, native/helper errors, pre/post
token errors and matching baseline/recovered identity. The controller requires
that exact source-bound receipt, one nonblocked completion and exit zero.

Anonymous denial is **not guaranteed**. Failed entry, a granted query HANDLE
(which is closed), or any native error other than 5 yields `control_blocked`
**only after recovery**, never a pass. The 192-query handle measurement still
runs and the diagnostic returns 3. Actual final-source GREEN still runs afterward,
and never erases a blocked/failed control. A preexisting token or recovery failure
is a hard failure, not an accepted skip. The legacy portable scan classifier is
retained for regression coverage but is no longer called by native main.

No process DACLs, foreign-process permissions, host permissions, privileges,
registry settings, credentials or elevation are changed. The only new security
context is this temporary anonymous **owned current-thread** context in the
isolated diagnostic process. Portable injected API fixtures are not native proof.

## Preserved instrumentation, isolation and containment

The existing canonical interpreter adapter restores only the pinned
`PYTEST_PLUGINS` after the wrapper clears caller environment. Final collection
uses `pytest_collection_finish`; selected function globals are verified against
the real node source before wrapping. Actual marker-bodies `_creation_time` and
`_run` alias receipts remain mandatory. Git Bash selection is absolute,
ProgramFiles/Git/bin/bash.exe only, without System32/WSL/PATH fallback.

Each worker joins the unchanged Windows kill-on-close job before creating owned
children. Timeout targets its exact owned Popen HANDLE; worker exit closes the
job and terminates remaining owned descendants. Disposable home/appdata/temp and
Hermes home, credential/runtime exclusions and zero retries are unchanged.
Both native scripts reject optimized Python before any native work.

Handle closure requires identical before/after counts across 64 live queries,
64 held-dead exit-0 queries and 64 held-dead exit-259 queries. Both real Popen
handles stay retained through measurement; exact ProcessLookupError containing
`has exited` remains required, not generic OSError.

Uploads list only same-run JSONL, JUnit, process/containment/timing/source/harness
receipts and logs. No source archive, home, credentials or worker spec is uploaded.
`diagnostic-sha256.json` pins runtime scripts; `prepared-sha256.json` retains
original prepared provenance. This follow-up changes neither helper candidate,
original/final test source, NoShell/audit script nor instrumentation plugin.

## Exact workflow invocation

```bash
"$DIAGNOSTIC_PYTHON" "$RUNNER_TEMP/windows-marker-timing/controller.py" \
  --repo "$GITHUB_WORKSPACE" --out "$RUNNER_TEMP/windows-marker-timing-evidence" \
  --green-repo "$GITHUB_WORKSPACE/.green-subject" \
  --green-subject 81f29fbf2ac018f41ec4325a229d2ee3e913222b
```

## Local verification and evidence

```bash
python3.12 -m unittest discover -s .github/scripts/windows-marker-timing -p 'test_*.py'
actionlint .github/workflows/windows-marker-timing.yml
ruff check .github/scripts/windows-marker-timing
git diff --check
```

Portable tests are classifier/control-flow fixtures, real local hashing,
immutable Git archive extraction, optimized-Python rejection and owned POSIX
cleanup only. They do not spoof the OS or constitute native API evidence.
Local source verification exports the actual approved final commit and verifies
its real imported module binding/hash; a disposable original-subject checkout
passes source guards before/after and `--guard-only`. Wrong-subject rejection is
also exercised without changing original tracked files.

Follow-up evidence, RED/GREEN logs, source receipts, report and review hash manifest:
`$LOCAL_EVIDENCE_ROOT/ci-windows-owned-denial-followup/`.
Anonymous owned-thread denial and recovery on real Windows remain unexecuted
here. Existing native full-suite/source-GREEN evidence is not replaced by these
portable tests. No new native, seven-case/60-case/ARM64 pass is claimed.
