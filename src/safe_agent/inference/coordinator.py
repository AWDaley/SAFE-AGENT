"""Opt-in offline coordinator: admit -> claim -> committed start -> one UDS call.

Only this configured path has the dispatch guarantee. The legacy in-process
protocol fixtures are not production entrypoints. No model tools are exposed.
"""
from __future__ import annotations

from safe_agent.contracts.inference_coordinator import InferenceDispatchReceipt
from safe_agent.contracts.planning import PlanningLease
from safe_agent.inference.coordinated_uds import CoordinatedUDSInferenceTransport
from safe_agent.security.principal import Principal
from safe_agent.storage.inference_admission import TrustedInferenceAdmission
from safe_agent.storage.inference_dispatch import (
    InferenceDispatchDenied,
    TrustedInferenceDispatchStore,
)


class TrustedInferenceCoordinator:
    def __init__(self, admission: TrustedInferenceAdmission,
                 transport: CoordinatedUDSInferenceTransport) -> None:
        if type(transport) is not CoordinatedUDSInferenceTransport:
            raise TypeError("exact offline coordinated UDS transport required")
        self.store = TrustedInferenceDispatchStore(admission)
        self._transport = transport

    def execute(self, principal: Principal, lease: PlanningLease, prompt: str) -> InferenceDispatchReceipt:
        try:
            self.store.admission.admit(principal, lease, prompt)
            view = self.store.recover(principal, lease, prompt)
            if view.state != "RESERVED":
                return InferenceDispatchReceipt(invocation=view)
            claim = self.store.claim(principal, lease, prompt)
            if claim is None:
                return InferenceDispatchReceipt(invocation=self.store.recover(principal, lease, prompt))
            # No network call unless this transaction's commit returns normally.
            request, marker = self.store.start(principal, lease, prompt, claim)
            got_reply = False
            try:
                reply = self._transport.send(request, claim, marker)
                got_reply = True
                view, outcome = self.store.finish(principal, lease, prompt, claim, reply)
            except Exception:
                # A lost finish-commit response may already have committed success.
                # Inspect first, never rewrite a terminal result or resend.
                view = self.store.recover(principal, lease, prompt)
                if view.state == "DISPATCH_STARTED":
                    view = self.store.unknown(principal, lease, prompt, claim, rejected=got_reply)
                return InferenceDispatchReceipt(invocation=view, dispatched_this_call=True)
            return InferenceDispatchReceipt(invocation=view, outcome=outcome, dispatched_this_call=True)
        except Exception:
            raise InferenceDispatchDenied() from None
