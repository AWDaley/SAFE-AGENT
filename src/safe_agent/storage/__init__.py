"""Register additive trusted mappings for both Alembic and isolated create_all."""
from safe_agent.storage.coordinated_models import (
    CoordinatedPlanningConsumptionRow,
    CoordinatedPlanningInputRow,
)
from safe_agent.storage.result_models import InferenceValidatedResultRow

__all__ = ["CoordinatedPlanningConsumptionRow", "CoordinatedPlanningInputRow", "InferenceValidatedResultRow"]
