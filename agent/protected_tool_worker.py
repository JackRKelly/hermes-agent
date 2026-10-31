"""Bounded tool-only wire format; never provider credentials or owner RPC."""
import json

MAX_FRAME = 8192
OPS = {'hello', 'bind', 'call', 'result', 'cancel', 'cancelled'}


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


def sentinel_main():
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
              'environment': sorted(os.environ)})
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
                    or type(frame['args']) is not dict or frame['args']):
                raise ValueError('invalid sentinel tool call')
            send({'op': 'result', 'sequence': sequence, 'result': tools[frame['name']]()})
        return 0
    except (ValueError, EOFError, OSError):
        return 1
    finally:
        watchdog.cancel()
