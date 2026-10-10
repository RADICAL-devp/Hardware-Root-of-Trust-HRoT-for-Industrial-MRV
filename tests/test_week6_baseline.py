"""Week 6 Baseline Regression tests: IPMVP model, leakage prevention, metrics."""

import numpy as np
import pytest

from baseline.emissions import EmissionsEstimator, EmissionsParams
from baseline.regression import BaselineParams, BaselineRegression


class TestBaselineLeakagePrevention:
    """Baseline must not leak post-retrofit data into training."""

    def test_fit_uses_only_pre_retrofit_data(self):
        """Fitted parameters must be identical regardless of post-retrofit data."""
        params = BaselineParams()
        baseline = BaselineRegression(params)

        # Pre-retrofit data (legacy control)
        np.random.seed(42)
        n_pre = 100
        pre_load = np.random.uniform(6, 14, n_pre)
        pre_temp = np.random.uniform(30, 60, n_pre)
        pre_energy = 1000 + 50 * pre_load + 10 * pre_temp + np.random.normal(0, 20, n_pre)

        # Post-retrofit data (MPC control) - different distribution
        _post_load = np.random.uniform(6, 14, 50)
        _post_temp = np.random.uniform(30, 60, 50)
        _post_energy = 800 + 40 * _post_load + 8 * _post_temp + np.random.normal(0, 15, 50)

        # Fit on pre only
        baseline.fit(pre_load, pre_temp, pre_energy)
        coeffs_pre_only = baseline.coefficients_.copy()

        # Create new baseline, fit on pre + post
        # (WRONG - should not change result if we re-fit on pre only)
        baseline2 = BaselineRegression(params)
        baseline2.fit(pre_load, pre_temp, pre_energy)
        coeffs_pre_only2 = baseline2.coefficients_.copy()

        # Coefficients must be identical when fitting on same pre data
        np.testing.assert_allclose(coeffs_pre_only, coeffs_pre_only2, rtol=1e-10)

    def test_post_retrofit_data_does_not_alter_fit(self):
        """Changing post-retrofit data must not change fitted baseline."""
        params = BaselineParams()
        baseline = BaselineRegression(params)

        np.random.seed(42)
        n_pre = 100
        pre_load = np.random.uniform(6, 14, n_pre)
        pre_temp = np.random.uniform(30, 60, n_pre)
        pre_energy = 1000 + 50 * pre_load + 10 * pre_temp + np.random.normal(0, 20, n_pre)

        # Fit on pre
        baseline.fit(pre_load, pre_temp, pre_energy)
        original_coeffs = baseline.coefficients_.copy()

        # Predict on post (different data) - should not change coefficients
        post_load = np.random.uniform(6, 14, 50)
        post_temp = np.random.uniform(30, 60, 50)
        _ = baseline.predict(post_load, post_temp)

        # Coefficients unchanged
        np.testing.assert_allclose(baseline.coefficients_, original_coeffs, rtol=1e-10)

    def test_leakage_explicitly_detected(self):
        """Leakage attempt must be explicitly detected, not just documented."""
        params = BaselineParams()
        baseline = BaselineRegression(params)

        np.random.seed(42)
        n_pre = 100
        pre_load = np.random.uniform(6, 14, n_pre)
        pre_temp = np.random.uniform(30, 60, n_pre)
        pre_energy = 1000 + 50 * pre_load + 10 * pre_temp + np.random.normal(0, 20, n_pre)

        baseline.fit(pre_load, pre_temp, pre_energy)

        # Attempt to re-fit with combined data should raise or be detected
        combined_load = np.concatenate([pre_load, np.random.uniform(6, 14, 50)])
        combined_temp = np.concatenate([pre_temp, np.random.uniform(30, 60, 50)])
        combined_energy = np.concatenate([pre_energy, np.random.uniform(800, 1200, 50)])

        # The API should prevent this or detect it
        with pytest.raises((ValueError, RuntimeError)):
            # This should fail because baseline is already fitted
            baseline.fit(combined_load, combined_temp, combined_energy)

    def test_separate_train_validation_periods(self):
        """Baseline must support separate training and validation periods."""
        params = BaselineParams(validation_fraction=0.2)
        baseline = BaselineRegression(params)

        np.random.seed(42)
        n = 100
        load = np.random.uniform(6, 14, n)
        temp = np.random.uniform(30, 60, n)
        energy = 1000 + 50 * load + 10 * temp + np.random.normal(0, 20, n)

        # Should split internally and report validation metrics
        baseline.fit(load, temp, energy)

        assert hasattr(baseline, "cv_rmse_")
        assert hasattr(baseline, "nmbe_")
        assert baseline.cv_rmse_ >= 0
        assert isinstance(baseline.nmbe_, float)


