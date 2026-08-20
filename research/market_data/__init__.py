"""Provider-neutral, read-only market-data boundary.

The package deliberately separates callable providers from locally admitted
datasets.  Importing it never imports an optional vendor SDK.
"""

from .admission import AdmissionDecision, evaluate_admission
from .contracts import (
    DATASET_SCHEMA_VERSIONS,
    MarketDataBatch,
    MarketDataContractError,
    MarketDataRequest,
    canonical_json_bytes,
    sha256_bytes,
)
from .registry import MarketDataRegistry
from .storage import MarketDataStorage

__all__ = [
    "AdmissionDecision",
    "DATASET_SCHEMA_VERSIONS",
    "MarketDataBatch",
    "MarketDataContractError",
    "MarketDataRegistry",
    "MarketDataRequest",
    "MarketDataStorage",
    "canonical_json_bytes",
    "evaluate_admission",
    "sha256_bytes",
]
