"""Docker boundary: private source-only tests, never live-provider admission."""
import os
from pathlib import Path

import pytest

# Preload the ordinary trusted owner before per-test home-I/O guards, as the
# existing protected fresh-process tests do (never bootstrap inside the guest).
import gateway.session_protected_scope


# Executed verbatim in the guest and by the portable metadata regressions.
_OWNER_HOME_PROBE = """
def owner_home_status():
 # exists() suppresses PermissionError on newer Python; stat preserves evidence.
 try: pathlib.Path('/root/.hermes').stat()
 except FileNotFoundError: return 'absent'
 except PermissionError: return 'inaccessible'
 return 'present'
"""


@pytest.mark.parametrize('exists_semantics', ['raises-permission', 'suppresses-permission'])
@pytest.mark.parametrize('stat_result,expected', [
    (FileNotFoundError(2, 'fixture absent'), 'absent'),
    (PermissionError(13, 'fixture inaccessible'), 'inaccessible'),
    (None, 'present'),
    (OSError(5, 'fixture I/O fault'), None),
])
def test_owner_home_probe_preserves_stat_metadata(monkeypatch, exists_semantics, stat_result, expected):
    import pathlib
    namespace = {'pathlib': pathlib}
    exec(_OWNER_HOME_PROBE, namespace)
    actual_stat, actual_exists = Path.stat, Path.exists
    def fixture_stat(path, *args, **kwargs):
        if str(path) != '/root/.hermes':
            return actual_stat(path, *args, **kwargs)
        if stat_result is not None:
            raise stat_result
        return os.stat_result((0,) * 10)
    def fixture_exists(path, *args, **kwargs):
        if str(path) != '/root/.hermes':
            return actual_exists(path, *args, **kwargs)
        if isinstance(stat_result, PermissionError):
            if exists_semantics == 'raises-permission':
                raise stat_result
            return False
        if isinstance(stat_result, FileNotFoundError):
            return False
        if stat_result is not None:
            raise stat_result
        return True
    monkeypatch.setattr(Path, 'stat', fixture_stat)
    monkeypatch.setattr(Path, 'exists', fixture_exists)
    if expected is None:
        with pytest.raises(OSError, match='fixture I/O fault'):
            namespace['owner_home_status']()
    else:
        assert namespace['owner_home_status']() == expected


@pytest.fixture(scope='session')
def docker_runtime(tmp_path_factory):
    from tests.gateway.docker_boundary_config import build_boundary_runtime
    return build_boundary_runtime(tmp_path_factory.mktemp('docker-runtime'))


@pytest.mark.platforms('posix')
def test_approved_projection_rejects_links_specials_and_quota(tmp_path):
    from gateway.session_protected_docker import approved_inputs
    root = tmp_path / 'input'
    root.mkdir()
    (root / 'allowed.txt').write_bytes(b'approved\n')
    assert approved_inputs(root, ('allowed.txt',)) == {'allowed.txt': b'approved\n'}
    (root / 'link').symlink_to(root / 'allowed.txt')
    with pytest.raises(ValueError):
        approved_inputs(root, ('link',))
    os.link(root / 'allowed.txt', root / 'hard')
    with pytest.raises(ValueError):
        approved_inputs(root, ('hard',))
    os.mkfifo(root / 'fifo')
    with pytest.raises(ValueError):
        approved_inputs(root, ('fifo',))
    for name in ('../escape', '/absolute', 'a//b', 'a/./b', 'a\nb', '.env', 'a/../b'):
        with pytest.raises(ValueError):
            approved_inputs(root, (name,))
    (root / 'large').write_bytes(b'x' * 4097)
    with pytest.raises(ValueError):
        approved_inputs(root, ('large',))
    with pytest.raises(ValueError):
        approved_inputs(root, ('a',) * 33)


