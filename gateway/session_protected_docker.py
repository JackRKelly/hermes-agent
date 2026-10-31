"""Strict owner-only Docker tool boundary. Never used by production_plan.

Docker Desktop, its administrators, daemon/kernel and immutable image producer are
trusted. This is a VM alternative, NOT a change to the macOS group contract.
"""
import os
from pathlib import Path
import stat
import unicodedata

MAX_FILES = 32
MAX_FILE = 4096
MAX_TOTAL = 65536


def checked_paths(names):
    if type(names) not in (tuple, list) or not 1 <= len(names) <= MAX_FILES:
        raise ValueError('invalid projection count')
    keys = set()
    for name in names:
        if (type(name) is not str or not 1 <= len(name) <= 160
                or any(ord(c) < 32 or ord(c) == 127 for c in name)
                or any(p in ('', '.', '..') or p.startswith('.') for p in name.split('/'))
                or '\\' in name or ':' in name):
            raise ValueError('invalid projection path')
        key = unicodedata.normalize('NFC', name).casefold()
        if key in keys:
            raise ValueError('projection collision')
        keys.add(key)
    return tuple(names)


def approved_inputs(root, names):
    """Read explicit regular files using held no-follow directory descriptors.

    Reject hardlinks and all specials before reads (NONBLOCK prevents FIFO hangs).
    The caller approves paths, never a recursive arbitrary workspace traversal.
    """
    names = checked_paths(names)
    result, total = {}, 0
    rootfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in names:
            parent = os.open('.', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rootfd)
            try:
                pieces = name.split('/')
                for part in pieces[:-1]:
                    nextfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    os.close(parent)
                    parent = nextfd
                fd = os.open(pieces[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE:
                        raise ValueError('unapproved file type or size')
                    body = os.read(fd, MAX_FILE + 1)
                    after = os.fstat(fd)
                    if len(body) > MAX_FILE or (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                        raise ValueError('projection changed during read')
                finally:
                    os.close(fd)
            except OSError as exc:
                raise ValueError('projection refused') from exc
            finally:
                os.close(parent)
            total += len(body)
            if total > MAX_TOTAL:
                raise ValueError('projection quota')
            result[name] = body
    finally:
        os.close(rootfd)
    return result


# Explicit reviewed closure only. No directory build context, auth or dotfiles.
CLOSURE = ('agent/__init__.py', 'agent/jiter_preload.py', 'agent/protected_tool_worker.py',
           'agent/protected_docker_guest.py', 'tools/__init__.py', 'tools/file_operations_native.py')


def _digest(value):
    import re
    if type(value) is not str or not re.fullmatch(r'sha256:[0-9a-f]{64}', value):
        raise ValueError('content-addressed image required')
    return value


def _bounded(command, env, *, data=None, seconds=20, limit=262144):
    """Kill the CLI on timeout/flood; container cleanup is separately exact-CID."""
    import selectors
    import subprocess
    import time
    process = subprocess.Popen(command, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True, env=env, bufsize=0)
    end, output = time.monotonic() + seconds, bytearray()
    # Data is small (<8KiB); don't wait for a malicious guest to drain stdin.
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            if data is not None:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE)
            sent = 0
            while selector.get_map():
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Docker command deadline')
                ready = selector.select(remaining)
                if not ready:
                    raise TimeoutError('Docker command deadline')
                for key, _ in ready:
                    if key.fileobj is process.stdin:
                        sent += os.write(process.stdin.fileno(), data[sent:])
                        if sent == len(data):
                            selector.unregister(process.stdin)
                            process.stdin.close()
                    else:
                        chunk = os.read(process.stdout.fileno(), min(8192, limit + 1 - len(output)))
                        if not chunk:
                            selector.unregister(process.stdout)
                            continue
                        output.extend(chunk)
                        if len(output) > limit:
                            raise ValueError('Docker output quota')
        code = process.wait(timeout=max(.01, end - time.monotonic()))
        return code, bytes(output)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()


class DockerRuntime:
    def __init__(self, docker, endpoint, private, image=None, source=None):
        import re
        self.docker, self.endpoint, self.private = str(docker), endpoint, Path(private)
        if not Path(docker).is_absolute() or not re.fullmatch(r'unix:///[^\x00\n]+', endpoint):
            raise ValueError('fixed local Docker executable/socket required')
        self.private.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.private.is_symlink() or stat.S_IMODE(self.private.stat().st_mode) != 0o700:
            raise ValueError('private Docker config required')
        self.config = self.private / 'client'
        self.config.mkdir(mode=0o700, exist_ok=True)
        if any(self.config.iterdir()):
            raise ValueError('Docker client config must be empty')
        self.env = {'PATH': '/usr/bin:/bin', 'HOME': str(self.private), 'DOCKER_CONFIG': str(self.config),
                    'LANG': 'C', 'LC_ALL': 'C', 'DOCKER_CLI_HINTS': 'false'}
        self.daemon = self.json(['info', '--format', '{{json .ID}}'])
        if type(self.daemon) is not str or not self.daemon:
            raise ValueError('unknown daemon')
        self.pending = {}
        self.evidence = os.environ.get('HERMES_DOCKER_EVIDENCE')  # health: allow HX002 -- private source-test proof sink only, never enables admission
        self.image, self.source = image, source
        if image is not None:
            _digest(image)
            self.verify_image()

    def record(self, name, value):
        if self.evidence:
            import json
            directory = Path(self.evidence)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            (directory / (name + '.json')).write_text(json.dumps(value, sort_keys=True), encoding='utf-8')

    def command(self, args):
        return [self.docker, '--host', self.endpoint, '--config', str(self.config), *args]

    def run(self, args, *, data=None, seconds=20, limit=262144, missing=False):
        code, output = _bounded(self.command(args), self.env, data=data, seconds=seconds, limit=limit)
        if code and not missing:
            raise RuntimeError('Docker operation failed: ' + args[0] + ': ' + output[:512].decode(errors='replace'))
        return code, output

    def json(self, args):
        import json
        return json.loads(self.run(args)[1])

    def reconcile(self, directory):
        """Owner restart only; attempt EVERY private lease, then report errors.

        Foreign runtime leases are reported/skipped, never guessed or broadly
        killed. Reopen their exact approved runtime to retire them separately.
        """
        import json
        if self.json(['info', '--format', '{{json .ID}}']) != self.daemon:
            raise ValueError('daemon identity changed')
        report = {'retired': [], 'skipped': [], 'errors': []}
        for path in sorted(Path(directory).glob('hermes-tool-*.json')):
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 8192:
                        raise ValueError('untrusted lease')
                    value = json.loads(os.read(fd, 8193))
                finally:
                    os.close(fd)
                if type(value) is not dict:
                    raise ValueError('invalid lease object')
                if value.get('state') == 'RETIRED':
                    continue  # retired foreign images must not wedge restart
                if value.get('image') != self.image or value.get('source') != self.source:
                    report['skipped'].append(path.name)
                    continue
                self._reconcile_lease(path, value)
                report['retired'].append(path.name)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                report['errors'].append({'lease': path.name, 'type': type(exc).__name__})
        self.last_reconciliation = report
        if report['errors']:
            raise RuntimeError('Docker lease reconciliation incomplete: ' + str(len(report['errors'])))
        return report

    def _reconcile_lease(self, path, value):
        import re
        import threading
        if (set(value) != {'state','name','labels','cid','image','source','endpoint','daemon'}
                or value['state'] not in ('PENDING_CREATE', 'CREATING', 'CREATED', 'ATTESTED', 'STARTED', 'RETIRING')
                or not re.fullmatch(r'hermes-tool-[0-9a-f]{32}-[1-9][0-9]*', value['name'])
                or (value['cid'] is not None and not re.fullmatch('[0-9a-f]{64}', value['cid']))
                or path.name != value['name'] + '.json' or value['daemon'] != self.daemon or value['endpoint'] != self.endpoint
                or value['labels'] != {'hermes.tool.lease': value['name'].split('-')[2],
                    'hermes.tool.generation': value['name'].split('-')[3], 'hermes.tool.source': self.source,
                    'hermes.tool.image': self.image, 'hermes.tool.role': value['labels'].get('hermes.tool.role')}
                or value['labels']['hermes.tool.role'] not in ('builder', 'reviewer')):
            raise ValueError('lease authority mismatch')
        c = self.pending.get(value['name'])
        if c is None:
            c = DockerContainer.__new__(DockerContainer)
            c.runtime, c.directory, c.path = self, path.parent, path
            c.name, c.labels, c.cid = value['name'], value['labels'], value['cid']
            c.role, c.process, c.retired, c.lock = value['labels']['hermes.tool.role'], None, False, threading.RLock()
            c.cancelled = threading.Event()
            c.create_uncertain = c.cid is None and value['state'] != 'PENDING_CREATE'
            self.pending[c.name] = c
        c.close()

    def verify_image(self):
        value = self.json(['image', 'inspect', _digest(self.image)])[0]
        if (value['Id'] != self.image or value['Os'] != 'linux' or value['Architecture'] not in ('arm64', 'amd64')
                or value['Config'].get('Labels', {}).get('hermes.closure') != self.source
                or value['Config'].get('User') != '65533:65533'
                or value['Config'].get('Volumes') or value['Config'].get('OnBuild')
                or value['Config'].get('Entrypoint') != ['python', '-I', '-S', '-B', '/opt/hermes/agent/protected_docker_guest.py']):
            raise ValueError('unapproved immutable runtime image')
        self.image_config = value['Config']
        return value

    @classmethod
    def build(cls, source, private, *, docker, endpoint, base):
        """Private explicit-closure build against an ALREADY PRESENT official base ID.

        The retained manifest binds source bytes; build never pulls or installs.
        No arbitrary contexts or Dockerfile instructions are accepted from guests.
        """
        import hashlib
        import json
        import uuid
        source, private = Path(source), Path(private)
        runtime = cls(docker, endpoint, private)
        base = _digest(base)
        info = runtime.json(['image', 'inspect', base])[0]
        if info['Id'] != base or info['Os'] != 'linux' or not any(x.startswith('python@sha256:') for x in info['RepoDigests']):
            raise ValueError('cached official Python base required')
        context = private / ('build-' + uuid.uuid4().hex)
        context.mkdir(mode=0o700)
        files = {}
        for name in CLOSURE:
            # Source path closure is fixed, bounded and no-follow just like input.
            body = approved_inputs(source, (name,))[name] if (source / name).stat().st_size <= MAX_FILE else None
            if body is None:
                fd = os.open(source / name, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    s = os.fstat(fd)
                    if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or s.st_size > 65536:
                        raise ValueError('invalid runtime source')
                    body = os.read(fd, 65537)
                finally:
                    os.close(fd)
            path = context / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            path.chmod(0o444)
            files[name] = hashlib.sha256(body).hexdigest()
        import subprocess
        revision = subprocess.check_output(['/usr/bin/git', '-C', str(source), 'rev-parse', 'HEAD'], text=True, encoding='utf-8', timeout=5).strip()
        tree = subprocess.check_output(['/usr/bin/git', '-C', str(source), 'write-tree'], text=True, encoding='utf-8', timeout=5).strip()
        dirty = bool(subprocess.check_output(['/usr/bin/git', '-C', str(source), 'status', '--porcelain'], timeout=5))
        manifest = {'base': base, 'revision': revision, 'tree': tree, 'dirty': dirty, 'files': files}
        closure = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        (private / 'closure.json').write_text(json.dumps(dict(manifest, closure=closure), sort_keys=True), encoding='utf-8')
        dockerfile = 'FROM ' + base + '\n'
        for name in CLOSURE:
            dockerfile += 'COPY ' + name + ' /opt/hermes/' + name + '\n'
        dockerfile += ('LABEL hermes.closure="' + closure + '"\nUSER 65533:65533\n'
            'ENV HOME=/nonexistent LANG=C LC_ALL=C\nWORKDIR /\n'
            'ENTRYPOINT ["python","-I","-S","-B","/opt/hermes/agent/protected_docker_guest.py"]\n')
        (context / 'Dockerfile').write_text(dockerfile, encoding='utf-8')
        runtime.run(['build', '--pull=false', '--network=none', '--no-cache', '--iidfile', str(private / 'image-id'), str(context)], seconds=90)
        runtime.image = _digest((private / 'image-id').read_text(encoding='utf-8-sig').strip())
        runtime.source = closure
        runtime.record('image-' + closure, {'image': runtime.image, 'manifest': manifest, 'closure': closure})
        runtime.verify_image()
        return runtime


class DockerContainer:
    """One private durable lease, fixed policy and exact daemon-owned object."""
    def __init__(self, runtime, directory, generation, role):
        import secrets
        if type(generation) is not int or generation < 1 or role not in ('builder', 'reviewer'):
            raise ValueError('invalid Docker lease')
        self.runtime, self.role = runtime, role
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink() or stat.S_IMODE(self.directory.stat().st_mode) != 0o700:
            raise ValueError('private lease directory required')
        nonce = secrets.token_hex(16)
        self.name = 'hermes-tool-' + nonce + '-' + str(generation)
        self.labels = {'hermes.tool.lease': nonce, 'hermes.tool.generation': str(generation),
            'hermes.tool.source': runtime.source, 'hermes.tool.image': runtime.image, 'hermes.tool.role': role}
        self.path = self.directory / (self.name + '.json')
        self.cid, self.process, self.retired = None, None, False
        self.create_uncertain = False
        import threading
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        runtime.pending[self.name] = self
        self.save('PENDING_CREATE')

    def save(self, state):
        import json
        value = dict(state=state, name=self.name, labels=self.labels, cid=self.cid,
            image=self.runtime.image, source=self.runtime.source, endpoint=self.runtime.endpoint, daemon=self.runtime.daemon)
        temporary = self.path.with_suffix('.new')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, json.dumps(value, sort_keys=True).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, self.path)
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self.runtime.record(self.name + '-' + state, value)

    def inspect(self):
        if self.cid is None:
            raise ValueError('unacquired container')
        return self.runtime.json(['inspect', self.cid])[0]

    def matches(self, value):
        return (value['Id'] == self.cid and value['Name'] == '/' + self.name
            and value['Image'] == self.runtime.image and value['Config']['Labels'] == self.labels_with_image_labels())

    def labels_with_image_labels(self):
        return {'hermes.closure': self.runtime.source, **self.labels}

    def attest(self, *, running=True):
        value = self.inspect()
        h, c = value['HostConfig'], value['Config']
        expected_tmpfs = self.tmpfs()
        if (not self.matches(value) or c['User'] != '65533:65533' or not h['ReadonlyRootfs']
                or h['CapDrop'] != ['ALL'] or h['CapAdd'] or h['Privileged'] or h['Devices']
                or h['DeviceRequests'] or h.get('DeviceCgroupRules') or h.get('GroupAdd')
                or h.get('ExtraHosts') or h.get('Links') or h.get('PortBindings') or h.get('PublishAllPorts')
                or h['Binds'] or value['Mounts'] or h.get('VolumesFrom')
                or h['NetworkMode'] != 'none' or h['PidMode'] or h['IpcMode'] != 'private'
                or h['CgroupnsMode'] != 'private' or h['PidsLimit'] != 48
                or h['Memory'] != 201326592 or h['MemorySwap'] != 201326592 or h['NanoCpus'] != 500000000
                or h['SecurityOpt'] != ['no-new-privileges=true'] or h['Tmpfs'] != expected_tmpfs
                or h['LogConfig'] != {'Type': 'none', 'Config': {}} or h['RestartPolicy']['Name'] != 'no'
                or h['ShmSize'] != 1048576 or h['Init'] is not False or h['AutoRemove']
                or c['Cmd'] != ['deadline', '30'] or c['WorkingDir'] != '/'
                or c['Env'] != self.runtime.image_config['Env']
                or c['Entrypoint'] != self.runtime.image_config['Entrypoint']
                or c['Image'] != self.runtime.image or c['Tty'] or c['OpenStdin']
                or h.get('UTSMode') or h.get('UsernsMode')
                or sorted(h['Ulimits'], key=lambda x: x['Name']) != [{'Name': 'fsize', 'Hard': 65536, 'Soft': 65536}, {'Name': 'nofile', 'Hard': 128, 'Soft': 128}]
                or (running and not value['State']['Running'])):
            raise ValueError('Docker launch attestation refused')
        self.runtime.record(self.name + '-attested', value)
        return dict(cid=self.cid, image=self.runtime.image, source=self.runtime.source,
                    generation=self.labels['hermes.tool.generation'], lease=self.labels['hermes.tool.lease'])

    def tmpfs(self):
        uid = 65532 if self.role == 'builder' else 65534
        return {'/workspace': f'rw,noexec,nosuid,nodev,size=1m,nr_inodes=128,uid={uid},gid={uid},mode=0755',
                '/tmp': 'rw,noexec,nosuid,nodev,size=4m,nr_inodes=128,mode=1777'}  # no-tmp: ok — quota-limited guest tmpfs, never host scratch

    def create_args(self):
        args = ['create', '--pull=never', '--name', self.name, '--read-only', '--user', '65533:65533',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges=true', '--network', 'none',
            '--ipc', 'private', '--cgroupns', 'private', '--pids-limit', '48', '--memory', '192m',
            '--memory-swap', '192m', '--cpus', '.5', '--restart', 'no', '--log-driver', 'none',
            '--shm-size', '1m', '--ulimit', 'nofile=128:128', '--ulimit', 'fsize=65536:65536', '--init=false']
        for key, val in self.labels.items():
            args += ['--label', key + '=' + val]
        for key, val in self.tmpfs().items():
            args += ['--tmpfs', key + ':' + val]
        return [*args, self.runtime.image, 'deadline', '30']

    def create(self, inputs):
        import json
        import re
        import subprocess
        self.runtime.verify_image()
        try:
            self.save('CREATING')
            self.create_uncertain = True
            cid = self.runtime.run(self.create_args())[1].decode().strip()
            if not re.fullmatch('[0-9a-f]{64}', cid):
                raise ValueError('invalid daemon CID')
            self.cid = cid
            self.create_uncertain = False
            self.save('CREATED')
            self.attest(running=False)  # no untrusted code before this verification
            self.save('ATTESTED')
            if self.cancelled.is_set():
                raise RuntimeError('Docker acquisition cancelled')
            self.runtime.run(['start', self.cid])
            self.save('STARTED')
            # All stages are image-baked; only bounded explicit bytes enter as JSON frames.
            for name, body in inputs.items():
                import base64
                frame = json.dumps({'path': name, 'body': base64.b64encode(body).decode()}).encode() + b'\n'
                self.runtime.run(['exec', '-i', '--user', '65532:65532' if self.role == 'builder' else '65534:65534',
                    self.cid, 'python', '-I', '-S', '-B', '/opt/hermes/agent/protected_docker_guest.py', 'stage'], data=frame, limit=8192)
            if self.role == 'reviewer':
                self.runtime.run(['exec', '--user', '65534:65534', self.cid, 'python', '-I', '-S', '-B',
                    '/opt/hermes/agent/protected_docker_guest.py', 'seal'], limit=8192)
            self.attest()
            if self.cancelled.is_set():
                raise RuntimeError('Docker acquisition cancelled')
            self.process = subprocess.Popen(self.runtime.command(['exec', '-i', '--user', '65532:65532', '-w', '/workspace',
                self.cid, 'python', '-I', '-S', '-B', '/opt/hermes/agent/protected_docker_guest.py', 'tools']),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                close_fds=True, start_new_session=True, env=self.runtime.env)
            # Host PID binds the native bridge reservation, NOT the guest principal.
            self.process._protected_container = self
            return self.process
        except BaseException:
            self.close()
            raise

    def export(self, names):
        import base64
        import hashlib
        import json
        names = checked_paths(names)
        with self.lock:
            if self.retired:
                raise ValueError('retired export')
            self.attest()
            # Stop EVERY tool-UID process, including setsid children. The trusted
            # stopper is excluded by kill(-1); it immediately exits. Distinct UID
            # verifier/exporter cannot be attacked by a stopped tool process.
            self.runtime.run(['exec', '--user', '65532:65532', self.cid,
                '/bin/bash', '--noprofile', '--norc', '-c', 'kill -STOP -1'])
            self.runtime.run(['exec', '--user', '65533:65533', self.cid, 'python', '-I', '-S', '-B',
                '/opt/hermes/agent/protected_docker_guest.py', 'quiescent'], limit=8192)
            result, total = {}, 0
            for name in names:
                _code, wire = self.runtime.run(['exec', '-i', '--user', '65534:65534', self.cid,
                    'python', '-I', '-S', '-B', '/opt/hermes/agent/protected_docker_guest.py', 'export'],
                    data=json.dumps({'path': name}).encode() + b'\n', limit=8192)
                def unique(items):
                    value = dict(items)
                    if len(value) != len(items):
                        raise ValueError('duplicate export field')
                    return value
                frame = json.loads(wire, object_pairs_hook=unique)
                if type(frame) is not dict or set(frame) != {'path', 'body', 'sha256'} or frame['path'] != name:
                    raise ValueError('artifact frame mismatch')
                body = base64.b64decode(frame['body'], validate=True)
                total += len(body)
                if len(body) > MAX_FILE or total > MAX_TOTAL or hashlib.sha256(body).hexdigest() != frame['sha256']:
                    raise ValueError('artifact quota/hash mismatch')
                result[name] = body
            return result

    def absent(self):
        code, output = self.runtime.run(['inspect', self.cid or self.name], missing=True)
        return code != 0 and (b'no such object' in output.lower() or b'no such container' in output.lower())

    def close(self):
        """Revoke first; exact object teardown and read-back; failed lease stays retryable."""
        with self.lock:
            if self.retired:
                return
            # On an interrupted create, recover only this immutable name+labels+image.
            if self.cid is None:
                code, output = self.runtime.run(['inspect', self.name], missing=True)
                if code == 0:
                    import json
                    value = json.loads(output)[0]
                    self.cid = value['Id']
                elif b'no such object' not in output.lower() and b'no such container' not in output.lower():
                    raise RuntimeError('Docker acquisition state unknown')
                elif self.create_uncertain:
                    # Reissue the SAME named request: either acquire its exact
                    # object or get a name conflict with the original request.
                    # Never start this recovery object or dispatch a tool.
                    self.runtime.run(self.create_args(), missing=True)
                    import json
                    value = self.runtime.json(['inspect', self.name])[0]
                    self.cid = value['Id']
            if self.cid is not None and not self.absent():
                if not self.matches(self.inspect()):
                    raise ValueError('Docker cleanup identity mismatch')
                self.runtime.run(['kill', '--signal', 'KILL', self.cid], missing=True)
                state = self.inspect()['State']
                if state['Running'] or state['Pid'] != 0:
                    raise RuntimeError('Docker namespace not reaped')
                self.runtime.record(self.name + '-reaped', {'cid': self.cid, 'state': state})
                self.runtime.run(['rm', self.cid])
                if not self.absent():
                    raise RuntimeError('Docker exact CID remains')
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.kill()
                self.process.wait(timeout=3)
            self.save('RETIRED')
            self.retired = True
            self.runtime.pending.pop(self.name, None)


class DockerLaunch:
    """Trusted owner construction only; there is no request/config selector."""
    def __init__(self, workspace, runtime, leases, paths, role):
        if type(runtime) is not DockerRuntime or type(workspace) is not type(Path()) or workspace.resolve() != workspace:
            raise ValueError('trusted runtime and canonical workspace required')
        self.workspace, self.runtime, self.leases = workspace, runtime, Path(leases)
        self.paths, self.role = checked_paths(paths), role

    async def acquire(self, generation):
        import asyncio
        inputs = approved_inputs(self.workspace, self.paths)
        container = DockerContainer(self.runtime, self.leases, generation, self.role)
        task = asyncio.create_task(asyncio.to_thread(container.create, inputs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            container.cancelled.set()
            # No orphaned late acquisition; wait through repeated cancellation before cleanup.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    pass
                except (OSError, RuntimeError, ValueError):
                    break
            try:
                task.result()
            except (OSError, RuntimeError, ValueError):
                pass
            finally:
                cleanup = asyncio.create_task(asyncio.to_thread(container.close))
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        pass
                cleanup.result()
            raise
