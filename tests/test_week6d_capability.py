"""Week 6D tests: coupled-plant torque-capability representation (test-first).

The affine MPC/failsafe constraint is re-derived from peak torque of the
actual equivalent circuit over the coupled plant's operating domain:

    L(u) = CAP_C0 + CAP_C1 * u  <=  T_peak(u)   (fit, conservative via offset)
    L(u) * (1 - ripple) >= torque_margin * load + CAP_OFFSET   (convex in u)

L(u) is a CAPABILITY map (maximum torque the motor can produce at throttle
u), derated by ripple as stall headroom. It is NOT a prediction of produced
torque and does NOT establish produced-minima compliance: the instantaneous
acceptance metric on produced T_e is unchanged and reported separately.
"""

import numpy as np
import pytest

from plant.motor import MotorParams


class TestCapabilityMap:
    """Representation validity against the real circuit (never the old surrogate)."""

    def test_peak_torque_reproduces_circuit(self):
        from control.capability import peak_torque_Nm

        mp = MotorParams()
        # Spot values measured during design (rounded here, asserted tightly).
        assert peak_torque_Nm(1.0, mp, 1.0) == pytest.approx(27.449, abs=0.01)
        assert peak_torque_Nm(0.4, mp, 1.0) == pytest.approx(4.392, abs=0.01)

    def test_affine_fit_error_bound_holds_on_heldout_grid(self):
        from control.capability import CAP_C0, CAP_C1, peak_torque_Nm

        mp = MotorParams()
        worst_over = 0.0
        for u in np.arange(0.41, 1.0, 0.02):  # held-out: fit used 0.02-offset grid
            over = (CAP_C0 + CAP_C1 * u) - peak_torque_Nm(float(u), mp, 1.0)
            worst_over = max(worst_over, over)
        # Design measurement: worst held-out overestimation ~0.876 Nm.
        assert worst_over <= 1.0

    def test_derated_fit_is_conservative_everywhere_on_domain(self):
        from control.capability import CAP_C0, CAP_C1, CAP_OFFSET, peak_torque_Nm

        mp = MotorParams()
        for u in np.arange(0.4, 1.001, 0.01):
            for r in (1.0, 1.005, 1.01):
                fit_derated = (CAP_C0 + CAP_C1 * float(u)) * 0.7
                true_derated = peak_torque_Nm(float(u), mp, r) * 0.7
                # Guarantee: derated fit minus offset never exceeds true derated.
                assert fit_derated - CAP_OFFSET <= true_derated + 1e-9

    def test_feasibility_shape(self):
        from control.capability import meets_demand

        # u=1.0 covers the whole declared envelope; u=0.4 covers none of it.
        for load in (6.0, 10.0, 14.0, 17.0):
            assert meets_demand(load, 1.0) is True
            assert meets_demand(load, 0.4) is False
        # Steady 10 Nm becomes feasible around u~0.70 (capability floor).
        assert meets_demand(10.0, 0.65) is False
        assert meets_demand(10.0, 0.75) is True


class TestMPCUsesCapability:
    """Optimizer behavior under the re-derived constraint (convex QP preserved)."""

    def test_mpc_reduces_throttle_where_capability_allows(self):
        from control.mpc import MPCController, MPCParams

        mpc = MPCController(MPCParams())
        # Load 10 Nm from u_prev=1.0: rate limit binds first step at u=0.9.
        u1 = mpc.solve(np.array([0.0]), 10.0, 1.0, step=0, sim_time=0.0)
        assert u1 == pytest.approx(0.9, abs=1e-3)
        # Walk down: controller keeps reducing while capability allows.
        u, u_prev = u1, u1
        for k in range(1, 8):
            u = mpc.solve(np.array([0.0]), 10.0, u_prev, step=k, sim_time=float(k))
            u_prev = u
        assert u < 0.9
        assert u >= 0.4

    def test_mpc_falls_back_with_report_on_infeasible_demand(self):
        from control.mpc import MPCController, MPCParams

        mpc = MPCController(MPCParams(), profile="steady", controller_id="mpc-t")
        before = len(mpc.get_failsafe_logs())
        # Load far beyond capability even at full voltage.
        u = mpc.solve(np.array([0.0]), 30.0, 1.0, step=0, sim_time=0.0)
        assert u == pytest.approx(1.0)
        logs = mpc.get_failsafe_logs()[before:]
        assert len(logs) == 1  # single-write site preserved
        assert "infeasible" in logs[0]["reason"]
        assert logs[0]["safety_ok"] is False  # not certified safe
        assert logs[0]["fallback_u"] == pytest.approx(1.0)

    def test_failsafe_capability_matches_mpc(self):
        from control.capability import meets_demand
        from control.failsafe import Failsafe, FailsafeParams

        fs = Failsafe(FailsafeParams())
        # Agreement between the two consumers of the same representation.
        for load, u in ((6.0, 0.6), (10.0, 0.8), (14.0, 0.7), (17.0, 1.0)):
            safe = fs._is_safe(u, np.array([0.0]), load)
            assert safe == meets_demand(load, u)
