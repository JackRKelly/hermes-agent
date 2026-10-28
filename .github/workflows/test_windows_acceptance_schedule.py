"""Local contract tests only; never native acceptance evidence."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

spec = importlib.util.spec_from_file_location('controller', Path(__file__).with_name('windows_acceptance_controller.py'))
assert spec is not None and spec.loader is not None
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class ScheduleTests(unittest.TestCase):
    def test_group_mapping_covers_each_whole_file_once_with_bounded_waves(self):
        for arch, max_files, groups in [('x64', 6, 2), ('arm64', 4, 3)]:
            mapping = c.group_mapping(arch)
            self.assertEqual(len(mapping), groups + 1)
            files = [f for key, names in mapping.items() for f in names]
            self.assertEqual(sorted(files), sorted(c.ALL_FILES))
            self.assertEqual(len(files), len(set(files)))
            self.assertTrue(all(len(names) <= max_files for names in mapping.values()))
            self.assertEqual(mapping['mutation'], [])
        self.assertEqual(len(c.EXPECTED_NODES), 60)
        self.assertEqual(len(set(c.EXPECTED_NODES)), 60)

    def test_openssl_artifact_roundtrip_and_fail_closed_controls(self):
        identity = {'subject': 'a' * 40, 'build_hashes': {'script': 'b' * 64},
                    'os': 'Windows', 'arch': 'arm64', 'controller_sha256': 'c' * 64,
                    'run_id': '123', 'run_attempt': '1'}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install, artifact, destination = [root / n for n in ('install', 'artifact', 'destination')]
            for name in c.OPENSSL_REQUIRED:
                p = install / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(('unit bytes ' + name).encode())
            (install / 'credentials.txt').write_text('not for export')
            digest = c.export_openssl(install, artifact, identity)
            self.assertFalse((artifact / 'credentials.txt').exists())
            c.verify_openssl(artifact, identity, digest, destination=destination)
            c.verify_openssl(artifact, identity, digest, installed=destination)
            for name in c.OPENSSL_REQUIRED:
                path = artifact / name
                original = path.read_bytes()
                path.unlink()
                with self.subTest(missing_required=name), self.assertRaises(RuntimeError):
                    c.verify_openssl(artifact, identity, digest)
                path.write_bytes(original)
            extra = artifact / 'credentials.json'
            extra.write_text('must not enter an artifact')
            with self.assertRaises(RuntimeError):
                c.verify_openssl(artifact, identity, digest)
            extra.unlink()
            for field in identity:
                wrong = copy.deepcopy(identity)
                wrong[field] = 'wrong'
                with self.subTest(identity=field), self.assertRaises(RuntimeError):
                    c.verify_openssl(artifact, wrong, digest)
            with self.assertRaises(RuntimeError):
                c.verify_openssl(root / 'absent', identity, digest)
            with self.assertRaises(RuntimeError):
                c.verify_openssl(artifact, identity, '0' * 64)
            target = destination / c.OPENSSL_REQUIRED[0]
            target.write_bytes(b'action cache replaced verified headers')
            with self.assertRaises(RuntimeError):
                c.verify_openssl(artifact, identity, digest, installed=destination)
            target = artifact / c.OPENSSL_REQUIRED[0]
            target.write_bytes(b'wrong hash')
            with self.assertRaises(RuntimeError):
                c.verify_openssl(artifact, identity, digest)

    def test_downloaded_mutation_log_accepts_identical_pytest_current_alias_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rel = 'test_result_finished_at_is_sta0/home/logs/desktop-update-handoff.log'
            path = root / 'tmp' / rel
            path.parent.mkdir(parents=True)
            path.write_bytes(b'unit handoff bytes')
            alias = root / 'tmp' / rel.replace('sta0/', 'stacurrent/')
            alias.parent.mkdir(parents=True)
            alias.write_bytes(path.read_bytes())
            result = {'command': [''] * 11 + ['D:/job/tmp'],
                      'handoff_log': 'D:/job/tmp/' + rel,
                      'handoff_log_sha256': c.sha(path.read_bytes())}
            self.assertEqual(c.resolve_mutation_log(root, result), path)
            alias.write_bytes(b'not the same log')
            with self.assertRaises(RuntimeError):
                c.resolve_mutation_log(root, result)
            alias.write_bytes(path.read_bytes())
            result['handoff_log'] = 'D:/job/tmp/../foreign/logs/desktop-update-handoff.log'
            with self.assertRaises(RuntimeError):
                c.resolve_mutation_log(root, result)

    def test_mutation_identity_replays_real_archive_crlf_contract(self):
        # Historical failed native run's source identity only; no new host pass.
        root = Path(__file__).resolve().parents[2]
        identity = c.mutation_source_identity(root, '2b682fffaa6c06fb700df0d32e21c0b4ac7370e6')
        self.assertEqual(identity, {
            'before_sha256': 'b9e83ea4819a1a4bcedd023f2b3caf145f303c209bbb88404834c8f16e1fb760',
            'after_sha256': '1da85f329fe6652bf2e147dfa253b996510d5129119e48735c67465a45a68d07',
            'test_sha256': '5d7ddb97bd93d21487e6ff18fe7ee83edf149bbdb79c4e5f19c7ef59aaf72493'})

    def test_install_does_not_create_directories_through_a_destination_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install, artifact, destination, outside = [root / n for n in ('install', 'artifact', 'dest', 'outside')]
            for name in c.OPENSSL_REQUIRED:
                path = install / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'unit fixture')
            digest = c.export_openssl(install, artifact, {})
            destination.mkdir()
            outside.mkdir()
            (destination / 'include').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(RuntimeError):
                c.verify_openssl(artifact, {}, digest, destination=destination)
            self.assertEqual(list(outside.iterdir()), [])

    def test_global_aggregate_rejects_partial_duplicate_wrong_identity_and_failure(self):
        # Real filesystem/JUnit verifier is exercised separately below. Here the
        # cross-group seam uses explicitly labelled unit records, not native data.
        records = []
        for arch in ('x64', 'arm64'):
            for group, names in c.group_mapping(arch).items():
                records.append({'architecture': arch, 'group': group, 'subject': 'a' * 40,
                                'controller_sha256': 'b' * 64, 'run_id': '123', 'run_attempt': '1',
                                'nodes': c.EXPECTED_NODES, 'files': [
                                    {'file': f, 'nodes': [n for n in c.EXPECTED_NODES if n.startswith(f + '::')],
                                     'accepted': True} for f in names],
                                'mutation': {'accepted': True} if group == 'mutation' else None,
                                'accepted': True, 'source_unchanged': True})
        identity = {'subject': 'a' * 40, 'controller_sha256': 'b' * 64, 'run_id': '123', 'run_attempt': '1'}
        self.assertEqual(c.aggregate_groups(records, identity)['x64']['passed'], 60)
        for bad in (records[:-1], records + records[:1]):
            with self.assertRaises(RuntimeError):
                c.aggregate_groups(bad, identity)
        for field, wrong in [('subject', 'c' * 40), ('architecture', 'wrong'), ('group', 'wrong'),
                             ('controller_sha256', 'c' * 64), ('run_attempt', '2'),
                             ('accepted', False), ('source_unchanged', False), ('nodes', c.EXPECTED_NODES[:-1])]:
            bad = copy.deepcopy(records)
            bad[0][field] = wrong
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                c.aggregate_groups(bad, identity)
        for changed in ('missing', 'duplicate', 'failure'):
            bad = copy.deepcopy(records)
            if changed == 'missing':
                bad[0]['files'].pop()
            elif changed == 'duplicate':
                bad[0]['files'].append(bad[0]['files'][0])
            else:
                bad[0]['files'][0].update(accepted=False, error='retained per-file failure')
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                c.aggregate_groups(bad, identity)

    def test_downloaded_group_rechecks_junit_process_and_policy_not_accepted_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            identity = {'subject': 'a' * 40, 'controller_sha256': 'b' * 64, 'run_id': '123', 'run_attempt': '1'}
            metadata = {**identity, 'architecture': 'arm64', 'group': 'files-3',
                        'nodes': c.EXPECTED_NODES, 'collected': 60, 'unique': 60,
                        'workers': 2, 'retries': 0, 'effective_file_budget_seconds': 300,
                        'validation_only': False, 'host_os': 'Windows', 'native_python_platform': 'win-arm64',
                        'python': '3.14.9 unit fixture', 'hashes': {n: {'sha256': h} for n, h in c.HASHES.items()}}
            summary = {**identity, 'architecture': 'arm64', 'group': 'files-3', 'accepted': True,
                       'source_unchanged': True, 'files': [], 'mutation': None}
            directory, process = folder, {}
            for name in c.group_mapping('arm64')['files-3']:
                directory = folder / Path(name).stem
                directory.mkdir()
                nodes = [n for n in c.EXPECTED_NODES if n.startswith(name + '::')]
                suite = ET.Element('testsuite', tests=str(len(nodes)), failures='0', errors='0', skipped='0')
                for node in nodes:
                    classname, testname = c.case_identity(node)
                    ET.SubElement(suite, 'testcase', classname=classname, name=testname)
                ET.ElementTree(suite).write(directory / 'junit.xml')
                process = {'state': 'completed', 'outer_budget_seconds': 300, 'outer_timeout': False,
                           'cleanup': None, 'returncode': 0, 'elapsed_seconds': 2,
                           'command': ['C:/Program Files/Git/bin/bash.exe', 'scripts/run_tests.sh',
                                       '-j', '1', '--file-retries', '0', '--file-timeout', '300',
                                       '--fail-on-skip', name, '--', '-vv', '--tb=long',
                                       '--basetemp', 'unit/tmp', '--junitxml', 'unit/junit.xml']}
                result = {**process, 'file': name, 'accepted': True, 'nodes': nodes,
                          'junit': {'tests': len(nodes), 'failures': 0, 'errors': 0, 'skipped': 0},
                          'cache_entry_absent': name, 'effective_file_timeout_seconds': 300}
                c.save(directory / 'result.json', result)
                c.save(directory / 'process.json', process)
                summary['files'].append(result)
            c.save(folder / 'metadata.json', metadata)
            c.save(folder / 'summary.json', summary)
            self.assertTrue(c.verify_group_artifact(folder, identity)['accepted'])
            # Real pytest collection places the portable closure case before
            # later marker-lock definitions, not last in EXPECTED_NODES.
            reordered = copy.deepcopy(summary)
            changed = next(r for r in reordered['files'] if r['file'] == c.MARKER_LOCK)
            changed['nodes'] = [changed['nodes'][-1], *changed['nodes'][:-1]]
            marker_result = folder / Path(c.MARKER_LOCK).stem / 'result.json'
            c.save(marker_result, changed)
            c.save(folder / 'summary.json', reordered)
            self.assertTrue(c.verify_group_artifact(folder, identity)['accepted'])
            for bad_nodes in (changed['nodes'][:-1],
                              [changed['nodes'][0], *changed['nodes'][:-1]],
                              ['foreign.py::wrong', *changed['nodes'][1:]]):
                invalid = copy.deepcopy(reordered)
                bad_result = next(r for r in invalid['files'] if r['file'] == c.MARKER_LOCK)
                bad_result['nodes'] = bad_nodes
                c.save(marker_result, bad_result)
                c.save(folder / 'summary.json', invalid)
                with self.subTest(case_inventory=bad_nodes), self.assertRaises(RuntimeError):
                    c.verify_group_artifact(folder, identity)
            c.save(folder / 'summary.json', summary)
            c.save(marker_result, next(r for r in summary['files'] if r['file'] == c.MARKER_LOCK))
            path = directory / 'process.json'
            for key, value in [('state', 'running'), ('outer_timeout', True), ('returncode', 1),
                               ('outer_budget_seconds', 301), ('elapsed_seconds', 300)]:
                bad = copy.deepcopy(process)
                bad[key] = value
                c.save(path, bad)
                with self.subTest(field=key), self.assertRaises(RuntimeError):
                    c.verify_group_artifact(folder, identity)
            c.save(path, process)
            tree = ET.parse(directory / 'junit.xml')
            ET.SubElement(next(tree.iter('testcase')), 'skipped')
            tree.write(directory / 'junit.xml')
            with self.assertRaises(RuntimeError):
                c.verify_group_artifact(folder, identity)

    def test_aggregate_keeps_diagnostics_inside_windows_vm_budget(self):
        workflow = Path(__file__).with_name('windows-contract-probe.yml').read_text()
        aggregate = workflow.split('  exact-subject-aggregate:', 1)[1]
        self.assertIn('runs-on: windows-2025', aggregate)
        self.assertIn('shell: bash', aggregate)
        self.assertIn('python "$RUNNER_TEMP/windows_acceptance_controller.py"', aggregate)

    def test_artifact_paths_cannot_escape_allowlist_even_with_rehashed_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for required in c.OPENSSL_REQUIRED:
                path = root / required
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'unit fixture')
            files = {n: c.sha((root / n).read_bytes()) for n in c.OPENSSL_REQUIRED}
            for name in ('../secret', '/absolute', 'C:/home/secret', 'include/openssl/../secret.h',
                         'include\\openssl\\ssl.h', 'include/openssl/SSL.H', 'home/token',
                         'lib/evil.lib', 'include/openssl/sub/secret.h'):
                manifest = {'identity': {}, 'files': {**files, name: 'a' * 64}}
                c.save(root / 'manifest.json', manifest)
                with self.subTest(path=name), self.assertRaisesRegex(RuntimeError, 'Non-allowlisted OpenSSL path'):
                    c.verify_openssl(root, {}, c.sha((root / 'manifest.json').read_bytes()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
