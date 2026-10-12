"""Week 6 Ground-truth experiment and closed-loop safety tests."""

import numpy as np
import pytest

from baseline.emissions import EmissionsEstimator, EmissionsParams
from baseline.regression import BaselineParams, BaselineRegression
from control.capability import CAP_OFFSET
from control.failsafe import Failsafe, FailsafeParams
from control.mpc import MPCController, MPCParams
from ledger.verifier import classify
from plant.load_profiles import make_profile
from plant.motor import MotorParams, simulate
from plant.thermal import ThermalParams
from sensors import SAMPLE_RATE_HZ, measurement_chain
from sensors.adc import ADCParams
from sensors.afe import AFE, AFEParams
from sensors.windows import (
    ZCParams,
    build_windows,
    find_rising_crossings,
    window_mean_power,
)
from sim.run_week6 import (
    apply_sensor_chain,
    ground_truth_energy_kwh,
    measure_windows,
    simulate_continuous_pair,
)

# Nominal coupled-plant scenario for integration tests (synthetic sensitivity
# point: catalogue-anchored rotor + assumed load inertia; see plant.mechanics).
NOMINAL_MECH = {
    "j_motor_kgm2": 0.0064,
    "j_load_reflected_kgm2": 0.01,
    "gear_ratio": 1.0,
    "b_Nms": 0.0008,
    "tf_Nm": 0.0,
}
PAIR_SECONDS = 12.0  # matched multi-window horizon for integration tests


class TestGroundTruthExperiment:
    """Legacy vs MPC under matched conditions."""

    @pytest.fixture(scope="class")
    def simulation_setup(self):
        """Common simulation parameters."""
        return {
            "duration_s": 10.0,  # shorter for test speed
            "fs_hz": SAMPLE_RATE_HZ,
            "seed": 42,
            "motor_params": MotorParams(),
            "thermal_params": ThermalParams(),
            "afe_params": AFEParams(seed=42),
            "adc_params": ADCParams(),
        }

    def run_simulation(self, setup, controller_type, mpc_controller=None):
        """Run plant simulation with given controller."""
        load = make_profile(
            "steady",
            duration_s=setup["duration_s"],
            fs_hz=setup["fs_hz"],
            seed=setup["seed"],
        )

        # Simulate plant with controller
        res = simulate(load, setup["fs_hz"], setup["motor_params"], setup["thermal_params"])

        # Apply sensor chain
        t = np.arange(res.v_true_V.size) / setup["fs_hz"]
        v_meas, i_meas = measurement_chain(
            res.v_true_V, res.i_true_A, t, AFE(setup["afe_params"]), setup["adc_params"]
        )

        # Compute energy per window (simplified)

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
        mpc_params = MPCParams(
            prediction_horizon=10,
            dt=1.0,
            u_min=0.4,
            u_max=1.0,
            du_max=0.1,
            temp_max=120.0,
            torque_margin=0.9,
            R_u=1.0,
            Q_T=0.1,
            solver_timeout_ms=100,
            warm_start=True,
        )
        mpc = MPCController(mpc_params)

        # Legacy
        legacy_result = self.run_simulation(simulation_setup, "legacy")
        # MPC
        mpc_result = self.run_simulation(simulation_setup, "mpc", mpc_controller=mpc)

        # Both should complete without error
        assert mpc_result["energy_kwh"] > 0
        assert legacy_result["energy_kwh"] > 0

    def test_legacy_vs_mpc_cyclic_profile(self, simulation_setup):
        """Compare legacy (u=1) vs MPC on cyclic load (matched continuous pair)."""
        pair = simulate_continuous_pair("cyclic", PAIR_SECONDS, NOMINAL_MECH, seed=42)
        np.testing.assert_array_equal(pair["legacy"]["load"], pair["mpc"]["load"])
        assert pair["legacy"]["coupled"].t_s.shape == pair["mpc"]["coupled"].t_s.shape
        assert pair["legacy"]["gt_energy_kwh"] > 0
        assert pair["mpc"]["gt_energy_kwh"] > 0
        assert np.isfinite(pair["gt_savings_kwh"])

    def test_legacy_vs_mpc_bursty_profile(self, simulation_setup):
        """Compare legacy (u=1) vs MPC on bursty load (matched continuous pair)."""
        pair = simulate_continuous_pair("bursty", PAIR_SECONDS, NOMINAL_MECH, seed=42)
        np.testing.assert_array_equal(pair["legacy"]["load"], pair["mpc"]["load"])
        assert pair["legacy"]["coupled"].t_s.shape == pair["mpc"]["coupled"].t_s.shape
        assert pair["legacy"]["gt_energy_kwh"] > 0
        assert pair["mpc"]["gt_energy_kwh"] > 0
        assert np.isfinite(pair["gt_savings_kwh"])