def test_real_docker_native_roundtrip(tmp_path, monkeypatch, docker_runtime):
    """Explicit opt-in: selected existing local daemon; no pulls or live provider."""
    import asyncio
    from types import SimpleNamespace
    from gateway.session_protected_docker import DockerRuntime, DockerLaunch
    from gateway.session_protected_process import protected_process_scope, SelectedAsyncProvider
    from tests.gateway.test_protected_conversation import native_claim
    from agent.transports.types import NormalizedResponse, ToolCall
    from hermes_state_runtime import get_session_admission
    authority, context, admission = native_claim(tmp_path, monkeypatch, persisted=True)
    (tmp_path / 'allowed.txt').write_text('DOCKER_NATIVE\n')
    runtime = docker_runtime
    calls, captured = [], []
    class Transport:
        def build_kwargs(self, model, messages, tools):
            return dict(messages=messages, tools=tools)
        def normalize_response(self, response):
            return response
    async def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return NormalizedResponse(None, [ToolCall('docker-read', 'read_file', '{}'),
                ToolCall('docker-edit', 'terminal', '{"command":"printf BUILT > result.txt; cat result.txt"}')], 'tool_calls')
        assert kwargs['messages'][-1]['content'] == 'BUILT'
        return NormalizedResponse('DOCKER_DONE', None, 'stop')
    async def close():
        pass
    async def run():
        launch = DockerLaunch(tmp_path.resolve(), runtime, tmp_path / 'leases', ('allowed.txt',), 'builder')
        provider = SelectedAsyncProvider(model='controlled-no-provider', provider_id='custom',
            transport=Transport(), create=create, aclose=close)
        async with protected_process_scope(authority, SimpleNamespace(session_id=admission['target_session_id']), admission,
                launch, principal_id='fixture-owner', context=context, provider=provider) as runner:
            captured.append(runner)
            result = await runner.run_conversation('CONTROLLED_DOCKER')
            assert result['final_response'] == 'DOCKER_DONE'
            worker = runner.turn.proxy.worker
            att = worker.container.attest()
            assert att['cid'] != str(worker.process.pid)
            assert att['image'] == runtime.image
            assert worker.container.inspect()['HostConfig']['LogConfig']['Type'] == 'none'
            import base64
            from hermes_cli import kanban_db as kb
            exports = []
            actual_export, actual_end = worker.container.export, kb._end_run
            def export_once(names):
                exports.append(names)
                assert len(exports) == 1, 'retry must use stored artifact bytes'
                return actual_export(names)
            def projection_fault(*args, **kwargs):
                raise OSError('controlled Docker projection fault')
            monkeypatch.setattr(worker.container, 'export', export_once)
            monkeypatch.setattr(kb, '_end_run', projection_fault)
            with pytest.raises(OSError, match='projection'):
                await runner.settle_success(('result.txt',))
            monkeypatch.setattr(kb, '_end_run', actual_end)
            receipt = await runner.settle_success(('result.txt',))
            assert exports == [('result.txt',)]
            with pytest.raises(ValueError):
                await runner.settle_success(('different.txt',))
            assert base64.b64decode(receipt['artifacts']['result.txt']['body']) == b'BUILT'
            assert receipt['outcome'] == 'completed'
            runtime.record('settlement-' + admission['admission_id'], receipt)
            with pytest.raises(ValueError):
                runner.turn.proxy.invoke('terminal', {'command': 'touch after-cancel'})
        assert captured[0].turn.proxy.worker.container.absent()
        assert authority._managed_workers == {}
        assert len(authority.db.get_messages(admission['target_session_id'])) == 5
        assert get_session_admission(authority.db, admission_id=admission['admission_id'])['outcome'] == 'completed'
    try:
        asyncio.run(run())
    finally:
        authority.db.close()


