"""Small, deterministic regression primitives for the strategy workspace.

The functions in this module deliberately expose diagnostics that are commonly
hidden by higher-level modelling libraries.  They accept only complete, finite
NumPy-compatible arrays.  Callers must perform any point-in-time filtering and
missing-value policy before fitting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from math import floor
from typing import Any, Mapping, Sequence

import numpy as np
from numpy.typing import ArrayLike, NDArray


DEFAULT_CONDITION_NUMBER_LIMIT = 1.0e12


class RegressionError(ValueError):
    """Raised when a regression cannot be estimated without hiding a failure."""


def _readonly_float_array(value: ArrayLike) -> NDArray[np.float64]:
    array = np.asarray(value, dtype=np.float64).copy()
    array.setflags(write=False)
    return array


def _json_safe(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return [_json_safe(item) for item in value.tolist()]
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


@dataclass(frozen=True, slots=True)
class RegressionDiagnostics:
    """Auditable fit statistics for one regression."""

    n_observations: int
    n_features: int
    degrees_of_freedom: int
    rss: float
    weighted_rss: float
    mse: float | None
    solver: str
    alpha: float
    fit_intercept: bool
    weighted: bool
    singular_value_tolerance: float
    regularized_condition_number: float | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(
            {
                "n_observations": self.n_observations,
                "n_features": self.n_features,
                "degrees_of_freedom": self.degrees_of_freedom,
                "rss": self.rss,
                "weighted_rss": self.weighted_rss,
                "mse": self.mse,
                "solver": self.solver,
                "alpha": self.alpha,
                "fit_intercept": self.fit_intercept,
                "weighted": self.weighted,
                "singular_value_tolerance": self.singular_value_tolerance,
                "regularized_condition_number": self.regularized_condition_number,
                "warnings": self.warnings,
            }
        )


@dataclass(frozen=True, slots=True)
class RegressionResult:
    """Result of an OLS or Ridge fit.

    ``condition_number`` and ``rank`` always describe the unregularized design
    matrix (including the intercept column when requested).  This keeps Ridge
    from concealing weak identification in the source exposures.
    """

    coefficients: NDArray[np.float64]
    intercept: float
    rank: int
    condition_number: float
    residuals: NDArray[np.float64]
    fitted_values: NDArray[np.float64]
    diagnostics: RegressionDiagnostics

    def __post_init__(self) -> None:
        object.__setattr__(self, "coefficients", _readonly_float_array(self.coefficients))
        object.__setattr__(self, "residuals", _readonly_float_array(self.residuals))
        object.__setattr__(self, "fitted_values", _readonly_float_array(self.fitted_values))

    def predict(self, exposures: ArrayLike) -> NDArray[np.float64]:
        matrix = _as_feature_matrix(exposures, name="exposures")
        if matrix.shape[1] != self.coefficients.shape[0]:
            raise RegressionError(
                "feature_count_mismatch: "
                f"expected {self.coefficients.shape[0]}, got {matrix.shape[1]}"
            )
        if not np.all(np.isfinite(matrix)):
            raise RegressionError("exposures_must_be_finite")
        return matrix @ self.coefficients + self.intercept

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(
            {
                "coefficients": self.coefficients,
                "intercept": self.intercept,
                "rank": self.rank,
                "condition_number": self.condition_number,
                "residuals": self.residuals,
                "fitted_values": self.fitted_values,
                "diagnostics": self.diagnostics.to_dict(),
            }
        )


@dataclass(frozen=True, slots=True)
class FamaMacBethResult:
    """Average cross-sectional coefficients with Newey-West uncertainty."""

    feature_names: tuple[str, ...]
    period_labels: tuple[Any, ...]
    coefficients: NDArray[np.float64]
    intercept: float
    standard_errors: NDArray[np.float64]
    intercept_standard_error: float | None
    t_statistics: NDArray[np.float64]
    intercept_t_statistic: float | None
    covariance: NDArray[np.float64]
    period_coefficients: NDArray[np.float64]
    period_intercepts: NDArray[np.float64]
    hac_lags: int
    diagnostics: Mapping[str, Any]

    def __post_init__(self) -> None:
        for name in (
            "coefficients",
            "standard_errors",
            "t_statistics",
            "covariance",
            "period_coefficients",
            "period_intercepts",
        ):
            object.__setattr__(self, name, _readonly_float_array(getattr(self, name)))
        object.__setattr__(self, "feature_names", tuple(self.feature_names))
        object.__setattr__(self, "period_labels", tuple(self.period_labels))
        object.__setattr__(self, "diagnostics", dict(self.diagnostics))

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(
            {
                "feature_names": self.feature_names,
                "period_labels": self.period_labels,
                "coefficients": self.coefficients,
                "intercept": self.intercept,
                "standard_errors": self.standard_errors,
                "intercept_standard_error": self.intercept_standard_error,
                "t_statistics": self.t_statistics,
                "intercept_t_statistic": self.intercept_t_statistic,
                "covariance": self.covariance,
                "period_coefficients": self.period_coefficients,
                "period_intercepts": self.period_intercepts,
                "hac_lags": self.hac_lags,
                "diagnostics": self.diagnostics,
            }
        )


def _as_feature_matrix(exposures: ArrayLike, *, name: str) -> NDArray[np.float64]:
    matrix = np.asarray(exposures, dtype=np.float64)
    if matrix.ndim == 1:
        matrix = matrix.reshape(-1, 1)
    if matrix.ndim != 2:
        raise RegressionError(f"{name}_must_be_one_or_two_dimensional")
    return matrix


def _validated_inputs(
    exposures: ArrayLike,
    outcomes: ArrayLike,
    *,
    sample_weight: ArrayLike | None,
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64] | None]:
    matrix = _as_feature_matrix(exposures, name="exposures")
    target = np.asarray(outcomes, dtype=np.float64)
    if target.ndim != 1:
        raise RegressionError("outcomes_must_be_one_dimensional")
    if matrix.shape[0] != target.shape[0]:
        raise RegressionError(
            "observation_count_mismatch: "
            f"exposures={matrix.shape[0]}, outcomes={target.shape[0]}"
        )
    if target.shape[0] == 0:
        raise RegressionError("at_least_one_observation_is_required")
    if not np.all(np.isfinite(matrix)) or not np.all(np.isfinite(target)):
        raise RegressionError("regression_inputs_must_be_finite")

    weights: NDArray[np.float64] | None = None
    if sample_weight is not None:
        weights = np.asarray(sample_weight, dtype=np.float64)
        if weights.ndim != 1 or weights.shape[0] != target.shape[0]:
            raise RegressionError("sample_weight_must_match_observations")
        if not np.all(np.isfinite(weights)) or np.any(weights <= 0.0):
            raise RegressionError("sample_weight_must_be_finite_and_positive")
    return matrix, target, weights


def _design_matrix(
    matrix: NDArray[np.float64], *, fit_intercept: bool
) -> NDArray[np.float64]:
    if not fit_intercept:
        return matrix
    return np.column_stack((np.ones(matrix.shape[0], dtype=np.float64), matrix))


def _weighted_design(
    design: NDArray[np.float64],
    target: NDArray[np.float64],
    weights: NDArray[np.float64] | None,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    if weights is None:
        return design, target
    root_weight = np.sqrt(weights)
    return design * root_weight[:, None], target * root_weight


def _rank_and_condition(
    design: NDArray[np.float64], *, rcond: float | None
) -> tuple[int, float, float]:
    singular_values = np.linalg.svd(design, compute_uv=False)
    if singular_values.size == 0:
        raise RegressionError("design_must_have_at_least_one_parameter")
    if rcond is not None:
        if not np.isfinite(rcond) or rcond < 0.0:
            raise RegressionError("rcond_must_be_non_negative_and_finite")
        tolerance = float(rcond * singular_values[0])
    else:
        tolerance = float(
            np.finfo(np.float64).eps * max(design.shape) * singular_values[0]
        )
    rank = int(np.count_nonzero(singular_values > tolerance))
    smallest = float(singular_values[-1])
    condition_number = (
        float(singular_values[0] / smallest) if smallest > 0.0 else float("inf")
    )
    return rank, condition_number, tolerance


def _validate_condition_limit(condition_number_limit: float | None) -> None:
    if condition_number_limit is None:
        return
    if not np.isfinite(condition_number_limit) or condition_number_limit <= 1.0:
        raise RegressionError("condition_number_limit_must_be_finite_and_greater_than_one")


def _build_result(
    *,
    parameters: NDArray[np.float64],
    matrix: NDArray[np.float64],
    target: NDArray[np.float64],
    weights: NDArray[np.float64] | None,
    fit_intercept: bool,
    rank: int,
    condition_number: float,
    tolerance: float,
    alpha: float,
    solver: str,
    regularized_condition_number: float | None = None,
    warnings: tuple[str, ...] = (),
) -> RegressionResult:
    if fit_intercept:
        intercept = float(parameters[0])
        coefficients = parameters[1:]
    else:
        intercept = 0.0
        coefficients = parameters
    fitted = matrix @ coefficients + intercept
    residuals = target - fitted
    rss = float(residuals @ residuals)
    weighted_rss = float(residuals @ residuals) if weights is None else float(
        residuals @ (weights * residuals)
    )
    degrees_of_freedom = int(target.shape[0] - rank)
    mse = weighted_rss / degrees_of_freedom if degrees_of_freedom > 0 else None
    diagnostics = RegressionDiagnostics(
        n_observations=int(target.shape[0]),
        n_features=int(matrix.shape[1]),
        degrees_of_freedom=degrees_of_freedom,
        rss=rss,
        weighted_rss=weighted_rss,
        mse=mse,
        solver=solver,
        alpha=alpha,
        fit_intercept=fit_intercept,
        weighted=weights is not None,
        singular_value_tolerance=tolerance,
        regularized_condition_number=regularized_condition_number,
        warnings=warnings,
    )
    return RegressionResult(
        coefficients=coefficients,
        intercept=intercept,
        rank=rank,
        condition_number=condition_number,
        residuals=residuals,
        fitted_values=fitted,
        diagnostics=diagnostics,
    )


def fit_ols(
    exposures: ArrayLike,
    outcomes: ArrayLike,
    *,
    fit_intercept: bool = True,
    sample_weight: ArrayLike | None = None,
    condition_number_limit: float | None = DEFAULT_CONDITION_NUMBER_LIMIT,
    rcond: float | None = None,
) -> RegressionResult:
    """Fit ordinary or weighted least squares using ``numpy.linalg.lstsq``.

    The fit fails closed if the (possibly weighted) design is rank deficient or
    exceeds ``condition_number_limit``.  No distributional assumption is made
    about the outcomes or residuals.
    """

    _validate_condition_limit(condition_number_limit)
    matrix, target, weights = _validated_inputs(
        exposures, outcomes, sample_weight=sample_weight
    )
    design = _design_matrix(matrix, fit_intercept=fit_intercept)
    if target.shape[0] < design.shape[1]:
        raise RegressionError(
            "insufficient_observations: "
            f"observations={target.shape[0]}, parameters={design.shape[1]}"
        )
    weighted_design, weighted_target = _weighted_design(design, target, weights)
    rank, condition_number, tolerance = _rank_and_condition(
        weighted_design, rcond=rcond
    )
    if rank < design.shape[1]:
        raise RegressionError(
            "rank_deficient_design: "
            f"rank={rank}, parameters={design.shape[1]}"
        )
    if condition_number_limit is not None and condition_number > condition_number_limit:
        raise RegressionError(
            "condition_number_exceeds_limit: "
            f"condition_number={condition_number:.12g}, "
            f"limit={condition_number_limit:.12g}"
        )
    parameters, _, solved_rank, _ = np.linalg.lstsq(
        weighted_design, weighted_target, rcond=rcond
    )
    if int(solved_rank) != rank:
        raise RegressionError("solver_rank_disagrees_with_design_diagnostics")
    return _build_result(
        parameters=parameters,
        matrix=matrix,
        target=target,
        weights=weights,
        fit_intercept=fit_intercept,
        rank=rank,
        condition_number=condition_number,
        tolerance=tolerance,
        alpha=0.0,
        solver="numpy.linalg.lstsq",
    )


def fit_ridge(
    exposures: ArrayLike,
    outcomes: ArrayLike,
    *,
    alpha: float,
    fit_intercept: bool = True,
    sample_weight: ArrayLike | None = None,
    condition_number_limit: float | None = DEFAULT_CONDITION_NUMBER_LIMIT,
    rcond: float | None = None,
) -> RegressionResult:
    """Fit Ridge by augmented least squares; the intercept is never penalized.

    Unlike OLS, Ridge can estimate a rank-deficient source design.  Its returned
    rank and condition number still describe that unregularized design, and the
    diagnostics explicitly warn when regularization is masking weak
    identification.
    """

    _validate_condition_limit(condition_number_limit)
    if not np.isfinite(alpha) or alpha <= 0.0:
        raise RegressionError("ridge_alpha_must_be_finite_and_positive")
    matrix, target, weights = _validated_inputs(
        exposures, outcomes, sample_weight=sample_weight
    )
    design = _design_matrix(matrix, fit_intercept=fit_intercept)
    weighted_design, weighted_target = _weighted_design(design, target, weights)
    rank, condition_number, tolerance = _rank_and_condition(
        weighted_design, rcond=rcond
    )

    parameter_count = design.shape[1]
    penalty = np.eye(parameter_count, dtype=np.float64)
    if fit_intercept:
        penalty[0, 0] = 0.0
    augmented_design = np.vstack((weighted_design, np.sqrt(alpha) * penalty))
    augmented_target = np.concatenate(
        (weighted_target, np.zeros(parameter_count, dtype=np.float64))
    )
    regularized_condition_number = float(np.linalg.cond(augmented_design))
    parameters, _, solved_rank, _ = np.linalg.lstsq(
        augmented_design, augmented_target, rcond=rcond
    )
    if int(solved_rank) < parameter_count:
        raise RegressionError(
            "regularized_design_rank_deficient: "
            f"rank={int(solved_rank)}, parameters={parameter_count}"
        )

    warnings: list[str] = []
    if rank < parameter_count:
        warnings.append("unregularized_design_rank_deficient")
    if condition_number_limit is not None and condition_number > condition_number_limit:
        warnings.append("unregularized_condition_number_exceeds_limit")
    return _build_result(
        parameters=parameters,
        matrix=matrix,
        target=target,
        weights=weights,
        fit_intercept=fit_intercept,
        rank=rank,
        condition_number=condition_number,
        tolerance=tolerance,
        alpha=float(alpha),
        solver="numpy.linalg.lstsq_augmented_ridge",
        regularized_condition_number=regularized_condition_number,
        warnings=tuple(warnings),
    )


def _ordered_period_labels(periods: NDArray[np.object_]) -> tuple[Any, ...]:
    ordered: list[Any] = []
    seen: set[Any] = set()
    for raw_label in periods.tolist():
        label = raw_label.item() if isinstance(raw_label, np.generic) else raw_label
        if label is None or (isinstance(label, float) and not np.isfinite(label)):
            raise RegressionError("period_labels_must_not_be_missing")
        if isinstance(label, np.datetime64) and np.isnat(label):
            raise RegressionError("period_labels_must_not_be_missing")
        try:
            is_new = label not in seen
        except TypeError as exc:
            raise RegressionError("period_labels_must_be_hashable") from exc
        if is_new:
            seen.add(label)
            ordered.append(label)
    return tuple(ordered)


def _newey_west_covariance_of_mean(
    parameter_series: NDArray[np.float64], *, lags: int
) -> NDArray[np.float64]:
    period_count = parameter_series.shape[0]
    centered = parameter_series - parameter_series.mean(axis=0)
    long_run_covariance = centered.T @ centered / period_count
    for lag in range(1, lags + 1):
        weight = 1.0 - lag / (lags + 1.0)
        lag_covariance = centered[lag:].T @ centered[:-lag] / period_count
        long_run_covariance += weight * (lag_covariance + lag_covariance.T)
    covariance_of_mean = long_run_covariance / period_count
    return (covariance_of_mean + covariance_of_mean.T) / 2.0


def fama_macbeth(
    exposures: ArrayLike,
    outcomes: ArrayLike,
    periods: Sequence[Any] | NDArray[np.object_],
    *,
    feature_names: Sequence[str] | None = None,
    fit_intercept: bool = True,
    sample_weight: ArrayLike | None = None,
    condition_number_limit: float | None = DEFAULT_CONDITION_NUMBER_LIMIT,
    rcond: float | None = None,
    hac_lags: int | None = None,
) -> FamaMacBethResult:
    """Run per-period cross-sectional OLS and aggregate its coefficients.

    Periods are consumed in order of first appearance; callers are responsible
    for supplying them chronologically.  Newey-West covariance uses Bartlett
    weights.  ``hac_lags=None`` selects ``floor(4*(T/100)**(2/9))``, capped at
    ``T-1``.  The reported t-statistics are descriptive coefficient-mean to HAC
    standard-error ratios; this function does not impose residual normality.
    """

    matrix, target, weights = _validated_inputs(
        exposures, outcomes, sample_weight=sample_weight
    )
    period_array = np.asarray(periods, dtype=object)
    if period_array.ndim != 1 or period_array.shape[0] != target.shape[0]:
        raise RegressionError("periods_must_be_one_dimensional_and_match_observations")
    labels = _ordered_period_labels(period_array)
    if len(labels) < 2:
        raise RegressionError("fama_macbeth_requires_at_least_two_periods")
    for previous, current in zip(labels, labels[1:]):
        try:
            is_increasing = bool(current > previous)
        except (TypeError, ValueError) as exc:
            raise RegressionError(
                "period_labels_must_be_strictly_chronological"
            ) from exc
        if not is_increasing:
            raise RegressionError("period_labels_must_be_strictly_chronological")

    if feature_names is None:
        names = tuple(f"x{index}" for index in range(matrix.shape[1]))
    else:
        names = tuple(str(name) for name in feature_names)
        if len(names) != matrix.shape[1]:
            raise RegressionError("feature_names_must_match_exposure_columns")
        if any(not name for name in names) or len(set(names)) != len(names):
            raise RegressionError("feature_names_must_be_non_empty_and_unique")

    period_coefficients: list[NDArray[np.float64]] = []
    period_intercepts: list[float] = []
    ranks: list[int] = []
    condition_numbers: list[float] = []
    observations_per_period: list[int] = []
    for label in labels:
        mask = np.asarray([item == label for item in period_array.tolist()], dtype=bool)
        observation_count = int(np.count_nonzero(mask))
        parameter_count = matrix.shape[1] + (1 if fit_intercept else 0)
        if observation_count <= parameter_count:
            raise RegressionError(
                f"cross_section_fit_failed[{label!r}]: "
                "positive_residual_degrees_of_freedom_required"
            )
        period_weights = weights[mask] if weights is not None else None
        try:
            fitted = fit_ols(
                matrix[mask],
                target[mask],
                fit_intercept=fit_intercept,
                sample_weight=period_weights,
                condition_number_limit=condition_number_limit,
                rcond=rcond,
            )
        except RegressionError as exc:
            raise RegressionError(
                f"cross_section_fit_failed[{label!r}]: {exc}"
            ) from exc
        period_coefficients.append(np.asarray(fitted.coefficients))
        period_intercepts.append(fitted.intercept)
        ranks.append(fitted.rank)
        condition_numbers.append(fitted.condition_number)
        observations_per_period.append(observation_count)

    coefficient_matrix = np.vstack(period_coefficients)
    intercept_vector = np.asarray(period_intercepts, dtype=np.float64)
    if fit_intercept:
        parameter_series = np.column_stack((intercept_vector, coefficient_matrix))
        covariance_order = ("intercept", *names)
    else:
        parameter_series = coefficient_matrix
        covariance_order = names

    period_count = len(labels)
    if hac_lags is None:
        resolved_lags = min(
            period_count - 1,
            floor(4.0 * (period_count / 100.0) ** (2.0 / 9.0)),
        )
    else:
        if isinstance(hac_lags, bool) or not isinstance(hac_lags, (int, np.integer)):
            raise RegressionError("hac_lags_must_be_an_integer_or_none")
        resolved_lags = int(hac_lags)
        if resolved_lags < 0 or resolved_lags >= period_count:
            raise RegressionError(
                f"hac_lags_out_of_range: lags={resolved_lags}, periods={period_count}"
            )

    covariance = _newey_west_covariance_of_mean(
        parameter_series, lags=resolved_lags
    )
    diagonal = np.maximum(np.diag(covariance), 0.0)
    parameter_standard_errors = np.sqrt(diagonal)
    parameter_means = parameter_series.mean(axis=0)
    parameter_t_statistics = np.divide(
        parameter_means,
        parameter_standard_errors,
        out=np.full_like(parameter_means, np.nan),
        where=parameter_standard_errors > 0.0,
    )

    if fit_intercept:
        intercept = float(parameter_means[0])
        intercept_standard_error: float | None = float(parameter_standard_errors[0])
        intercept_t_statistic: float | None = float(parameter_t_statistics[0])
        coefficients = parameter_means[1:]
        standard_errors = parameter_standard_errors[1:]
        t_statistics = parameter_t_statistics[1:]
    else:
        intercept = 0.0
        intercept_standard_error = None
        intercept_t_statistic = None
        coefficients = parameter_means
        standard_errors = parameter_standard_errors
        t_statistics = parameter_t_statistics

    diagnostics = {
        "n_periods": period_count,
        "n_observations": int(target.shape[0]),
        "observations_per_period": observations_per_period,
        "period_ranks": ranks,
        "period_condition_numbers": condition_numbers,
        "fit_intercept": fit_intercept,
        "weighted": weights is not None,
        "hac_kernel": "Bartlett",
        "hac_lags": resolved_lags,
        "period_order_policy": "strictly_increasing_first_appearance",
        "covariance_order": covariance_order,
        "inference_note": (
            "HAC standard errors describe time-series variation in period "
            "coefficients; residual normality is not assumed."
        ),
    }
    return FamaMacBethResult(
        feature_names=names,
        period_labels=labels,
        coefficients=coefficients,
        intercept=intercept,
        standard_errors=standard_errors,
        intercept_standard_error=intercept_standard_error,
        t_statistics=t_statistics,
        intercept_t_statistic=intercept_t_statistic,
        covariance=covariance,
        period_coefficients=coefficient_matrix,
        period_intercepts=intercept_vector,
        hac_lags=resolved_lags,
        diagnostics=diagnostics,
    )


fama_macbeth_regression = fama_macbeth


__all__ = [
    "DEFAULT_CONDITION_NUMBER_LIMIT",
    "FamaMacBethResult",
    "RegressionDiagnostics",
    "RegressionError",
    "RegressionResult",
    "fama_macbeth",
    "fama_macbeth_regression",
    "fit_ols",
    "fit_ridge",
]