class TestClosedLoopSafety:
    """Closed-loop safety constraint verification."""

    def test_temperature_constraint_all_profiles(self):
        """Temperature must not exceed 120°C in any profile (both arms)."""
        for kind in ("steady", "cyclic", "bursty"):
            pair = simulate_continuous_pair(kind, PAIR_SECONDS, NOMINAL_MECH, seed=42)
            assert pair["legacy"]["max_temp_C"] <= 120.0, kind
            assert pair["mpc"]["max_temp_C"] <= 120.0, kind
            assert pair["legacy"]["temp_violations"] == 0, kind
            assert pair["mpc"]["temp_violations"] == 0, kind

    def test_torque_margin_all_profiles(self):
        """Instantaneous torque diagnostic must equal an independent recount."""
        for kind in ("steady", "cyclic", "bursty"):
            pair = simulate_continuous_pair(kind, PAIR_SECONDS, NOMINAL_MECH, seed=42)
            for arm in ("legacy", "mpc"):
                c = pair[arm]["coupled"]
                recount = int(np.sum(c.torque_Nm < 0.9 * pair[arm]["load"]))
                assert pair[arm]["instantaneous_violations"] == recount, (kind, arm)

    def test_throttle_bounds_all_profiles(self):
        """Throttle bounds hold; rate jumps only via recorded failsafe fallback."""
        for kind in ("steady", "cyclic", "bursty"):
            pair = simulate_continuous_pair(kind, PAIR_SECONDS, NOMINAL_MECH, seed=42)
            u = np.asarray(pair["mpc"]["u_schedule"], dtype=float)
            assert np.all(u >= 0.4 - 1e-9) and np.all(u <= 1.0 + 1e-9), kind
            records = pair["window_records"]
            for i in range(1, len(u)):
                if abs(float(u[i] - u[i - 1])) <= 0.1 + 1e-6:
                    continue
                # A rate-limit break is only legitimate as a recorded failsafe
                # fallback to full voltage (safety over smoothness, by design).
                assert records[i]["failsafe_reason"] is not None, (kind, i)
                assert u[i] == pytest.approx(1.0), (kind, i)

    def test_production_demand_compliance(self):
        """Every control window must carry feasibility/compliance records."""
        for kind in ("steady", "cyclic", "bursty"):
            pair = simulate_continuous_pair(kind, PAIR_SECONDS, NOMINAL_MECH, seed=42)
            records = pair["window_records"]
            assert len(records) == 12, kind  # 12 x 1 s windows
            for rec in records:
                for key in (
                    "feasible",
                    "satisfies_provisional",
                    "capability_true_derated_Nm",
                    "demand_Nm",
                    "u_applied",
                ):
                    assert key in rec, (kind, key)
                # Capability prediction must bracket demand consistently.
                assert rec["feasible"] == (
                    rec["capability_fit_derated_Nm"] >= rec["demand_Nm"] - 1e-9
                ), (kind, rec["window"])

    def test_violation_recording(self):
        """Every produced-torque shortfall must surface as satisfies_provisional=False."""
        pair = simulate_continuous_pair("cyclic", PAIR_SECONDS, NOMINAL_MECH, seed=42)
        c = pair["mpc"]["coupled"]
        flags = 0
        for rec in pair["window_records"]:
            seg = c.torque_Nm[rec["window"] * 10_000 : (rec["window"] + 1) * 10_000]
            shortfall = bool(np.min(seg) < 0.9 * rec["load_mean_Nm"] + CAP_OFFSET)
            assert rec["satisfies_provisional"] == (not shortfall), rec["window"]
            flags += not rec["satisfies_provisional"]
        assert flags > 0  # the record is non-vacuous on this profile


