"""Canonical CLI zero-retry preparation, without a claimable handoff."""
import argparse
import json

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    kb.init_db()


def parser():
    root = argparse.ArgumentParser()
    cli.build_parser(root.add_subparsers(dest='command'))
    return root


def test_cli_prepares_zero_retry_blocked_and_keeps_recompute_sticky(home, capsys):
    args = parser().parse_args(['kanban', 'create', 'protected', '--initial-status', 'blocked', '--max-retries', '0', '--json'])
    assert cli.kanban_command(args) == 0
    created = json.loads(capsys.readouterr().out)
    tid = created['id']
    assert created['status'] == 'blocked'
    assert created['max_retries'] == 0
    assert cli.kanban_command(parser().parse_args(['kanban', 'show', tid, '--json'])) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown['task']['status'] == 'blocked'
    assert shown['task']['max_retries'] == 0
    assert shown['runs'] == []
    with kbc.connect_closing() as conn:
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == 'blocked'
        assert kb.claim_task(conn, tid) is None


@pytest.mark.parametrize('value', [-1, True, False, 0.5, '0'])
def test_direct_cli_rejects_invalid_retry_types(home, capsys, value):
    args = parser().parse_args(['kanban', 'create', 'invalid', '--json'])
    args.max_retries = value
    assert cli.kanban_command(args) == 2
    with kbc.connect_closing() as conn:
        assert kb.list_tasks(conn) == []


@pytest.mark.parametrize('value', [None, 1, 3])
def test_cli_preserves_default_and_positive_retry_values(home, capsys, value):
    argv = ['kanban', 'create', 'ordinary', '--json']
    if value is not None:
        argv += ['--max-retries', str(value)]
    assert cli.kanban_command(parser().parse_args(argv)) == 0
    created = json.loads(capsys.readouterr().out)
    assert created['max_retries'] == value