def test_real_docker_reviewer_and_hostile_native_tools(tmp_path, docker_runtime):
    import json
    import socket
    from gateway.session_protected_docker import DockerRuntime, DockerContainer
    runtime = docker_runtime
    # Nonsecret owner/FD/socket positive controls never enter the guest projection.
    sentinel = tmp_path / 'owner'
    sentinel.write_text('HOST_ONLY')
    assert sentinel.read_text() == 'HOST_ONLY'
    listener = socket.socket(socket.AF_UNIX)
    socketpath = str(tmp_path.parent / 'docker-negative.sock')
    cwd = os.open('.', os.O_RDONLY)
    try:
        os.chdir(tmp_path.parent)
        listener.bind(Path(socketpath).name)  # relative bind avoids AF_UNIX path length limits
        listener.listen(1)
        control = socket.socket(socket.AF_UNIX)
        control.connect(Path(socketpath).name)
        accepted, _ = listener.accept()
    finally:
        os.fchdir(cwd)
        os.close(cwd)
    accepted.close()
    control.close()
    fd = os.open(sentinel, os.O_RDONLY)
    os.set_inheritable(fd, True)
    container = DockerContainer(runtime, tmp_path / 'leases', 9, 'reviewer')
    try:
        container.create({'allowed.txt': b'REVIEW_ORIGINAL\n'})
        original = container.inspect()
        assert original['Mounts'] == []
        import copy
        actual_inspect = container.inspect
        for field, bad in {'ReadonlyRootfs': False, 'CapDrop': [], 'Privileged': True,
                'NetworkMode': 'bridge', 'PidMode': 'host', 'IpcMode': 'host', 'CgroupnsMode': 'host',
                'PidsLimit': 0, 'Memory': 0, 'MemorySwap': -1, 'NanoCpus': 0,
                'SecurityOpt': [], 'Tmpfs': {}, 'ShmSize': 67108864,
                'LogConfig': {'Type': 'json-file'}, 'GroupAdd': ['0'],
                'DeviceCgroupRules': ['a *:* rwm']}.items():
            altered = copy.deepcopy(original)
            altered['HostConfig'][field] = bad
            container.inspect = lambda: altered
            try:
                with pytest.raises(ValueError):
                    container.attest()
            finally:
                container.inspect = actual_inspect
        script = '''import os,sys,json,pathlib,socket,subprocess,stat
sys.path.insert(0,'/opt/hermes')
from tools.file_operations_native import atomic_write,_open_regular
r={}
def denied(name,fn):
 try: fn();r[name]=False
 except (OSError,ValueError):r[name]=True
p=pathlib.Path('/workspace/allowed.txt')
native_fd=_open_regular(str(p),nofollow=True)
r['review_native_read']=os.read(native_fd,64)==b'REVIEW_ORIGINAL\\n'
os.close(native_fd)
r['review_native_write']=atomic_write(str(p),b'BAD') is None
r['review_native_write']=not r['review_native_write']
denied('review_shell_write',lambda:subprocess.run(['/bin/bash','-c','printf BAD > /workspace/allowed.txt'],check=True,stderr=subprocess.DEVNULL))
# subprocess nonzero is explicitly checked without masking it as an OSError.
'''
        # The shell failure is checked separately (CalledProcessError isn't OSError).
        script = script[:script.index("denied('review_shell_write'")] + '''r['review_shell_write']=subprocess.run(['/bin/bash','-c','printf BAD > /workspace/allowed.txt'],stderr=subprocess.DEVNULL).returncode!=0
denied('review_unlink',lambda:p.unlink())
denied('review_rename',lambda:p.rename('/workspace/renamed'))
denied('review_chmod',lambda:p.chmod(0o666))
denied('review_hardlink',lambda:os.link(str(p),'/workspace/hard'))
denied('review_newfile',lambda:pathlib.Path('/workspace/new').write_text('BAD'))
denied('runtime_write',lambda:pathlib.Path('/opt/hermes/agent/protected_tool_worker.py').write_text('BAD'))
denied('root_write',lambda:pathlib.Path('/etc/probe').write_text('BAD'))
denied('pid1_stop',lambda:os.kill(1,19))
denied('pid1_kill',lambda:os.kill(1,9))
r['nonroot']=os.getuid()==65532 and os.geteuid()==65532
s=dict(l.split(':',1) for l in pathlib.Path('/proc/self/status').read_text().splitlines() if ':' in l)
r['caps_empty']=s['CapEff'].strip()=='0000000000000000' and s['CapBnd'].strip()=='0000000000000000'
r['no_new_privs']=s['NoNewPrivs'].strip()=='1'
r['seccomp']=s['Seccomp'].strip()=='2'
r['no_docker_socket']=not pathlib.Path('/var/run/docker.sock').exists() and not pathlib.Path('/run/docker.sock').exists()
r['owner_home_status']=owner_home_status()
r['owner_home_denied']=r['owner_home_status'] in ('absent','inaccessible')
r['no_docker_cli']=subprocess.run(['/bin/bash','-c','command -v docker'],stdout=subprocess.DEVNULL).returncode!=0
r['fd_closure']=all(int(x)<=2 for x in os.listdir('/proc/self/fd') if os.path.exists('/proc/self/fd/'+x))
r['no_provider_env']=not any(any(v in k for v in ('TOKEN','SECRET','API_KEY','HERMES_HOME','SSH','AUTH')) for k in os.environ)
for host in ['1.1.1.1','192.168.65.1','127.0.0.1']:
 s=socket.socket();s.settimeout(.2)
 try:s.connect((host,2375));r['network_'+host]=False
 except OSError:r['network_'+host]=True
 finally:s.close()
r['original_hash']=p.read_bytes()==b'REVIEW_ORIGINAL\\n'
print(json.dumps(r))
'''
        script = 'import pathlib\n' + _OWNER_HOME_PROBE + script
        _code, wire = runtime.run(['exec', '--user', '65532:65532', container.cid,
            'python', '-I', '-S', '-B', '-c', script], limit=8192)
        checks = json.loads(wire)
        owner_home_status = checks.pop('owner_home_status')
        assert owner_home_status in ('absent', 'inaccessible')
        # Independently prove guest image absence as root, without reading any
        # host credentials or treating nonroot inaccessibility as file absence.
        image_probe = '''import json,pathlib
paths=['/root/.hermes','/root/.docker','/root/.aws','/root/.ssh','/root/.config','/home/guest/.hermes','/home/guest/.docker','/home/guest/.config']
r={}
for name in paths:
 try: pathlib.Path(name).stat()
 except FileNotFoundError: r[name]='absent'
 else: r[name]='present'
print(json.dumps(r))
'''
        image_paths = json.loads(runtime.run(['exec', '--user', '0:0', container.cid,
            'python', '-I', '-S', '-B', '-c', image_probe])[1])
        assert image_paths and set(image_paths.values()) == {'absent'}
        assert not any(any(name in key.upper() for name in ('TOKEN', 'SECRET', 'API_KEY', 'AUTH', 'PROXY', 'HERMES_HOME', 'SSH'))
            for key in (entry.split('=', 1)[0] for entry in original['Config']['Env']))
        # Actual guest read request of the nonsecret owner fixture, not just an
        # existence probe. Only the two expected denial classes are accepted.
        read_probe = f'''import pathlib,json
try: pathlib.Path({str(sentinel)!r}).read_bytes()
except FileNotFoundError: status='absent'
except PermissionError: status='inaccessible'
else: status='readable'
print(json.dumps(status))
'''
        owner_read_status = json.loads(runtime.run(['exec', '--user', '65532:65532', container.cid,
            'python', '-I', '-S', '-B', '-c', read_probe])[1])
        assert owner_read_status in ('absent', 'inaccessible')
        runtime.record('denied-path-' + container.name, dict(owner_home_status=owner_home_status,
            owner_fixture_read_status=owner_read_status, image_paths=image_paths))
        checks['host_sentinel_absent'] = runtime.run(['exec', '--user', '65532:65532', container.cid,
            '/bin/bash', '-c', 'test ! -e "$1"', 'probe', str(sentinel)], missing=True)[0] == 0
        checks['host_pid_absent'] = runtime.run(['exec', '--user', '65532:65532', container.cid,
            '/bin/bash', '-c', 'test ! -e /proc/' + str(os.getpid())], missing=True)[0] == 0
        checks['host_socket_absent'] = runtime.run(['exec', '--user', '65532:65532', container.cid,
            '/bin/bash', '-c', 'test ! -S "$1"', 'probe', socketpath], missing=True)[0] == 0
        checks['host_unchanged'] = sentinel.read_text() == 'HOST_ONLY'
        checks['no_mounts'] = not container.inspect()['Mounts']
        checks['log_none'] = original['HostConfig']['LogConfig']['Type'] == 'none'
        checks['quota_limits'] = original['HostConfig']['PidsLimit'] == 48 and original['HostConfig']['Memory'] == 201326592
        checks['review_native_patch'] = runtime.run(['exec', '--user', '65532:65532', container.cid,
            '/bin/bash', '-c', "sed -i s/ORIGINAL/BAD/ /workspace/allowed.txt"], missing=True)[0] != 0
        checks['review_native_search'] = b'REVIEW_ORIGINAL' in runtime.run(['exec', '--user', '65532:65532', container.cid,
            '/bin/grep', '-R', 'REVIEW_ORIGINAL', '/workspace'])[1]
        checks['review_original_after_patch'] = runtime.run(['exec', '--user', '65532:65532', container.cid,
            '/bin/cat', '/workspace/allowed.txt'])[1] == b'REVIEW_ORIGINAL\n'
        assert len(checks) >= 34
        assert all(checks.values()), {k: v for k, v in checks.items() if not v}
    finally:
        os.close(fd)
        listener.close()
        container.close()
    assert container.absent()