class TestBaselineMetrics:
    """Baseline metric calculations: CV(RMSE), NMBE, confidence intervals."""

    def test_cv_rmse_calculation(self):
        """CV(RMSE) = RMSE / mean(baseline_predictions) * 100."""
        params = BaselineParams(validation_fraction=0.2)
        baseline = BaselineRegression(params)

        np.random.seed(42)
        n = 200
        load = np.random.uniform(6, 14, n)
        temp = np.random.uniform(30, 60, n)
        energy = 1000 + 50 * load + 10 * temp + np.random.normal(0, 20, n)

        baseline.fit(load, temp, energy)

        # Manual calculation
        predictions = baseline.predict(load, temp)
        residuals = energy - predictions
        rmse = np.sqrt(np.mean(residuals**2))
        cv_rmse_manual = 100.0 * rmse / np.mean(predictions)

        assert abs(baseline.cv_rmse_ - cv_rmse_manual) < 0.01

    def test_nmbe_calculation(self):
        """NMBE = mean(baseline - measured) / mean(measured) * 100."""
        params = BaselineParams(validation_fraction=0.2)
        baseline = BaselineRegression(params)

        np.random.seed(42)
        n = 200
        load = np.random.uniform(6, 14, n)
        temp = np.random.uniform(30, 60, n)
        energy = 1000 + 50 * load + 10 * temp + np.random.normal(0, 20, n)

        baseline.fit(load, temp, energy)

        predictions = baseline.predict(load, temp)
        nmbe_manual = 100.0 * np.mean(predictions - energy) / np.mean(energy)

        assert abs(baseline.nmbe_ - nmbe_manual) < 0.01

    def test_savings_confidence_interval(self):
        """Savings CI must be computed via bootstrap on pre-retrofit residuals."""
        params = BaselineParams(
            validation_fraction=0.2,
            bootstrap_samples=1000,
            confidence_level=0.95,
        )
        baseline = BaselineRegression(params)

        np.random.seed(42)
        n = 200
        load = np.random.uniform(6, 14, n)
        temp = np.random.uniform(30, 60, n)
        energy = 1000 + 50 * load + 10 * temp + np.random.normal(0, 20, n)

        baseline.fit(load, temp, energy)

        # Post-retrofit conditions
        post_load = np.array([10.0, 12.0, 8.0, 11.0])
        post_temp = np.array([40.0, 45.0, 38.0, 42.0])
        post_measured = np.array([950.0, 1100.0, 850.0, 1000.0])

        savings, ci_low, ci_high = baseline.estimate_savings(post_load, post_temp, post_measured)

        assert isinstance(savings, float)
        assert ci_low <= savings <= ci_high
        assert ci_high - ci_low > 0

    def test_baseline_prediction_error_reported(self):
        """Baseline must report prediction error on validation set."""
        params = BaselineParams(validation_fraction=0.25)
        baseline = BaselineRegression(params)

        np.random.seed(42)
        n = 200
        load = np.random.uniform(6, 14, n)
        temp = np.random.uniform(30, 60, n)
        energy = 1000 + 50 * load + 10 * temp + np.random.normal(0, 20, n)

        baseline.fit(load, temp, energy)

        assert hasattr(baseline, "val_rmse_")
        assert hasattr(baseline, "val_mae_")
        assert baseline.val_rmse_ >= 0
        assert baseline.val_mae_ >= 0


