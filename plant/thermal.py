"""First-order thermal state for the Week 2 motor plant.

Model (lumped frame + winding, SI units throughout):
    Cth [J/K] * dT/dt = P_loss [W] - (T [°C] - T_amb [°C]) / Rth [K/W],
where the driving loss is stator copper loss ``P_loss = i(t)^2 [A^2] * R(T)``::

    R(T) = R0 [Ohm] * (1 + alpha [1/K] * (T [°C] - T0 [°C])),

with ``alpha`` the copper temperature coefficient. Forward-Euler integration
at the telemetry sample rate is ample: the thermal time constant
``tau = Rth * Cth`` [s] is hours, so ``dt << tau`` by ~7 orders of magnitude.

The plant feeds the thermal state back into :mod:`plant.motor` one way
(temperature raises winding resistance); there is no controller in Week 2.
"""

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ThermalParams:
    """Lumped thermal parameters (units documented per field)."""

    r_winding_Ohm: float = 0.8  # [Ohm] stator resistance at T0
    rth_K_per_W: float = 0.8  # [K/W] frame-to-ambient thermal resistance
    cth_J_per_K: float = 8000.0  # [J/K] lumped heat capacity of frame+winding
    t_amb_C: float = 25.0  # [°C] ambient temperature
    alpha_Cu_1_per_K: float = 0.00393  # [1/K] copper temperature coefficient
    t0_C: float = 25.0  # [°C] reference temperature for r_winding_Ohm


def winding_resistance(temp_C: np.ndarray | float, params: ThermalParams) -> np.ndarray | float:
    """Return temperature-corrected winding resistance [Ohm]."""
    return params.r_winding_Ohm * (1.0 + params.alpha_Cu_1_per_K * (temp_C - params.t0_C))


def simulate_temperature(current_A: np.ndarray, fs_hz: float, params: ThermalParams) -> np.ndarray:
    """Integrate the thermal state driven by ``i(t)^2 * R(T)`` losses.

    Args:
        current_A: instantaneous phase current [A], 1-D array.
        fs_hz: sample rate [samples/s].
        params: :class:`ThermalParams`.

    Returns:
        Winding/frame temperature [°C], same shape as ``current_A``.
        Starts at ambient; rises monotonically for any nonzero current.
    """
    dt_s = 1.0 / fs_hz  # [s] integration step
    temp_C = np.empty_like(current_A, dtype=float)
    t_c = params.t_amb_C
    for k, i_a in enumerate(current_A.tolist()):
        temp_C[k] = t_c
        p_loss_W = i_a * i_a * winding_resistance(t_c, params)  # [W]
        dT = (p_loss_W - (t_c - params.t_amb_C) / params.rth_K_per_W) / params.cth_J_per_K
        t_c += dt_s * dT  # [°C]
    return temp_C
