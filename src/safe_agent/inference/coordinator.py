"""Offline coordinator: durable admission, one send, atomic result and recovery.

Only this configured path has the dispatch guarantee. Models never receive
credentials, SQL access, an execution permit or business-system tools.
"""
from __future__ import annotations

from safe_agent.contracts.inference_coordinator import InferenceDispatchReceipt
from safe_agent.contracts.inference_results import InferenceResultRecovery
from safe_agent.contracts.planning import PlanningLease
from safe_agent.inference.coordinated_uds import CoordinatedUDSInferenceTransport
from safe_agent.security.principal import Principal
from safe_agent.storage.inference_admission import TrustedInferenceAdmission
from safe_agent.storage.inference_dispatch import (
    InferenceDispatchDenied,
    TrustedInferenceDispatchStore,
)
from safe_agent.storage.inference_results import recover_validated_result


class TrustedInferenceCoordinator:
    def __init__(self, admission: TrustedInferenceAdmission,
                 transport: CoordinatedUDSInferenceTransport) -> None:
        if type(transport) is not CoordinatedUDSInferenceTransport:
            raise TypeError("exact offline coordinated UDS transport required")
        self.store = TrustedInferenceDispatchStore(admission)
        self._transport = transport

    def resume(self, principal: Principal, lease: PlanningLease) -> InferenceResultRecovery:
        """Load the original result/snapshot without a prompt or backend request.

        STALE and LEGACY_MISSING never return a candidate or permit redispatch.
        NOT_VALIDATED is metadata only; normal dispatch recovery owns timeouts.
        Consumers must independently revalidate source/broker/task authority.
        """
        return recover_validated_result(self.store.admission, principal, lease)

    def _existing(self, principal: Principal, lease: PlanningLease, prompt: str,
                  *, dispatched: bool = False) -> InferenceDispatchReceipt:
        recovered = recover_validated_result(
            self.store.admission, principal, lease, expected_prompt=prompt,
        )
        return InferenceDispatchReceipt(invocation=recovered.invocation,
                                        outcome=recovered.outcome,
                                        dispatched_this_call=dispatched)

    def execute(self, principal: Principal, lease: PlanningLease, prompt: str) -> InferenceDispatchReceipt:
        try:
            self.store.admission.admit(principal, lease, prompt)
            view = self.store.recover(principal, lease, prompt)
            if view.state != "RESERVED":
                return self._existing(principal, lease, prompt)
            claim = self.store.claim(principal, lease, prompt)
            if claim is None:
                self.store.recover(principal, lease, prompt)
                return self._existing(principal, lease, prompt)
            # No network call unless this transaction's commit returns normally.
            request, marker = self.store.start(principal, lease, prompt, claim)
            got_reply = False
            try:
                reply = self._transport.send(request, claim, marker)
                got_reply = True
                view, outcome = self.store.finish(principal, lease, prompt, claim, reply)
            except Exception:
                # A lost finish-commit response may already have committed both
                # result and validation. Inspect first; never rewrite or resend.
                view = self.store.recover(principal, lease, prompt)
                if view.state == "DISPATCH_STARTED":
                    self.store.unknown(principal, lease, prompt, claim, rejected=got_reply)
                return self._existing(principal, lease, prompt, dispatched=True)
            return InferenceDispatchReceipt(invocation=view, outcome=outcome, dispatched_this_call=True)
        except Exception:
            raise InferenceDispatchDenied() from None
