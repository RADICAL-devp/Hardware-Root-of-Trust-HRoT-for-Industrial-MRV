"""Constrained MPC controller for motor throttle optimization.

State-space model (linearized around rated operating point):
    x[k+1] = A x[k] + B u[k] + E d[k]

State vector x = [ΔT, Δs] where:
    ΔT = temperature rise above ambient [°C]
    Δs = slip deviation from rated [dimensionless]

Control input u = throttle command [0, 1] (scales V_rms = u * 230V)

Disturbance d = load torque [N·m] (measured)

Sampling interval: dt = 1.0 s (aligned with 1 Hz attestation windows)
Prediction horizon: N_p = 10 (10 s)

Constraints:
    u_min ≤ u ≤ u_max                          (throttle bounds)
    |u[k] - u[k-1]| ≤ du_max                   (rate limit)
    T[k] = T_amb + ΔT[k] ≤ T_max               (temperature limit)
    Torque capability ≥ torque_margin × load   (torque margin)

Objective:
    J = Σ (u' R u + ΔT' Q ΔT) + ΔT_N' P ΔT_N

where R, Q are weights, P is terminal weight from DARE.
"""

from dataclasses import dataclass

import cvxpy as cp
import numpy as np

from control.failsafe import Failsafe, FailsafeParams


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
    torque_margin: float = 1.1  # [ratio] torque capability / load
    R_u: float = 1.0  # [weight] control effort
    Q_T: float = 0.1  # [weight] temperature deviation
    solver_timeout_ms: int = 100  # [ms] OSQP timeout
    warm_start: bool = True  # [flag] use warm start


# Linearized plant model coefficients (identified at rated: 10 Nm, 230V, 40°C)
# Continuous-time thermal: τ_th dΔT/dt = -ΔT + k_T * u²
# Discretized (Euler, dt=1s): ΔT[k+1] = a_T ΔT[k] + b_T u[k]²
# Linearized around u=1: u² ≈ 1 + 2(u-1) → b_T * (1 + 2Δu)
# But we keep it simple: ΔT[k+1] = a_T ΔT[k] + b_T u[k]
#
# Motor torque (steady-state): T ≈ T_rated * (u / u_rated) * f(slip)
# For small slip variations: ΔT_motor ≈ k_torque * Δu
# We enforce torque constraint directly via steady-state map.

# Thermal time constant τ_th = Rth * Cth = 0.8 * 8000 = 6400 s
# a_T = exp(-dt/τ_th) ≈ 0.99984
# b_T ≈ (1 - a_T) * T_rise_at_rated / u_rated²
# At rated (10 Nm, u=1): P_cu ≈ I²R ≈ (15A)² * 0.8 = 180W, ΔT ≈ 144°C rise
# So b_T ≈ (1-0.99984) * 144 ≈ 0.023 °C/pu

A_THERMAL = 0.99984
B_THERMAL = 0.023  # °C per pu throttle (approximate)

# Slip dynamics: fast (τ_mech = 0.35s), but we control at 1s
# Slip reaches steady-state within one step: Δs[k+1] ≈ k_slip * (load - torque(u))
# Linearized: Δs[k+1] = a_s Δs[k] + b_s u[k] + c_s load[k]
# With a_s ≈ 0 (fast), b_s < 0 (more throttle → less slip)

A_SLIP = 0.0
B_SLIP = -0.02  # slip decreases with throttle
C_SLIP = 0.003  # slip increases with load

# State transition matrix
A = np.array([[A_THERMAL, 0.0], [0.0, A_SLIP]], dtype=float)

B = np.array([[B_THERMAL], [B_SLIP]], dtype=float)

E = np.array([[0.0], [C_SLIP]], dtype=float)

