"""Leakage-resistant primitives for one cross-section at a time.

These functions intentionally accept a single vector/cross-section rather than
an entire date panel.  The caller must group by the decision-time cross-section
before invoking them, which makes accidental pooled future-aware transforms
harder to introduce.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np
from numpy.typing import ArrayLike, NDArray

from .regression import (
    DEFAULT_CONDITION_NUMBER_LIMIT,
    RegressionError,
    RegressionResult,
    fit_ols,
)


class PreprocessingError(ValueError):
    """Raised when a cross-sectional transform is not statistically defined."""


def _readonly_float_array(value: ArrayLike) -> NDArray[np.float64]:
    array = np.asarray(value, dtype=np.float64).copy()
    array.setflags(write=False)
    return array


def _finite_vector(values: ArrayLike, *, name: str) -> NDArray[np.float64]:
    vector = np.asarray(values, dtype=np.float64)
    if vector.ndim != 1:
        raise PreprocessingError(f"{name}_must_be_one_dimensional")
    if vector.size == 0:
        raise PreprocessingError(f"{name}_must_not_be_empty")
    if not np.all(np.isfinite(vector)):
        raise PreprocessingError(f"{name}_must_be_finite")
    return vector


def _exposure_matrix(exposures: ArrayLike, *, n_rows: int) -> NDArray[np.float64]:
    matrix = np.asarray(exposures, dtype=np.float64)
    if matrix.ndim == 1:
        matrix = matrix.reshape(-1, 1)
    if matrix.ndim != 2:
        raise PreprocessingError("exposures_must_be_one_or_two_dimensional")
    if matrix.shape[0] != n_rows:
        raise PreprocessingError(
            "exposure_observation_count_mismatch: "
            f"expected {n_rows}, got {matrix.shape[0]}"
        )
    if matrix.shape[1] == 0:
        raise PreprocessingError("at_least_one_exposure_is_required")
    if not np.all(np.isfinite(matrix)):
        raise PreprocessingError("exposures_must_be_finite")
    return matrix


def winsorize_cross_section(
    values: ArrayLike,
    *,
    lower_quantile: float = 0.01,
    upper_quantile: float = 0.99,
) -> NDArray[np.float64]:
    """Clip one complete cross-section to empirical quantile bounds."""

    vector = _finite_vector(values, name="values")
    if (
        not np.isfinite(lower_quantile)
        or not np.isfinite(upper_quantile)
        or lower_quantile < 0.0
        or upper_quantile > 1.0
        or lower_quantile >= upper_quantile
    ):
        raise PreprocessingError(
            "quantiles_must_satisfy_0_le_lower_lt_upper_le_1"
        )
    lower, upper = np.quantile(
        vector, [lower_quantile, upper_quantile], method="linear"
    )
    return _readonly_float_array(np.clip(vector, lower, upper))


def zscore_cross_section(
    values: ArrayLike,
    *,
    ddof: int = 0,
) -> NDArray[np.float64]:
    """Mean-center and scale one cross-section, rejecting zero variance."""

    vector = _finite_vector(values, name="values")
    if isinstance(ddof, bool) or not isinstance(ddof, (int, np.integer)):
        raise PreprocessingError("ddof_must_be_an_integer")
    resolved_ddof = int(ddof)
    if resolved_ddof < 0 or resolved_ddof >= vector.size:
        raise PreprocessingError(
            f"ddof_out_of_range: ddof={resolved_ddof}, observations={vector.size}"
        )
    scale = float(np.std(vector, ddof=resolved_ddof))
    numerical_floor = float(
        np.finfo(np.float64).eps * max(1.0, float(np.max(np.abs(vector))))
    )
    if not np.isfinite(scale) or scale <= numerical_floor:
        raise PreprocessingError("zero_variance_cross_section")
    standardized = (vector - float(np.mean(vector))) / scale
    return _readonly_float_array(standardized)


def rank_cross_section(
    values: ArrayLike,
    *,
    ascending: bool = True,
    percentile: bool = False,
) -> NDArray[np.float64]:
    """Return stable average ranks, assigning tied values their mean rank.

    Raw ranks are one-based.  Percentile ranks map the lowest and highest ranks
    to 0 and 1; a singleton cross-section receives the neutral value 0.5.
    """

    vector = _finite_vector(values, name="values")
    if not isinstance(ascending, (bool, np.bool_)):
        raise PreprocessingError("ascending_must_be_boolean")
    if not isinstance(percentile, (bool, np.bool_)):
        raise PreprocessingError("percentile_must_be_boolean")
    sort_values = vector if ascending else -vector
    order = np.argsort(sort_values, kind="stable")
    ranks = np.empty(vector.size, dtype=np.float64)
    start = 0
    while start < vector.size:
        end = start + 1
        ordered_value = sort_values[order[start]]
        while end < vector.size and sort_values[order[end]] == ordered_value:
            end += 1
        average_rank = ((start + 1) + end) / 2.0
        ranks[order[start:end]] = average_rank
        start = end
    if percentile:
        if vector.size == 1:
            ranks[0] = 0.5
        else:
            ranks = (ranks - 1.0) / (vector.size - 1.0)
    return _readonly_float_array(ranks)


@dataclass(frozen=True, slots=True)
class ResidualizationResult:
    """Values after an exposure regression within one cross-section."""

    exposure_names: tuple[str, ...]
    residuals: NDArray[np.float64]
    fitted_values: NDArray[np.float64]
    regression: RegressionResult

    def __post_init__(self) -> None:
        object.__setattr__(self, "exposure_names", tuple(self.exposure_names))
        object.__setattr__(self, "residuals", _readonly_float_array(self.residuals))
        object.__setattr__(self, "fitted_values", _readonly_float_array(self.fitted_values))

    @property
    def coefficients(self) -> NDArray[np.float64]:
        return self.regression.coefficients

    @property
    def intercept(self) -> float:
        return self.regression.intercept

    def to_dict(self) -> dict[str, Any]:
        return {
            "exposure_names": list(self.exposure_names),
            "residuals": self.residuals.tolist(),
            "fitted_values": self.fitted_values.tolist(),
            "regression": self.regression.to_dict(),
        }


def residualize_cross_section(
    values: ArrayLike,
    exposures: ArrayLike,
    *,
    exposure_names: Sequence[str] | None = None,
    sample_weight: ArrayLike | None = None,
    weights: ArrayLike | None = None,
    fit_intercept: bool = True,
    condition_number_limit: float | None = DEFAULT_CONDITION_NUMBER_LIMIT,
    rcond: float | None = None,
) -> ResidualizationResult:
    """Remove linear exposure effects from a single cross-section.

    With ``sample_weight`` the returned residuals satisfy the weighted normal
    equations in sample.  ``weights`` is a compatibility alias; callers must
    not pass both.  Full dummy sets plus an intercept are intentionally rejected
    as rank deficient instead of silently dropping a column.
    """

    vector = _finite_vector(values, name="values")
    matrix = _exposure_matrix(exposures, n_rows=vector.size)
    if sample_weight is not None and weights is not None:
        raise PreprocessingError("pass_only_one_of_sample_weight_or_weights")
    resolved_weight = sample_weight if sample_weight is not None else weights

    if exposure_names is None:
        names = tuple(f"exposure_{index}" for index in range(matrix.shape[1]))
    else:
        names = tuple(str(name) for name in exposure_names)
        if len(names) != matrix.shape[1]:
            raise PreprocessingError("exposure_names_must_match_exposure_columns")
        if any(not name for name in names) or len(set(names)) != len(names):
            raise PreprocessingError("exposure_names_must_be_non_empty_and_unique")
    try:
        regression = fit_ols(
            matrix,
            vector,
            fit_intercept=fit_intercept,
            sample_weight=resolved_weight,
            condition_number_limit=condition_number_limit,
            rcond=rcond,
        )
    except RegressionError as exc:
        raise PreprocessingError(f"residualization_failed: {exc}") from exc
    return ResidualizationResult(
        exposure_names=names,
        residuals=regression.residuals,
        fitted_values=regression.fitted_values,
        regression=regression,
    )


__all__ = [
    "PreprocessingError",
    "ResidualizationResult",
    "rank_cross_section",
    "residualize_cross_section",
    "winsorize_cross_section",
    "zscore_cross_section",
]
