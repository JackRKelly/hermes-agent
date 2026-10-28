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

### Access-denied precondition, not a waived negative contract

`psutil.pids()` enumerates actual native PIDs. The control requests only
`PROCESS_QUERY_LIMITED_INFORMATION` (0x1000), closes every granted HANDLE, and
captures GetLastError immediately for each failed OpenProcess. PID 0 is not a
valid denied-process precondition. A readable PID 4 emits `control_info`, never a
pass. The scan continues until a real enumerated PID actually denies access with
WinError 5. The unchanged candidate must then raise WinError 5 and return no
identity. The pass receipt records selected PID, native error and candidate error.
Other native errors or an empty scan cannot count as success.

The scan never kills or changes foreign processes and never changes privileges,
permissions, tokens or UAC. No fake native API is used. If no real denied process
exists, `control_blocked` and `blocked_controls` remain, handle measurements still
run, and `native_controls.py` returns 3. The controller still collects patched
GREEN, retains all raw evidence and returns nonzero overall. GREEN never erases a
blocked equality/control result. The remaining native diagnostics also continue
after a RED/equality failure, with individual failures accumulated.

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
`$LOCAL_EVIDENCE_ROOT/ci-windows-denied-control-followup/`.
Native denied-process availability, actual Windows final-source GREEN and job
cleanup remain unexecuted here. No full seven-case/60-case/ARM64 pass is claimed.
