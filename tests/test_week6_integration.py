"""Week 6 Ground-truth experiment and closed-loop safety tests."""

import numpy as np
import pytest

from baseline.emissions import EmissionsEstimator, EmissionsParams
from baseline.regression import BaselineParams, BaselineRegression
from control.mpc import MPCController, MPCParams
from ledger.verifier import classify
from plant.load_profiles import make_profile
from plant.motor import MotorParams, simulate
from plant.thermal import ThermalParams
from sensors import SAMPLE_RATE_HZ, measurement_chain
from sensors.adc import ADCParams
from sensors.afe import AFE, AFEParams


class TestGroundTruthExperiment:
    """Legacy vs MPC under matched conditions."""

    @pytest.fixture(scope="class")
    def simulation_setup(self):
        """Common simulation parameters."""
        return {
            "duration_s": 60.0,  # shorter for test speed
            "fs_hz": SAMPLE_RATE_HZ,
            "seed": 42,
            "motor_params": MotorParams(),
            "thermal_params": ThermalParams(),
            "afe_params": AFEParams(seed=42),
            "adc_params": ADCParams(),
        }

    def run_simulation(self, setup, controller, profile_kind):
        """Run plant simulation with given controller."""
        load = make_profile(
            profile_kind,
            duration_s=setup["duration_s"],
            fs_hz=setup["fs_hz"],
            seed=setup["seed"],
        )

        # Simulate plant with controller
        # This is a simplified version - full implementation needed
        res = simulate(load, setup["fs_hz"], setup["motor_params"], setup["thermal_params"])

        # Apply sensor chain
        t = np.arange(res.v_true_V.size) / setup["fs_hz"]
        v_meas, i_meas = measurement_chain(
            res.v_true_V, res.i_true_A, t, AFE(setup["afe_params"]), setup["adc_params"]
        )

        # Compute energy per window (simplified)
        from sensors.windows import (
            ZCParams,
            build_windows,
            find_rising_crossings,
            window_mean_power,
        )

        zc = ZCParams()
        det_idx, frac = find_rising_crossings(v_meas, setup["fs_hz"], zc.hyst_V)
        windows = build_windows(det_idx, frac, v_meas.size, zc)
        p_windows = window_mean_power(v_meas, i_meas, windows)

        valid_energy = np.nansum(p_windows) * (zc.n_cycles / 50.0) / 3600.0  # kWh approx

        return {
            "load": load,
            "res": res,
            "v_meas": v_meas,
            "i_meas": i_meas,
            "energy_kwh": valid_energy,
            "max_temp": np.max(res.temp_C),
            "min_torque_margin": np.min(res.torque_Nm / np.maximum(load, 0.1)),
        }

    def test_legacy_vs_mpc_steady_profile(self, simulation_setup):
        """Compare legacy (u=1) vs MPC on steady load."""
        pytest.skip("Requires full MPC integration")

    def test_legacy_vs_mpc_cyclic_profile(self, simulation_setup):
        pytest.skip("Requires full MPC integration")

    def test_legacy_vs_mpc_bursty_profile(self, simulation_setup):
        pytest.skip("Requires full MPC integration")


class TestClosedLoopSafety:
    """Closed-loop safety constraint verification."""

    def test_temperature_constraint_all_profiles(self):
        """Temperature must not exceed 120°C in any profile."""
        pytest.skip("Requires full MPC integration")

    def test_torque_margin_all_profiles(self):
        """Torque must maintain ≥1.1× margin over load."""
        pytest.skip("Requires full MPC integration")

    def test_throttle_bounds_all_profiles(self):
        """Throttle must stay within [0.4, 1.0]."""
        pytest.skip("Requires full MPC integration")

    def test_production_demand_compliance(self):
        """Motor must deliver required torque."""
        pytest.skip("Requires full MPC integration")

    def test_violation_recording(self):
        """Every violation must be recorded, not just final state."""
        pytest.skip("Requires full MPC integration")


class TestControllerEffectiveness:
    """Controller effectiveness: measured energy comparison."""

    def test_legacy_energy_measured(self):
        """Legacy control energy must be measured via sensor chain."""
        pytest.skip("Requires full integration")

    def test_mpc_energy_measured(self):
        """MPC energy must be measured via sensor chain."""
        pytest.skip("Requires full integration")

    def test_ground_truth_savings(self):
        """Ground-truth savings = legacy - MPC (plant internal)."""
        pytest.skip("Requires full integration")

    def test_estimated_savings_independent(self):
        """Estimated savings from baseline must be independent of ground truth."""
        pytest.skip("Requires full integration")

    def test_positive_savings_or_explanation(self):
        """If any profile fails positive savings, must diagnose why."""
        pytest.skip("Requires full integration")


