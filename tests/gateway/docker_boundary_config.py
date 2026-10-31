"""Test-supervisor selection only. No ambient Docker context/auth discovery."""
from pathlib import Path
import os
import stat


def boundary_config(env):
    if env.get('HERMES_DOCKER_BOUNDARY_TEST') != '1':
        raise ValueError('explicit Docker boundary opt-in required')
    values = {}
    for field in ('EXECUTABLE', 'ENDPOINT', 'BASE_IMAGE'):
        key = 'HERMES_TEST_DOCKER_' + field
        value = env.get(key)
        if not value:
            raise ValueError('explicit ' + key + ' required')
        values[field] = value
    def canonical(value):
        path = Path(value)
        if not path.is_absolute() or str(path) != value or path.resolve(strict=True) != path:
            raise ValueError('canonical absolute Docker path required; select the real target explicitly')
        return path
    try:
        executable = canonical(values['EXECUTABLE'])
        if not stat.S_ISREG(executable.stat().st_mode) or not os.access(executable, os.X_OK):
            raise ValueError('executable Docker binary required')
        endpoint = values['ENDPOINT']
        if not endpoint.startswith('unix:///') or any(ord(c) < 32 for c in endpoint):
            raise ValueError('explicit local Unix endpoint required')
        sock = canonical(endpoint[len('unix://'):])
        if not stat.S_ISSOCK(sock.stat().st_mode):
            raise ValueError('existing Unix Docker socket required')
    except OSError as exc:
        raise ValueError('unknown Docker executable or endpoint') from exc
    from gateway.session_protected_docker import _digest
    return dict(docker=str(executable), endpoint=endpoint, base=_digest(values['BASE_IMAGE']))


def build_boundary_runtime(private):
    import pytest
    if os.environ.get('HERMES_DOCKER_BOUNDARY_TEST') != '1':
        pytest.skip('explicit existing-daemon Docker acceptance only')
    from gateway.session_protected_docker import DockerRuntime
    # Errors after opt-in are failures, never skipped acceptance evidence.
    return DockerRuntime.build(Path(__file__).resolve().parents[2], private, **boundary_config(os.environ))
