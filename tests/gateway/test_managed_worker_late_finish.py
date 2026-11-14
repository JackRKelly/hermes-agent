"""A worker whose result the owner accepted waits for the owner's ``finish`` as long as the owner lives.

Live incident (Nov 13, run 5301): the owner stalled ~75 s after the worker sent its result; the worker
gave up after a fixed 30 s (``managed_finish_timeout``) and exited, so the owner's late ``finish`` met a
closed pipe and a completed, accepted turn settled ``unknown``. The owner's word still decides: the
durable ``execution.finish`` follows only its ``finish`` (or the pipe's end).
"""
from types import SimpleNamespace

from tests.gateway.test_managed_turn_contract import assignment


class _OwnerClock:
    """``controls.finish`` with a simulated clock: the owner sends ``finish`` ``late_s`` seconds after the
    result. Each bounded wait that ends first advances the clock by its timeout and reports no finish."""
    def __init__(self, late_s):
        self.late_s, self.elapsed, self.waits = late_s, 0.0, []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if timeout is not None and self.elapsed + timeout < self.late_s:
            self.elapsed += timeout
            return False
        self.elapsed = self.late_s
        return True

    def set(self):
        self.elapsed = self.late_s


def _execute(tmp_path, monkeypatch, events, finish):
    import threading
    from agent import managed_worker as worker

    class RPC:
        def __init__(self, home): pass
        def __call__(self, method, **params): return {'owner_epoch': 1}
        def close(self): events.append('rpc.close')

    class Agent:
        def __init__(self, **kwargs):
            self.session_prompt_tokens = self.session_completion_tokens = 0
        def run_conversation(self, text, **kwargs):
            return {'final_response': 'done', 'completed': True}

    class Store:
        def __init__(self, *args): pass
        def get_messages_as_conversation(self, sid): return []
        def flush_token_counts(self): pass
        def finish(self): events.append('store.finish')
        def close(self): events.append('store.close')

    class Controls:
        def __init__(self, *args):
            self.stopped, self.finish = threading.Event(), finish
        def approval(self, data): pass
        def clarify(self, questions): pass

    frame, _ = assignment(tmp_path, monkeypatch)
    monkeypatch.setattr(worker, 'bind_worker_policy', lambda frame: None)
    monkeypatch.setattr(worker, 'discover_profile_mcp', lambda policy: None)
    monkeypatch.setattr(worker, 'retire_agent', lambda agent: None)
    monkeypatch.setattr(worker, 'WorkerControls', Controls)
    monkeypatch.setattr('agent.runtime_session_store.WorkerRPC', RPC)
    monkeypatch.setattr('agent.runtime_session_store.RuntimeSessionStore', Store)
    monkeypatch.setattr('tools.process_registry.process_registry.recover_from_checkpoint', lambda: None)
    monkeypatch.setattr('run_agent.AIAgent', Agent)
    monkeypatch.setattr('agent.title_generator.wait_for_title_upgrades', lambda: None)
    worker.execute(frame, SimpleNamespace(send=lambda kind, **payload: events.append(kind)))


def test_a_finish_seventy_seconds_after_the_result_completes_the_turn(tmp_path, monkeypatch, caplog):
    events, finish = [], _OwnerClock(late_s=70)
    _execute(tmp_path, monkeypatch, events, finish)
    # The turn completes on the owner's late word: durable finish, then 'finished', never an error frame.
    assert events[-5:] == ['result', 'store.finish', 'finished', 'store.close', 'rpc.close']
    assert 'error' not in events
    assert finish.elapsed == 70
    assert 'managed_finish_late' in caplog.text
