"""Constrained MPC controller for motor throttle optimization.

State-space model (linearized around rated operating point):
    x[k+1] = A x[k] + B u[k] + E d[k]

State vector x = [ΔT] where:
    ΔT = temperature rise above ambient [°C]

Control input u = throttle command [0, 1] (scales V_rms = u * 230V)

Disturbance d = load torque [N·m] (measured)

Sampling interval: dt = 1.0 s (aligned with 1 Hz attestation windows)
Prediction horizon: N_p = 10 (10 s)

Constraints:
    u_min ≤ u ≤ u_max                          (throttle bounds)
    |u[k] - u[k-1]| ≤ du_max                   (rate limit)
    T[k] = T_amb + ΔT[k] ≤ T_max               (temperature limit)
    Torque capability ≥ torque_margin × load   (torque margin)

Torque capability model (Week 6D peak-torque map from the coupled plant):
    L(u) = CAP_C0 + CAP_C1 * u  (maximum mean torque at throttle u [N*m])

Control uses the measured affine fit (control.capability); load enters the
derated constraint only as a demand parameter. Produced-torque compliance
is measured separately, never assumed from this constraint.

Where load is the measured load torque [N·m], u is throttle [pu].
This affine model is calibrated against plant simulation (plant/motor.py)
across the operating envelope (load ∈ [6, 17] Nm, u ∈ [0.4, 1.0]).
The model is a conservative approximation (underestimates torque at u=1.0)
derived from plant simulation with SEED=42.

Torque margin constraint (DCP-compliant, affine in u):
    a(load) + b(load) * u ≥ torque_margin * load

Objective:
    J = Σ (u' R u + ΔT' Q ΔT) + ΔT_N' P ΔT_N

where R, Q are weights, P is terminal weight from DARE.
"""

from dataclasses import dataclass

import cvxpy as cp
import numpy as np

from control.capability import CAP_C0, CAP_C1
from control.failsafe import Failsafe, FailsafeParams
from control.torque_model import TORQUE_SAFETY_OFFSET


@dataclass(frozen=True)
class MPCParams:
    """MPC configuration parameters."""

    prediction_horizon: int = 10  # [steps] N_p
    dt: float = 1.0  # [s] control interval
    u_min: float = 0.4  # [pu] minimum throttle
    u_max: float = 1.0  # [pu] maximum throttle
    du_max: float = 0.1  # [pu/step] max throttle rate
    temp_max: float = 120.0  # [°C] max winding temperature
    t_amb: float = 25.0  # [°C] ambient temperature
    torque_margin: float = 0.9  # [ratio] torque capability / load (calibrated)
    R_u: float = 1.0  # [weight] control effort
    Q_T: float = 0.1  # [weight] temperature deviation
    solver_timeout_ms: int = 100  # [ms] OSQP timeout
    warm_start: bool = True  # [flag] use warm start
    ripple_frac: float = 0.3  # [ratio] torque ripple fraction (plant model)


# Thermal time constant τ_th = Rth * Cth = 0.8 * 8000 = 6400 s
# a_T = exp(-dt/τ_th) ≈ 0.99984
# b_T ≈ (1 - a_T) * T_rise_at_rated / u_rated²
# At rated (10 Nm, u=1): P_cu ≈ I²R ≈ (15A)² * 0.8 = 180W, ΔT ≈ 144°C rise
# So b_T ≈ (1-0.99984) * 144 ≈ 0.023 °C/pu

A_THERMAL = 0.99984
B_THERMAL = 0.023  # °C per pu throttle (approximate)

# State transition matrix (1 state: ΔT)
A = np.array([[A_THERMAL]], dtype=float)
B = np.array([[B_THERMAL]], dtype=float)
E = np.array([[0.0]], dtype=float)  # Load does not directly affect temperature in this model


# Coupled-plant peak-torque capability L(u) = CAP_C0 + CAP_C1 * u [N*m].
# Single source of truth: control.capability (measured fit, see module docs).
# The pre-Stage-A operating-point coefficients were removed with the
# superseded surrogate; do not reintroduce them here.


