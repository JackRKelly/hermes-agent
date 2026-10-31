"""Bounded tool-only wire format; never provider credentials or owner RPC."""
import json

MAX_FRAME = 8192
OPS = {'hello', 'bind', 'call', 'result', 'cancel', 'cancelled'}
SOURCE_IMPORTS = ('agent', 'agent.jiter_preload', 'agent.protected_tool_worker',
                  'tools', 'tools.file_operations_native')


def source_imports():
    import sys
    names = sorted(name for name in sys.modules
        if name != '__main__' and name.split('.')[0] not in sys.stdlib_module_names)
    if set(names) - set(SOURCE_IMPORTS):
        raise ValueError('unexpected non-stdlib import')
    return names


def _unique(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('duplicate field')
    return result


def read_frame(stream):
    data = stream.readline(MAX_FRAME + 1)
    if not data:
        raise EOFError('tool channel closed')
    if len(data) > MAX_FRAME or not data.endswith(b'\n'):
        raise ValueError('tool frame exceeds bound or truncated')
    try:
        frame = json.loads(data, object_pairs_hook=_unique)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError('invalid tool frame') from exc
    if (type(frame) is not dict or type(frame.get('op')) is not str
            or frame['op'] not in OPS):
        raise ValueError('unsupported tool operation')
    return frame


def encode_frame(frame):
    data = json.dumps(frame, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode() + b'\n'
    if len(data) > MAX_FRAME:
        raise ValueError('tool frame exceeds bound')
    return data


def read_sentinel_file(workspace):
    # Reuse the built-in regular-file primitive without its optional shell fallback.
    import os
    from pathlib import Path
    from tools.file_operations_native import _open_regular
    fd = _open_regular(str(Path(workspace) / 'allowed.txt'), nofollow=True)
    if fd is None:
        raise ValueError('missing sentinel')
    with os.fdopen(fd, 'rb') as stream:
        body = stream.read(4097)
    if len(body) > 4096:
        raise ValueError('sentinel file too large')
    return body


def _read_sentinel():
    return '\n'.join(f'{index}|{line}' for index, line in enumerate(read_sentinel_file('.').decode().splitlines(), 1))


def _terminal_sentinel():
    import subprocess
    # Freeze the bounded native read before the subprocess. cat never reopens a
    # model-controlled path; it receives only this explicitly selected stdin.
    body = read_sentinel_file('.')
    result = subprocess.run(['/bin/cat'], input=body,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True,
        pass_fds=(), env={'LANG': 'C', 'LC_ALL': 'C'}, timeout=2, check=True)
    return result.stdout.decode()


def docker_tool(name, args):
    """Small native Docker-only file/shell consumer; no host fallback at all.

    Guest code/results are untrusted. Private subprocess stdio cannot inherit the
    channel; CID identity comes only from the trusted owner/daemon.
    """
    import os
    import selectors
    import subprocess
    import time
    if not args:
        return _read_sentinel() if name == 'read_file' else _terminal_sentinel()
    key = 'path' if name == 'read_file' else 'command'
    if (set(args) != {key} or type(args[key]) is not str
            or not 1 <= len(args[key].encode()) <= 2048 or '\x00' in args[key]):
        raise ValueError('invalid Docker tool arguments')
    if name == 'read_file':
        from tools.file_operations_native import _open_regular
        fd = _open_regular(args['path'], nofollow=True)
        if fd is None:
            raise ValueError('missing native file')
        with os.fdopen(fd, 'rb') as stream:
            body = stream.read(4097)
        if len(body) > 4096:
            raise ValueError('native file quota')
        return '\n'.join(f'{i}|{line}' for i, line in enumerate(body.decode().splitlines(), 1))
    # Load the native file module before shell too, so the pinned import closure
    # remains invariant independently of tool order.
    from tools import file_operations_native
    process = subprocess.Popen(['/bin/bash', '--noprofile', '--norc', '-c', args['command']],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        close_fds=True, pass_fds=(), env={'LANG': 'C', 'LC_ALL': 'C', 'PATH': '/usr/local/bin:/usr/bin:/bin'}, bufsize=0)
    end, body = time.monotonic() + 2, bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = end - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError('Docker shell deadline')
                chunk = os.read(process.stdout.fileno(), min(1024, 4097 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > 4096:
                    raise ValueError('Docker shell output quota')
        process.wait(timeout=max(.01, end - time.monotonic()))
        return body.decode(errors='replace')
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)
        process.stdout.close()


def sentinel_main(*, docker=False):
    import os
    import sys
    import threading
    if sys.platform not in ("darwin", "linux") or 0 in (os.getuid(), os.geteuid()):  # windows-footgun: ok — short-circuit POSIX guard
        return 1
    # A fixed hard lifetime bounds silent/partial bootstrap and control frames.
    watchdog = threading.Timer(5, lambda: os._exit(1))
    watchdog.daemon = True
    watchdog.start()
    def send(frame):
        sys.stdout.buffer.write(encode_frame(frame))
        sys.stdout.buffer.flush()
    try:
        fds = []
        for name in os.listdir('/dev/fd'):
            try:
                os.fstat(int(name))
                fds.append(name)
            except OSError as exc:
                import errno
                if exc.errno != errno.EBADF:
                    raise
        send({'op': 'hello', 'pid': os.getpid(), 'fds': sorted(fds),
              'environment': sorted(os.environ), 'imports': source_imports()})
        binding = read_frame(sys.stdin.buffer)
        if binding != {'op': 'bind', 'pid': os.getpid(), 'adapter': 'sentinel-only-v1'}:
            raise ValueError('invalid sentinel binding')
        tools = {'read_file': _read_sentinel, 'terminal': _terminal_sentinel}
        for sequence in range(1, 17):
            frame = read_frame(sys.stdin.buffer)
            if frame == {'op': 'cancel'}:
                send({'op': 'cancelled'})
                return 0
            if (set(frame) != {'op', 'sequence', 'name', 'args'} or frame['op'] != 'call'
                    or type(frame['sequence']) is not int or frame['sequence'] != sequence
                    or type(frame['name']) is not str or frame['name'] not in tools
                    or type(frame['args']) is not dict or (frame['args'] and not docker)):
                raise ValueError('invalid sentinel tool call')
            result = docker_tool(frame['name'], frame['args']) if docker else tools[frame['name']]()
            send({'op': 'result', 'sequence': sequence, 'result': result, 'imports': source_imports()})
        return 0
    except (ValueError, EOFError, OSError):
        return 1
    finally:
        watchdog.cancel()