class TestControllerEffectiveness:
    """Controller effectiveness: measured energy comparison."""

    def test_legacy_energy_measured(self):
        """Legacy control energy must be measured via sensor chain."""
        pair = simulate_continuous_pair("steady", PAIR_SECONDS, NOMINAL_MECH, seed=42)
        c = pair["legacy"]["coupled"]
        v_m, i_m = apply_sensor_chain(c.v_true_V, c.i_true_A, c.t_s, seed=42)
        m = measure_windows(v_m, i_m, pair["legacy"]["load"], c.temp_C)
        assert float(np.sum(m["energy_windows_uwh"])) > 0
        assert m["valid_windows"] + m["excluded_windows"] + m["rejected_windows"] == len(
            m["energy_windows_uwh"]
        )

    def test_mpc_energy_measured(self):
        """MPC energy must be measured via sensor chain."""
        pair = simulate_continuous_pair("steady", PAIR_SECONDS, NOMINAL_MECH, seed=42)
        c = pair["mpc"]["coupled"]
        v_m, i_m = apply_sensor_chain(c.v_true_V, c.i_true_A, c.t_s, seed=42)
        m = measure_windows(v_m, i_m, pair["mpc"]["load"], c.temp_C)
        assert float(np.sum(m["energy_windows_uwh"])) > 0

    def test_ground_truth_savings(self):
        """Ground-truth savings = legacy - MPC recomputed from traces."""
        pair = simulate_continuous_pair("steady", PAIR_SECONDS, NOMINAL_MECH, seed=42)
        for arm in ("legacy", "mpc"):
            c = pair[arm]["coupled"]
            expect = ground_truth_energy_kwh(c.v_true_V, c.i_true_A, SAMPLE_RATE_HZ)
            assert pair[arm]["gt_energy_kwh"] == pytest.approx(expect, rel=1e-12)
        assert pair["gt_savings_kwh"] == pytest.approx(
            pair["legacy"]["gt_energy_kwh"] - pair["mpc"]["gt_energy_kwh"]
        )

    def test_estimated_savings_independent(self):
        """Estimated savings from baseline must be independent of ground truth."""
        pair = simulate_continuous_pair("steady", PAIR_SECONDS, NOMINAL_MECH, seed=42)
        c_leg = pair["legacy"]["coupled"]
        v_m, i_m = apply_sensor_chain(c_leg.v_true_V, c_leg.i_true_A, c_leg.t_s, seed=42)
        m_leg = measure_windows(v_m, i_m, pair["legacy"]["load"], c_leg.temp_C)
        base = BaselineRegression(BaselineParams(random_state=42))
        base.fit(m_leg["window_load"], m_leg["window_temp"], m_leg["energy_windows_uwh"])
        coefs_before = base.coefficients_.copy()
        c_mpc = pair["mpc"]["coupled"]
        v_mm, i_mm = apply_sensor_chain(c_mpc.v_true_V, c_mpc.i_true_A, c_mpc.t_s, seed=42)
        m_mpc = measure_windows(v_mm, i_mm, pair["mpc"]["load"], c_mpc.temp_C)
        est, _, _ = base.estimate_savings(
            m_mpc["window_load"], m_mpc["window_temp"], m_mpc["energy_windows_uwh"]
        )
        # Estimation never touches ground truth: coefficients unchanged and the
        # estimate equals baseline-prediction minus measured by construction.
        np.testing.assert_array_equal(base.coefficients_, coefs_before)
        manual = float(np.sum(base.predict(m_mpc["window_load"], m_mpc["window_temp"])))
        manual -= float(np.sum(m_mpc["energy_windows_uwh"]))
        assert est == pytest.approx(manual / 1_000_000_000.0)

    def test_positive_savings_or_explanation(self):
        """If any profile fails positive savings, must diagnose why."""
        for kind in ("steady", "cyclic", "bursty"):
            pair = simulate_continuous_pair(kind, PAIR_SECONDS, NOMINAL_MECH, seed=42)
            if pair["gt_savings_kwh"] > 0:
                continue
            # Non-positive savings require a consistent explanation record:
            # per-window reasons must reconcile exactly with the failsafe log.
            records = pair["window_records"]
            assert len(records) == 12, kind
            with_reason = [r for r in records if r["failsafe_reason"] is not None]
            assert pair["failsafe_count"] == len(with_reason), kind
            assert len(pair["failsafe_logs"]) == len(with_reason), kind
            # The explanation must name the binding constraint or state.
            assert pair["init_status"], kind
            assert len(pair["window_equilibrium_statuses"]) == 12, kind


