"""Controlled provider fixtures only; terminal success is not scope teardown."""
import asyncio
from contextlib import closing
import json
from types import SimpleNamespace

import pytest
import gateway.session_protected_scope


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('fault', ['none', 'not-finished', 'cancelled', 'generation', 'epoch', 'admission', 'transcript', 'store', 'card', 'expired', 'board-fault', 'cancel-write', 'scratch-prose', 'stop-after-commit', 'board-stale', 'cancel-after-commit'])
def test_owner_success_is_observed_correlated_and_atomic(tmp_path, monkeypatch, fault):
    from agent.transports.types import NormalizedResponse
    from gateway.session_protected_process import protected_process_scope, SelectedAsyncProvider
    from gateway.session_protected_tools import SentinelLaunch
    from hermes_state_runtime import get_session_admission, RuntimeStoreError
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    from tests.gateway.test_protected_conversation import native_claim
    workspace = tmp_path
    final_response = 'CONTROLLED_SUCCESS'
    if fault == 'scratch-prose':
        workspace = tmp_path / 'workspaces' / 'card'
        workspace.mkdir(parents=True)
        monkeypatch.setenv('HERMES_KANBAN_WORKSPACES_ROOT', str(workspace.parent))
        host_file = workspace / 'host-only.txt'
        host_file.write_text('HOST_MUST_NOT_BE_ATTACHED')
        final_response = str(host_file)
    authority, context, admission = native_claim(tmp_path, monkeypatch, persisted=True, workspace=workspace)
    if fault == 'scratch-prose':
        with closing(connect(board='controlled')) as conn:
            conn.execute("UPDATE tasks SET workspace_kind='scratch' WHERE id=?", (context['task_id'],))
            assert kb._is_managed_scratch_path(workspace)
    class Transport:
        def build_kwargs(self, model, messages, tools):
            return dict(messages=messages, tools=tools)
        def normalize_response(self, response):
            return response
    async def create(**kwargs):
        return NormalizedResponse(final_response, None, 'stop')
    async def close():
        pass
    async def run():
        provider = SelectedAsyncProvider(model='controlled-no-provider', provider_id='custom',
            transport=Transport(), create=create, aclose=close)
        async with protected_process_scope(authority, SimpleNamespace(session_id=admission['target_session_id']), admission,
                SentinelLaunch(workspace.resolve()), principal_id='fixture-owner', context=context, provider=provider) as runner:
            if fault != 'not-finished':
                await runner.run_conversation('CONTROLLED_ONLY')
            if fault == 'cancelled':  # health: allow HX011 -- explicit adversarial fixture fault injection, not runtime routing
                runner.turn.proxy.worker.stop.set()
            elif fault == 'generation':
                authority.db._execute_write(lambda conn: conn.execute('UPDATE sessions SET runtime_generation=runtime_generation+1'))
            elif fault == 'epoch':
                authority.db._execute_write(lambda conn: conn.execute('UPDATE runtime_epoch SET epoch=epoch+1'))
            elif fault == 'admission':
                authority.db._execute_write(lambda conn: conn.execute("UPDATE session_admissions SET status='unknown'"))
            elif fault == 'transcript':
                authority.db._execute_write(lambda conn: conn.execute("UPDATE messages SET content='SUBSTITUTED' WHERE role='assistant'"))
            elif fault == 'expired':
                with closing(connect(board='controlled')) as conn:
                    conn.execute('UPDATE tasks SET claim_expires=1 WHERE id=?', (context['task_id'],))
            elif fault == 'card':
                with closing(connect(board='controlled')) as conn:
                    kb.block_task(conn, context['task_id'], reason='cancel', expected_run_id=context['run_id'])
            elif fault == 'store':
                # Failure after the receipt write, within settle_session_input's transaction.
                def refuse(*args, **kwargs):
                    raise OSError('controlled terminal write failure')
                from hermes_state_runtime import settle_steer_transfers
                monkeypatch.setattr('hermes_state_runtime.settle_steer_transfers', refuse)
            if fault in ('board-fault', 'board-stale'):
                actual_end = kb._end_run
                def fail_end(*args, **kwargs):
                    raise OSError('controlled card projection failure')
                monkeypatch.setattr(kb, '_end_run', fail_end)
                with pytest.raises(OSError, match='projection'):
                    await runner.settle_success()
                assert get_session_admission(authority.db, admission_id=admission['admission_id'])['outcome'] == 'completed'
                with closing(connect(board='controlled')) as conn:
                    assert kb.get_task(conn, context['task_id']).status == 'running'
                monkeypatch.setattr(kb, '_end_run', actual_end)
                if fault == 'board-stale':
                    with closing(connect(board='controlled')) as conn:
                        kb.block_task(conn, context['task_id'], reason='stale projection', expected_run_id=context['run_id'])
            if fault == 'stop-after-commit':
                actual_end = kb._end_run
                def stop_end(*args, **kwargs):
                    result = actual_end(*args, **kwargs)
                    runner.turn.proxy.worker.stop.set()
                    return result
                monkeypatch.setattr(kb, '_end_run', stop_end)
            if fault == 'cancel-after-commit':
                import threading
                entered, release = threading.Event(), threading.Event()
                actual_end = kb._end_run
                def delayed_end(*args, **kwargs):
                    result = actual_end(*args, **kwargs)
                    entered.set()
                    assert release.wait(5)
                    return result
                monkeypatch.setattr(kb, '_end_run', delayed_end)
                task = asyncio.create_task(runner.settle_success())
                await asyncio.to_thread(entered.wait, 5)
                assert entered.is_set()
                task.cancel()
                await asyncio.sleep(0)
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                retained = runner._settlement_task.result()
                assert retained['outcome'] == 'completed'
            if fault in ('none', 'board-fault', 'scratch-prose', 'stop-after-commit', 'cancel-after-commit'):
                receipt = await runner.settle_success()
                assert receipt['outcome'] == 'completed'
                assert receipt['card'] == {k: context[k] for k in ('board', 'task_id', 'run_id')}
                assert receipt['session_id'] == admission['target_session_id']
                assert receipt['generation'] == admission['generation']
                assert receipt['artifacts'] == {}
                assert await runner.settle_success() == receipt  # exact owner retry only
                with pytest.raises(RuntimeStoreError):
                    await runner.settle_success(('different.txt',))
                saved = get_session_admission(authority.db, admission_id=admission['admission_id'])
                assert (saved['status'], saved['outcome']) == ('terminal', 'completed')
                with authority.db._read_ctx() as conn:
                    record = json.loads(conn.execute('SELECT value FROM state_meta WHERE key=?',
                        ('protected.success.v1.' + admission['admission_id'],)).fetchone()[0])
                    assert record == receipt
                    assert conn.execute('SELECT status FROM worker_executions').fetchone()[0] == 'terminal'
                with closing(connect(board='controlled')) as conn:
                    assert kb.get_task(conn, context['task_id']).status == 'done'
                    row = conn.execute('SELECT metadata FROM task_runs WHERE id=?', (context['run_id'],)).fetchone()
                    assert json.loads(row[0])['protected_success_v1'] == receipt
                    if fault == 'scratch-prose':
                        assert conn.execute('SELECT COUNT(*) FROM task_attachments WHERE task_id=?', (context['task_id'],)).fetchone()[0] == 0
                        event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='completed'", (context['task_id'],)).fetchone()
                        assert 'artifacts' not in json.loads(event[0])
                        assert final_response not in json.loads(event[0])['summary']
            else:
                if fault == 'cancel-write':
                    import threading
                    import hermes_state_runtime as runtime
                    entered, release = threading.Event(), threading.Event()
                    actual_settle = runtime.settle_session_input
                    def delayed(*args, **kwargs):
                        entered.set()
                        assert release.wait(5)
                        return actual_settle(*args, **kwargs)
                    monkeypatch.setattr(runtime, 'settle_session_input', delayed)
                    task = asyncio.create_task(runner.settle_success())
                    await asyncio.to_thread(entered.wait, 5)
                    assert entered.is_set()
                    task.cancel()
                    await asyncio.sleep(0)
                    release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    assert runner._settlement_task.done()
                else:
                    with pytest.raises((OSError, RuntimeError, RuntimeStoreError)):
                        await runner.settle_success()
                with authority.db._read_ctx() as conn:
                    count = conn.execute("SELECT COUNT(*) FROM state_meta WHERE key LIKE 'protected.success.v1.%'").fetchone()[0]
                    assert count == (1 if fault == 'board-stale' else 0)
                saved = get_session_admission(authority.db, admission_id=admission['admission_id'])
                assert (saved['outcome'] == 'completed') == (fault == 'board-stale')
                if fault == 'store':
                    with closing(connect(board='controlled')) as conn:
                        assert kb.get_task(conn, context['task_id']).status == 'running'
                    monkeypatch.setattr('hermes_state_runtime.settle_steer_transfers', settle_steer_transfers)
        assert authority._managed_workers == {}
        assert not authority.adopted
        assert not provider._tasks and not provider._waiters
        assert all(not thread.is_alive() for thread in runner.threads)
        if fault in ('none', 'board-fault', 'scratch-prose', 'stop-after-commit', 'cancel-after-commit'):
            assert await runner.settle_success() == receipt  # durable confirmation after resource teardown
        else:
            with pytest.raises(RuntimeStoreError):
                await runner.settle_success()
        assert not getattr(authority, '_protected_retirements', {})
        assert not getattr(authority, '_protected_process_closes', {})
    try:
        asyncio.run(run())
    finally:
        authority.db.close()
