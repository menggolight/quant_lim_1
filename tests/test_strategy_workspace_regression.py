from __future__ import annotations

import json
import unittest

import numpy as np

from research.strategy_workspace.preprocessing import (
    PreprocessingError,
    rank_cross_section,
    residualize_cross_section,
    winsorize_cross_section,
    zscore_cross_section,
)
from research.strategy_workspace.regression import (
    RegressionError,
    fama_macbeth,
    fit_ols,
    fit_ridge,
)


class LinearRegressionTests(unittest.TestCase):
    def test_ols_recovers_coefficients_and_exposes_diagnostics(self) -> None:
        exposures = np.asarray(
            [
                [-2.0, 1.0],
                [-1.0, -1.0],
                [0.0, 2.0],
                [1.0, -2.0],
                [2.0, 0.5],
                [3.0, 1.5],
            ]
        )
        outcomes = 2.0 + exposures @ np.asarray([3.0, -1.5])

        result = fit_ols(exposures, outcomes)

        np.testing.assert_allclose(result.coefficients, [3.0, -1.5], atol=1e-12)
        self.assertAlmostEqual(result.intercept, 2.0, places=12)
        np.testing.assert_allclose(result.residuals, 0.0, atol=1e-12)
        self.assertEqual(result.rank, 3)
        self.assertTrue(np.isfinite(result.condition_number))
        self.assertEqual(result.diagnostics.solver, "numpy.linalg.lstsq")
        self.assertEqual(result.diagnostics.n_observations, 6)
        self.assertEqual(result.diagnostics.n_features, 2)
        json.dumps(result.to_dict(), allow_nan=False)

    def test_ols_fails_closed_for_rank_deficiency(self) -> None:
        first = np.linspace(-2.0, 2.0, 9)
        exposures = np.column_stack((first, 2.0 * first))
        outcomes = 1.0 + 3.0 * first

        with self.assertRaisesRegex(RegressionError, "rank_deficient_design"):
            fit_ols(exposures, outcomes)

    def test_ols_fails_closed_for_excessive_condition_number(self) -> None:
        first = np.linspace(-2.0, 2.0, 9)
        small_independent = np.asarray(
            [-1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0]
        ) * 1.0e-9
        exposures = np.column_stack((first, small_independent))
        outcomes = 1.0 + 2.0 * first + 3.0 * small_independent

        with self.assertRaisesRegex(
            RegressionError, "condition_number_exceeds_limit"
        ):
            fit_ols(exposures, outcomes, condition_number_limit=1.0e6)

    def test_ridge_allows_collinearity_and_does_not_penalize_intercept(self) -> None:
        first = np.linspace(-2.0, 2.0, 9)
        exposures = np.column_stack((first, 2.0 * first))
        outcomes = 5.0 + 4.0 * first

        result = fit_ridge(exposures, outcomes, alpha=100.0)

        self.assertAlmostEqual(result.intercept, 5.0, places=12)
        self.assertLess(result.rank, exposures.shape[1] + 1)
        self.assertIn(
            "unregularized_design_rank_deficient", result.diagnostics.warnings
        )
        self.assertTrue(np.isfinite(result.diagnostics.regularized_condition_number))
        np.testing.assert_allclose(
            result.predict(exposures),
            exposures @ result.coefficients + result.intercept,
        )

    def test_weighted_ols_satisfies_weighted_normal_equations(self) -> None:
        exposures = np.column_stack(
            (np.linspace(-2.0, 2.0, 11), np.linspace(-1.0, 1.0, 11) ** 2)
        )
        outcomes = 1.0 + exposures @ np.asarray([0.7, -0.3]) + np.sin(
            np.arange(11)
        )
        weights = np.linspace(0.5, 2.0, 11)

        result = fit_ols(exposures, outcomes, sample_weight=weights)

        weighted_residuals = weights * result.residuals
        self.assertAlmostEqual(float(np.sum(weighted_residuals)), 0.0, places=11)
        np.testing.assert_allclose(
            exposures.T @ weighted_residuals,
            np.zeros(exposures.shape[1]),
            atol=1e-11,
        )


