"""Side-effect-free synthetic Virtual Office; never an effect dispatcher."""

from .service import (
    StaleProjection,
    UnissuedProjection,
    VirtualOffice,
)
from .state import SyntheticOfficeState

__all__ = ["StaleProjection", "SyntheticOfficeState", "UnissuedProjection", "VirtualOffice"]