# Steady-state torque map: T_motor ≈ k_t * u * (1 - s/s_max)
# At rated: u=1, s=0.04, T=12 Nm → k_t = 12 / (1 * (1 - 0.04/0.15)) = 16.36
# Linearized torque constraint around rated point (u=1, s=0.04):
# T = K_TORQUE * u * (1 - s/S_MAX)
# Linearization:
# dT/du = K_TORQUE * (1 - s/S_MAX) |_{u=1,s=0.04} = 16.36 * 0.7333 = 12.0
# dT/ds = -K_TORQUE * u / S_MAX |_{u=1,s=0.04} = -16.36 / 0.15 = -109.1
# T(1, 0.04) = 16.36 * 0.7333 = 12.0
# T ≈ 12.0 + 12.0*(u-1) - 109.1*(s-0.04)
#   = 12.0*u - 109.1*s + 109.1*0.04
#   = 12.0*u - 109.1*s + 4.36
# With s = 0.04 + Δs:
#   12.0*u - 109.1*(0.04 + Δs) + 4.36 = 12.0*u - 4.36 - 109.1*Δs + 4.36
#   = 12.0*u - 109.1*Δs
# Constraint: 12.0*u - 109.1*Δs ≥ torque_margin * load
# This is DCP-compliant (affine in u and Δs)
K_TORQUE = 16.363636363636363
S_MAX = 0.15
S_RATED = 0.04

# Linearized torque coefficients
K_TORQUE_U_LIN = 12.0  # dT/du at rated
K_TORQUE_S_LIN = -109.09090909090909  # dT/ds at rated


class MPCController:
    """Model Predictive Controller with CVXPY/OSQP."""

    def __init__(self, params: MPCParams):
        self.params = params
        self.N = params.prediction_horizon
        self.dt = params.dt

        # Problem dimensions
        self.nx = 2  # [ΔT, Δs]
        self.nu = 1  # [u]
        self.nd = 1  # [load]

        # Build CVXPY problem (reused each solve)
        self._build_problem()

        # Failsafe
        self.failsafe = Failsafe(FailsafeParams())
        self._last_u = params.u_max  # initial safe setpoint
        self._last_x = np.zeros(self.nx)

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
            constraints.append(
                self.x_var[:, k + 1]
                == A @ self.x_var[:, k] + B @ self.u_var[:, k] + E @ self.load_param[:, k]
            )

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

            # Torque margin constraint (linearized steady-state approximation)
            # Linearized around u=1, s=0.04: 9.17*u - 83.33*Δs ≥ torque_margin * load
            # Δs = x[1] (slip deviation)
            torque_cap_lin = K_TORQUE_U_LIN * self.u_var[0, k] + K_TORQUE_S_LIN * self.x_var[1, k]
            constraints.append(torque_cap_lin >= self.torque_margin_param * self.load_param[0, k])

        self.constraints = constraints
        self.problem = cp.Problem(self.objective, self.constraints)

    def solve(
        self,
        x0: np.ndarray,
        load: float,
        u_prev: float,
    ) -> float:
        """Solve MPC problem for one step.

        Args:
            x0: current state [ΔT, Δs]
            load: current load torque [N·m]
            u_prev: previous throttle command

        Returns:
            Optimal throttle command u* [pu]
        """
        # Update parameters
        self.x0_param.value = x0
        self.u_prev_param.value = np.array([u_prev])
        self.load_param.value = np.full((1, self.N), load)

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
            # Failsafe handles all exceptions
            fallback_u, reason = self.failsafe.fallback(
                solver_status="error",
                proposed_u=None,
                x=x0,
                load=load,
                u_prev=u_prev,
                step=0,  # step tracking done by caller
                sim_time=0.0,
            )
            # Log the fallback reason
            self.failsafe.log_event(
                step=0, sim_time=0.0, reason=f"solver_error: {e}", fallback_u=fallback_u
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
        x: current state [ΔT, Δs]
        u: throttle command
        load: load torque
        dt: time step
        t_amb: ambient temperature

    Returns:
        Next state [ΔT, Δs]
    """
    # This uses the same linearized model as MPC for consistency
    x_next = A @ x + B.flatten() * u + E.flatten() * load
    return x_next
