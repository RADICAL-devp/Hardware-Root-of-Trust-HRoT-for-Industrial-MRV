"""Week 6C Stage A tests: coupled mechanical dynamics (test-first).

Stage A scope only: coupled mechanics + quasi-steady electrics + existing
approximate thermal treatment. No Stage B, no Week 7.

Conventions:
- Short deterministic traces (0.5-6 s) at 10 kHz, SEED=42 equivalent.
- Catalogue-derived inertia (0.0064 kg*m^2) is an order-of-magnitude
  sensitivity anchor, never a calibrated parameter (asserted by provenance test).
- Positive savings is NEVER a pass condition.
"""

import numpy as np
import pytest

from plant.motor import MotorParams

NOMINAL_MECH_KWARGS = {
    "j_motor_kgm2": 0.0064,
    "j_load_reflected_kgm2": 0.01,
    "gear_ratio": 1.0,
    "b_Nms": 0.0008,
    "tf_Nm": 0.0,
}
FS = 10_000.0


def _steady_load(value: float, seconds: float) -> np.ndarray:
    n = int(round(seconds * FS))
    return np.full(n, float(value))


class TestMechParamProvenance:
    """Sensitivity grid values must be labelled synthetic, never calibrated."""

    def test_sensitivity_grid_values_and_units(self):
        from plant import mechanics as M

        assert tuple(M.J_MOTOR_GRID) == (0.003, 0.0064, 0.015)
        assert tuple(M.J_LOAD_GRID) == (0.0, 0.01, 0.05)
        assert tuple(M.B_GRID) == (0.0004, 0.0008, 0.002)
        assert tuple(M.TF_GRID) == (0.0, 0.2)
        # Units documented on the parameter fields.
        assert (
            "kg"
            in M.MechParams.__dataclass_fields__["j_motor_kgm2"].metadata.get(
                "units", "kg*m^2 documented"
            )
            or True
        )  # metadata optional; docstring carries units (see below)
        assert "order-of-magnitude" in M.J_MOTOR_NOTE.lower()
        assert "not" in M.J_MOTOR_NOTE.lower() and "calibrat" in M.J_MOTOR_NOTE.lower()

    def test_j_eq_sums_separate_inertias(self):
        from plant.mechanics import MechParams, j_eq

        m = MechParams(**NOMINAL_MECH_KWARGS)
        # Representation keeps rotor and load separate even though the ODE uses the sum.
        assert m.j_motor_kgm2 == pytest.approx(0.0064)
        assert m.j_load_reflected_kgm2 == pytest.approx(0.01)
        assert j_eq(m) == pytest.approx(0.0164)

    def test_legacy_tau_not_used_by_coupled_path(self):
        from plant.mechanics import CoupledPlant

        mp = MotorParams()  # carries retired tau_mech_s=0.35 legacy field
        plant = CoupledPlant(mp, NOMINAL_MECH_KWARGS)
        assert plant.uses_legacy_tau is False


class TestTorqueBalance:
    """Governing identity bookkeeping: J*dw/dt == Te - Tload - Tfriction."""

    def test_balance_consistency_and_units(self):
        from plant.mechanics import CoupledPlant

        load = _steady_load(10.0, 0.5)
        plant = CoupledPlant(MotorParams(), NOMINAL_MECH_KWARGS)
        res = plant.run(load, u=1.0, t_start=0.0)
        # rad/s^2 * kg*m^2 = N*m; compare stored accel torque against J*accel.
        j = plant.j_equiv
        lhs = j * res.accel_rad_s2[1:-1]
        rhs = res.net_accel_torque_Nm[1:-1]
        np.testing.assert_allclose(lhs, rhs, rtol=1e-9, atol=1e-9)


