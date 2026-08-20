"""Independent, research-only factor discovery laboratory.

The package deliberately has no dependency on ``trading`` or broker-report
internals.  Its public surface is small enough for controlled adapters to feed
strict JSON evidence without exposing provider SDK objects to the research
domain.
"""

from .engine import (
    ARTIFACT_FILENAMES,
    EvidenceBundle,
    ExperimentRunner,
    FactorLabError,
    FactorObservation,
    FactorPlugin,
    FactorSpec,
    RelativeMomentumPlugin,
)

__all__ = [
    "ARTIFACT_FILENAMES",
    "EvidenceBundle",
    "ExperimentRunner",
    "FactorLabError",
    "FactorObservation",
    "FactorPlugin",
    "FactorSpec",
    "RelativeMomentumPlugin",
]
