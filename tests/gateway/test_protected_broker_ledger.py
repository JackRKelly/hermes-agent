"""Exact merged broker callback shape over a controlled durable SQLite ledger."""
import time

import pytest

# Real fixture storage, not a mocked production response/authority.
from tests.gateway.test_protected_native_admission import DurableFixtureLedger


class BrokerShapeFixture:
    def __init__(self, path):
        self.ledger = DurableFixtureLedger(path)
    def snapshot(self):
        value = self.ledger.check({})
        return dict(mode='subscription_request_bound', request_count=value['requests'],
            elapsed_seconds=time.time() - value['started_at'], usd_guarantee=False)
    def reserve(self, binding):
        return self.ledger.reserve(binding)
    def reconcile(self, receipt, usage):
        return self.ledger.reconcile(receipt, usage)
    def freeze(self, receipt, reason):
        return self.ledger.freeze(receipt, reason)


def test_broker_snapshot_adapter_reserves_and_freezes_known_close_receipt(tmp_path):
    from gateway.session_protected_broker import BrokerRequestLedgerAdapter
    callbacks = BrokerShapeFixture(tmp_path / 'ledger.db')
    start = callbacks.ledger.check({})['started_at']
    adapter = BrokerRequestLedgerAdapter(callbacks, start)
    assert adapter.check({}) == dict(requests=0, started_at=start, frozen=False)
    receipt = adapter.reserve({'CONTROLLED': True})
    assert adapter.check({})['requests'] == 1
    adapter.reconcile(receipt, dict(model='controlled', input_tokens=3, output_tokens=2))
    # The merged broker accepts only real request receipts, not None. Core must
    # map a post-response close fault to the exact last charged receipt.
    adapter.freeze(None, 'controlled_close_fault')
    with pytest.raises(RuntimeError, match='frozen'):
        adapter.check({})


@pytest.mark.parametrize('value', [None, {}, {'mode': 'strict'},
    dict(mode='subscription_request_bound', request_count=13, elapsed_seconds=0, usd_guarantee=False),
    dict(mode='subscription_request_bound', request_count=True, elapsed_seconds=0, usd_guarantee=False),
    dict(mode='subscription_request_bound', request_count=0, elapsed_seconds=600, usd_guarantee=False),
    dict(mode='subscription_request_bound', request_count=0, elapsed_seconds=float('nan'), usd_guarantee=False)])
def test_unknown_broker_snapshots_deny(tmp_path, monkeypatch, value):
    from gateway.session_protected_broker import BrokerRequestLedgerAdapter
    callbacks = BrokerShapeFixture(tmp_path / 'ledger.db')
    monkeypatch.setattr(callbacks, 'snapshot', lambda: value)
    adapter = BrokerRequestLedgerAdapter(callbacks, time.time())
    with pytest.raises(ValueError, match='protected_owner_budget_unknown'):
        adapter.check({})


def test_installed_bridge_never_falls_back_to_ordinary_managed_execution():
    import asyncio
    from types import SimpleNamespace
    from gateway.session_managed_worker import execute_managed
    authority = SimpleNamespace(_protected_admission_bridge={'untrusted': True})
    with pytest.raises(ValueError, match='protected_owner_bridge_required'):
        asyncio.run(execute_managed(authority, SimpleNamespace(session_id='other-session'), {}, None))