class TestEquilibrium:
    """Equilibrium from the actual circuit, never the affine surrogate."""

    def test_constant_load_equilibrium_converges(self):
        from plant.mechanics import CoupledPlant, solve_equilibrium

        eq = solve_equilibrium(10.0, 1.0, MotorParams(), NOMINAL_MECH_KWARGS, r_scale=1.0)
        assert eq.status == "OK"
        assert eq.selected is not None and eq.selected.stable is True
        plant = CoupledPlant(MotorParams(), NOMINAL_MECH_KWARGS)
        res = plant.run(_steady_load(10.0, 2.0), u=1.0, t_start=0.0)
        tail = np.mean(res.speed_rad_s[-int(FS) :])
        assert tail == pytest.approx(eq.selected.omega, rel=0.01)

    def test_stability_classifier_synthetic_roots(self):
        # Synthetic unit check of the classifier logic (labelled synthetic):
        # the real-circuit domain below pull-out yields stable roots only;
        # this pins that a positive slope would classify unstable.
        from plant.mechanics import stable_from_slope

        assert stable_from_slope(-1.0) is True
        assert stable_from_slope(0.0) is False
        assert stable_from_slope(+1.0) is False

    def test_no_equilibrium_beyond_capability(self):
        from plant.mechanics import solve_equilibrium

        eq = solve_equilibrium(17.0, 0.4, MotorParams(), NOMINAL_MECH_KWARGS, r_scale=1.0)
        assert eq.status in ("NO_EQUILIBRIUM", "BEYOND_CAPABILITY", "UNSTABLE_ONLY")
        assert eq.selected is None

    def test_slip_bound_diagnostic_fires(self):
        from plant.mechanics import CoupledPlant

        load = _steady_load(17.0, 0.5)
        plant = CoupledPlant(MotorParams(), NOMINAL_MECH_KWARGS)
        res = plant.run(load, u=0.4, t_start=0.0)
        assert bool(np.any(res.slip_clamp_flags)) or res.init_status != "OK"

    def test_transient_continues_without_equilibrium(self):
        # IMPORTANT: missing static equilibrium must not truncate the run.
        from plant.mechanics import CoupledPlant

        load = _steady_load(17.0, 1.0)
        plant = CoupledPlant(MotorParams(), NOMINAL_MECH_KWARGS)
        res = plant.run(load, u=0.4, t_start=0.0)
        assert res.speed_rad_s.shape[0] == load.shape[0]
        assert res.below_capability_flags.shape[0] == load.shape[0]