class CrossSectionPreprocessingTests(unittest.TestCase):
    def test_winsorize_and_tie_aware_rank_are_deterministic(self) -> None:
        values = np.asarray([0.0, 1.0, 1.0, 100.0])

        clipped = winsorize_cross_section(
            values, lower_quantile=0.25, upper_quantile=0.75
        )
        np.testing.assert_allclose(clipped, [0.75, 1.0, 1.0, 25.75])
        np.testing.assert_allclose(rank_cross_section(values), [1.0, 2.5, 2.5, 4.0])
        np.testing.assert_allclose(
            rank_cross_section(values, percentile=True),
            [0.0, 0.5, 0.5, 1.0],
        )
        np.testing.assert_allclose(
            rank_cross_section(values, ascending=False),
            [4.0, 2.5, 2.5, 1.0],
        )

    def test_zscore_has_zero_mean_unit_scale_and_rejects_zero_variance(self) -> None:
        standardized = zscore_cross_section([1.0, 2.0, 3.0, 4.0])
        self.assertAlmostEqual(float(np.mean(standardized)), 0.0, places=12)
        self.assertAlmostEqual(float(np.std(standardized)), 1.0, places=12)

        with self.assertRaisesRegex(
            PreprocessingError, "zero_variance_cross_section"
        ):
            zscore_cross_section([3.0, 3.0, 3.0])

    def test_residualization_is_orthogonal_to_named_exposures_in_sample(self) -> None:
        first = np.linspace(-2.0, 2.0, 17)
        second = first**2 - np.mean(first**2)
        exposures = np.column_stack((first, second))
        values = 2.0 + exposures @ np.asarray([1.5, -0.7]) + np.sin(
            np.arange(first.size)
        )

        result = residualize_cross_section(
            values,
            exposures,
            exposure_names=("size", "industry_style"),
        )

        self.assertEqual(result.exposure_names, ("size", "industry_style"))
        self.assertAlmostEqual(float(np.sum(result.residuals)), 0.0, places=11)
        np.testing.assert_allclose(
            exposures.T @ result.residuals,
            np.zeros(exposures.shape[1]),
            atol=1e-11,
        )
        json.dumps(result.to_dict(), allow_nan=False)

    def test_weighted_residualization_is_weighted_orthogonal(self) -> None:
        first = np.linspace(-1.5, 1.5, 13)
        exposures = np.column_stack((first, first**2 - np.mean(first**2)))
        values = 3.0 + exposures @ np.asarray([0.8, -0.4]) + np.cos(
            np.arange(first.size)
        )
        weights = np.linspace(0.25, 2.0, first.size)

        result = residualize_cross_section(
            values,
            exposures,
            exposure_names=("size", "value"),
            weights=weights,
        )

        weighted_residuals = weights * result.residuals
        self.assertAlmostEqual(float(np.sum(weighted_residuals)), 0.0, places=11)
        np.testing.assert_allclose(
            exposures.T @ weighted_residuals,
            np.zeros(exposures.shape[1]),
            atol=1e-11,
        )


class FamaMacBethTests(unittest.TestCase):
    def test_fama_macbeth_averages_period_coefficients_with_hac_se(self) -> None:
        period_count = 12
        observations_per_period = 10
        base_exposure = np.linspace(-1.0, 1.0, observations_per_period)
        periods = np.repeat(np.arange(period_count), observations_per_period)
        exposures = np.tile(base_exposure, period_count).reshape(-1, 1)
        outcomes: list[float] = []
        period_betas: list[float] = []
        period_intercepts: list[float] = []
        for period in range(period_count):
            beta = 0.5 + 0.1 * period
            intercept = 2.0 + (0.05 if period % 2 == 0 else -0.05)
            period_betas.append(beta)
            period_intercepts.append(intercept)
            outcomes.extend((intercept + beta * base_exposure).tolist())

        result = fama_macbeth(
            exposures,
            outcomes,
            periods,
            feature_names=("signal",),
            hac_lags=2,
        )

        self.assertAlmostEqual(result.coefficients[0], np.mean(period_betas), places=12)
        self.assertAlmostEqual(result.intercept, np.mean(period_intercepts), places=12)
        self.assertEqual(result.hac_lags, 2)
        self.assertGreater(result.standard_errors[0], 0.0)
        self.assertEqual(result.period_coefficients.shape, (period_count, 1))
        self.assertEqual(result.covariance.shape, (2, 2))
        self.assertIn("normality is not assumed", result.diagnostics["inference_note"])
        json.dumps(result.to_dict(), allow_nan=False)

    def test_fama_macbeth_rejects_a_rank_deficient_cross_section(self) -> None:
        periods = np.repeat(["p1", "p2"], 4)
        first = np.tile(np.arange(4.0), 2)
        exposures = np.column_stack((first, 2.0 * first))
        outcomes = 1.0 + first

        with self.assertRaisesRegex(
            RegressionError, "cross_section_fit_failed.*rank_deficient_design"
        ):
            fama_macbeth(exposures, outcomes, periods, hac_lags=0)

    def test_fama_macbeth_rejects_non_chronological_periods(self) -> None:
        periods = np.repeat([2, 1], 4)
        exposures = np.tile(np.arange(4.0), 2).reshape(-1, 1)
        outcomes = 1.0 + exposures[:, 0]

        with self.assertRaisesRegex(RegressionError, "strictly_chronological"):
            fama_macbeth(exposures, outcomes, periods, hac_lags=0)


if __name__ == "__main__":
    unittest.main()
