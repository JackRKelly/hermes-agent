"""Docker boundary: private source-only tests, never live-provider admission."""
import os
from pathlib import Path

import pytest

# Preload the ordinary trusted owner before per-test home-I/O guards, as the
# existing protected fresh-process tests do (never bootstrap inside the guest).
import gateway.session_protected_scope


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


def test_real_docker_native_roundtrip(tmp_path, monkeypatch):
    """Explicit opt-in: existing Desktop daemon only, no pulls or live provider."""
    if os.environ.get('HERMES_DOCKER_BOUNDARY_TEST') != '1':
        pytest.skip('private existing-daemon Docker acceptance only')
    import asyncio
    from types import SimpleNamespace
    from gateway.session_protected_docker import DockerRuntime, DockerLaunch
    from gateway.session_protected_process import protected_process_scope, SelectedAsyncProvider
    from tests.gateway.test_protected_conversation import native_claim
    from agent.transports.types import NormalizedResponse, ToolCall
    from hermes_state_runtime import get_session_admission
    authority, context, admission = native_claim(tmp_path, monkeypatch, persisted=True)
    (tmp_path / 'allowed.txt').write_text('DOCKER_NATIVE\n')
    runtime = DockerRuntime.build(Path(__file__).resolve().parents[2], tmp_path / 'runtime',
        docker='/usr/local/bin/docker', endpoint='unix:///Users/jack/.docker/run/docker.sock',
        base='sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8')
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
            artifacts = worker.export(('result.txt',))
            assert artifacts == {'result.txt': b'BUILT'}
            with pytest.raises(ValueError):
                runner.turn.proxy.invoke('terminal', {'command': 'touch after-cancel'})
        assert captured[0].turn.proxy.worker.container.absent()
        assert authority._managed_workers == {}
        assert len(authority.db.get_messages(admission['target_session_id'])) == 5
        assert get_session_admission(authority.db, admission_id=admission['admission_id'])['status'] == 'unknown'
    try:
        asyncio.run(run())
    finally:
        authority.db.close()


def test_real_docker_reviewer_and_hostile_native_tools(tmp_path):
    if os.environ.get('HERMES_DOCKER_BOUNDARY_TEST') != '1':
        pytest.skip('private existing-daemon Docker acceptance only')
    import json
    import socket
    from gateway.session_protected_docker import DockerRuntime, DockerContainer
    runtime = DockerRuntime.build(Path(__file__).resolve().parents[2], tmp_path / 'runtime',
        docker='/usr/local/bin/docker', endpoint='unix:///Users/jack/.docker/run/docker.sock',
        base='sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8')
    # Nonsecret owner/FD/socket positive controls never enter the guest projection.
    sentinel = tmp_path / 'owner'
    sentinel.write_text('HOST_ONLY')
    assert sentinel.read_text() == 'HOST_ONLY'
    listener = socket.socket(socket.AF_UNIX)
    socketpath = str(tmp_path.parent / 'docker-negative.sock')
    listener.bind(socketpath)
    listener.listen(1)
    control = socket.socket(socket.AF_UNIX)
    control.connect(socketpath)
    accepted, _ = listener.accept()
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
r['no_owner_home']=not pathlib.Path('/Users/jack/.hermes').exists()
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
        _code, wire = runtime.run(['exec', '--user', '65532:65532', container.cid,
            'python', '-I', '-S', '-B', '-c', script], limit=8192)
        checks = json.loads(wire)
        for name in ('host', 'owner-store', 'broker', 'fd'):
            checks['unmounted_' + name] = runtime.run(['exec', '--user', '65532:65532', container.cid,
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
        assert len(checks) >= 37
        assert all(checks.values()), {k: v for k, v in checks.items() if not v}
    finally:
        os.close(fd)
        listener.close()
        container.close()
    assert container.absent()


def test_real_docker_deadline_setsids_and_output_flood(tmp_path):
    if os.environ.get('HERMES_DOCKER_BOUNDARY_TEST') != '1':
        pytest.skip('private existing-daemon Docker acceptance only')
    import time
    from gateway.session_protected_docker import DockerRuntime, DockerContainer
    runtime = DockerRuntime.build(Path(__file__).resolve().parents[2], tmp_path / 'runtime',
        docker='/usr/local/bin/docker', endpoint='unix:///Users/jack/.docker/run/docker.sock',
        base='sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8')
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


def test_real_docker_idle_eof_tears_down_namespace(tmp_path):
    if os.environ.get('HERMES_DOCKER_BOUNDARY_TEST') != '1':
        pytest.skip('private existing-daemon Docker acceptance only')
    import time
    from gateway.session_protected_docker import DockerRuntime, DockerContainer
    runtime = DockerRuntime.build(Path(__file__).resolve().parents[2], tmp_path / 'runtime',
        docker='/usr/local/bin/docker', endpoint='unix:///Users/jack/.docker/run/docker.sock',
        base='sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8')
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


def test_real_supervisor_sigkill_leaves_only_hard_deadline(tmp_path):
    if os.environ.get('HERMES_DOCKER_BOUNDARY_TEST') != '1':
        pytest.skip('private existing-daemon Docker acceptance only')
    import json
    import selectors
    import subprocess
    import sys
    import time
    from gateway.session_protected_docker import DockerRuntime
    root = Path(__file__).resolve().parents[2]
    runtime = DockerRuntime.build(root, tmp_path / 'runtime', docker='/usr/local/bin/docker',
        endpoint='unix:///Users/jack/.docker/run/docker.sock',
        base='sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8')
    script = f'''import sys,json,time
sys.path.insert(0,{str(root)!r})
from gateway.session_protected_docker import DockerRuntime,DockerContainer
r=DockerRuntime('/usr/local/bin/docker','unix:///Users/jack/.docker/run/docker.sock',{str(tmp_path / 'supervisor')!r}, {runtime.image!r},{runtime.source!r})
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
        if cid is not None:
            runtime.reconcile(tmp_path / 'leases')


@pytest.mark.parametrize('mode', ['cancel-acquire', 'store-fault', 'recover', 'late-create'])
def test_real_docker_acquisition_and_recovery(tmp_path, monkeypatch, mode):
    if os.environ.get('HERMES_DOCKER_BOUNDARY_TEST') != '1':
        pytest.skip('private existing-daemon Docker acceptance only')
    import asyncio
    import threading
    from gateway.session_protected_docker import DockerRuntime, DockerContainer, DockerLaunch
    runtime = DockerRuntime.build(Path(__file__).resolve().parents[2], tmp_path / 'runtime',
        docker='/usr/local/bin/docker', endpoint='unix:///Users/jack/.docker/run/docker.sock',
        base='sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8')
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
def test_real_docker_artifact_and_resource_negatives(tmp_path, attack):
    if os.environ.get('HERMES_DOCKER_BOUNDARY_TEST') != '1':
        pytest.skip('private existing-daemon Docker acceptance only')
    from gateway.session_protected_docker import DockerRuntime, DockerContainer
    runtime = DockerRuntime.build(Path(__file__).resolve().parents[2], tmp_path / 'runtime',
        docker='/usr/local/bin/docker', endpoint='unix:///Users/jack/.docker/run/docker.sock',
        base='sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8')
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
