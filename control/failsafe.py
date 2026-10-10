"""Failsafe controller: fall back to last safe setpoint on solver failure."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np


@dataclass(frozen=True)
class FailsafeParams:
    """Failsafe configuration."""

    safe_fallback_u: float = 1.0  # [pu] conservative safe throttle
    u_min: float = 0.4  # [pu] minimum throttle
    u_max: float = 1.0  # [pu] maximum throttle
    temp_max: float = 120.0  # [°C] max temperature
    t_amb: float = 25.0  # [°C] ambient
    torque_margin: float = 1.1  # [ratio] torque margin
    s_max: float = 0.15  # [pu] max slip
    k_torque: float = 12.5  # [Nm/pu] torque constant
    s_rated: float = 0.04  # [pu] rated slip


@dataclass
class FailsafeEvent:
    """Single failsafe activation record."""

    step: int
    sim_time: float
    reason: str
    fallback_u: float
    x: np.ndarray
    load: float
    u_prev: float


class Failsafe:
    """Failsafe logic with event logging."""

    def __init__(self, params: FailsafeParams):
        self.params = params
        self._logs: List[FailsafeEvent] = []

    def fallback(
        self,
        solver_status: str,
        proposed_u: Optional[float],
        x: np.ndarray,
        load: float,
        u_prev: float,
        step: int,
        sim_time: float,
    ) -> tuple[float, str]:
        """Determine fallback control action and reason.

        Args:
            solver_status: "optimal", "timeout", "infeasible", "error", etc.
            proposed_u: solver's proposed output (may be None or invalid)
            x: current state [ΔT, Δs]
            load: load torque [N·m]
            u_prev: previous throttle
            step: control step index
            sim_time: simulation time [s]

        Returns:
            (fallback_u, reason_string)
        """
        # Check failure modes in priority order
        reason = ""

        if solver_status in ("timeout", "timed_out"):
            reason = "solver_timeout"
        elif solver_status in ("infeasible", "infeasible_inaccurate"):
            reason = "solver_infeasible"
        elif solver_status in ("failed", "error", "exception"):
            reason = "solver_error"
        elif proposed_u is not None and not np.isfinite(proposed_u):
            reason = "non_finite_output"
        elif proposed_u is not None and (
            proposed_u < self.params.u_min - 1e-6 or proposed_u > self.params.u_max + 1e-6
        ):
            reason = "throttle_bounds_violation"
        elif proposed_u is not None and self._violates_temperature(x, proposed_u, load):
            reason = "temperature_constraint_violation"
        elif proposed_u is not None and self._violates_torque_margin(x, proposed_u, load):
            reason = "torque_margin_violation"
        elif proposed_u is not None and self._violates_rate_limit(u_prev, proposed_u):
            reason = "rate_limit_violation"
        else:
            reason = "unknown"

        # Determine fallback value
        if reason in ("solver_timeout", "solver_error"):
            # Use last known safe setpoint
            fallback_u = u_prev
        elif reason == "solver_infeasible":
            # Problem infeasible - use conservative safe value
            fallback_u = self.params.safe_fallback_u
        else:
            # Output violation - use safe conservative
            fallback_u = self.params.safe_fallback_u

        # Validate fallback itself
        if not self._is_safe(fallback_u, x, load):
            # Even safe_fallback_u might be unsafe - clamp to bounds
            fallback_u = np.clip(fallback_u, self.params.u_min, self.params.u_max)
            if not self._is_safe(fallback_u, x, load):
                # Last resort: minimum throttle that might work
                fallback_u = self.params.u_max  # full voltage for max torque
                reason += "_fallback_clamped"

        # Log event
        self._log_event(step, sim_time, reason, fallback_u, x, load, u_prev)

        return fallback_u, reason

    def _is_safe(self, u: float, x: np.ndarray, load: float) -> bool:
        """Check if a throttle value is safe for current conditions."""
        # Temperature check
        delta_T = x[0]
        T = self.params.t_amb + delta_T
        if T > self.params.temp_max:
            return False

        # Torque margin check (linearized steady-state)
        # 9.17*u - 83.33*Δs ≥ torque_margin * load
        torque_cap_lin = K_TORQUE_U_LIN * u + K_TORQUE_S_LIN * x[1]
        if torque_cap_lin < self.params.torque_margin * load:
            return False

        # Bounds check
        if u < self.params.u_min or u > self.params.u_max:
            return False

        return True

    def _violates_temperature(self, x: np.ndarray, u: float, load: float) -> bool:
        """Check if proposed u would violate temperature constraint."""
        # Predict next temperature (one step)
        delta_T_next = A_THERMAL * x[0] + B_THERMAL * u
        T_next = self.params.t_amb + delta_T_next
        return T_next > self.params.temp_max

    def _violates_torque_margin(self, x: np.ndarray, u: float, load: float) -> bool:
        """Check if proposed u violates torque margin (linearized)."""
        torque_cap_lin = K_TORQUE_U_LIN * u + K_TORQUE_S_LIN * x[1]
        return torque_cap_lin < self.params.torque_margin * load

    def _violates_rate_limit(self, u_prev: float, u_proposed: float) -> bool:
        """Check rate limit violation."""
        # This is checked in MPC, but double-check here
        du_max = 0.1  # Should match MPCParams
        return abs(u_proposed - u_prev) > du_max + 1e-6

    def _log_event(
        self,
        step: int,
        sim_time: float,
        reason: str,
        fallback_u: float,
        x: np.ndarray,
        load: float,
        u_prev: float,
    ):
        """Record failsafe activation."""
        event = FailsafeEvent(
            step=step,
            sim_time=sim_time,
            reason=reason,
            fallback_u=fallback_u,
            x=x.copy(),
            load=load,
            u_prev=u_prev,
        )
        self._logs.append(event)

    def log_event(self, step: int, sim_time: float, reason: str, fallback_u: float):
        """Manual log entry (e.g., from MPC)."""
        # Create minimal event for external logging
        event = FailsafeEvent(
            step=step,
            sim_time=sim_time,
            reason=reason,
            fallback_u=fallback_u,
            x=np.zeros(2),
            load=0.0,
            u_prev=0.0,
        )
        self._logs.append(event)

    def get_logs(self) -> List[Dict[str, Any]]:
        """Get all failsafe events as list of dicts."""
        return [
            {
                "step": e.step,
                "sim_time": e.sim_time,
                "reason": e.reason,
                "fallback_u": e.fallback_u,
                "x": e.x.tolist(),
                "load": e.load,
                "u_prev": e.u_prev,
            }
            for e in self._logs
        ]

    def clear_logs(self):
        """Clear event log."""
        self._logs.clear()


# Constants needed for failsafe temperature prediction
A_THERMAL = 0.99984
B_THERMAL = 0.023

# Linearized torque coefficients (same as MPC)
K_TORQUE_U_LIN = 12.0
K_TORQUE_S_LIN = -109.09090909090909
