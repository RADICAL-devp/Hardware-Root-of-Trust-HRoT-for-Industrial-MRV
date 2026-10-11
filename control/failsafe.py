"""Failsafe controller: fall back to last safe setpoint on solver failure."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np

from control.torque_model import TORQUE_SAFETY_OFFSET, torque_capability_coeffs


@dataclass(frozen=True)
class FailsafeParams:
    """Failsafe configuration."""

    safe_fallback_u: float = 1.0  # [pu] conservative safe throttle
    u_min: float = 0.4  # [pu] minimum throttle
    u_max: float = 1.0  # [pu] maximum throttle
    temp_max: float = 120.0  # [°C] max temperature
    t_amb: float = 25.0  # [°C] ambient
    torque_margin: float = 0.9  # [ratio] torque margin (calibrated)
    ripple_frac: float = 0.3  # [ratio] torque ripple fraction (plant model)


@dataclass
class FailsafeEvent:
    """Single failsafe activation record (one record per fallback, see fallback())."""

    step: int
    sim_time: float
    reason: str
    fallback_u: float
    x: np.ndarray
    load: float
    u_prev: float
    # Stage A accounting fields (defaults preserve old constructions).
    profile: str = ""
    controller_id: str = ""
    solver_status: str = ""
    u_requested: float | None = None
    safety_ok: bool = False
    detail: str = ""


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
        profile: str = "",
        controller_id: str = "",
        detail: str = "",
    ) -> tuple[float, str]:
        """Determine fallback control action and reason.

        SINGLE WRITE SITE: every fallback appends exactly one event here.
        Callers must not log a second record for the same fallback.

        Args:
            solver_status: raw solver status ("optimal", "timeout", "infeasible",
                "infeasible_inaccurate", "time_limit", "time_limit_inaccurate",
                "error", "exception", etc.).
            proposed_u: solver's proposed output (may be None or invalid)
            x: current state [ΔT]
            load: load torque [N·m]
            u_prev: previous throttle
            step: control step index
            sim_time: simulation time [s]
            profile: load-profile identifier for per-profile attribution
            controller_id: controller instance identifier
            detail: free-form diagnostic detail (e.g. exception text)

        Returns:
            (fallback_u, reason_string)
        """
        # Check failure modes in priority order
        reason = ""
        raw = str(solver_status)

        if raw in ("timeout", "timed_out", "time_limit", "time_limit_inaccurate"):
            reason = "solver_timeout"
        elif raw in ("infeasible", "infeasible_inaccurate"):
            reason = "solver_infeasible"
        elif raw in ("failed", "error", "exception"):
            reason = "solver_error"
        elif proposed_u is not None and not np.isfinite(proposed_u):
            reason = "non_finite_output"
        elif proposed_u is not None:
            u_min_v = self.params.u_min - 1e-6
            u_max_v = self.params.u_max + 1e-6
            if proposed_u < u_min_v or proposed_u > u_max_v:
                reason = "throttle_bounds_violation"
        elif proposed_u is not None and self._violates_temperature(x, proposed_u):
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

        safety_ok = bool(self._is_safe(fallback_u, x, load))

        # Log event (sole write site for this fallback)
        self._log_event(
            step,
            sim_time,
            reason,
            fallback_u,
            x,
            load,
            u_prev,
            profile=profile,
            controller_id=controller_id,
            solver_status=raw,
            u_requested=proposed_u,
            safety_ok=safety_ok,
            detail=detail,
        )

        return fallback_u, reason

    def _is_safe(self, u: float, x: np.ndarray, load: float) -> bool:
        """Check if a throttle value is safe for current conditions."""
        # Temperature check
        delta_T = x[0]
        T = self.params.t_amb + delta_T
        if T > self.params.temp_max:
            return False

        # Torque margin check (using calibrated model with safety offset and ripple)
        # T_min = T_avg * (1 - ripple_frac) >= torque_margin * load + TORQUE_SAFETY_OFFSET
        # T_avg = a + b * u
        # (a + b * u) * (1 - ripple_frac) >= torque_margin * load + TORQUE_SAFETY_OFFSET
        a, b = torque_capability_coeffs(load)
        torque_cap_min = (a + b * u) * (1.0 - self.params.ripple_frac)
        if torque_cap_min < self.params.torque_margin * load + TORQUE_SAFETY_OFFSET:
            return False

        # Bounds check
        if u < self.params.u_min or u > self.params.u_max:
            return False

        return True

    def _violates_temperature(self, x: np.ndarray, u: float) -> bool:
        """Check if proposed u would violate temperature constraint."""
        # Predict next temperature (one step)
        delta_T_next = A_THERMAL * x[0] + B_THERMAL * u
        T_next = self.params.t_amb + delta_T_next
        return T_next > self.params.temp_max

    def _violates_torque_margin(self, x: np.ndarray, u: float, load: float) -> bool:
        """Check if proposed u violates torque margin."""
        a, b = torque_capability_coeffs(load)
        torque_cap_min = (a + b * u) * (1.0 - self.params.ripple_frac)
        return torque_cap_min < self.params.torque_margin * load + TORQUE_SAFETY_OFFSET

    def _violates_rate_limit(self, u_prev: float, u_proposed: float) -> bool:
        """Check rate limit violation."""
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
        profile: str = "",
        controller_id: str = "",
        solver_status: str = "",
        u_requested: Optional[float] = None,
        safety_ok: bool = False,
        detail: str = "",
    ):
        """Record failsafe activation (exactly one record per fallback)."""
        event = FailsafeEvent(
            step=step,
            sim_time=sim_time,
            reason=reason,
            fallback_u=fallback_u,
            x=x.copy(),
            load=load,
            u_prev=u_prev,
            profile=profile,
            controller_id=controller_id,
            solver_status=solver_status,
            u_requested=u_requested,
            safety_ok=safety_ok,
            detail=detail,
        )
        self._logs.append(event)

    def log_event(self, step: int, sim_time: float, reason: str, fallback_u: float):
        """Manual log entry (e.g., from MPC)."""
        event = FailsafeEvent(
            step=step,
            sim_time=sim_time,
            reason=reason,
            fallback_u=fallback_u,
            x=np.zeros(1),
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
                "profile": e.profile,
                "controller_id": e.controller_id,
                "solver_status": e.solver_status,
                "u_requested": e.u_requested,
                "safety_ok": e.safety_ok,
                "detail": e.detail,
            }
            for e in self._logs
        ]

    def clear_logs(self):
        """Clear event log.

        Explicit harness operation only: the harness calls this at profile
        start and documents the call. Never invoked silently to reduce counts.
        """
        self._logs.clear()


# Constants needed for failsafe temperature prediction
A_THERMAL = 0.99984
B_THERMAL = 0.023
