"""Torque-capability representation for MPC and failsafe (Week 6D).

The pre-Stage-A affine operating-point surrogate (a(load) + b(load) * u)
is SUPERSEDED: it was calibrated against the legacy plant and is no longer
validated. The single source of truth is now :mod:`control.capability`
(peak-torque map derived from the actual equivalent circuit with measured
approximation error). This module re-exports the offset name used across
the codebase; do not reintroduce operating-point coefficients here.
"""

from control.capability import CAP_C0, CAP_C1, CAP_OFFSET, RIPPLE_FRAC

TORQUE_SAFETY_OFFSET = CAP_OFFSET  # [N*m] derated-scale offset (measured, see capability)

__all__ = ["TORQUE_SAFETY_OFFSET", "CAP_C0", "CAP_C1", "CAP_OFFSET", "RIPPLE_FRAC"]
