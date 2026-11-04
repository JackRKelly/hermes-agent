"""Portable supervisor configuration is explicit; never inherited by the guest."""
import os
from pathlib import Path
import socket

import pytest


def test_configuration_requires_explicit_opt_in_and_all_fields(tmp_path):
    from tests.gateway.docker_boundary_config import boundary_config
    with pytest.raises(ValueError, match='opt-in'):
        boundary_config({})
    with pytest.raises(ValueError, match='HERMES_TEST_DOCKER_EXECUTABLE'):
        boundary_config({'HERMES_DOCKER_BOUNDARY_TEST': '1'})


@pytest.mark.parametrize('field,value', [
    ('executable', 'docker'), ('executable', '/nonexistent/docker'),
    ('endpoint', 'tcp://localhost:2375'), ('endpoint', 'ssh://host'),
    ('endpoint', 'unix:///nonexistent/docker.sock'), ('base', 'python:latest'),
    ('base', 'sha256:unknown'),
])
def test_invalid_configuration_refused_before_daemon(tmp_path, field, value):
    from tests.gateway.docker_boundary_config import boundary_config
    executable = tmp_path / 'docker'
    executable.write_text('#!/bin/sh\nexit 1\n')
    executable.chmod(0o700)
    with socket.socket(socket.AF_UNIX) as sock:
        # Bind relative to a held short cwd, avoiding AF_UNIX pathname limits.
        endpoint = tmp_path / 's'
        fd = os.open('.', os.O_RDONLY)
        try:
            os.chdir(tmp_path)
            sock.bind(endpoint.name)
        finally:
            os.fchdir(fd)
            os.close(fd)
        values = dict(executable=str(executable), endpoint='unix://' + str(endpoint), base='sha256:' + 'a' * 64)
        values[field] = value
        env = {'HERMES_DOCKER_BOUNDARY_TEST': '1', **{
            'HERMES_TEST_DOCKER_' + k.upper().replace('BASE', 'BASE_IMAGE'): v for k, v in values.items()}}
        with pytest.raises(ValueError):
            boundary_config(env)


def test_symlink_executable_and_endpoint_refused(tmp_path):
    from tests.gateway.docker_boundary_config import boundary_config
    executable = tmp_path / 'docker'
    executable.write_text('#!/bin/sh\nexit 1\n')
    executable.chmod(0o700)
    linked = tmp_path / 'link'
    linked.symlink_to(executable)
    with socket.socket(socket.AF_UNIX) as sock:
        # Bind relative to a held short cwd, avoiding AF_UNIX pathname limits.
        endpoint = tmp_path / 's'
        fd = os.open('.', os.O_RDONLY)
        try:
            os.chdir(tmp_path)
            sock.bind(endpoint.name)
        finally:
            os.fchdir(fd)
            os.close(fd)
        env = {'HERMES_DOCKER_BOUNDARY_TEST': '1', 'HERMES_TEST_DOCKER_EXECUTABLE': str(linked),
            'HERMES_TEST_DOCKER_ENDPOINT': 'unix://' + str(endpoint),
            'HERMES_TEST_DOCKER_BASE_IMAGE': 'sha256:' + 'a' * 64}
        with pytest.raises(ValueError, match='canonical'):
            boundary_config(env)
        env['HERMES_TEST_DOCKER_EXECUTABLE'] = str(executable)
        linked.unlink()
        linked.symlink_to(endpoint)
        env['HERMES_TEST_DOCKER_ENDPOINT'] = 'unix://' + str(linked)
        with pytest.raises(ValueError, match='canonical'):
            boundary_config(env)
        env['HERMES_TEST_DOCKER_ENDPOINT'] = 'unix://' + str(endpoint)
        env['DOCKER_HOST'] = 'tcp://unselected:2375'
        assert boundary_config(env) == dict(docker=str(executable), endpoint='unix://' + str(endpoint), base='sha256:' + 'a' * 64)


def test_public_boundary_tests_have_no_personal_paths_or_host_image_pin():
    source = Path(__file__).with_name('test_protected_docker.py').read_text()
    assert '/Users/' not in source
    assert "docker='/usr/local/bin/docker'" not in source
    assert 'sha256:cd89' not in source
    assert 'DockerRuntime.build(' not in source  # one shared explicit session fixture
    doc = Path(__file__).resolve().parents[2] / 'docs/protected-docker-tools.md'
    guidance = doc.read_text()
    assert 'unix:///var/run/docker.sock' not in guidance  # /var/run is a symlink on hosted Linux
    assert 'unix:///run/docker.sock' in guidance
    assert 'non-Alpine' in guidance
    assert 'symlinked parent' in guidance
