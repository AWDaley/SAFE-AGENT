"""Explicit trusted application construction; no model/operator mode selection."""
from typing import Literal

from safe_agent.inference.coordinated_uds import CoordinatedUDSInferenceTransport
from safe_agent.inference.coordinator import TrustedInferenceCoordinator
from safe_agent.storage.inference_admission import TrustedInferenceAdmission


def create_application_inference(
    *, mode: Literal["coordinated_offline"],
    admission: TrustedInferenceAdmission,
    transport: CoordinatedUDSInferenceTransport,
) -> TrustedInferenceCoordinator:
    """Construct only the admitted, attempt-scoped offline coordinator.

    The returned API requires execute(principal, planning_lease, prompt). There
    is deliberately no stateless generate(prompt) wrapper or fallback gateway.
    This factory does not wire the M6 operator routes; W3/W4 still must do so.
    """
    if (mode != "coordinated_offline"
            or type(admission) is not TrustedInferenceAdmission
            or type(transport) is not CoordinatedUDSInferenceTransport):
        raise TypeError("coordinated offline admission and transport required")
    return TrustedInferenceCoordinator(admission, transport)
