"""Owner-only adapter for the existing signed broker's request ledger callbacks.

No second store/control plane. Every observation comes from the broker's actual
snapshot/reserve/reconcile/freeze transactions. Never construct from guest JSON.
"""
import math

from hermes_state_runtime import RuntimeStoreError


class BrokerRequestLedgerAdapter:
    def __init__(self, callbacks, controller_started_at):
        self.callbacks, self.controller_started_at = callbacks, controller_started_at
        self.last_receipt = None
        if not all(callable(getattr(callbacks, name, None)) for name in ('snapshot', 'reserve', 'reconcile', 'freeze')):
            raise RuntimeStoreError('protected_request_ledger_required')

    def check(self, binding):
        value = self.callbacks.snapshot()
        elapsed = value.get('elapsed_seconds') if type(value) is dict else None
        if (type(value) is not dict or value.get('mode') != 'subscription_request_bound'
                or value.get('usd_guarantee') is not False or type(value.get('request_count')) is not int
                or not 0 <= value['request_count'] <= 12 or type(elapsed) not in (int, float)
                or not math.isfinite(elapsed) or not 0 <= elapsed < 600):
            raise RuntimeStoreError('protected_owner_budget_unknown_or_exhausted')
        # Successful snapshot already verifies active/non-suspended signed graph
        # in PilotRequestLedger. Core ALSO fences the signed original start time,
        # independently of the broker's earliest-controller/provider elapsed clock.
        return dict(requests=value['request_count'], started_at=self.controller_started_at, frozen=False)

    def reserve(self, binding):
        receipt = self.callbacks.reserve(binding)
        self.last_receipt = receipt
        return receipt

    def reconcile(self, receipt, usage):
        return self.callbacks.reconcile(receipt, usage)

    def freeze(self, receipt, reason):
        target = receipt if receipt is not None else self.last_receipt
        if target is None:
            # No known receipt can be invented after an ambiguous transaction.
            # The broker's reserve transaction must already suspend its own
            # ambiguous failure; native admission stays single-use/unknown.
            raise RuntimeStoreError('protected_reservation_recovery_required')
        return self.callbacks.freeze(target, reason)