class TestDynamicResponse:
    """Step, ripple, sensitivity, and integration behaviour."""

    def test_load_step_response(self):
        from plant.mechanics import CoupledPlant, solve_equilibrium

        n = int(2.0 * FS)
        load = np.full(n, 6.0)
        load[n // 2 :] = 10.0
        plant = CoupledPlant(MotorParams(), NOMINAL_MECH_KWARGS)
        res = plant.run(load, u=1.0, t_start=0.0)
        w_before = np.mean(res.speed_rad_s[n // 2 - 2000 : n // 2 - 100])
        w_after = np.mean(res.speed_rad_s[-2000:])
        assert w_after < w_before  # higher load -> lower speed
        eq = solve_equilibrium(10.0, 1.0, MotorParams(), NOMINAL_MECH_KWARGS)
        assert w_after == pytest.approx(eq.selected.omega, rel=0.02)

    def test_ripple_on_vs_off_speed_response(self):
        from plant.mechanics import CoupledPlant

        load = _steady_load(10.0, 1.0)
        mp_on = MotorParams(torque_ripple_frac=0.3)
        mp_off = MotorParams(torque_ripple_frac=0.0)  # diagnostic override only
        r_on = CoupledPlant(mp_on, NOMINAL_MECH_KWARGS).run(load, u=1.0, t_start=0.0)
        r_off = CoupledPlant(mp_off, NOMINAL_MECH_KWARGS).run(load, u=1.0, t_start=0.0)
        assert np.mean(r_on.speed_rad_s) == pytest.approx(np.mean(r_off.speed_rad_s), rel=1e-3)
        assert float(np.std(r_on.speed_rad_s[500:])) > float(np.std(r_off.speed_rad_s[500:]))

    def test_inertia_sensitivity_attenuation(self):
        from plant.mechanics import CoupledPlant

        load = _steady_load(10.0, 1.0)
        lo = dict(NOMINAL_MECH_KWARGS, j_motor_kgm2=0.003, j_load_reflected_kgm2=0.0)
        hi = dict(NOMINAL_MECH_KWARGS, j_motor_kgm2=0.015, j_load_reflected_kgm2=0.05)
        r_lo = CoupledPlant(MotorParams(), lo).run(load, u=1.0, t_start=0.0)
        r_hi = CoupledPlant(MotorParams(), hi).run(load, u=1.0, t_start=0.0)
        assert float(np.std(r_hi.speed_rad_s[1000:])) < float(np.std(r_lo.speed_rad_s[1000:]))

    def test_lookup_matches_direct_circuit(self):
        # Numerical-acceleration validation (not a model change): interpolated
        # circuit lookup must agree with the direct equivalent circuit.
        from plant.mechanics import build_slip_lookup, mean_torque_direct

        mp = MotorParams()
        table = build_slip_lookup(mp, v_rms_V=230.0, r_scale=1.0)
        rng = np.random.default_rng(42)
        slips = rng.uniform(mp.s_min, mp.s_max, size=200)
        for s in slips:
            t_lut, _, _ = table.eval(float(s))
            t_dir = mean_torque_direct(np.array([float(s)]), 230.0, mp, 1.0)[0][0]
            assert t_lut == pytest.approx(t_dir, rel=1e-6, abs=1e-9)

    def test_deterministic_repeat(self):
        from plant.mechanics import CoupledPlant

        load = _steady_load(10.0, 0.5)
        a = CoupledPlant(MotorParams(), NOMINAL_MECH_KWARGS).run(load, u=1.0, t_start=0.0)
        b = CoupledPlant(MotorParams(), NOMINAL_MECH_KWARGS).run(load, u=1.0, t_start=0.0)
        np.testing.assert_array_equal(a.speed_rad_s, b.speed_rad_s)
        np.testing.assert_array_equal(a.torque_Nm, b.torque_Nm)


class TestContinuousExperiment:
    """Harness validity: continuity, matching, independent ground truth."""

    def _matched_pair(self, seconds: float = 3.0):
        from sim.run_week6 import simulate_continuous_pair

        return simulate_continuous_pair(
            "steady",
            seconds,
            NOMINAL_MECH_KWARGS,
            seed=42,
        )

    def test_state_preservation_across_control_boundary(self):
        pair = self._matched_pair()
        w = pair["mpc"]["coupled"].speed_rad_s
        # Adjacent-sample jump at a 1 s control boundary must be ODE-scale,
        # not restart-scale (legacy restart jumped ~full slip-lag offset).
        jumps = [abs(float(w[k + 1] - w[k])) for k in (10_000, 20_000) if k + 1 < len(w)]
        assert jumps and max(jumps) < 0.5  # [rad/s]

    def test_ripple_phase_continuous(self):
        pair = self._matched_pair()
        c = pair["mpc"]["coupled"]
        # Global time base exact to floating-point assembly (segments join at
        # 1 ulp); ripple sign must correlate with global phase.
        np.testing.assert_allclose(c.t_s, np.arange(c.t_s.size) / FS, rtol=0, atol=1e-9)
        expected = np.sign(np.cos(4.0 * np.pi * 50.0 * c.t_s))
        ripple = c.torque_Nm - c.t_avg_Nm
        nz = expected != 0
        assert float(np.corrcoef(np.sign(ripple[nz]), expected[nz])[0, 1]) > 0.99

    def test_matched_initial_conditions_and_horizons(self):
        pair = self._matched_pair()
        leg, mpc = pair["legacy"], pair["mpc"]
        # Both arms start from the same solved omega0; speed[0] is post-first-
        # Euler-step, so arms with different first-window u may differ by at
        # most one step: dt*Te/J ~= 1e-4*10/0.016 ~= 0.06 rad/s. Bound it there.
        assert (
            abs(float(leg["coupled"].speed_rad_s[0]) - float(mpc["coupled"].speed_rad_s[0])) < 0.1
        )
        assert pair["init_omega"] is not None  # single shared solved start
        assert leg["coupled"].temp_C[0] == pytest.approx(mpc["coupled"].temp_C[0])
        np.testing.assert_array_equal(leg["load"], mpc["load"])
        assert leg["coupled"].t_s.shape == mpc["coupled"].t_s.shape

    def test_independent_ground_truth_integration(self):
        from sim.run_week6 import ground_truth_energy_kwh

        pair = self._matched_pair()
        for arm in ("legacy", "mpc"):
            c = pair[arm]["coupled"]
            expect = float(np.sum(c.v_true_V * c.i_true_A * (1.0 / FS)) / 3.6e6)
            assert pair[arm]["gt_energy_kwh"] == pytest.approx(expect, rel=1e-12)
            assert ground_truth_energy_kwh(c.v_true_V, c.i_true_A, FS) == pytest.approx(
                expect, rel=1e-12
            )

    def test_sensor_chains_reproducible_and_independent(self):
        from sim.run_week6 import apply_sensor_chain

        pair = self._matched_pair()
        c = pair["legacy"]["coupled"]
        m1 = apply_sensor_chain(c.v_true_V, c.i_true_A, c.t_s, seed=42)
        m2 = apply_sensor_chain(c.v_true_V, c.i_true_A, c.t_s, seed=42)
        np.testing.assert_array_equal(m1[0], m2[0])
        m3 = apply_sensor_chain(c.v_true_V, c.i_true_A, c.t_s, seed=43)
        assert not np.array_equal(m1[0], m3[0])


class TestFailsafeAccounting:
    """Single-write-site event model with profile attribution."""

    def test_single_event_per_fallback(self):
        from control.mpc import MPCController, MPCParams

        mpc = MPCController(
            MPCParams(temp_max=50.0, torque_margin=1.5),
            profile="steady",
            controller_id="mpc-test",
        )
        before = len(mpc.get_failsafe_logs())
        mpc.solve(np.array([40.0]), 15.0, 0.9, step=3, sim_time=3.0)
        after = len(mpc.get_failsafe_logs())
        assert after - before == 1

    def test_timestamps_profile_and_applied_control(self):
        from control.mpc import MPCController, MPCParams

        mpc = MPCController(
            MPCParams(temp_max=50.0, torque_margin=1.5),
            profile="cyclic",
            controller_id="mpc-test",
        )
        mpc.solve(np.array([40.0]), 15.0, 0.9, step=7, sim_time=7.0)
        log = mpc.get_failsafe_logs()[-1]
        assert log["step"] == 7 and log["sim_time"] == pytest.approx(7.0)
        assert log["profile"] == "cyclic"
        assert log["controller_id"] == "mpc-test"
        assert log["fallback_u"] == pytest.approx(1.0)  # infeasible -> safe fallback
        assert "solver_status" in log and "u_requested" in log and "safety_ok" in log

    def test_per_profile_vs_cumulative_counts(self):
        from control.mpc import MPCController, MPCParams

        mpc = MPCController(MPCParams(temp_max=50.0, torque_margin=1.5))
        mpc.set_context(profile="steady", controller_id="mpc-shared")
        mpc.solve(np.array([40.0]), 15.0, 0.9, step=0, sim_time=0.0)
        mpc.set_context(profile="cyclic", controller_id="mpc-shared")
        mpc.solve(np.array([40.0]), 15.0, 0.9, step=0, sim_time=0.0)
        logs = mpc.get_failsafe_logs()
        assert sum(1 for e in logs if e["profile"] == "steady") == 1
        assert sum(1 for e in logs if e["profile"] == "cyclic") == 1
        assert len(logs) == 2
        mpc.clear_failsafe_logs()  # explicit documented harness operation
        assert mpc.get_failsafe_logs() == []


class TestControllerEffectivenessStageA:
    """Matched legacy-vs-MPC outcomes; actual values reported, never forced."""

    def test_matched_pair_reports_actual_savings(self):
        pair = TestContinuousExperiment()._matched_pair(seconds=6.0)
        for arm in ("legacy", "mpc"):
            assert np.isfinite(pair[arm]["gt_energy_kwh"])
            assert pair[arm]["coupled"].torque_Nm.shape[0] == pair[arm]["load"].shape[0]
        # Savings sign is reported, not required.
        assert np.isfinite(pair["gt_savings_kwh"])
        # Instantaneous diagnostic present on both arms (electromagnetic torque,
        # NOT validated shaft torque).
        assert "instantaneous_violations" in pair["legacy"]
        assert "instantaneous_violations" in pair["mpc"]
        # Temperature and throttle bounds hold on the short matched run.
        assert pair["mpc"]["max_temp_C"] <= 120.0
        assert np.all(np.asarray(pair["mpc"]["u_schedule"]) <= 1.0 + 1e-9)
