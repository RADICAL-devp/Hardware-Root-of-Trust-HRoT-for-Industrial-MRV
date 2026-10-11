"""Calibrated torque capability model for MPC and failsafe.

This module provides the torque capability model T = a(load) + b(load) * u
calibrated against plant/motor.py simulation.
"""

# Calibrated torque capability model coefficients
# T_capability(load, u) = a(load) + b(load) * u
# a(load) = TORQUE_A0 + TORQUE_A1 * load
# b(load) = TORQUE_B0 + TORQUE_B1 * load
# Calibrated against plant/motor.py simulation (SEED=42) over
# load ∈ [6, 17] Nm, u ∈ [0.4, 1.0].
# The model is conservative (underestimates torque at u=1.0 by 0.1-0.75 Nm).
TORQUE_A0 = -0.578889
TORQUE_A1 = -0.394881
TORQUE_B0 = 1.797576
TORQUE_B1 = 1.229083

# Safety offset for torque constraint [N·m]
# Accounts for model approximation error at low throttle.
# 0.2 Nm covers the maximum model overestimation error.
TORQUE_SAFETY_OFFSET = 0.2


def torque_capability_coeffs(load: float) -> tuple[float, float]:
    """Return (a, b) for torque capability model T = a(load) + b(load) * u.

    Args:
        load: Load torque [N·m].

    Returns:
        (a, b) coefficients where a = TORQUE_A0 + TORQUE_A1 * load,
        b = TORQUE_B0 + TORQUE_B1 * load.
    """
    a = TORQUE_A0 + TORQUE_A1 * load
    b = TORQUE_B0 + TORQUE_B1 * load
    return a, b
