"""Import-pure bounded JSON channels for the SOURCE protected conversation.

No discovery, dynamic methods, pickle, files, credentials or provider SDK imports.
The kernel peer check authenticates each connected Unix socket, not a hello PID.
Same-UID processes/source remain mutable: this is not privileged isolation.
"""
import json
import os
import socket
import struct
import sys

from agent.protected_conversation import MAX_BYTES, CapabilityClient, ProtectedTurn
from agent.transports.types import NormalizedResponse, ToolCall, Usage


def _unique(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError('duplicate protected IPC field')
    return value


def _invalid_constant(value):
    raise ValueError('invalid protected IPC constant')


def encode(value):
    raw = json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(',', ':')).encode()
    if len(raw) > MAX_BYTES:
        raise ValueError('protected IPC frame exceeds bound')
    return struct.pack('!I', len(raw)) + raw


def receive(read):
    def exact(count):
        chunks = bytearray()
        while len(chunks) < count:
            data = read(count - len(chunks))
            if not data:
                raise EOFError('protected IPC closed')
            chunks.extend(data)
        return bytes(chunks)
    size = struct.unpack('!I', exact(4))[0]
    if not 0 < size <= MAX_BYTES:
        raise ValueError('protected IPC frame exceeds bound')
    return json.loads(exact(size), object_pairs_hook=_unique, parse_constant=_invalid_constant)


def current_uid():
    if sys.platform not in ('linux', 'darwin'):
        raise RuntimeError('protected IPC platform unsupported')
    return os.getuid()  # windows-footgun: ok -- guarded supported Unix platforms only


def verify_peer(sock, *, pid, uid):
    if sys.platform == 'linux':
        peer_pid, peer_uid, _ = struct.unpack('iII', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    elif sys.platform == 'darwin':
        # sys/un.h: SOL_LOCAL=0, LOCAL_PEERPID=0x002. No caller-provided hello identity.
        peer_pid = sock.getsockopt(0, 0x002)
        # CPython does not expose getpeereid on every macOS build. LOCAL_PEERCRED
        # returns sys/ucred.h xucred (version uint32, uid uint32, ngroups int16).
        credentials = sock.getsockopt(0, 0x001, 1024)
        version, peer_uid = struct.unpack_from('=II', credentials)
        if version != 0:
            raise RuntimeError('protected IPC peer credentials unsupported')
    else:
        raise RuntimeError('protected IPC platform unsupported')
    if type(pid) is not int or type(uid) is not int or (peer_pid, peer_uid) != (pid, uid):
        raise RuntimeError('protected IPC peer denied')


def response_from_wire(value):
    if type(value) is not dict or set(value) != {'content', 'tool_calls', 'finish_reason', 'reasoning', 'usage', 'provider_data'}:
        raise ValueError('invalid protected response envelope')
    value = dict(value)
    if value['tool_calls'] is not None:
        if type(value['tool_calls']) is not list or len(value['tool_calls']) > 4:
            raise ValueError('invalid protected tool calls')
        value['tool_calls'] = [ToolCall(**row) for row in value['tool_calls']]
    if value['usage'] is not None:
        value['usage'] = Usage(**value['usage'])
    return NormalizedResponse(**value)


class SocketCapability:
    def __init__(self, path, config, *, owner_pid, owner_uid):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(5)
        try:
            self.socket.connect(path)
            verify_peer(self.socket, pid=owner_pid, uid=owner_uid)
            self.socket.sendall(encode({'channel': config['channel'], 'token': config['token']}))
            self.client = CapabilityClient(self._send, config['token'], config['binding'], config['channel'])
        except BaseException:
            self.close()
            raise

    def _send(self, frame):
        self.socket.sendall(encode(frame))
        reply = receive(self.socket.recv)
        if type(reply) is not dict or set(reply) != {'ok', 'value'} or reply['ok'] is not True:
            raise RuntimeError('protected capability request failed')
        return response_from_wire(reply['value']) if self.client.channel == 'inference' else reply['value']

    def close(self):
        self.socket.close()


class RemoteTools:
    def __init__(self, client):
        from types import SimpleNamespace
        import threading
        self.client = client
        self.worker = SimpleNamespace(stop=threading.Event(), close=lambda: None)

    def invoke(self, name, args):
        return self.client.request('call', {'name': name, 'args': args})


class ProtectedConversation:
    """Public completion facade that never imports ordinary AIAgent bootstrap."""
    def __init__(self, inference, lifecycle, tools):
        self.turn = ProtectedTurn(inference, lifecycle, RemoteTools(tools))

    def run_conversation(self, user_message):
        return self.turn.run(user_message)


def conversation_main():
    channels = []
    try:
        config = receive(sys.stdin.buffer.read)
        if (type(config) is not dict or set(config) != {'path', 'owner_pid', 'owner_uid', 'channels', 'prompt'}
                or config['owner_pid'] != os.getppid() or config['owner_uid'] != current_uid()
                or set(config['channels']) != {'inference', 'lifecycle', 'tools'}):
            raise ValueError('invalid protected bootstrap')
        for name in ('inference', 'lifecycle', 'tools'):
            channel = config['channels'][name]
            if type(channel) is not dict or set(channel) != {'channel', 'token', 'binding'} or channel['channel'] != name:
                raise ValueError('invalid protected channel')
            channels.append(SocketCapability(config['path'], channel,
                owner_pid=config['owner_pid'], owner_uid=config['owner_uid']))
        facade = ProtectedConversation(*(channel.client for channel in channels))
        result = facade.run_conversation(config['prompt'])
        sys.stdout.buffer.write(encode({'result': result, 'imports': sorted(sys.modules)}))
        sys.stdout.buffer.flush()
        return 0
    except (ValueError, RuntimeError, OSError, EOFError, TypeError, KeyError):
        sys.stdout.buffer.write(encode({'error': 'protected conversation failed'}))
        sys.stdout.buffer.flush()
        return 1
    finally:
        for channel in channels:
            channel.close()