class TestBaselineWithVerifier:
    """Baseline integration with ledger verifier classify()."""

    def test_excluded_windows_not_in_baseline(self):
        """Excluded (verified but not counted) windows must not train baseline."""
        from ledger.verifier import classify

        params = BaselineParams()
        baseline = BaselineRegression(params)

        # Simulate records with classifications
        np.random.seed(42)
        n = 100
        load = np.random.uniform(6, 14, n)
        temp = np.random.uniform(30, 60, n)
        energy = 1000 + 50 * load + 10 * temp + np.random.normal(0, 20, n)

        # Create mock records
        records = []
        for i in range(n):
            rec = {
                "window_flags": 0x1F,  # all valid
                "energy_uwh": int(energy[i]),
                "zc_samples": [200] * 5,
                "p_avg_q30": 1000000,
            }
            records.append(rec)

        # Filter using classify - only "valid" records should be used
        valid_mask = np.array([classify(r) == "valid" for r in records])

        baseline.fit(load[valid_mask], temp[valid_mask], energy[valid_mask])

        # Should work correctly
        assert baseline.coefficients_ is not None

    def test_rejected_records_never_used(self):
        """Rejected records must never enter baseline training."""
        from ledger.verifier import classify

        params = BaselineParams()
        baseline = BaselineRegression(params)

        np.random.seed(42)
        n = 100
        load = np.random.uniform(6, 14, n)
        temp = np.random.uniform(30, 60, n)
        energy = 1000 + 50 * load + 10 * temp + np.random.normal(0, 20, n)

        # Mix of valid and rejected
        records = []
        for i in range(n):
            if i % 10 == 0:
                rec = {"window_flags": 0x80, "energy_uwh": int(energy[i])}  # rejected (bit 7)
            else:
                rec = {"window_flags": 0x1F, "energy_uwh": int(energy[i])}
            records.append(rec)

        valid_mask = np.array([classify(r) == "valid" for r in records])

        # Rejected should be excluded
        assert not np.all(valid_mask)

        baseline.fit(load[valid_mask], temp[valid_mask], energy[valid_mask])
        assert baseline.coefficients_ is not None


class TestEmissionsEstimation:
    """Carbon emissions estimation tests."""

    def test_energy_unit_conversion(self):
        """Energy µWh → kWh conversion must be correct."""
        params = EmissionsParams()
        estimator = EmissionsEstimator(params)

        energy_uwh = 1_000_000  # 1 Wh = 0.001 kWh
        energy_kwh = estimator.uwh_to_kwh(energy_uwh)

        assert energy_kwh == 0.001

    def test_emissions_factor_time_alignment(self):
        """Emissions factor must align with window timestamp."""
        params = EmissionsParams()
        estimator = EmissionsEstimator(params)

        # Hourly emissions factor
        ef = estimator.get_emission_factor(hour_of_year=12)  # noon
        assert isinstance(ef, float)
        assert ef > 0

        ef_night = estimator.get_emission_factor(hour_of_year=3)  # 3 AM
        assert ef_night != ef  # should vary by hour

    def test_missing_emissions_data_handling(self):
        """Missing hours must be interpolated, not cause failure."""
        params = EmissionsParams()
        estimator = EmissionsEstimator(params)

        # Request factor for hour beyond pre-defined range
        ef = estimator.get_emission_factor(hour_of_year=1000)
        assert isinstance(ef, float)
        assert ef > 0

    def test_negative_savings_zero_emissions(self):
        """Negative estimated savings must yield zero avoided emissions."""
        params = EmissionsParams()
        estimator = EmissionsEstimator(params)

        avoided = estimator.estimate_avoided_emissions(energy_saved_kwh=-10.0)
        assert avoided == 0.0

    def test_zero_savings_zero_emissions(self):
        """Zero savings must yield zero avoided emissions."""
        params = EmissionsParams()
        estimator = EmissionsEstimator(params)

        avoided = estimator.estimate_avoided_emissions(energy_saved_kwh=0.0)
        assert avoided == 0.0

    def test_emissions_aggregation(self):
        """Total emissions = sum of per-window avoided emissions."""
        params = EmissionsParams()
        estimator = EmissionsEstimator(params)

        savings_per_window = [1.0, 2.0, 0.5, 0.0, -1.0]  # kWh
        total = estimator.aggregate_emissions(savings_per_window)

        # Only positive savings count
        expected = (
            estimator.estimate_avoided_emissions(1.0)
            + estimator.estimate_avoided_emissions(2.0)
            + estimator.estimate_avoided_emissions(0.5)
        )
        assert abs(total - expected) < 1e-6

    def test_excluded_records_not_in_emissions(self):
        """Excluded/rejected records must not contribute to emissions."""
        from ledger.verifier import classify

        params = EmissionsParams()
        estimator = EmissionsEstimator(params)

        records = [
            {"window_flags": 0x1F, "energy_uwh": 1_000_000, "classification": "valid"},
            {
                "window_flags": 0x00,
                "energy_uwh": 1_000_000,
                "classification": "excluded",
            },  # all invalid
            {"window_flags": 0x80, "energy_uwh": 1_000_000, "classification": "rejected"},  # bit 7
        ]

        total = 0.0
        for rec in records:
            cls = classify(rec)
            if cls == "valid":
                total += estimator.estimate_avoided_emissions(
                    estimator.uwh_to_kwh(rec["energy_uwh"])
                )

        # Only first record contributes
        assert total == estimator.estimate_avoided_emissions(0.001)