def test_real_docker_deadline_setsids_and_output_flood(tmp_path, docker_runtime):
    import time
    from gateway.session_protected_docker import DockerRuntime, DockerContainer
    runtime = docker_runtime
    c = DockerContainer(runtime, tmp_path / 'leases', 10, 'builder')
    try:
        c.create({'allowed.txt': b'deadline'})
        # Untrusted stdout flood is bounded before buffering even outside ToolProxy.
        with pytest.raises(ValueError, match='quota'):
            runtime.run(['exec', '--user', '65532:65532', c.cid, '/bin/bash', '-c', 'yes FLOOD'], limit=4096)
        runtime.run(['exec', '--user', '65532:65532', c.cid, 'python', '-I', '-S', '-c',
            "import subprocess;subprocess.Popen(['/bin/sleep','90'],start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"])
        assert c.inspect()['State']['Running']
        # Simulate lost bridge/owner: no in-process watchdog/close until immutable PID1 expires.
        c.process.kill()
        c.process.wait(timeout=3)
        end = time.monotonic() + 35
        while c.inspect()['State']['Running'] and time.monotonic() < end:
            time.sleep(.2)
        state = c.inspect()['State']
        assert not state['Running'] and state['Pid'] == 0
        assert runtime.run(['top', c.cid], missing=True)[0] != 0
    finally:
        c.close()
    assert c.absent()