class MPCController:
    """Model Predictive Controller with CVXPY/OSQP."""

    def __init__(self, params: MPCParams, profile: str = "", controller_id: str = "mpc"):
        self.params = params
        self.N = params.prediction_horizon
        self.dt = params.dt
        # Event-attribution context (set explicitly by the harness per profile).
        self.profile = profile
        self.controller_id = controller_id

        # Problem dimensions
        self.nx = 1  # [ΔT]
        self.nu = 1  # [u]
        self.nd = 1  # [load]

        # Build CVXPY problem (reused each solve)
        self._build_problem()

        # Failsafe
        self.failsafe = Failsafe(FailsafeParams(ripple_frac=params.ripple_frac))
        self._last_u = params.u_max  # initial safe setpoint
        self._last_x = np.zeros(self.nx)

    def set_context(self, profile: str, controller_id: str) -> None:
        """Set event-attribution context (explicit harness operation)."""
        self.profile = profile
        self.controller_id = controller_id

    def clear_failsafe_logs(self) -> None:
        """Clear failsafe history (explicit harness operation, documented at call site)."""
        self.failsafe.clear_logs()

    def _build_problem(self):
        """Construct the CVXPY optimization problem."""
        N = self.N
        nx = self.nx
        nu = self.nu
        nd = self.nd

        # Variables
        self.x_var = cp.Variable((nx, N + 1))
        self.u_var = cp.Variable((nu, N))
        self.u_prev_param = cp.Parameter(nu)
        self.x0_param = cp.Parameter(nx)
        self.load_param = cp.Parameter((nd, N))

        # Parameters for constraints
        self.u_min_param = cp.Parameter(nu)
        self.u_max_param = cp.Parameter(nu)
        self.du_max_param = cp.Parameter(nu)
        self.temp_max_param = cp.Parameter()
        self.t_amb_param = cp.Parameter()
        self.torque_margin_param = cp.Parameter()

        # Set parameter values
        self.u_min_param.value = np.array([self.params.u_min])
        self.u_max_param.value = np.array([self.params.u_max])
        self.du_max_param.value = np.array([self.params.du_max])
        self.temp_max_param.value = self.params.temp_max
        self.t_amb_param.value = self.params.t_amb
        self.torque_margin_param.value = self.params.torque_margin

        # Objective
        obj = 0
        for k in range(N):
            # Control effort: R * u²
            obj += self.params.R_u * cp.sum_squares(self.u_var[:, k])
            # Temperature penalty: Q * (ΔT)²
            obj += self.params.Q_T * cp.sum_squares(self.x_var[0, k])

        # Terminal cost (use Q_T as terminal weight)
        obj += self.params.Q_T * cp.sum_squares(self.x_var[0, N])

        self.objective = cp.Minimize(obj)

        # Constraints
        constraints = []

        # Initial condition
        constraints.append(self.x_var[:, 0] == self.x0_param)

        # Dynamics and constraints for each step
        for k in range(N):
            # State transition: x[k+1] = A x[k] + B u[k] + E d[k]
            # Note: E = 0 (load does not directly affect temperature in this model)
            constraints.append(self.x_var[:, k + 1] == A @ self.x_var[:, k] + B @ self.u_var[:, k])

            # Throttle bounds
            constraints.append(self.u_var[:, k] >= self.u_min_param)
            constraints.append(self.u_var[:, k] <= self.u_max_param)

            # Rate limit
            if k == 0:
                constraints.append(self.u_var[:, k] - self.u_prev_param <= self.du_max_param)
                constraints.append(self.u_prev_param - self.u_var[:, k] <= self.du_max_param)
            else:
                constraints.append(self.u_var[:, k] - self.u_var[:, k - 1] <= self.du_max_param)
                constraints.append(self.u_var[:, k - 1] - self.u_var[:, k] <= self.du_max_param)

            # Temperature constraint: T_amb + ΔT ≤ T_max
            constraints.append(self.t_amb_param + self.x_var[0, k] <= self.temp_max_param)
            constraints.append(self.t_amb_param + self.x_var[0, k + 1] <= self.temp_max_param)

            # Torque capability constraint (Week 6D peak-torque map, convex).
            # L(u) = CAP_C0 + CAP_C1 * u is the maximum mean torque the motor
            # can produce at throttle u (control.capability, measured fit).
            # Derated by ripple as stall headroom:
            #   L(u) * (1 - ripple_frac) >= torque_margin * load + OFFSET.
            # Affine in the decision variable u (load is a parameter), so the
            # QP stays convex; OSQP settings, warm start, and horizon behavior
            # are unchanged. This constrains CAPABILITY (achievable envelope),
            # not produced torque: produced-minima compliance is measured
            # separately and is not established by this constraint.
            load_k = self.load_param[0, k]
            lhs = (CAP_C0 + CAP_C1 * self.u_var[0, k]) * (1.0 - self.params.ripple_frac)
            rhs = self.torque_margin_param * load_k + TORQUE_SAFETY_OFFSET
            constraints.append(lhs >= rhs)

        self.constraints = constraints
        self.problem = cp.Problem(self.objective, self.constraints)

    def solve(
        self,
        x0: np.ndarray,
        load: float,
        u_prev: float,
        step: int = 0,
        sim_time: float = 0.0,
        profile: str | None = None,
        controller_id: str | None = None,
    ) -> float:
        """Solve MPC problem for one step.

        Args:
            x0: current state [ΔT]
            load: current load torque [N·m]
            u_prev: previous throttle command
            step: control step index
            sim_time: simulation time [s]
            profile: load-profile identifier (defaults to controller context)
            controller_id: controller identifier (defaults to controller context)

        Returns:
            Optimal throttle command u* [pu]
        """
        eff_profile = self.profile if profile is None else profile
        eff_controller = self.controller_id if controller_id is None else controller_id
        # Update parameters
        self.x0_param.value = x0
        self.u_prev_param.value = np.array([u_prev])
        self.load_param.value = np.full((1, self.N), load)

        raw_status: str | None = None
        # Solve
        try:
            solver_kwargs = {
                "solver": cp.OSQP,
                "verbose": False,
                "max_iter": 4000,
                "eps_abs": 1e-4,
                "eps_rel": 1e-4,
                "polish": True,
            }
            if self.params.solver_timeout_ms > 0:
                solver_kwargs["time_limit"] = self.params.solver_timeout_ms / 1000.0

            if self.params.warm_start and hasattr(self, "_last_u_var_value"):
                self.u_var.value = self._last_u_var_value
                self.x_var.value = self._last_x_var_value

            self.problem.solve(**solver_kwargs)

            status = self.problem.status
            raw_status = str(status)

            if status not in ("optimal", "optimal_inaccurate"):
                raise RuntimeError(f"Solver status: {status}")

            u_opt = float(self.u_var.value[0, 0])

            # Validate solution
            if not np.isfinite(u_opt):
                raise ValueError(f"Non-finite solution: {u_opt}")

            # Check constraints explicitly (solver tolerances)
            if u_opt < self.params.u_min - 1e-4 or u_opt > self.params.u_max + 1e-4:
                raise ValueError(f"Solution violates throttle bounds: {u_opt}")

            # Store for warm start
            self._last_u_var_value = self.u_var.value.copy()
            self._last_x_var_value = self.x_var.value.copy()

            self._last_u = u_opt
            return u_opt

        except Exception as e:
            # Single write site: Failsafe.fallback() records the one event.
            # Forward the raw solver status so infeasible/timeout reasons are
            # classified correctly (previously everything arrived as "error").
            fwd_status = (
                raw_status if raw_status not in (None, "optimal", "optimal_inaccurate") else "error"
            )
            fallback_u, _reason = self.failsafe.fallback(
                solver_status=fwd_status,
                proposed_u=None,
                x=x0,
                load=load,
                u_prev=u_prev,
                step=step,
                sim_time=sim_time,
                profile=eff_profile,
                controller_id=eff_controller,
                detail=str(e)[:500],
            )
            self._last_u = fallback_u
            return fallback_u

    def get_failsafe_logs(self):
        """Get failsafe event logs."""
        return self.failsafe.get_logs()


def simulate_plant_step(
    x: np.ndarray,
    u: float,
    load: float,
    dt: float,
    t_amb: float = 25.0,
) -> np.ndarray:
    """Simulate one step of the linearized plant (for MPC prediction validation).

    Args:
        x: current state [ΔT]
        u: throttle command
        load: load torque
        dt: time step
        t_amb: ambient temperature

    Returns:
        Next state [ΔT]
    """
    # This uses the same linearized model as MPC for consistency
    x_next = A @ x + B.flatten() * u
    return x_next
