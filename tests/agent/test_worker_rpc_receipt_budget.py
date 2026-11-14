"""A worker's persistence call survives an owner stall shorter than its receipt budget, and still fails
within that budget when the owner never answers.

Live incident (Nov 13-14): an owner-wide stall held every worker's ``worker.persist`` reply past the old
fixed 20 s receipt wait; each worker failed sticky (``worker_receipt_timeout``) and its turn was lost
as ``unknown``, though the owner had committed the write and sent the reply a moment later.
The owner here is a real ``SessionDB`` applying real worker receipts, so "applied once" is the ledger's
own count, not a mock's.
"""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from tests.hermes_state.test_runtime_worker import setup_worker


@pytest.fixture
def owner(monkeypatch, tmp_path):
    from websockets.sync.server import serve
    from hermes_cli import gateway_client, gateway_runtime
    import hermes_cli.gateway_runtime_discovery as discovery
    import hermes_state_runtime as rt

    db, epoch, assignment = setup_worker(tmp_path)
    state = SimpleNamespace(db=db, epoch=epoch, assignment=assignment, frames=[], hold_s=0.0,
                            released=threading.Event())

    def handler(ws):
        for raw in ws:
            request = json.loads(raw)
            state.frames.append((request['id'], request['params'].get('sequence')))
            result = rt.mutate_worker_execution(db, **request['params'])
            # The commit has landed; a stalled owner loop sends its reply only later.
            state.released.wait(state.hold_s)
            ws.send(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}))

    with serve(handler, '127.0.0.1', 0, subprotocols=['hermes-gateway-v1']) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        endpoint = SimpleNamespace(api_origin=f'http://127.0.0.1:{server.socket.getsockname()[1]}', control_home=None)
        monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint',
                            lambda home, timeout: SimpleNamespace(state='ready', endpoint=endpoint))
        monkeypatch.setattr(discovery, 'query_identify', lambda home, timeout: {'pid': -1})
        monkeypatch.setattr(gateway_client, '_session_ticket', lambda home, endpoint, purpose: 'ticket')
        try:
            yield state
        finally:
            state.released.set()
            server.shutdown()
            db.close()


def _store(owner, tmp_path):
    from agent.runtime_session_store import RuntimeSessionStore, WorkerRPC
    rpc = WorkerRPC(tmp_path)
    scope = dict(owner.assignment, epoch=owner.epoch)
    return rpc, RuntimeSessionStore(rpc, scope, tmp_path / 'outbox')


def _receipts(db):
    with db._read_ctx() as conn:
        return [tuple(row) for row in conn.execute(
            'SELECT execution_id, sequence FROM worker_receipts ORDER BY sequence').fetchall()]


def test_a_reply_later_than_twenty_seconds_lands_once_and_the_turn_keeps_going(owner, tmp_path, caplog):
    """22 s: past the old fixed 20 s wait, inside the budget. The call waits for the same reply (one frame,
    never a resend), the store stays healthy, and the next write goes straight through."""
    rpc, store = _store(owner, tmp_path)
    try:
        owner.hold_s = 22.0
        started = time.monotonic()
        assert store.append_messages_batch('s', [{'role': 'assistant', 'content': 'late'}]) == 1
        assert time.monotonic() - started >= 21
        assert store.failure is None and store.journal['pending'] == []
        assert 'worker_receipt_late' in caplog.text
        owner.hold_s = 0.0
        assert store.append_messages_batch('s', [{'role': 'assistant', 'content': 'next'}]) == 1
        store.flush_token_counts()
        assert [frame[1] for frame in owner.frames] == [1, 2]
        assert len({frame[0] for frame in owner.frames}) == 2
        assert _receipts(owner.db) == [('worker', 1), ('worker', 2)]
        assert [m['content'] for m in owner.db.get_messages('s')] == ['late', 'next']
    finally:
        store.close()
        rpc.close()


def test_an_owner_that_never_answers_fails_within_the_budget_and_stays_failed(owner, tmp_path, monkeypatch):
    from agent import runtime_session_store
    from agent.runtime_session_store import WorkerPersistenceError
    monkeypatch.setattr(runtime_session_store, 'RECEIPT_LATE_S', 0.5, raising=False)
    monkeypatch.setattr(runtime_session_store, 'RECEIPT_BUDGET_S', 3.0, raising=False)
    rpc, store = _store(owner, tmp_path)
    try:
        owner.hold_s = 600.0
        started = time.monotonic()
        with pytest.raises(TimeoutError, match='worker_receipt_timeout'):
            store.append_messages_batch('s', [{'role': 'assistant', 'content': 'unanswered'}])
        assert 2.5 <= time.monotonic() - started < 10
        assert rpc.link is None  # the outcome is unknown: the connection is dropped, nothing resent
        assert owner.frames == [(1, 1)]
        with pytest.raises(WorkerPersistenceError, match='worker_receipt_timeout'):
            store.flush_token_counts()
    finally:
        owner.released.set()
        with pytest.raises(WorkerPersistenceError):
            store.close()
        rpc.close()