def test_real_docker_idle_eof_tears_down_namespace(tmp_path, docker_runtime):
    import time
    from gateway.session_protected_docker import DockerRuntime, DockerContainer
    runtime = docker_runtime
    c = DockerContainer(runtime, tmp_path / 'leases', 15, 'reviewer')
    try:
        process = c.create({'allowed.txt': b'EOF'})
        time.sleep(.2)  # health: allow HX013 -- ensure the trusted PID1 observed a live tool before EOF; the idle delay is the tested behavior
        process.stdin.close()
        process.wait(timeout=3)
        end = time.monotonic() + 4
        while c.inspect()['State']['Running'] and time.monotonic() < end:
            time.sleep(.1)
        assert not c.inspect()['State']['Running']
    finally:
        c.close()
    assert c.absent()


def test_real_supervisor_sigkill_leaves_only_hard_deadline(tmp_path, docker_runtime):
    import json
    import selectors
    import subprocess
    import sys
    import time
    from gateway.session_protected_docker import DockerRuntime
    root = Path(__file__).resolve().parents[2]
    runtime = docker_runtime
    script = f'''import sys,json,time
sys.path.insert(0,{str(root)!r})
from gateway.session_protected_docker import DockerRuntime,DockerContainer
r=DockerRuntime({runtime.docker!r},{runtime.endpoint!r},{str(tmp_path / 'supervisor')!r}, {runtime.image!r},{runtime.source!r})
c=DockerContainer(r,{str(tmp_path / 'leases')!r},14,'builder')
c.create({{'allowed.txt':b'CRASH'}})
r.run(['exec','--user','65532:65532',c.cid,'python','-I','-S','-c',"import subprocess;subprocess.Popen(['/bin/sleep','90'],start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"])
print(json.dumps({{'cid':c.cid,'bridge':c.process.pid}}),flush=True)
time.sleep(90)
'''
    supervisor = subprocess.Popen([sys.executable, '-I', '-S', '-B', '-c', script],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={'LANG': 'C', 'LC_ALL': 'C'}, close_fds=True)
    cid = None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(supervisor.stdout, selectors.EVENT_READ)
            assert selector.select(60), 'bounded supervisor acquisition'
            frame = json.loads(supervisor.stdout.readline(4096))
        cid = frame['cid']
        assert runtime.json(['inspect', cid])[0]['State']['Running']
        supervisor.kill()  # real trusted Python owner SIGKILL, no finally/watchdog
        supervisor.wait(timeout=3)
        end = time.monotonic() + 35
        while runtime.json(['inspect', cid])[0]['State']['Running'] and time.monotonic() < end:
            time.sleep(.2)
        state = runtime.json(['inspect', cid])[0]['State']
        assert not state['Running'] and state['Pid'] == 0
        runtime.reconcile(tmp_path / 'leases')
        assert runtime.run(['inspect', cid], missing=True)[0] != 0
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
        supervisor.wait(timeout=3)
        supervisor.stdout.close()
        supervisor.stderr.close()
        runtime.reconcile(tmp_path / 'leases')