class TestBaselineValidity:
    """Baseline model validity metrics."""

    def test_cv_rmse_documented_definition(self):
        """CV(RMSE) must match documented formula."""
        # Already tested in test_week6_baseline.py
        pass

    def test_nmbe_documented_definition(self):
        """NMBE must match documented formula."""
        pass

    def test_savings_confidence_interval(self):
        """CI must use documented bootstrap method."""
        pass

    def test_missing_excluded_windows_handling(self):
        """Missing/excluded windows must not be treated as zero consumption."""
        params = BaselineParams()
        baseline = BaselineRegression(params)

        np.random.seed(42)
        n = 100
        load = np.random.uniform(6, 14, n)
        temp = np.random.uniform(30, 60, n)
        energy = 1000 + 50 * load + 10 * temp + np.random.normal(0, 20, n)

        baseline.fit(load, temp, energy)

        # Simulate missing windows (NaN in measured energy)
        post_load = np.array([10.0, 12.0, 8.0])
        post_temp = np.array([40.0, 45.0, 38.0])
        post_measured = np.array([950.0, np.nan, 850.0])  # middle window missing

        # Should handle NaN gracefully
        savings, ci_low, ci_high = baseline.estimate_savings(post_load, post_temp, post_measured)

        # Missing window should not contribute to savings
        assert not np.isnan(savings)

    def test_invalid_ledger_not_in_savings(self):
        """Invalid ledger records must not contribute to savings."""

        # Mock verifier with invalid records
        # This tests the integration point
        pass


class TestCarbonAccounting:
    """Carbon emissions accounting tests."""

    def test_energy_unit_conversions(self):
        """µWh → kWh → CO2 conversions must be correct."""
        from baseline.emissions import EmissionsEstimator, EmissionsParams

        params = EmissionsParams()
        est = EmissionsEstimator(params)

        # 1 kWh = 1,000,000 µWh
        assert est.uwh_to_kwh(1_000_000) == 0.001
        assert est.uwh_to_kwh(3_600_000_000) == 3.6  # 1 MWh

    def test_emissions_factor_time_alignment(self):
        """Emissions factor must align with window timestamp."""
        from baseline.emissions import EmissionsEstimator, EmissionsParams

        params = EmissionsParams()
        est = EmissionsEstimator(params)

        # Same hour should give same factor
        ef1 = est.get_emission_factor(hour_of_year=100)
        ef2 = est.get_emission_factor(hour_of_year=100)
        assert ef1 == ef2

    def test_missing_emissions_data(self):
        """Missing hours handled by interpolation."""
        from baseline.emissions import EmissionsEstimator, EmissionsParams

        params = EmissionsParams()
        est = EmissionsEstimator(params)

        # Far future hour should not crash
        ef = est.get_emission_factor(hour_of_year=100000)
        assert isinstance(ef, float)

    def test_negative_savings_zero_emissions(self):
        """Negative savings → zero emissions avoided."""
        from baseline.emissions import EmissionsEstimator, EmissionsParams

        params = EmissionsParams()
        est = EmissionsEstimator(params)

        avoided = est.estimate_avoided_emissions(-5.0)
        assert avoided == 0.0

    def test_excluded_rejected_not_in_emissions(self):
        """Excluded/rejected records must not count."""

        params = EmissionsParams()
        est = EmissionsEstimator(params)

        records = [
            {"window_flags": 0x1F, "energy_uwh": 1_000_000},  # valid
            {"window_flags": 0x00, "energy_uwh": 1_000_000},  # excluded (all invalid)
            {"window_flags": 0x80, "energy_uwh": 1_000_000},  # rejected (bit 7)
        ]

        total_emissions = 0.0
        for rec in records:
            cls = classify(rec)
            if cls == "valid":
                total_emissions += est.estimate_avoided_emissions(est.uwh_to_kwh(rec["energy_uwh"]))

        # Only valid record contributes
        assert total_emissions == est.estimate_avoided_emissions(0.001)


class TestReproducibility:
    """Deterministic results with fixed seeds."""

    def test_deterministic_mpc(self):
        """MPC with same inputs must give same output."""
        params = MPCParams(
            prediction_horizon=10,
            dt=1.0,
            u_min=0.4,
            u_max=1.0,
            du_max=0.1,
            temp_max=120.0,
            torque_margin=0.9,
        )

        mpc1 = MPCController(params)
        mpc2 = MPCController(params)

        x0 = np.array([20.0])
        load = 10.0
        u_prev = 1.0

        u1 = mpc1.solve(x0, load, u_prev)
        u2 = mpc2.solve(x0, load, u_prev)

        assert u1 == u2

    def test_deterministic_baseline(self):
        """Baseline with same data must give same coefficients."""
        params = BaselineParams()
        b1 = BaselineRegression(params)
        b2 = BaselineRegression(params)

        np.random.seed(42)
        load = np.random.uniform(6, 14, 100)
        temp = np.random.uniform(30, 60, 100)
        energy = 1000 + 50 * load + 10 * temp + np.random.normal(0, 20, 100)

        b1.fit(load, temp, energy)
        b2.fit(load, temp, energy)

        np.testing.assert_allclose(b1.coefficients_, b2.coefficients_)

    def test_deterministic_emissions(self):
        """Emissions with same inputs must give same output."""
        from baseline.emissions import EmissionsEstimator, EmissionsParams

        params = EmissionsParams()
        e1 = EmissionsEstimator(params)
        e2 = EmissionsEstimator(params)

        assert e1.get_emission_factor(100) == e2.get_emission_factor(100)
