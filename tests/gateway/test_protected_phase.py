"""Controlled signed host operations, never publication authority from model JSON."""
from dataclasses import replace
from contextlib import closing
import time

import pytest

from tests.gateway.test_protected_native_admission import SignedFixture, DurableFixtureLedger, fixture_source


@pytest.mark.asyncio
@pytest.mark.parametrize('used', [0,12])
async def test_bootstrap_claim_is_atomic_blocked_to_owned_canonical_run(tmp_path, monkeypatch, used):
    from gateway.session_card_owner import card_owner_runtime
    from gateway.session_protected_phase import (HeldPhasePolicy, install_held_card,
        native_phase_bootstrap_expected, native_phase_expected, native_phase_commit)
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setattr('gateway.session_protected_admission._source_identity', fixture_source)
    home = tmp_path / 'profiles' / 'builder'
    home.mkdir(parents=True)
    reviewer = tmp_path / 'profiles' / 'reviewer'
    reviewer.mkdir()
    monkeypatch.setattr('hermes_cli.profiles.resolve_profile_env', lambda name: str(home if name == 'builder' else reviewer))
    kb.create_board('controlled')
    with closing(connect(board='controlled')) as conn:
        task_id = kb.create_task(conn, title='CONTROLLED HELD', assignee='builder', workspace_kind='dir',
                                 workspace_path=str(tmp_path), max_retries=0)
        assert kb.block_task(conn, task_id, reason='controlled prepared')
    policy = HeldPhasePolicy(graph_digest='a' * 64, campaign_key='controlled', card_id='logical-card',
        board='controlled', task_id=task_id, role='builder', implementer_profile='builder', reviewer_profile='reviewer', input_head='b' * 40)
    signer, ledger = SignedFixture(), DurableFixtureLedger(tmp_path / 'ledger.db')
    started = ledger.check({})['started_at']
    def install(authority):
        expected = native_phase_bootstrap_expected(authority, policy=policy, controller_started_at=started, publication_identity={'app_id':123,'login':'controlled-review[bot]'})
        install_held_card(authority, policy=policy, bootstrap_envelope=signer.sign(expected), verify=signer.verify,
                          ledger=ledger, controller_started_at=started, publication_identity={'app_id':123,'login':'controlled-review[bot]'})
    async with card_owner_runtime(profile_home=home, kanban_home=tmp_path, install=install) as owner:
        params = dict(board='controlled', task_id=task_id, admission_id=None, action='claim_builder', published_head=None, publication=None)
        for _ in range(used):
            ledger.reserve({})
        if used == 12:
            with pytest.raises(Exception):
                native_phase_expected(owner.authority, **params)
            assert ledger.check({})['requests'] == 12
            return
        from gateway.session_protected_phase import _budget
        with monkeypatch.context() as clock:
            clock.setattr('gateway.session_protected_phase.time.monotonic',lambda:1e99)
            with pytest.raises(Exception):
                _budget(owner.authority._held_card,claim=True)
        expected = native_phase_expected(owner.authority, **params)
        envelope = signer.sign(expected)
        context = native_phase_commit(owner.authority, envelope=envelope)
        assert native_phase_commit(owner.authority, envelope=envelope) == context
        with closing(connect(board='controlled')) as conn:
            task = kb.get_task(conn, task_id)
            assert task.status == 'running' and task.current_run_id == context['run_id']
            assert task.claim_lock == context['claim_lock']
            assert conn.execute("SELECT count(*) FROM task_runs WHERE task_id=? AND status='running'", (task_id,)).fetchone()[0] == 1
            assert kb._latest_event(conn, task_id, 'claimed', context['run_id']) is not None
        with pytest.raises(Exception):
            native_phase_commit(owner.authority, envelope={**envelope, 'signature': 'unsigned'})


@pytest.mark.parametrize('field,value', [('role','other'),('input_head','bad'),('graph_digest','bad'),('implementer_profile','../escape'),('graph_digest',None)])
def test_phase_policy_rejects_malformed_scope(field, value):
    from gateway.session_protected_phase import HeldPhasePolicy
    with pytest.raises(ValueError):
        HeldPhasePolicy(**(dict(graph_digest='a'*64, campaign_key='campaign', card_id='card', board='controlled',
            task_id='t_1234', role='builder', implementer_profile='builder', reviewer_profile='reviewer', input_head='b'*40) | {field: value}))


@pytest.mark.parametrize('text', [
    '{"verdict":"REQUEST_CHANGES","verdict":"APPROVE","head":"'+'b'*40+'"}',
    '{"verdict":"COMMENT","head":"'+'b'*40+'"}',
    '{"verdict":"APPROVE","head":"short"}',
    'APPROVE',
])
def test_ambiguous_reviewer_json_is_not_an_observed_decision(text):
    from gateway.session_protected_phase import _observed_decision
    with pytest.raises(ValueError):
        _observed_decision(text)
