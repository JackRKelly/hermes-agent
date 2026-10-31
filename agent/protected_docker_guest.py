"""Immutable stdlib-only Docker PID1 and pre-tool projection entry.

No provider/lifecycle authority. Owner execs use different unprivileged UIDs;
PID1 UID65533 cannot be signalled by the tool UID65532. Its exit tears down the
PID namespace, not just a process group. Model tools never select this mode.
"""
import base64
import json
import os
from pathlib import Path
import sys
import time


def deadline(seconds):
    if sys.platform != 'linux' or os.getpid() != 1 or os.getuid() != 65533 or seconds != 30:  # windows-footgun: ok — short-circuit Linux-only immutable image entry
        raise ValueError('invalid namespace deadline')
    clock = lambda: time.clock_gettime(time.CLOCK_BOOTTIME)
    start = clock()
    end, last_tool, observed = start + seconds, start, False
    while clock() < end:
        active = False
        for path in Path('/proc').glob('[0-9]*/status'):
            try:
                fields = dict(line.split(':', 1) for line in path.read_text(encoding='utf-8-sig').splitlines() if ':' in line)
                if fields['Uid'].split()[0] == '65532' and fields['State'].strip()[0] not in ('Z', 'X'):
                    active = True
            except FileNotFoundError:
                pass
        if active:
            last_tool, observed = clock(), True
        elif (observed and clock() - last_tool > 2) or (not observed and clock() - start > 15):
            os._exit(0)
        try:
            while os.waitpid(-1, os.WNOHANG)[0] > 0:
                pass
        except ChildProcessError:
            pass
        time.sleep(.05)
    os._exit(0)


def stage():
    if sys.platform != 'linux' or os.getuid() not in (65532, 65534):  # windows-footgun: ok — short-circuit Linux-only immutable image entry
        raise ValueError('invalid staging UID')
    data = sys.stdin.buffer.readline(8193)
    if len(data) > 8192 or not data.endswith(b'\n'):
        raise ValueError('projection frame')
    def unique(items):
        value = dict(items)
        if len(value) != len(items):
            raise ValueError('duplicate projection field')
        return value
    frame = json.loads(data, object_pairs_hook=unique)
    if type(frame) is not dict or set(frame) != {'path', 'body'}:
        raise ValueError('projection schema')
    name = frame['path']
    if (type(name) is not str or len(name) > 160 or '\\' in name or ':' in name
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or any(p in ('', '.', '..') or p.startswith('.') for p in name.split('/'))):
        raise ValueError('projection path')
    body = base64.b64decode(frame['body'], validate=True)
    if len(body) > 4096:
        raise ValueError('projection size')
    path = Path('/workspace') / name
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        os.write(fd, body)
    finally:
        os.close(fd)


def seal():
    if sys.platform != 'linux' or os.getuid() != 65534:  # windows-footgun: ok — short-circuit Linux-only immutable image entry
        raise ValueError('invalid reviewer seal UID')
    root = Path('/workspace')
    for path in sorted(root.rglob('*'), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def quiescent():
    if sys.platform != 'linux' or os.getuid() != 65533:  # windows-footgun: ok — short-circuit Linux-only immutable image entry
        raise ValueError('invalid quiescence verifier')
    for path in Path('/proc').glob('[0-9]*/status'):
        try:
            fields = dict(line.split(':', 1) for line in path.read_text(encoding='utf-8-sig').splitlines() if ':' in line)
            if fields['Uid'].split()[0] == '65532' and fields['State'].strip()[0] not in ('T', 'Z', 'X'):
                raise ValueError('tool process remains active')
        except FileNotFoundError:
            pass


def export():
    import hashlib
    import stat
    if sys.platform != 'linux' or os.getuid() != 65534:  # windows-footgun: ok — short-circuit Linux-only immutable image entry
        raise ValueError('invalid exporter')
    data = sys.stdin.buffer.readline(8193)
    if len(data) > 8192 or not data.endswith(b'\n'):
        raise ValueError('artifact request quota')
    frame = json.loads(data)
    if type(frame) is not dict or set(frame) != {'path'}:
        raise ValueError('artifact request schema')
    name = frame['path']
    if (type(name) is not str or len(name) > 160 or '\\' in name or ':' in name
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or any(p in ('', '.', '..') or p.startswith('.') for p in name.split('/'))):
        raise ValueError('artifact path')
    parent = os.open('/workspace', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        pieces = name.split('/')
        for piece in pieces[:-1]:
            child = os.open(piece, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(pieces[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096:
                raise ValueError('artifact type/quota')
            body = os.read(fd, 4097)
            if len(body) > 4096:
                raise ValueError('artifact quota')
        finally:
            os.close(fd)
    finally:
        os.close(parent)
    print(json.dumps({'path': name, 'body': base64.b64encode(body).decode(), 'sha256': hashlib.sha256(body).hexdigest()}))


def tools():
    if sys.platform != 'linux' or os.getuid() != 65532:  # windows-footgun: ok — short-circuit Linux-only immutable image entry
        raise ValueError('invalid tool UID')
    # The image contains exactly the explicit source closure, never workspace
    # imports/site-packages. Environment metadata is not an authority token.
    sys.path.insert(0, '/opt/hermes')
    os.environ.clear()
    os.environ.update(LANG='C', LC_ALL='C')
    from agent.protected_tool_worker import sentinel_main
    return sentinel_main(docker=True)


def main():
    modes = {('deadline', '30'): lambda: deadline(30), ('stage',): stage,
             ('seal',): seal, ('quiescent',): quiescent, ('export',): export, ('tools',): tools}
    handler = modes.get(tuple(sys.argv[1:]))
    if handler is None:
        raise ValueError('unsupported immutable guest entry')
    return handler() or 0


if __name__ == '__main__':
    raise SystemExit(main())
