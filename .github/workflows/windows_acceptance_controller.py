"""Native Windows x64/ARM64 canonical whole-file acceptance and mutation challenge.

--list and --self-test do not execute Windows cases and allow real macOS Python.
All execution requires native Windows, exact subject SHA and Python 3.14.9.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET

FILES = [
    'tests/scripts/desktop_update/test_desktop_update_windows_commit_outcome.py',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker.py',
    'tests/scripts/desktop_update/test_desktop_update_windows_handoff_lineage.py',
    'tests/scripts/desktop_update/test_desktop_update_windows_commit_release.py',
    'tests/scripts/desktop_update/test_desktop_update_windows_commit_supervision.py',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_bodies.py',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lifecycle.py',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_custodian.py',
    'tests/scripts/desktop_update/test_desktop_update_windows_handoff_protocol.py',
]
FIXTURE = 'tests/scripts/desktop_update/test_r6_checkout_fixture.py'
MARKER_LOCK = 'tests/scripts/desktop_update/test_desktop_update_windows_marker_lock.py'
ALL_FILES = [*FILES, MARKER_LOCK, FIXTURE]
POWERSHELL = 'scripts/desktop-update/windows.ps1'
TIMESTAMP_FILE = 'tests/scripts/desktop_update/test_desktop_update_windows_commit_release.py'
TIMESTAMP_NAME = 'test_result_finished_at_is_stamped_after_the_r6_release_wait'
TIMESTAMP = TIMESTAMP_FILE + '::' + TIMESTAMP_NAME
HASHES = {
    'tests/scripts/desktop_update/conftest.py': 'd3f427f92034b64755a846c185d31ac8e55231dc5664cc26e098f5fe1bffb82b',
    'scripts/desktop-update/windows.ps1': 'b9e83ea4819a1a4bcedd023f2b3caf145f303c209bbb88404834c8f16e1fb760',
    'scripts/desktop-update/marker.ps1': 'fd9bb4a536f0dbf37c54d1e902eef0635e2c04913dd631e91b468696ce526299',
    'scripts/desktop-update/marker-claim.ps1': '43fabb7a293eba12e8044d26447952891f9a481b61e76205326de9e0b1df11cf',
    'scripts/desktop-update/update-job.ps1': 'a2f599925aca103b5204e6c9de44a94efa9dd0dcba0cf10af8e381e52643eda6',
    'tests/scripts/desktop_update/test_desktop_update_windows_commit_outcome.py': '225de09a1420cf406964a6e313d3a9e235c3149ac2fd7c42a9cc5f6bbafc67aa',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker.py': '9d19bbd5320e6dcdafa2b6fdd96c54401848e72c1e92ee5459ace3a18e972331',
    'tests/scripts/desktop_update/test_desktop_update_windows_handoff_lineage.py': 'd9199d78cee359d015ebb571cdefa94212217264c772e270e4105ba983651a76',
    'tests/scripts/desktop_update/test_desktop_update_windows_commit_release.py': '5d7ddb97bd93d21487e6ff18fe7ee83edf149bbdb79c4e5f19c7ef59aaf72493',
    'tests/scripts/desktop_update/test_desktop_update_windows_commit_supervision.py': 'f1587fb0129b92623c72c7ad097bf918e710164302daf41bffea3cce1a101d57',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_bodies.py': '58c35527edc253fb8daa4c8ba5a248582c08ef72d537811792b79d2657cace89',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lifecycle.py': 'f6f8ca08272e68db0a396344f39be82878132bb3caac9837f7d2b7defd9f1b1c',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_custodian.py': '443d2ad33d33052e0f1ad15e2e856fb36b8ab863a128da15fbaa265e23d39964',
    'tests/scripts/desktop_update/test_desktop_update_windows_handoff_protocol.py': '0659269e51cea52c893aaa372d6669ad35fbaadb2ebae5ee3d8d43f508ec4c74',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lock.py': '814ad155401ca6439b36ac77d7b6c99a1c6693428f5cd0ddbbc3bb0c6c8635ae',
    'tests/scripts/desktop_update/test_r6_checkout_fixture.py': '378e9d0691ec996c9e52e8615af98d165bdf321ac7f0b4dc1c079bdb1c0276d1',
    'tests/installation_launcher_fixture.py': '071870cf46a2d9b718f984521af4414f4f3d361f220484f31ca573a628492643',
    'pm/__init__.py': '67adcba1f0e4db2b2967d07e6b630aca4214b1fac9bd8649dcf221b4bf3fcfac',
    'pm/paths.py': '8f15860b82f443a424186317dc361ea6063e0e7c3113af9b56a76f6b89e97737',
    'pm/environments.py': '40b5fb014b07ad7c7187352bacb9b79581da4c1304d2e86768eb5dfd95a2ea2b',
    'hermes_cli/_launchers.py': '4defa7fd066a8cb582befab800f39a9488289519125bc8ccc2e92b8e439e3311',
    'hermes_constants.py': '74d849629c176703d48f9aa43a245fd8819545ac908c805e347db7d691976510',
}
EXPECTED_NODES = [
    'tests/scripts/desktop_update/test_desktop_update_windows_commit_release.py::test_result_finished_at_is_stamped_after_the_r6_release_wait',
    'tests/scripts/desktop_update/test_desktop_update_windows_commit_outcome.py::test_verify_failure_after_a_committed_update_is_ok_with_warnings',
    'tests/scripts/desktop_update/test_desktop_update_windows_commit_outcome.py::test_result_is_atomic_json_carrying_started_at_and_warnings',
    'tests/scripts/desktop_update/test_desktop_update_windows_commit_outcome.py::test_a_result_that_cannot_be_published_leaves_no_tmp_file',
    'tests/scripts/desktop_update/test_desktop_update_windows_commit_outcome.py::test_nonzero_exit_after_the_commit_point_is_installed_with_a_followup[interrupted-True]',
    'tests/scripts/desktop_update/test_desktop_update_windows_commit_outcome.py::test_nonzero_exit_after_the_commit_point_is_installed_with_a_followup[partial-True]',
    'tests/scripts/desktop_update/test_desktop_update_windows_commit_outcome.py::test_nonzero_exit_after_the_commit_point_is_installed_with_a_followup[interrupted,another-run-False]',
    'tests/scripts/desktop_update/test_desktop_update_windows_commit_outcome.py::test_nonzero_exit_after_the_commit_point_is_installed_with_a_followup[failed-False]',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker.py::test_claim_is_the_first_action_and_writes_a_v2_body',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker.py::test_live_foreign_owner_refuses_and_reused_pid_is_reclaimed[True]',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker.py::test_live_foreign_owner_refuses_and_reused_pid_is_reclaimed[False]',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker.py::test_owner_creation_time_is_read_for_system_processes[True]',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker.py::test_owner_creation_time_is_read_for_system_processes[False]',
    'tests/scripts/desktop_update/test_desktop_update_windows_handoff_lineage.py::test_r4_old_desktop_wrapper_claim_is_adopted_by_lineage[start-b]',
    'tests/scripts/desktop_update/test_desktop_update_windows_handoff_lineage.py::test_r4_old_desktop_wrapper_claim_is_adopted_by_lineage[waiting-wrapper]',
    'tests/scripts/desktop_update/test_desktop_update_windows_handoff_lineage.py::test_r4_old_desktop_lineage_never_adopts_an_unrelated_live_claim',
    'tests/scripts/desktop_update/test_desktop_update_windows_handoff_lineage.py::test_r4_live_wrapper_not_the_desktops_child_is_adopted_only_with_the_handoff_started_at[env-matches]',
    'tests/scripts/desktop_update/test_desktop_update_windows_handoff_lineage.py::test_r4_live_wrapper_not_the_desktops_child_is_adopted_only_with_the_handoff_started_at[env-differs]',
    'tests/scripts/desktop_update/test_desktop_update_windows_commit_release.py::test_marker_with_a_live_delegate_is_handed_to_it_at_finish',
    'tests/scripts/desktop_update/test_desktop_update_windows_commit_supervision.py::test_watchdog_remap_after_banner_reports_interrupted_followups',
    'tests/scripts/desktop_update/test_desktop_update_windows_commit_supervision.py::test_cpu_busy_pipe_silent_update_is_not_killed_by_the_idle_watchdog',
    'tests/scripts/desktop_update/test_desktop_update_windows_commit_supervision.py::test_hanging_update_help_probe_is_bounded',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_bodies.py::test_marker_bodies_are_parsed_positionally_like_every_other_reader[crlf-bom-v2]',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_bodies.py::test_marker_bodies_are_parsed_positionally_like_every_other_reader[delegate-without-ct-is-ignored]',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_bodies.py::test_marker_bodies_are_parsed_positionally_like_every_other_reader[fractional-started-at]',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_bodies.py::test_marker_bodies_are_parsed_positionally_like_every_other_reader[garbled-ct-is-v1-fresh]',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_bodies.py::test_marker_bodies_are_parsed_positionally_like_every_other_reader[garbled-ct-is-v1-past-20min]',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_bodies.py::test_marker_bodies_are_parsed_positionally_like_every_other_reader[garbled-line-2]',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_bodies.py::test_marker_bodies_are_parsed_positionally_like_every_other_reader[missing-line-2]',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lifecycle.py::test_desktop_started_handoff_only_adopts_its_bridge[absent]',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lifecycle.py::test_desktop_started_handoff_only_adopts_its_bridge[someone-else]',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lifecycle.py::test_live_desktop_bridge_marker_is_adopted_keeping_its_started_at',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lifecycle.py::test_dead_owner_marker_is_reclaimed_and_released',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lifecycle.py::test_desktop_that_never_exits_is_not_relaunched_over',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lifecycle.py::test_ui_profile_sweep_keeps_dirs_of_live_handoffs',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_custodian.py::test_script_killed_right_after_spawning_the_update_leaves_a_live_marker',
    'tests/scripts/desktop_update/test_desktop_update_windows_handoff_protocol.py::test_launcher_lineage_rule_matches_the_shared_table',
    'tests/scripts/desktop_update/test_desktop_update_windows_handoff_protocol.py::test_handoff_run_adopts_only_the_desktop_bridge_for_that_run[ok]',
    'tests/scripts/desktop_update/test_desktop_update_windows_handoff_protocol.py::test_handoff_run_adopts_only_the_desktop_bridge_for_that_run[other-run]',
    'tests/scripts/desktop_update/test_desktop_update_windows_handoff_protocol.py::test_handoff_run_adopts_only_the_desktop_bridge_for_that_run[stale-ct]',
    'tests/scripts/desktop_update/test_desktop_update_windows_handoff_protocol.py::test_handoff_run_adopts_only_the_desktop_bridge_for_that_run[v1]',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lock.py::test_a7_claim_is_decided_inside_the_marker_lock',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lock.py::test_a7_concurrent_claimants_over_a_dead_marker_yield_one_owner',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lock.py::test_child_observer_uses_live_identity_not_initialized_command_line',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lock.py::test_child_observer_identifies_real_suspended_child',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lock.py::test_child_observer_rejects_non_update_process[existing-identity]',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lock.py::test_child_observer_rejects_non_update_process[wrong-image]',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lock.py::test_child_observer_rejects_descendants',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lock.py::test_help_probe_handshake_gates_real_launcher[False]',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lock.py::test_help_probe_handshake_gates_real_launcher[True]',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lock.py::test_help_probe_rejects_foreign_identity[nonce-another-run-foreign help readiness]',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lock.py::test_help_probe_rejects_foreign_identity[parent--1-help has wrong parent]',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lock.py::test_help_probe_rejects_foreign_identity[created-0.0-help has wrong creation identity]',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lock.py::test_script_killed_before_publishing_the_delegate_runs_no_update',
    'tests/scripts/desktop_update/test_r6_checkout_fixture.py::test_checkout_holder_waits_for_real_r6_before_releasing',
    'tests/scripts/desktop_update/test_r6_checkout_fixture.py::test_checkout_holder_eof_cleanup_unlocks_without_observed_wait',
    'tests/scripts/desktop_update/test_r6_checkout_fixture.py::test_checkout_holder_rejects_failed_acquisition',
    'tests/scripts/desktop_update/test_r6_checkout_fixture.py::test_checkout_holder_cancels_a_missing_release_ack',
    'tests/scripts/desktop_update/test_r6_checkout_fixture.py::test_checkout_holder_reports_missing_r6_without_leaking',
    'tests/scripts/desktop_update/test_desktop_update_windows_marker_lock.py::test_published_fixture_runtime_command_uses_its_own_import_closure',
]

def group_mapping(arch):
    require(arch in ('x64', 'arm64'), 'Unknown architecture')
    groups = ([ALL_FILES[:6], ALL_FILES[6:]] if arch == 'x64'
              else [ALL_FILES[:4], ALL_FILES[4:8], ALL_FILES[8:]])
    return {**{f'files-{i + 1}': names for i, names in enumerate(groups)}, 'mutation': []}


OPENSSL_REQUIRED = ('include/openssl/ssl.h', 'lib/libcrypto.lib', 'lib/libssl.lib')
BUILD_INPUTS = ('.github/actions/setup-windows-build-deps/action.yml',
                'scripts/windows-build-deps.ps1', 'scripts/build/windows-deps.ps1')


def openssl_path(name):
    # Deliberately no general archive extraction or arbitrary filesystem roots.
    return (name in (*OPENSSL_REQUIRED[1:], 'include/openssl/__DECC_INCLUDE_EPILOGUE.H',
                     'include/openssl/__DECC_INCLUDE_PROLOGUE.H') or
            re.fullmatch(r'include/openssl/[a-z0-9_][a-z0-9_.-]*\.h', name) is not None)


def regular_file(path, root):
    require(path.is_relative_to(root) and path.is_file(), f'Missing artifact file: {path}')
    for part in [path, *path.parents]:
        require(not part.is_symlink() and not getattr(part, 'is_junction', lambda: False)(),
                f'Link/reparse path forbidden: {part}')
        if part == root:
            break
    require(path.resolve().is_relative_to(root.resolve()), 'Path escaped root')


def openssl_files(root):
    # glob('*.h') matches .H on Windows but omits it on case-sensitive hosts.
    names = {relative_key(p, root) for p in (root / 'include' / 'openssl').iterdir()
             if p.suffix.lower() == '.h'}
    names.update(OPENSSL_REQUIRED[1:])
    require(set(OPENSSL_REQUIRED) <= names, 'Missing required OpenSSL files')
    for name in names:
        require(openssl_path(name), f'Non-allowlisted OpenSSL path: {name}')
        regular_file(root / name, root)
    return {n: sha((root / n).read_bytes()) for n in sorted(names)}


def export_openssl(installed, output, identity):
    import shutil
    require(not output.exists(), 'Artifact export must be fresh')
    files = openssl_files(installed)
    output.mkdir(parents=True)
    for name in files:
        dest = output / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(installed / name, dest)
    save(output / 'manifest.json', {'identity': identity, 'files': files})
    digest = sha((output / 'manifest.json').read_bytes())
    verify_openssl(output, identity, digest)
    return digest


def verify_openssl(artifact, identity, digest, destination=None, installed=None):
    import shutil
    path = artifact / 'manifest.json'
    regular_file(path, artifact)
    require(re.fullmatch(r'[0-9a-f]{64}', digest or '') is not None and sha(path.read_bytes()) == digest,
            'OpenSSL manifest digest mismatch')
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            require(key not in obj, f'Duplicate manifest key: {key}')
            obj[key] = value
        return obj
    data = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique)
    require(set(data) == {'identity', 'files'} and data['identity'] == identity,
            'Wrong OpenSSL source/build/host/run/controller identity')
    files = data['files']
    require(isinstance(files, dict) and set(OPENSSL_REQUIRED) <= files.keys(), 'Missing required OpenSSL files')
    for name, expected in files.items():
        require(openssl_path(name), f'Non-allowlisted OpenSSL path: {name}')
        require(isinstance(expected, str) and re.fullmatch(r'[0-9a-f]{64}', expected) is not None,
                'Invalid file digest')
        regular_file(artifact / name, artifact)
        require(sha((artifact / name).read_bytes()) == expected, f'OpenSSL hash mismatch: {name}')
    actual = set()
    for p in artifact.rglob('*'):
        require(not p.is_symlink() and not getattr(p, 'is_junction', lambda: False)(), 'Artifact link forbidden')
        if p.is_file():
            actual.add(relative_key(p, artifact))
        else:
            require(p.is_dir() and relative_key(p, artifact) in ('include', 'include/openssl', 'lib'),
                    'Unexpected artifact directory')
    require(actual == set(files) | {'manifest.json'}, 'Unexpected/missing artifact contents')
    if destination is not None:
        # Install only verified, allowlisted headers/libs. Never a home/venv/checkout.
        destination.mkdir(parents=True, exist_ok=True)
        for name in files:
            dest = destination / name
            for part in [dest, *dest.parents]:
                require(not part.is_symlink() and not getattr(part, 'is_junction', lambda: False)(),
                        'Destination link forbidden')
                if part == destination:
                    break
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(artifact / name, dest)
        installed = destination
    if installed is not None:
        require(openssl_files(installed) == files, 'Installed OpenSSL changed after artifact verification/build-deps cache restore')
    return data


def build_identity(root, subject):
    require(re.fullmatch(r'[0-9a-f]{40}', subject or '') is not None, 'Full subject SHA required')
    require(git(root, 'rev-parse', 'HEAD').decode().strip() == subject, 'Wrong subject HEAD')
    git(root, 'diff', '--exit-code', 'HEAD', '--')
    require(platform.system() == 'Windows' and os.name == 'nt' and platform.machine().lower() == 'arm64',
            'Real Windows ARM64 required for OpenSSL artifact')
    return {'subject': subject, 'build_hashes': {n: sha(git(root, 'show', subject + ':' + n)) for n in BUILD_INPUTS},
            'os': 'Windows', 'arch': 'arm64', 'controller_sha256': sha(Path(__file__).read_bytes()),
            'run_id': os.environ['GITHUB_RUN_ID'], 'run_attempt': os.environ['GITHUB_RUN_ATTEMPT']}


RESTAMP = (
    '        if ($script:MarkerReleaseWaited -and [System.IO.File]::Exists($ResultPath)) {\n'
    '            Write-Result ($finalCode -eq 0) $finalCode $finalMsg $manualAction\n'
    '        }\n'
)
ASSERTION = "assert result['ok'] is True and result['finished_at'] >= int(holders[0].released), result"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, timeout=60)


def attributes(root):
    raw = git(root, 'check-attr', '-z', 'text', 'eol', '--', *HASHES).decode().split('\0')
    attrs = {name: {} for name in HASHES}
    for i in range(0, len(raw) - 1, 3):
        name, key, value = raw[i:i + 3]
        attrs[name][key] = value
    return attrs


def projected(data, attrs):
    # Only the repository's explicit text/eol contract permits normalization.
    require(attrs.get('text') == 'set' and attrs.get('eol') in ('lf', 'crlf'),
            f'Unexpected Git attributes: {attrs}')
    lf = data.replace(b'\r\n', b'\n')
    require(b'\r' not in lf, 'Bare carriage return in scoped text')
    if attrs['eol'] == 'lf':
        require(data == lf, 'LF-pinned file contains CRLF')
        return lf
    return lf.replace(b'\n', b'\r\n')


def verify_hashes(root, attrs, subject=None):
    details = {}
    for name, expected in HASHES.items():
        data = (root / name).read_bytes()
        digest = sha(projected(data, attrs[name]))
        require(digest == expected, f'Scoped hash mismatch: {name}: {digest}')
        if subject:
            blob = git(root, 'show', subject + ':' + name)
            require(sha(projected(blob, attrs[name])) == expected, f'Git blob mismatch: {name}')
        details[name] = {'sha256': digest, 'disk_sha256': sha(data), 'attributes': attrs[name]}
    return details


def source_guard(root, subject):
    require(re.fullmatch(r'[0-9a-f]{40}', subject) is not None, 'Subject must be a full SHA; bind placeholder before commit')
    require(git(root, 'rev-parse', 'HEAD').decode().strip() == subject, 'Wrong subject HEAD')
    git(root, 'diff', '--exit-code', 'HEAD', '--')
    attrs = attributes(root)
    return attrs, verify_hashes(root, attrs, subject)


def sandbox(folder, root):
    # Allowlisted locations only: never inherit providers, tokens, profile state,
    # activation, PYTEST_ADDOPTS, external plugins or Git configuration env vars.
    allowed = {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'PROGRAMFILES',
               'PROGRAMFILES(X86)', 'PROGRAMW6432', 'PROGRAMDATA', 'CI', 'GITHUB_ACTIONS'}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    for name in ('home', 'appdata', 'localappdata', 'temp', 'hermes'):
        (folder / name).mkdir(parents=True, exist_ok=True)
    env.update(HOME=str(folder / 'home'), USERPROFILE=str(folder / 'home'),
               APPDATA=str(folder / 'appdata'), LOCALAPPDATA=str(folder / 'localappdata'),
               HERMES_HOME=str(folder / 'hermes'), TEMP=str(folder / 'temp'),
               TMP=str(folder / 'temp'), TMPDIR=str(folder / 'temp'),
               HERMES_PYTHON=sys.executable, HERMES_TEST_WORKERS='2',
               HERMES_TEST_FILE_RETRIES='0', HERMES_TEST_FILE_TIMEOUT='300',
               TZ='UTC', LANG='C.UTF-8', LC_ALL='C.UTF-8', PYTHONHASHSEED='0',
               PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PYTHONPATH=str(root),
               OPENAI_API_KEY='', OPENROUTER_API_KEY='', NOUS_API_KEY='')
    return env


def collect(root, env):
    proc = subprocess.run([sys.executable, '-m', 'pytest', *ALL_FILES, '--collect-only', '-q'],
                          cwd=root, env=env, capture_output=True, text=True, timeout=60, check=False)
    require(proc.returncode == 0, proc.stdout + proc.stderr)
    nodes = [line.strip() for line in proc.stdout.splitlines()
             if any(line.startswith(name + '::') for name in ALL_FILES)]
    require(len(nodes) == len(set(nodes)), 'Duplicate collected nodes')
    counts = {name: sum(n.startswith(name + '::') for n in nodes) for name in ALL_FILES}
    require(all(counts.values()) and sum(counts[n] for n in FILES) == 41
            and counts[MARKER_LOCK] == 14 and counts[FIXTURE] == 5, f'Wrong collection: {counts}')
    require(sorted(nodes) == sorted(EXPECTED_NODES), 'Original 59 plus portable fixture case identities changed')
    selector = subprocess.run([sys.executable, 'scripts/ci/list_os_marked_tests.py', 'windows'],
                              cwd=root, env=env, capture_output=True, text=True, timeout=60, check=True)
    require(set(FILES) <= set(selector.stdout.splitlines()), 'Windows selector omitted partition files')
    require(TIMESTAMP in nodes, 'Timestamp node missing')
    return [TIMESTAMP] + [node for node in nodes if node != TIMESTAMP]


def junit(path):
    tree = ET.parse(path).getroot()
    cases = list(tree.iter('testcase'))
    suites = list(tree.iter('testsuite'))
    require(bool(suites), 'Missing JUnit testsuite')
    # Inspect actual cases AND declared totals; setup errors/skips are not passes.
    counts = {'tests': len(cases), 'failures': len(list(tree.iter('failure'))),
              'errors': len(list(tree.iter('error'))), 'skipped': len(list(tree.iter('skipped')))}
    for key, actual in counts.items():
        declared = sum(int(s.get(key, '0')) for s in suites)
        require(declared == actual, f'JUnit declared {key}={declared}, actual={actual}')
    return counts, cases


def timestamp_mismatch(text):
    match = re.search(r'^E\s+assert \(True is True and (\d+) >= (\d+)\)', text, re.MULTILINE)
    if match is None:
        raise RuntimeError('No actual True result / numeric timestamp mismatch in rewritten assertion')
    finished, released = map(int, match.groups())
    require(finished < released, 'Expected a stale result, not another line-34 failure')
    return {'finished_at': finished, 'release_lower_bound_integer': released, 'result_ok': True}


def classify(path, code, expected_count=1, node=None, red=False):
    counts, cases = junit(path)
    expected = {'tests': expected_count, 'failures': int(red), 'errors': 0, 'skipped': 0}
    require(counts == expected, f'JUnit outcomes {counts}, expected {expected}')
    require(code == (1 if red else 0), f'Unexpected pytest/runner exit {code}')
    if node:
        require(len(cases) == 1 and cases[0].get('name') == node.split('::')[-1], 'Wrong JUnit testcase name')
        parts = node.split('::')
        classname = '.'.join([parts[0].removesuffix('.py').replace('/', '.'), *parts[1:-1]])
        require(cases[0].get('classname') == classname,
                'Wrong JUnit testcase class/file')
    if red:
        failure = cases[0].find('failure')
        if failure is None:
            raise RuntimeError('Expected timestamp failure element')
        text = failure.text or ''
        # Earlier passed code may appear in a long traceback. Only the actual
        # > failure source line, the terminal location AND assertion type count.
        arrows = [line[1:].strip() for line in text.splitlines() if line.startswith('>')]
        require(arrows == [ASSERTION], f'Not the timestamp assertion: {arrows}')
        require(re.search(r'(?:^|[/\\])test_desktop_update_windows_commit_release\.py:34: AssertionError', text) is not None,
                'Missing exact timestamp assertion location (line 34)')
        require('AssertionError' in failure.get('message', ''), 'Not an assertion failure')
        timestamp_mismatch(text)
    return counts


def execute(command, root, folder, env, timeout=300):
    folder.mkdir(parents=True, exist_ok=True)
    start, clock = time.time(), time.monotonic()
    result = {'command': command, 'cwd': str(root), 'started_epoch': start,
              'outer_budget_seconds': timeout, 'outer_timeout': False, 'cleanup': None,
              'state': 'running'}
    with (folder / 'pytest.log').open('w', encoding='utf-8') as log:
        proc = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
        result['owned_pid'] = proc.pid
        # Cancellation can kill this controller before wait() returns. Retain
        # the exact owned invocation/start, without claiming it completed.
        save(folder / 'process.json', result)
        try:
            result['returncode'] = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            result['outer_timeout'] = True
            # Exact owned PID tree only; never kill by executable image name.
            kill = subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)],
                                  capture_output=True, text=True, timeout=15, check=False)
            result['cleanup'] = {'returncode': kill.returncode, 'stdout': kill.stdout, 'stderr': kill.stderr}
            result['returncode'] = proc.wait(timeout=15)
    result['elapsed_seconds'] = time.monotonic() - clock
    result['state'] = 'completed'
    save(folder / 'process.json', result)
    return result


def run_case(root, folder, node, red=False):
    result = {'node': node, 'accepted': False, 'expected_mutation_red': red}
    try:
        env = sandbox(folder / 'sandbox', root)
        command = [sys.executable, '-m', 'pytest', node, '-x', '-vv', '--tb=long', '--durations=0',
                   '-o', 'faulthandler_timeout=60', '--basetemp', str(folder / 'tmp'),
                   '--junitxml', str(folder / 'junit.xml')]
        result.update(execute(command, root, folder, env))
        require(not result['outer_timeout'], 'Existing 300s outer deadline exceeded')
        result['junit'] = classify(folder / 'junit.xml', result['returncode'], node=node, red=red)
        if red:
            logs = list((folder / 'tmp').rglob('desktop-update-handoff.log'))
            require(len(logs) == 1, f'Expected one retained handoff log; got {len(logs)}')
            text = logs[0].read_text(encoding='utf-8-sig')
            require('keeping the update marker' in text, 'No real R6 wait in retained log')
            require('removed update marker (owned)' in text, 'No owned marker removal in retained log')
            require(not (logs[0].parent.parent / '.hermes-update-in-progress').exists(), 'Owned marker remains')
            _, cases = junit(folder / 'junit.xml')
            failure = cases[0].find('failure')
            if failure is None:
                raise RuntimeError('Timestamp failure element disappeared')
            result['timestamp_evidence'] = timestamp_mismatch(failure.text or '')
            published = json.loads((logs[0].parent.parent / '.hermes-update-result.json').read_text(encoding='utf-8-sig'))
            require(published['ok'] is True and published['finished_at'] == result['timestamp_evidence']['finished_at'],
                    'Retained actual result does not match timestamp assertion evidence')
            result['handoff_log'] = str(logs[0])
            result['handoff_log_sha256'] = sha(logs[0].read_bytes())
            result['release_ack_proof'] = 'Unchanged test passed holders[0].finish() at line 30 and failed only line 34'
        result['accepted'] = True
    except Exception as exc:
        result['error'] = repr(exc)
    folder.mkdir(parents=True, exist_ok=True)
    save(folder / 'result.json', result)
    print(json.dumps(result), flush=True)
    return result


def cache_guard(root, name):
    cache = root / 'test_durations.json'
    data = json.loads(cache.read_text(encoding='utf-8')) if cache.exists() else {}
    require(isinstance(data, dict) and name not in data, f'Cached timeout scaler entry forbidden: {name}')
    return {'cache_entry_absent': name, 'effective_file_timeout_seconds': 300}


def case_identity(node):
    parts = node.split('::')
    return ('.'.join([parts[0].removesuffix('.py').replace('/', '.'), *parts[1:-1]]), parts[-1])


def match_inventory(path, expected):
    _, cases = junit(path)
    actual = [(c.get('classname'), c.get('name')) for c in cases]
    wanted = [case_identity(n) for n in expected]
    require(len(actual) == len(set(actual)) and sorted(actual) == sorted(wanted),
            f'JUnit inventory mismatch: actual={actual}, expected={wanted}')
    return list(expected)


def aggregate(results, nodes):
    actual = [n for r in results for n in r.get('nodes', [])]
    underlying = [{'file': r.get('file'), 'error': r.get('error', 'No accepted outcome')}
                  for r in results if not r.get('accepted')]
    require(len(actual) == len(set(actual)) and sorted(actual) == sorted(nodes),
            'Missing/duplicate whole-file case inventory; underlying file failures: '
            + json.dumps(underlying, sort_keys=True))
    require(len(results) == len(ALL_FILES) and {r['file'] for r in results} == set(ALL_FILES),
            'Missing/duplicate whole-file executions')
    return {'collected': len(nodes), 'completed': len(actual), 'unique': len(set(actual)),
            'passed': sum(len(r['nodes']) for r in results if r['accepted']),
            'whole_files': len(results), 'retries': 0, 'skipped': 0}


def aggregate_groups(records, identity):
    expected_groups = {(a, g) for a in ('x64', 'arm64') for g in group_mapping(a)}
    keys = [(r.get('architecture'), r.get('group')) for r in records]
    errors = [{'arch': r.get('architecture'), 'group': r.get('group'),
               'error': r.get('error'), 'files': [{'file': f.get('file'), 'error': f.get('error')}
               for f in r.get('files', []) if not f.get('accepted')]}
              for r in records if not r.get('accepted')]
    context = '; underlying group/file failures: ' + json.dumps(errors, sort_keys=True)
    require(len(keys) == len(set(keys)) and set(keys) == expected_groups,
            'Missing/duplicate/unexpected architecture groups' + context)
    totals = {}
    for arch in ('x64', 'arm64'):
        files = []
        for r in records:
            if r['architecture'] != arch:
                continue
            require(all(r.get(k) == v for k, v in identity.items()), 'Wrong subject/controller/run identity' + context)
            require(r.get('accepted') is True and r.get('source_unchanged') is True,
                    'Group failed or source changed' + context)
            require(len(r.get('nodes', [])) == len(set(r.get('nodes', []))) and
                    sorted(r.get('nodes', [])) == sorted(EXPECTED_NODES), 'Wrong exact global case identities')
            wanted = group_mapping(arch)[r['group']]
            actual = [f.get('file') for f in r.get('files', [])]
            require(len(actual) == len(set(actual)) and set(actual) == set(wanted),
                    'Missing/duplicate/wrong group whole files' + context)
            require(all(f.get('accepted') is True for f in r['files']), 'Rejected whole-file outcome' + context)
            if r['group'] == 'mutation':
                require(r.get('mutation', {}).get('accepted') is True, 'Missing real mutation RED')
            else:
                require(r.get('mutation') is None, 'Unexpected duplicate mutation')
            files.extend(r['files'])
        totals[arch] = aggregate(files, EXPECTED_NODES)
        require(totals[arch]['passed'] == 60, 'Not the complete 60-case acceptance inventory')
    return totals


def run_file(root, folder, name, nodes):
    result = {'file': name, 'accepted': False, 'nodes': []}
    try:
        env = sandbox(folder / 'sandbox', root)
        result.update(cache_guard(root, name))
        command = [str(resolve_native_bash(Path(os.environ['ProgramFiles']))), 'scripts/run_tests.sh',
                   '-j', '1', '--file-retries', '0', '--file-timeout', '300', '--fail-on-skip', name,
                   '--', '-vv', '--tb=long', '--basetemp', str(folder / 'tmp'),
                   '--junitxml', str(folder / 'junit.xml')]
        result.update(execute(command, root, folder, env))
        require(not result['outer_timeout'] and result['elapsed_seconds'] < 300,
                'Whole-file wall exceeded unchanged 300s budget')
        result['junit'] = classify(folder / 'junit.xml', result['returncode'], expected_count=len(nodes))
        result['nodes'] = match_inventory(folder / 'junit.xml', nodes)
        result['accepted'] = True
    except Exception as exc:
        result['error'] = repr(exc)
    folder.mkdir(parents=True, exist_ok=True)
    save(folder / 'result.json', result)
    print(json.dumps(result), flush=True)
    return result


def mutate(data):
    newline = b'\r\n' if b'\r\n' in data else b'\n'
    require(b'\r' not in data.replace(b'\r\n', b''), 'Mixed/bare CR in mutation input')
    block = RESTAMP.encode().replace(b'\n', newline)
    require(data.count(block) == 1, 'Expected exactly one documented post-R6 restamp block')
    before, after = data.split(block)
    require(before.rstrip().endswith(b'# finish time (unless something already consumed it).'), 'Wrong restamp context')
    require(b'Invoke-MarkerRelease' in before and b'Write-Result ($finalCode -eq 0)' in before,
            'Initial result and R6 release must remain')
    return before + after


def relative_key(path, root):
    return path.relative_to(root).as_posix()


def resolve_native_bash(program_files):
    require(program_files.is_absolute(), 'Program Files must be an absolute path')
    git_root = (program_files / 'Git').resolve()
    executable = (git_root / 'bin' / 'bash.exe').resolve()
    require(executable.is_file() and executable.is_relative_to(git_root), 'Native Git Bash is required')
    return executable


def manifest(root):
    return {relative_key(p, root): sha(p.read_bytes()) for p in root.rglob('*') if p.is_file()}


def mutation(root, output, subject, attrs):
    folder = output / 'mutation'
    folder.mkdir()
    archive = folder / 'subject.tar'
    with archive.open('wb') as stream:
        subprocess.run(['git', 'archive', '--format=tar', subject], cwd=root, stdout=stream, check=True, timeout=60)
    copy = folder / 'subject'
    copy.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(copy, filter='data')
    verify_hashes(copy, attrs)
    before = manifest(copy)
    ps = copy / POWERSHELL
    ps.write_bytes(mutate(ps.read_bytes()))
    after = manifest(copy)
    changed = [name for name in before.keys() | after.keys() if before.get(name) != after.get(name)]
    require(changed == [POWERSHELL], f'Mutation changed unexpected paths: {changed}')
    save(folder / 'mutation.json', {'subject': subject, 'archive_sha256': sha(archive.read_bytes()),
                                  'changed_paths': changed, 'before_sha256': before[POWERSHELL],
                                  'after_sha256': after[POWERSHELL], 'test_sha256': after[TIMESTAMP_FILE]})
    return run_case(copy, folder / 'timestamp-red', TIMESTAMP, red=True)


class ControllerUnitTests(unittest.TestCase):
    """Synthetic XML/bytes only; these are NOT native Windows behavioral evidence."""
    def test_relative_keys_are_portable_without_os_spoofing(self):
        from pathlib import PurePosixPath, PureWindowsPath
        for kind, prefix in ((PurePosixPath, '/root'), (PureWindowsPath, 'C:/root')):
            with self.subTest(kind=kind):
                root = kind(prefix)
                self.assertEqual(relative_key(root / POWERSHELL, root), POWERSHELL)

    def test_git_bash_resolution_never_uses_bare_or_system32_bash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git_bash = root / 'Git' / 'bin' / 'bash.exe'
            git_bash.parent.mkdir(parents=True)
            git_bash.write_bytes(b'unit fixture only; never executed')
            self.assertEqual(resolve_native_bash(root), git_bash.resolve())
            git_bash.unlink()
            wsl = root / 'System32' / 'bash.exe'
            wsl.parent.mkdir()
            wsl.write_bytes(b'unit fixture only; never executed')
            with self.assertRaises(RuntimeError):
                resolve_native_bash(root)

    def test_junit_class_nodes_keep_all_class_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'junit.xml'
            suite = ET.Element('testsuite', tests='1', failures='0', errors='0', skipped='0')
            ET.SubElement(suite, 'testcase', name='test_case[param]', classname='tests.sample.TestOuter.TestInner')
            ET.ElementTree(suite).write(path)
            self.assertEqual(classify(path, 0, node='tests/sample.py::TestOuter::TestInner::test_case[param]')['tests'], 1)

    def test_whole_file_inventory_and_cache_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'junit.xml'
            nodes = ['tests/sample.py::TestOuter::test_case[a]', 'tests/sample.py::TestInner::test_case[b]']
            suite = ET.Element('testsuite', tests='2', failures='0', errors='0', skipped='0')
            for node in nodes:
                classname, name = case_identity(node)
                ET.SubElement(suite, 'testcase', name=name, classname=classname)
            ET.ElementTree(suite).write(path)
            self.assertEqual(match_inventory(path, nodes), nodes)
            for wrong in (nodes[:1], [nodes[0], nodes[0]], [n.replace('TestInner', 'TestWrong') for n in nodes]):
                with self.assertRaises(RuntimeError):
                    match_inventory(path, wrong)
            self.assertEqual(cache_guard(root, FILES[0])['effective_file_timeout_seconds'], 300)
            save(root / 'test_durations.json', {FILES[1]: 1000})
            cache_guard(root, FILES[0])  # Completed other files cannot inflate this file.
            save(root / 'test_durations.json', {FILES[0]: 1000})
            with self.assertRaises(RuntimeError):
                cache_guard(root, FILES[0])
            results = [{'file': f, 'nodes': [f + '::test_case'], 'accepted': True} for f in ALL_FILES]
            expected = [r['nodes'][0] for r in results]
            self.assertEqual(aggregate(results, expected)['whole_files'], 11)
            for bad in (results[:-1], results + results[:1], results[:-1] + results[:1]):
                with self.assertRaises(RuntimeError):
                    aggregate(bad, expected)

    def test_inventory_failure_retains_underlying_file_errors(self):
        results = [{'file': f, 'nodes': [f + '::test_case'], 'accepted': True} for f in ALL_FILES]
        expected = [r['nodes'][0] for r in results]
        results[0].update(nodes=[], accepted=False, error='Whole-file wall exceeded unchanged 300s budget')
        results[1].update(nodes=[], accepted=False, error='JUnit outcomes: 2 fixture import failures')
        with self.assertRaises(RuntimeError) as raised:
            aggregate(results, expected)
        message = str(raised.exception)
        self.assertIn('Missing/duplicate whole-file case inventory', message)
        for result in results[:2]:
            self.assertIn(result['file'], message)
            self.assertIn(result['error'], message)

    def test_process_record_survives_interrupted_controller(self):
        # A real local child checks the durable record before it exits. This
        # exercises telemetry only, not Windows acceptance or a fake host.
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            command = [sys.executable, '-c',
                       'import json, pathlib, sys, time; '
                       'p = pathlib.Path(sys.argv[1]); '
                       'deadline = time.monotonic() + 2; '
                       '\nwhile not p.exists() and time.monotonic() < deadline: time.sleep(.01)\n'
                       'r = json.loads(p.read_text()); '
                       'assert r["state"] == "running"; '
                       'assert r["outer_budget_seconds"] == 300',
                       str(folder / 'process.json')]
            result = execute(command, folder, folder, dict(os.environ))
            self.assertEqual(result['returncode'], 0)
            self.assertEqual(result['state'], 'completed')
            self.assertEqual(json.loads((folder / 'process.json').read_text()), result)

    def test_mutation_exact_and_guarded(self):
        context = b'Write-Result ($finalCode -eq 0)\nInvoke-MarkerRelease\n# finish time (unless something already consumed it).\n'
        for nl in (b'\n', b'\r\n'):
            source = (context + RESTAMP.encode() + b'unchanged\n').replace(b'\n', nl)
            self.assertEqual(mutate(source), (context + b'unchanged\n').replace(b'\n', nl))
            for bad in (source + RESTAMP.encode().replace(b'\n', nl), b'none', RESTAMP.encode()):
                with self.assertRaises(RuntimeError):
                    mutate(bad)

    def test_attributes_not_blind_normalization(self):
        self.assertEqual(projected(b'a\nb\n', {'text': 'set', 'eol': 'crlf'}), b'a\r\nb\r\n')
        for data, attrs in ((b'a\r\n', {'text': 'set', 'eol': 'lf'}),
                            (b'a\rb', {'text': 'set', 'eol': 'crlf'}), (b'a\n', {})):
            with self.assertRaises(RuntimeError):
                projected(data, attrs)

    def test_junit_accepts_only_exact_pass_and_actual_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'junit.xml'
            def xml(fail=0, errors=0, skip=0, body='', tests=1, name=TIMESTAMP_NAME):
                suite = ET.Element('testsuite', tests=str(tests), failures=str(fail), errors=str(errors), skipped=str(skip))
                case = ET.SubElement(suite, 'testcase', name=name,
                                     classname=TIMESTAMP_FILE.removesuffix('.py').replace('/', '.'))
                if fail:
                    ET.SubElement(case, 'failure', message='AssertionError').text = body
                if errors:
                    ET.SubElement(case, 'error').text = body
                if skip:
                    ET.SubElement(case, 'skipped')
                ET.ElementTree(suite).write(path)
            xml()
            self.assertEqual(classify(path, 0, node=TIMESTAMP)['tests'], 1)
            red = '>       ' + ASSERTION + '\nE       assert (True is True and 1 >= 2)\n' + TIMESTAMP_FILE + ':34: AssertionError'
            xml(fail=1, body=red)
            self.assertEqual(classify(path, 1, node=TIMESTAMP, red=True)['failures'], 1)
            # Critical trap: passed source contains target assertion, but > is a different failure.
            for body in (ASSERTION + '\n>       holders[0].finish()\n' + TIMESTAMP_FILE + ':34: AssertionError',
                         red.replace(':34:', ':33:'), red.replace(ASSERTION, 'assert False'),
                         red.replace('True is True', 'False is True'), red.replace('1 >= 2', '2 >= 1')):
                xml(fail=1, body=body)
                with self.assertRaises(RuntimeError):
                    classify(path, 1, node=TIMESTAMP, red=True)
            for kwargs in ({'skip': 1}, {'errors': 1}, {'tests': 2}):
                xml(skip=kwargs.get('skip', 0), errors=kwargs.get('errors', 0), tests=kwargs.get('tests', 1))
                with self.assertRaises(RuntimeError):
                    classify(path, 0, node=TIMESTAMP)
            xml(name='wrong')
            with self.assertRaises(RuntimeError):
                classify(path, 0, node=TIMESTAMP)
            xml()
            with self.assertRaises(RuntimeError):
                classify(path, 1, node=TIMESTAMP)


def load_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def verify_process(folder, result, red=False):
    process = load_json(folder / 'process.json')
    require(all(result.get(k) == v for k, v in process.items()), 'Process/result record mismatch')
    require(process.get('state') == 'completed' and process.get('outer_timeout') is False
            and process.get('outer_budget_seconds') == 300 and process.get('cleanup') is None
            and process.get('returncode') == int(red)
            and 0 <= process.get('elapsed_seconds', 300) < 300, 'Rejected process outcome/deadline')
    return process


def resolve_mutation_log(directory, result):
    from pathlib import PureWindowsPath
    # Upload-artifact dereferences pytest's `...current` directory junction.
    # The original native rglob sees one log; the downloaded artifact has two
    # byte-identical paths. Anchor to the recorded canonical path, not a glob.
    base = PureWindowsPath(result['command'][11])
    recorded = PureWindowsPath(result['handoff_log'])
    require(base.is_absolute() and recorded.is_absolute(), 'Absolute recorded Windows paths required')
    relative = recorded.relative_to(base)
    require(len(relative.parts) == 4 and relative.parts[1:] == ('home', 'logs', 'desktop-update-handoff.log')
            and re.fullmatch(r'test_result_finished_at_[a-z_]+[0-9]+', relative.parts[0]) is not None,
            'Wrong canonical mutation log path')
    canonical = directory / 'tmp' / Path(*relative.parts)
    regular_file(canonical, directory / 'tmp')
    alias = directory / 'tmp' / Path(re.sub(r'[0-9]+$', 'current', relative.parts[0]), *relative.parts[1:])
    logs = list((directory / 'tmp').rglob('desktop-update-handoff.log'))
    require(canonical in logs and set(logs) <= {canonical, alias}, 'Unexpected mutation log paths')
    for log in logs:
        regular_file(log, directory / 'tmp')
        require(sha(log.read_bytes()) == result.get('handoff_log_sha256'), 'Changed/foreign mutation log or alias')
    return canonical


def mutation_source_identity(root, subject):
    # Native git archive applies the explicit text/eol export conversion too.
    # Compare Windows CRLF script bytes, not the raw LF Git blob on Linux.
    attrs = attributes(root)
    original = projected(git(root, 'show', subject + ':' + POWERSHELL), attrs[POWERSHELL])
    test = projected(git(root, 'show', subject + ':' + TIMESTAMP_FILE), attrs[TIMESTAMP_FILE])
    return {'before_sha256': sha(original), 'after_sha256': sha(mutate(original)), 'test_sha256': sha(test)}


def verify_group_artifact(folder, identity, root=None):
    metadata, summary = [load_json(folder / n) for n in ('metadata.json', 'summary.json')]
    for data in (metadata, summary):
        require(all(data.get(k) == v for k, v in identity.items()), 'Wrong downloaded source/controller/run')
    arch, group = metadata.get('architecture'), metadata.get('group')
    require(arch in ('x64', 'arm64') and group in group_mapping(arch), 'Wrong downloaded architecture/group')
    require(summary.get('architecture') == arch and summary.get('group') == group, 'Summary/metadata group mismatch')
    require(metadata.get('host_os') == 'Windows' and
            metadata.get('native_python_platform') == {'x64': 'win-amd64', 'arm64': 'win-arm64'}[arch]
            and metadata.get('python', '').startswith('3.14.9 '), 'Not recorded native Windows Python 3.14.9')
    require(metadata.get('workers') == 2 and metadata.get('retries') == 0
            and metadata.get('effective_file_budget_seconds') == 300 and metadata.get('validation_only') is False,
            'Wrong worker/retry/cap/execution policy')
    require(metadata.get('collected') == 60 and metadata.get('unique') == 60 and
            len(metadata.get('nodes', [])) == 60 and sorted(metadata['nodes']) == sorted(EXPECTED_NODES),
            'Wrong exact collected case identities')
    require({n: d.get('sha256') for n, d in metadata.get('hashes', {}).items()} == HASHES,
            'Wrong scoped source manifest')
    # Keep the original per-file error rather than masking it with an XML error.
    require(summary.get('accepted') is True and summary.get('source_unchanged') is True,
            'Rejected group: ' + json.dumps(summary, sort_keys=True))
    results = summary.get('files', [])
    wanted = group_mapping(arch)[group]
    require(len(results) == len(wanted) and {r.get('file') for r in results} == set(wanted),
            'Wrong group whole-file inventory')
    for result in results:
        name = result['file']
        directory = folder / Path(name).stem
        require(result == load_json(directory / 'result.json') and result.get('accepted') is True,
                'Whole-file result mismatch/failure: ' + name)
        process = verify_process(directory, result)
        command = process.get('command', [])
        require(len(command) == 17 and command[0].replace('\\', '/').endswith('/Git/bin/bash.exe')
                and command[1:13] == ['scripts/run_tests.sh', '-j', '1', '--file-retries', '0',
                                     '--file-timeout', '300', '--fail-on-skip', name, '--', '-vv', '--tb=long']
                and command[13] == '--basetemp' and command[15] == '--junitxml',
                'Noncanonical/split/deselected/retried whole-file command')
        nodes = [n for n in EXPECTED_NODES if n.startswith(name + '::')]
        require(result.get('cache_entry_absent') == name and result.get('effective_file_timeout_seconds') == 300,
                'Timeout scaling/cache guard missing')
        require(result.get('junit') == classify(directory / 'junit.xml', process['returncode'], len(nodes)),
                'Recorded/rechecked JUnit outcomes differ')
        require(sorted(result.get('nodes', [])) == sorted(match_inventory(directory / 'junit.xml', nodes)),
                'Wrong file identities')
    red = summary.get('mutation')
    if group == 'mutation':
        require(root is not None, 'Exact source needed to verify mutation')
        directory = folder / 'mutation' / 'timestamp-red'
        require(red == load_json(directory / 'result.json') and red.get('accepted') is True
                and red.get('node') == TIMESTAMP and red.get('expected_mutation_red') is True, 'Wrong mutation result')
        process = verify_process(directory, red, red=True)
        command = process['command']
        require(len(command) == 14 and command[1:11] == ['-m', 'pytest', TIMESTAMP, '-x', '-vv', '--tb=long',
                '--durations=0', '-o', 'faulthandler_timeout=60', '--basetemp'] and command[12] == '--junitxml',
                'Wrong mutation command')
        require(red['junit'] == classify(directory / 'junit.xml', 1, node=TIMESTAMP, red=True), 'Wrong mutation JUnit')
        record = load_json(folder / 'mutation' / 'mutation.json')
        expected_source = mutation_source_identity(root, identity['subject'])
        require(record.get('subject') == identity['subject'] and record.get('changed_paths') == [POWERSHELL]
                and all(record.get(k) == v for k, v in expected_source.items()),
                'Wrong/nonreal mutation source identity')
        log = resolve_mutation_log(directory, red)
        text = log.read_text(encoding='utf-8-sig')
        require('keeping the update marker' in text and 'removed update marker (owned)' in text,
                'Wrong real R6 mutation evidence')
        _, cases = junit(directory / 'junit.xml')
        failure = cases[0].find('failure')
        require(failure is not None, 'Mutation failure element missing')
        if failure is None:
            raise RuntimeError('Mutation failure element missing')
        evidence = timestamp_mismatch(failure.text or '')
        published = load_json(log.parent.parent / '.hermes-update-result.json')
        require(evidence == red.get('timestamp_evidence') and published.get('ok') is True
                and published.get('finished_at') == evidence['finished_at'], 'Wrong actual stale result')
    else:
        require(red is None, 'Unexpected mutation outside dedicated group')
    return {**summary, 'nodes': metadata['nodes']}


def aggregate_artifacts(root, artifacts, subject, output):
    identity = {'subject': subject, 'controller_sha256': sha(Path(__file__).read_bytes()),
                'run_id': os.environ['GITHUB_RUN_ID'], 'run_attempt': os.environ['GITHUB_RUN_ATTEMPT']}
    result = {**identity, 'accepted': False, 'errors': []}
    records = []
    try:
        source_guard(root, subject)
        expected = {f'windows-acceptance-{a}-{g}-{subject}-{identity["run_id"]}-{identity["run_attempt"]}'
                    for a in ('x64', 'arm64') for g in group_mapping(a)}
        actual = {p.name for p in artifacts.iterdir()}
        if actual != expected:
            result['errors'].append({'missing_artifacts': sorted(expected - actual),
                                     'extra_artifacts': sorted(actual - expected)})
        for name in sorted(expected & actual):
            try:
                records.append(verify_group_artifact(artifacts / name, identity, root))
            except Exception as exc:
                result['errors'].append({'artifact': name, 'error': repr(exc)})
        require(not result['errors'], 'Rejected group evidence; see per-group errors')
        result['totals'] = aggregate_groups(records, identity)
        needs = json.loads(os.environ['ACCEPTANCE_NEEDS_JSON'])
        require(set(needs) == {'prepare-arm-openssl', 'whole-file-acceptance'} and
                all(v.get('result') == 'success' for v in needs.values()), 'Preparation or matrix job did not succeed')
        result['accepted'] = True
    except Exception as exc:
        result['error'] = repr(exc)
    output.mkdir(parents=True, exist_ok=False)
    save(output / 'aggregate.json', result)
    print(json.dumps(result, indent=2))
    return int(not result['accepted'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path)
    parser.add_argument('--subject')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--arch', choices=('x64', 'arm64'))
    parser.add_argument('--group')
    parser.add_argument('--openssl', choices=('export', 'install', 'verify'))
    parser.add_argument('--artifact', type=Path)
    parser.add_argument('--manifest-sha')
    parser.add_argument('--aggregate-dir', type=Path)
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        return int(not unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ControllerUnitTests)).wasSuccessful())
    if args.repo is None or not args.subject or args.output is None:
        parser.error('--repo --subject --output required')
    if args.openssl:
        identity = build_identity(args.repo.resolve(), args.subject)
        installed = Path('C:/vcpkg/installed/arm64-windows-static-md')
        require(args.artifact is not None, '--artifact required')
        if args.openssl == 'export':
            digest = export_openssl(installed, args.artifact, identity)
            with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
                stream.write('manifest_sha256=' + digest + '\n')
        else:
            verify_openssl(args.artifact, identity, args.manifest_sha,
                           destination=installed if args.openssl == 'install' else None,
                           installed=installed if args.openssl == 'verify' else None)
        return 0
    if args.aggregate_dir is not None:
        return aggregate_artifacts(args.repo.resolve(), args.aggregate_dir, args.subject, args.output)
    require(sys.version_info[:3] == (3, 14, 9), 'Exact Python 3.14.9 required')
    if not args.list:
        require(platform.system() == 'Windows' and os.name == 'nt', 'Native Windows required; no spoofing')
        require(args.arch is not None and platform.machine().lower() == {'x64': 'amd64', 'arm64': 'arm64'}[args.arch],
                'Native host architecture must match matrix')
        require(__import__('struct').calcsize('P') == 8, '64-bit Python required')
        require(__import__('sysconfig').get_platform() == {'x64': 'win-amd64', 'arm64': 'win-arm64'}[args.arch or ''],
                'Python must be native to the matrix architecture, not emulated')
    require(args.arch in ('x64', 'arm64') and args.group in group_mapping(args.arch), 'Explicit bounded group required')
    root, output = args.repo.resolve(), args.output.resolve()
    require(not output.is_relative_to(root), 'Evidence must be outside source checkout')
    output.mkdir(parents=True, exist_ok=False)
    attrs, hashes = source_guard(root, args.subject)
    nodes = collect(root, sandbox(output / 'collection-sandbox', root))
    metadata = {'subject': args.subject, 'hashes': hashes, 'nodes': nodes, 'collected': len(nodes),
                'group': args.group, 'run_id': os.environ['GITHUB_RUN_ID'], 'run_attempt': os.environ['GITHUB_RUN_ATTEMPT'],
                'host_os': platform.system(), 'native_python_platform': __import__('sysconfig').get_platform(),
                'unique': len(set(nodes)), 'workers': 2, 'retries': 0, 'effective_file_budget_seconds': 300, 'architecture': args.arch,
                'python': sys.version, 'executable': sys.executable, 'platform': platform.platform(),
                'controller_sha256': sha(Path(__file__).read_bytes()), 'validation_only': args.list,
                'full_file_CI_gate': 'Canonical whole-file -j1, two simultaneous files, 300s, no retries/skips'}
    save(output / 'metadata.json', metadata)
    if args.list:
        print(json.dumps(metadata, indent=2))
        return 0
    summary = {k: metadata[k] for k in ('subject', 'architecture', 'group', 'controller_sha256', 'run_id', 'run_attempt')}
    summary.update(accepted=False, files=[], mutation=None)
    try:
        cache = root / 'test_durations.json'
        require(not cache.exists(), 'Inherited duration cache is forbidden; use a fresh checkout')
        def whole_file(name):
            expected = [n for n in nodes if n.startswith(name + '::')]
            return run_file(root, output / Path(name).stem, name, expected)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(whole_file, group_mapping(args.arch)[args.group]))
        summary['files'] = results
        # Dedicated sequential group: no concurrent mutation/shared cache risk.
        if args.group == 'mutation':
            summary['mutation'] = mutation(root, output, args.subject, attrs)
        expected = [n for n in nodes if n.split('::')[0] in group_mapping(args.arch)[args.group]]
        actual = [n for r in results for n in r.get('nodes', [])]
        require(len(actual) == len(set(actual)) and sorted(actual) == sorted(expected),
                'Group case inventory mismatch; underlying file failures: ' + json.dumps(results))
        summary['group_totals'] = {'completed': len(actual), 'whole_files': len(results), 'retries': 0, 'skipped': 0}
        require(all(r['accepted'] for r in results), 'Not every canonical whole-file run passed')
        if args.group == 'mutation':
            require(summary['mutation']['accepted'], 'Mutation did not produce exact timestamp RED')
        summary['accepted'] = True
    except Exception as exc:
        summary['error'] = repr(exc)
    finally:
        try:
            _, final = source_guard(root, args.subject)
            require(final == hashes, 'Source hashes changed during execution')
            summary['source_unchanged'] = True
        except Exception as exc:
            summary['source_unchanged'] = False
            summary['accepted'] = False
            summary['source_error'] = repr(exc)
        save(output / 'summary.json', summary)
    print(json.dumps(summary, indent=2))
    return int(not summary['accepted'])


if __name__ == '__main__':
    raise SystemExit(main())
