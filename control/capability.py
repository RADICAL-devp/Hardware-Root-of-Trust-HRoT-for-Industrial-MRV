"""Coupled-plant torque-capability representation for MPC/failsafe (Week 6D).

The affine surrogate calibrated against the pre-Stage-A plant is superseded.
Capability is derived from peak torque of the actual equivalent circuit:

    T_peak(u) = max over slip in [s_min, s_max] of T_avg(slip, u)   [N*m]
    L(u) = CAP_C0 + CAP_C1 * u                                      [N*m]

with the derated constraint (convex: affine in the decision variable u,
load enters only as a parameter):

    L(u) * (1 - RIPPLE_FRAC) >= torque_margin * load + CAP_OFFSET

Semantics (read carefully): L(u) is a CAPABILITY map — the maximum mean
torque the motor can produce at throttle u — derated by ripple as stall
headroom. It guarantees the achievable-torque envelope covers demand; it
does NOT predict produced torque and does NOT establish produced-minima
compliance. The instantaneous acceptance metric on produced T_e is unchanged
and reported separately (it remains violated; see metrics).

Fit provenance (measured, SEED-independent deterministic circuit eval):
least-squares affine fit of T_peak over u in [0.4, 1.0] step 0.02
(dense 20001-point slip grid per voltage). Errors below include the held-out
0.01-offset grid and resistance scalings r in {1.0, 1.005, 1.01}:

    fit grid:    max|err| = 1.5920, max overestimation = 0.8784
    held-out:    max|err| = 1.4301, max overestimation = 0.8756
    r = 1.01 worst overestimation = 0.9313  ->  derated 0.9313*0.7 = 0.6519

CAP_OFFSET = 0.7 [N*m, derated scale] covers the worst measured derated
overestimation (0.6519) with margin. Validity domain: u in [0.4, 1.0]
(MPC throttle bounds), r_scale in [1.0, 1.01] (Stage A thermal range),
demand side exact in load (no fit). Outside this domain the
representation is unsupported: classify as infeasible.
"""

import numpy as np

from plant.mechanics import mean_torque_direct
from plant.motor import MotorParams

RIPPLE_FRAC = 0.3  # [dimensionless] plant torque-ripple fraction (reference only)

# Affine peak-torque fit L(u) = CAP_C0 + CAP_C1 * u  [N*m]; see provenance above.
CAP_C0 = -12.571547
CAP_C1 = 38.428309

# Derated-scale safety offset [N*m]: covers worst measured derated
# overestimation 0.6519 with margin. Supersedes the old surrogate's 0.2.
CAP_OFFSET = 0.7

# Validity domain (representation unsupported outside; classify infeasible).
U_MIN_FIT = 0.4
U_MAX_FIT = 1.0
R_MAX_FIT = 1.01


def peak_torque_Nm(u: float, mp: MotorParams | None = None, r_scale: float = 1.0) -> float:
    """Maximum circuit mean torque [N*m] at throttle u (dense slip scan)."""
    mp = mp or MotorParams()
    s = np.linspace(mp.s_min, mp.s_max, 20001)
    t_avg, _, _ = mean_torque_direct(s, float(mp.v_rms_nom_V * u), mp, float(r_scale))
    return float(np.max(t_avg))


def capability_floor(u: float) -> float:
    """Derated affine capability [N*m]: L(u) * (1 - ripple)."""
    return float((CAP_C0 + CAP_C1 * float(u)) * (1.0 - RIPPLE_FRAC))


def meets_demand(
    load_Nm: float,
    u: float,
    torque_margin: float = 0.9,
    offset_Nm: float = CAP_OFFSET,
) -> bool:
    """True iff derated capability covers margin*load + offset at throttle u."""
    return bool(capability_floor(u) >= torque_margin * float(load_Nm) + offset_Nm)