@pytest.mark.parametrize('mode', ['cancel-acquire', 'store-fault', 'recover', 'late-create'])
def test_real_docker_acquisition_and_recovery(tmp_path, monkeypatch, mode, docker_runtime):
    import asyncio
    import threading
    from gateway.session_protected_docker import DockerRuntime, DockerContainer, DockerLaunch
    runtime = docker_runtime
    (tmp_path / 'allowed.txt').write_text('CANCEL')
    if mode == 'cancel-acquire':
        entered, release = threading.Event(), threading.Event()
        actual = DockerContainer.save
        def delayed(self, state):
            actual(self, state)
            if state == 'CREATED':
                entered.set()
                assert release.wait(5)
        monkeypatch.setattr(DockerContainer, 'save', delayed)
        async def run():
            task = asyncio.create_task(DockerLaunch(tmp_path.resolve(), runtime, tmp_path / 'leases', ('allowed.txt',), 'builder').acquire(11))
            await asyncio.to_thread(entered.wait, 5)
            assert entered.is_set()
            task.cancel()
            asyncio.get_running_loop().call_later(.01, task.cancel)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        asyncio.run(run())
    else:
        c = DockerContainer(runtime, tmp_path / 'leases', 12, 'builder')
        if mode == 'late-create':
            actual = runtime.run
            late_args = []
            def late(args, **kwargs):
                if args[0] == 'create':
                    late_args.append(args)
                    raise TimeoutError('controlled lost create response')
                return actual(args, **kwargs)
            monkeypatch.setattr(runtime, 'run', late)
            with pytest.raises((TimeoutError, RuntimeError)):
                c.create({'allowed.txt': b'LATE'})
            assert not c.retired  # absence now cannot prove a late daemon create cannot arrive
            monkeypatch.setattr(runtime, 'run', actual)
            actual(late_args[0])  # exact fixed create completes late; never start guest
            runtime.reconcile(tmp_path / 'leases')
            assert c.absent()
        elif mode == 'store-fault':
            actual = c.save
            def fail(state):
                if state in ('CREATED', 'RETIRED'):
                    raise OSError('controlled lease store failure')
                actual(state)
            monkeypatch.setattr(c, 'save', fail)
            with pytest.raises(OSError):
                c.create({'allowed.txt': b'FAULT'})
            assert c.absent()
            monkeypatch.setattr(c, 'save', actual)
            c.close()
            c.close()
        else:
            c.create({'allowed.txt': b'RECOVER'})
            # Lost supervisor handles: read only exact persisted immutable lease.
            runtime.pending.clear()
            runtime.reconcile(tmp_path / 'leases')
            assert c.absent()
            c.process.wait(timeout=3)
            c.close()
    runtime.reconcile(tmp_path / 'leases')
    assert not runtime.run(['ps', '-aq', '--filter', 'label=hermes.tool.source=' + runtime.source])[1].strip()