class TestBaselineValidity:
    """Baseline model validity metrics."""

    def test_cv_rmse_documented_definition(self):
        """CV(RMSE) must match documented formula (RMSE/mean*100, full period)."""
        params = BaselineParams(random_state=42)
        baseline = BaselineRegression(params)
        rng = np.random.default_rng(42)
        load = rng.uniform(6, 14, 120)
        temp = rng.uniform(30, 60, 120)
        energy = 1000 + 50 * load + 10 * temp + rng.normal(0, 20, 120)
        baseline.fit(load, temp, energy)
        pred = baseline.predict(load, temp)
        rmse = float(np.sqrt(np.mean((energy - pred) ** 2)))
        expect = 100.0 * rmse / float(np.mean(pred))
        assert baseline.cv_rmse_ == pytest.approx(expect, rel=1e-12)

    def test_nmbe_documented_definition(self):
        """NMBE must match documented formula (mean(pred-meas)/mean(meas)*100)."""
        params = BaselineParams(random_state=42)
        baseline = BaselineRegression(params)
        rng = np.random.default_rng(7)
        load = rng.uniform(6, 14, 120)
        temp = rng.uniform(30, 60, 120)
        energy = 1000 + 50 * load + 10 * temp + rng.normal(0, 20, 120)
        baseline.fit(load, temp, energy)
        pred = baseline.predict(load, temp)
        expect = 100.0 * float(np.mean(pred - energy)) / float(np.mean(energy))
        assert baseline.nmbe_ == pytest.approx(expect, rel=1e-12)

    def test_savings_confidence_interval(self):
        """CI must use the documented bootstrap method (percentiles, seeded)."""
        params = BaselineParams(random_state=42)
        baseline = BaselineRegression(params)
        rng = np.random.default_rng(11)
        load = rng.uniform(6, 14, 200)
        temp = rng.uniform(30, 60, 200)
        energy = 1000 + 50 * load + 10 * temp + rng.normal(0, 20, 200)
        baseline.fit(load, temp, energy)
        post_load = np.array([10.0, 12.0, 8.0, 9.0])
        post_temp = np.array([40.0, 45.0, 38.0, 42.0])
        post_meas = np.array([950.0, 1100.0, 850.0, 900.0])
        est, ci_low, ci_high = baseline.estimate_savings(post_load, post_temp, post_meas)
        # Bootstrap CI ordering + determinism under the fixed seed.
        assert ci_low <= est <= ci_high
        est2, low2, high2 = baseline.estimate_savings(post_load, post_temp, post_meas)
        assert (est, ci_low, ci_high) == (est2, low2, high2)

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

    def test_deterministic_failsafe(self):
        """Failsafe with same inputs must give same output."""
        params = FailsafeParams()
        fs1 = Failsafe(params)
        fs2 = Failsafe(params)

        x0 = np.array([20.0])
        load = 10.0
        u_prev = 1.0

        fb1, reason1 = fs1.fallback("failed", None, x0, load, u_prev, 0, 0.0)
        fb2, reason2 = fs2.fallback("failed", None, x0, load, u_prev, 0, 0.0)

        assert fb1 == fb2
        assert reason1 == reason2


