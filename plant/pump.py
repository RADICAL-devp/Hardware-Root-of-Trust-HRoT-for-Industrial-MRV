"""Week 6M: pump/fan model for the VSD retrofit case (decision C).

Compares a constant-speed pump with a throttle valve (legacy) against a
variable-speed drive meeting the same demand (retrofit), under the approved
decision-C curves:

* Pump curve:      H_p(Q; n) = (H0 - a (Q n_nom/n)^2) (n/n_nom)^2
* System curve:    H_sys(Q)  = H_static + R_s Q^2
* Efficiency:       eta(Q, n) = eta_max [1 - kappa (Q/Q_BEP(n) - 1)^2],
                   Q_BEP(n) = Q_BEP,nom (n/n_nom)
* Retrofit speed:  solved (not assumed) from H_p(Q_d; n) = H_sys(Q_d).

Legacy arm: constant-speed motor, valve throttled to meet the demand; the
pump runs at n_nom and the valve wastes the head difference. Retrofit arm: VSD
at the solved speed, no valve.

All constants are the committed synthetic design set (decision C). The motor
is ideal in both arms; the retrofit carries a declared drive loss eta_drive.
"""

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class PumpParams:
    """Synthetic pump/system design constants (decision C)."""

    q_design_m3s: float = 0.01  # [m^3/s] design (BEP) flow
    h_design_m: float = 20.0  # [m] design head
    h0_m: float = 26.0  # [m] shut-off head at n_nom
    a: float = 60000.0  # [m/(m^3/s)^2] pump-curve coefficient
    h_static_m: float = 7.0  # [m] static head (s0 H_design)
    r_s: float = 130000.0  # [m/(m^3/s)^2] system-curve coefficient
    eta_max: float = 0.75  # [-] best-efficiency-point efficiency
    kappa: float = 0.5  # [-] efficiency-curve width parameter
    rho_kgm3: float = 1000.0  # [kg/m^3] water density
    g_ms2: float = 9.81  # [m/s^2] gravity
    eta_drive: float = 0.95  # [-] declared VSD drive efficiency (synthetic)
    n_nom_Hz: float = 50.0  # [Hz] nominal (supply) frequency


def system_head(q: np.ndarray | float, p: PumpParams) -> np.ndarray | float:
    """System head H_sys(Q) = H_static + R_s Q^2 [m]."""
    return p.h_static_m + p.r_s * np.asarray(q, dtype=float) ** 2


def pump_head_nom(q: np.ndarray | float, p: PumpParams) -> np.ndarray | float:
    """Pump curve at nominal speed H_p,nom(Q) = H0 - a Q^2 [m]."""
    return p.h0_m - p.a * np.asarray(q, dtype=float) ** 2


def pump_head(q: np.ndarray | float, n_Hz: float, p: PumpParams) -> np.ndarray | float:
    """Affinity-scaled pump curve H_p(Q; n) [m]."""
    r = n_Hz / p.n_nom_Hz
    q = np.asarray(q, dtype=float)
    return (p.h0_m - p.a * (q / r) ** 2) * r * r


def bep_flow(n_Hz: float, p: PumpParams) -> float:
    """Best-efficiency-point flow at speed n [m^3/s] (scales with speed)."""
    return p.q_design_m3s * (n_Hz / p.n_nom_Hz)


def efficiency(q: np.ndarray | float, n_Hz: float, p: PumpParams) -> np.ndarray | float:
    """Pump efficiency eta(Q, n), clipped to [0, eta_max]."""
    q_bep = bep_flow(n_Hz, p)
    eta = p.eta_max * (1.0 - p.kappa * (np.asarray(q, dtype=float) / q_bep - 1.0) ** 2)
    return np.clip(eta, 0.0, p.eta_max)


def solve_speed(q: np.ndarray | float, p: PumpParams) -> np.ndarray | float:
    """Retrofit speed n solving H_p(Q; n) = H_sys(Q) [Hz].

    H0 (n/n)^2 - a Q^2 = H_sys  =>  (n/n_nom)^2 = (H_sys + a Q^2)/H0.
    """
    q = np.asarray(q, dtype=float)
    h_sys = system_head(q, p)
    u2 = (h_sys + p.a * q**2) / p.h0_m
    return p.n_nom_Hz * np.sqrt(np.clip(u2, 0.0, None))


def shaft_power_legacy(q: np.ndarray | float, p: PumpParams) -> np.ndarray | float:
    """Legacy (constant-speed + throttle) shaft power [W].

    The pump runs at n_nom; the valve wastes H_p - H_sys. Shaft power is the
    total pumped head times flow over efficiency.
    """
    q = np.asarray(q, dtype=float)
    h_p = pump_head_nom(q, p)
    eta = efficiency(q, p.n_nom_Hz, p)
    return p.rho_kgm3 * p.g_ms2 * q * h_p / np.where(eta > 0, eta, np.nan)


def shaft_power_retrofit(q: np.ndarray | float, p: PumpParams) -> np.ndarray | float:
    """Retrofit (VSD, solved speed) shaft power [W]."""
    q = np.asarray(q, dtype=float)
    n = solve_speed(q, p)
    h_sys = system_head(q, p)
    eta = efficiency(q, n, p)
    return p.rho_kgm3 * p.g_ms2 * q * h_sys / np.where(eta > 0, eta, np.nan)


def grid_power_legacy(q: np.ndarray | float, p: PumpParams) -> np.ndarray | float:
    """Legacy grid-side power [W] (direct-on-line, ideal motor)."""
    return shaft_power_legacy(q, p)


def grid_power_retrofit(q: np.ndarray | float, p: PumpParams) -> np.ndarray | float:
    """Retrofit grid-side power [W] (VSD carries eta_drive)."""
    return shaft_power_retrofit(q, p) / p.eta_drive


def demand_profile(t: np.ndarray, p: PumpParams) -> np.ndarray:
    """Committed demand Q_d(t) as a fraction of Q_design (decision C).

    0-10 s 0.5 Q_d, 10-40 s Q_d, 40-55 s 0.75 Q_d, 55-60 s 0.3 Q_d.
    """
    t = np.asarray(t, dtype=float)
    frac = np.where(
        t < 10.0,
        0.5,
        np.where(t < 40.0, 1.0, np.where(t < 55.0, 0.75, 0.3)),
    )
    return frac * p.q_design_m3s


def hydraulic_energy(q: np.ndarray, h: np.ndarray, dt: float, p: PumpParams) -> float:
    """Useful hydraulic energy rho g int Q H dt [J]."""
    return p.rho_kgm3 * p.g_ms2 * float(np.trapezoid(q * h, dx=dt))
