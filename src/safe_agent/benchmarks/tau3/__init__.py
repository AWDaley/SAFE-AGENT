"""The pinned τ³ text integration; importing it performs no model or tool calls."""

from typing import Final, Literal

TAU_REPOSITORY = "sierra-research/tau2-bench"
TAU_RELEASE = "v1.0.1"
TAU_COMMIT: Final[Literal["fc0055dc4e0a316c3f83133267fbd6faaa770992"]] = (
    "fc0055dc4e0a316c3f83133267fbd6faaa770992"
)
EXPECTED_TASK_COUNTS = {"retail": 114, "banking_knowledge": 97}