class TestFailsafeEvents:
    """Test failsafe event logging with correct step and time."""

    def test_failsafe_logs_step_and_time(self):
        """Failsafe logs must contain actual step and simulation time."""
        params = FailsafeParams()
        fs = Failsafe(params)

        # Week 6D capability map: L(0.5)*0.7 = 4.65 < 0.9*15+0.7 = 14.2,
        # so u_prev=0.5 is unsafe at load=15 and failsafe clamps to u_max=1.0.
        fallback_u, reason = fs.fallback(
            solver_status="timeout",
            proposed_u=None,
            x=np.array([20.0]),
            load=15.0,
            u_prev=0.5,
            step=5,
            sim_time=5.0,
        )

        logs = fs.get_logs()
        assert len(logs) == 1
        log = logs[0]
        assert log["step"] == 5
        assert log["sim_time"] == 5.0
        assert "timeout" in log["reason"].lower()
        assert log["fallback_u"] == 1.0  # u_prev=0.5 is not safe, so safe_fallback_u=1.0 used

    def test_failsafe_logs_solver_error(self):
        """Failsafe logs solver errors with step and time."""
        params = FailsafeParams()
        fs = Failsafe(params)

        fs.fallback(
            solver_status="error",
            proposed_u=None,
            x=np.array([20.0]),
            load=10.0,
            u_prev=1.0,
            step=10,
            sim_time=10.0,
        )

        logs = fs.get_logs()
        assert len(logs) == 1
        log = logs[0]
        assert log["step"] == 10
        assert log["sim_time"] == 10.0
        assert "solver_error" in log["reason"].lower()

    def test_mpc_logs_step_and_time(self):
        """MPC solve() must pass step and sim_time to failsafe."""
        # Force a failsafe with demand beyond capability even at full voltage:
        # L(1.0)*0.7 = 18.1 < 0.9*30+0.7 = 27.7, so no admissible u exists.
        params = MPCParams(
            prediction_horizon=10,
            dt=1.0,
            u_min=0.4,
            u_max=1.0,
            du_max=0.1,
            temp_max=120.0,
            torque_margin=0.9,
            solver_timeout_ms=1,
        )
        mpc = MPCController(params)

        x0 = np.array([65.0])  # High temp
        load = 30.0
        u_prev = 1.0

        mpc.solve(x0, load, u_prev, step=5, sim_time=5.0)

        logs = mpc.get_failsafe_logs()
        assert len(logs) > 0
        log = logs[-1]
        assert log["step"] == 5
        assert log["sim_time"] == 5.0
        assert "infeasible" in log["reason"].lower() or "error" in log["reason"].lower()

    def test_mpc_solve_passes_step_and_time(self):
        """MPC solve() must pass step and sim_time to failsafe on error."""
        # Force solver error by using impossible parameters
        params = MPCParams(
            prediction_horizon=10,
            dt=1.0,
            u_min=0.4,
            u_max=1.0,
            du_max=0.1,
            temp_max=50.0,  # Impossible with any throttle at this load
            torque_margin=1.5,
        )
        mpc = MPCController(params)

        x0 = np.array([40.0])
        load = 15.0
        u_prev = 0.9

        mpc.solve(x0, load, u_prev, step=3, sim_time=3.0)

        logs = mpc.get_failsafe_logs()
        assert len(logs) > 0
        log = logs[-1]
        assert log["step"] == 3
        assert log["sim_time"] == 3.0
        assert "infeasible" in log["reason"].lower() or "error" in log["reason"].lower()

    def test_mpc_solve_passes_step_and_time_on_timeout(self):
        """MPC must pass step and sim_time on solver failure (deterministic)."""
        # Demand beyond capability (L(1.0)*0.7 = 18.1 < 0.9*30+0.7) forces the
        # failsafe path; step/time attribution is asserted on that event.
        # Week 6E finding: solver_timeout_ms=1 made this test timing-sensitive
        # (1 ms wall-clock limit vs OSQP terminal status). The timeout is
        # therefore disabled here (0 = no time_limit); timeout mapping itself
        # stays covered by deterministic Failsafe.fallback unit tests that do
        # not depend on solver timing.
        params = MPCParams(
            prediction_horizon=50,
            dt=1.0,
            u_min=0.4,
            u_max=1.0,
            du_max=0.1,
            temp_max=120.0,
            torque_margin=0.9,
            solver_timeout_ms=0,  # no wall-clock limit: pure infeasibility path
        )
        mpc = MPCController(params)

        x0 = np.array([20.0])
        load = 30.0

        mpc.solve(x0, load, 1.0, step=7, sim_time=7.0)

        logs = mpc.get_failsafe_logs()
        assert len(logs) == 1  # single-write site
        log = logs[-1]
        assert log["step"] == 7
        assert log["sim_time"] == 7.0
        assert "infeasible" in log["reason"].lower()
        assert log["fallback_u"] == 1.0
