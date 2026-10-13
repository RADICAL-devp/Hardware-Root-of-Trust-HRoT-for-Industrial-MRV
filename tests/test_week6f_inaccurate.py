"""Week 6F tests: optimal_inaccurate acceptance policy (Option 2, test-first).

Policy (approved): an `optimal_inaccurate` solver result is ACCEPTED only if
the returned u passes the same full safety check the failsafe uses
(temperature + torque capability + bounds); otherwise the documented
fallback runs. Acceptance logs a distinct `solver_optimal_inaccurate`
event with a saturating counter; re-validation failure logs separately.
`user_limit` terminal statuses map to a named timeout reason, never
"unknown". Production uses an iteration limit only (no wall-clock limit);
wall-clock limits remain a test-only override.
"""

import contextlib

import numpy as np
import pytest

from control.mpc import MPCController, MPCParams


def _mpc(**over):
    kw = {
        "prediction_horizon": 10,
        "dt": 1.0,
        "u_min": 0.4,
        "u_max": 1.0,
        "du_max": 0.1,
        "temp_max": 120.0,
        "torque_margin": 0.9,
        "solver_timeout_ms": 0,
    }
    kw.update(over)
    return MPCController(MPCParams(**kw), profile="p", controller_id="c")


@contextlib.contextmanager
def _force_status(mpc, status, u_value):
    """Emulate one solver terminal status (test-only seam, no source change)."""
    real_problem = mpc.problem

    class _Shim:
        def solve(self, **kwargs):
            mpc.problem.status = status
            mpc.u_var.value = np.full((1, mpc.N), float(u_value))
            mpc.x_var.value = np.zeros((1, mpc.N + 1))

    mpc.problem = _Shim()
    try:
        yield mpc
    finally:
        mpc.problem = real_problem


class TestOptimalInaccuratePolicy:
    """Option 2 acceptance with mandatory re-validation."""

    def test_accept_path_safe_u_applied_and_counted(self):
        mpc = _mpc()
        before = mpc.get_inaccurate_count()
        with _force_status(mpc, "optimal_inaccurate", 0.9):
            u = mpc.solve(np.array([0.0]), 10.0, 1.0, step=2, sim_time=2.0)
        assert u == pytest.approx(0.9)
        logs = mpc.get_failsafe_logs()
        assert len(logs) == 1
        assert logs[0]["reason"] == "solver_optimal_inaccurate"
        assert logs[0]["fallback_u"] == pytest.approx(0.9)
        assert logs[0]["safety_ok"] is True
        assert mpc.get_inaccurate_count() == before + 1

    def test_revalidation_fail_falls_back_with_distinct_reason(self):
        mpc = _mpc()
        with _force_status(mpc, "optimal_inaccurate", 0.4):
            # u=0.4 at load 15 is capability-unsafe: L(0.4)*0.7=3.07 < 14.2.
            u = mpc.solve(np.array([0.0]), 15.0, 1.0, step=3, sim_time=3.0)
        assert u == pytest.approx(1.0)
        logs = mpc.get_failsafe_logs()
        assert len(logs) == 1
        assert logs[0]["reason"] == "solver_optimal_inaccurate_revalidation_failed"
        # safety_ok describes the APPLIED fallback (u=1.0, safe here); the
        # failure is carried by the distinct reason string, not by this flag.
        assert logs[0]["safety_ok"] is True
        assert logs[0]["fallback_u"] == pytest.approx(1.0)

    def test_user_limit_has_named_reason(self):
        from control.failsafe import Failsafe, FailsafeParams

        fs = Failsafe(FailsafeParams())
        _, reason = fs.fallback(
            solver_status="user_limit",
            proposed_u=None,
            x=np.array([0.0]),
            load=10.0,
            u_prev=1.0,
            step=0,
            sim_time=0.0,
        )
        assert "unknown" not in reason.lower()
        assert "timeout" in reason.lower()

    def test_inaccurate_counter_saturates(self):
        mpc = _mpc()
        mpc.failsafe.inaccurate_accepted_cnt = 0xFFFE
        for _ in range(3):
            with _force_status(mpc, "optimal_inaccurate", 0.9):
                mpc.solve(np.array([0.0]), 10.0, 1.0, step=0, sim_time=0.0)
        assert mpc.get_inaccurate_count() == 0xFFFF