@pytest.mark.parametrize('attack', ['symlink', 'fifo', 'hardlink', 'oversize', 'case-collision', 'tmpfs-full', 'pids-full', 'stderr-flood'])
def test_real_docker_artifact_and_resource_negatives(tmp_path, attack, docker_runtime):
    from gateway.session_protected_docker import DockerRuntime, DockerContainer
    runtime = docker_runtime
    c = DockerContainer(runtime, tmp_path / 'leases', 13, 'builder')
    try:
        c.create({'allowed.txt': b'QUOTA'})
        commands = {'symlink': 'ln -s /etc/passwd /workspace/evil', 'fifo': 'mkfifo /workspace/evil',
            'hardlink': 'ln /workspace/allowed.txt /workspace/evil',
            'oversize': 'head -c 4097 /dev/zero > /workspace/evil',
            'case-collision': 'touch /workspace/A /workspace/a',
            'tmpfs-full': 'for n in {1..40}; do head -c 65536 /dev/zero > /workspace/f$n || exit 7; done',
            'pids-full': 'for n in {1..70}; do sleep 60 & done; wait',
            'stderr-flood': 'yes FLOOD >&2'}
        if attack in ('pids-full', 'stderr-flood'):
            with pytest.raises((ValueError, TimeoutError)):
                runtime.run(['exec', '--user', '65532:65532', c.cid, '/bin/bash', '-c', commands[attack]], limit=4096, seconds=3)
        else:
            code, wire = runtime.run(['exec', '--user', '65532:65532', c.cid, '/bin/bash', '-c', commands[attack]], missing=True, limit=4096)
            if attack == 'tmpfs-full':
                assert code != 0 and b'No space left' in wire
            else:
                assert code == 0
                with pytest.raises((RuntimeError, ValueError)):
                    c.export(('A', 'a') if attack == 'case-collision' else ('evil',))
    finally:
        c.close()
    assert c.absent()


@pytest.mark.parametrize('mode', ['recreate', 'name-conflict', 'rejected'])
def test_real_creating_recovery_faults(tmp_path, monkeypatch, docker_runtime, mode):
    import json
    from gateway.session_protected_docker import DockerContainer
    runtime = docker_runtime
    c = DockerContainer(runtime, tmp_path / 'leases', 16, 'builder')
    c.save('CREATING')
    runtime.pending.clear()  # restart: no in-process handles/CID
    actual = runtime.run
    calls, conflicts = [], []
    def fault(args, **kwargs):
        calls.append(args)
        if args[0] == 'create':
            assert args == c.create_args()
            if mode == 'name-conflict':
                actual(args)  # exact original arrives immediately before retry
                result = actual(args, **kwargs)
                conflicts.append(result[0])
                return result
            if mode == 'rejected':
                args = list(args)
                args[-3] = 'sha256:' + '0' * 64  # real --pull=never rejection
        return actual(args, **kwargs)
    monkeypatch.setattr(runtime, 'run', fault)
    try:
        if mode == 'rejected':
            with pytest.raises(RuntimeError, match='reconciliation'):
                runtime.reconcile(c.directory)
            assert json.loads(c.path.read_text())['state'] == 'CREATING'
            assert runtime.last_reconciliation['errors'] == [{'lease': c.path.name, 'type': 'RuntimeError'}]
            assert c.absent()
        else:
            report = runtime.reconcile(c.directory)
            assert report == {'retired': [c.path.name], 'skipped': [], 'errors': []}
            assert json.loads(c.path.read_text())['state'] == 'RETIRED'
            assert c.absent()
            if mode == 'name-conflict':
                assert conflicts and conflicts[0] != 0
        assert not any(args[0] in ('start', 'exec') for args in calls)
    finally:
        monkeypatch.setattr(runtime, 'run', actual)
        runtime.reconcile(c.directory)
    assert c.absent()
