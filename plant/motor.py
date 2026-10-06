"""Single-phase induction motor plant (equivalent-circuit approximation).

What this is: a lumped, per-phase steady-state equivalent circuit plus
first-order rotor dynamics. It produces true (pre-sensor) waveforms
``v(t)`` [V], ``i(t)`` [A], shaft torque [N·m] and speed [rad/s] from a
shaft load-torque input [N·m]. Good enough for Week 2 measurement-chain
validation; NOT a finite-element or d-q transient model.

Electrical model (all impedances in [Ohm], slip ``s`` [dimensionless]):
    Z_rotor(s) = R2/s + j*X2                        (rotor branch)
    Z_mag      = (Rc * j*Xm) / (Rc + j*Xm)          (magnetizing branch)
    Z_total(s) = R1 + j*X1 + Z_mag || Z_rotor(s)     (seen by the grid)
    I_rms = V_rms / |Z_total|,  phi = arg(Z_total)   (current lags by phi)

    v(t) = Vpk * sin(2π f_grid t)
    i(t) = Ipk(t) * sin(2π f_grid t - phi(t))

with ``f_grid = f_nom + f_grid_offset`` [Hz]. ``f_grid_offset_Hz`` models
real grid drift (default 0; evaluation uses ±0.2 Hz, sweep to ±0.5 Hz).
The window logic under test is NEVER given the true frequency.

Mechanical model:
    slip target  s(t) = s_rated * (T_load(t) / T_rated), clipped [s_min, s_max]
    ω_target(t) = ω_sync * (1 - s(t)),  ω_sync = 2π f_grid / pole_pairs
    first-order lag: τ_mech dω/dt + ω = ω_target   (rotor inertia + friction)
    T_avg(t) = P_airgap(t) / ω_sync,  P_airgap = P_in - I²R1 - V²/Rc   [W]
    T_e(t) = T_avg(t) * (1 + ripple_frac * cos(4π f_grid t))           [N·m]
(the 2nd-harmonic pulsation is characteristic of single-phase machines).

Calibration: with the default parameters the circuit torque agrees with the
slip-curve load within ~6 % over 6–14 N·m (exact at the 12 N·m rated point).
Winding resistance follows :mod:`plant.thermal` (two-pass: nominal-R current
gives a temperature trace, then R is rescaled by the mean temperature rise).

Every field below documents its unit. Single-phase only (AGENTS.md).
"""

from dataclasses import dataclass

import numpy as np

from plant.thermal import ThermalParams, simulate_temperature, winding_resistance


@dataclass(frozen=True)
class MotorParams:
    """Motor + grid parameters (units documented per field)."""

    v_rms_nom_V: float = 230.0  # [V rms] stiff-grid phase voltage
    f_nom_Hz: float = 50.0  # [Hz] nominal grid frequency
    f_grid_offset_Hz: float = 0.0  # [Hz] grid drift; eval default ±0.2, sweep ±0.5
    n_poles: int = 2  # [count] pole number (2 → 3000 rpm synchronous at 50 Hz)
    t_rated_Nm: float = 12.0  # [N·m] rated shaft torque (slip-curve anchor)
    s_rated: float = 0.04  # [dimensionless] slip at rated torque
    s_min: float = 0.001  # [dimensionless] slip floor (avoids R2/s singularity)
    s_max: float = 0.15  # [dimensionless] slip ceiling (pull-out region edge)
    r1_Ohm: float = 0.4  # [Ohm] stator resistance at T0
    x1_Ohm: float = 1.2  # [Ohm] stator leakage reactance at 50 Hz
    r2_Ohm: float = 0.48  # [Ohm] rotor resistance at T0 (referred to stator)
    x2_Ohm: float = 1.2  # [Ohm] rotor leakage reactance at 50 Hz
    rc_Ohm: float = 350.0  # [Ohm] core-loss shunt resistance
    xm_Ohm: float = 45.0  # [Ohm] magnetizing reactance at 50 Hz
    tau_mech_s: float = 0.35  # [s] lumped rotor-speed time constant (J/B)
    torque_ripple_frac: float = 0.3  # [dimensionless] 2nd-harmonic amplitude/T_avg


@dataclass
class MotorResult:
    """True (pre-sensor) simulation traces."""

    t_s: np.ndarray  # [s] sample instants
    v_true_V: np.ndarray  # [V] true terminal voltage
    i_true_A: np.ndarray  # [A] true phase current
    torque_Nm: np.ndarray  # [N·m] true electromagnetic torque
    speed_rad_s: np.ndarray  # [rad/s] true rotor speed
    speed_rpm: np.ndarray  # [rev/min] true rotor speed
    slip: np.ndarray  # [dimensionless] operating slip
    temp_C: np.ndarray  # [°C] winding/frame temperature
    f_grid_Hz: float  # [Hz] actual grid frequency used
    i_rms_A: np.ndarray  # [A rms] quasi-static current magnitude
    phi_rad: np.ndarray  # [rad] quasi-static current lag


def _impedance(
    slip: np.ndarray, r1: float, x1: float, r2: float, x2: float, rc: float, xm: float
) -> tuple[np.ndarray, np.ndarray]:
    """Return (|Z_total| [Ohm], arg(Z_total) [rad]) for slip array."""
    z_rotor = r2 / slip + 1j * x2
    z_mag = (rc * 1j * xm) / (rc + 1j * xm)
    z_par = z_mag * z_rotor / (z_mag + z_rotor)
    z_tot = (r1 + 1j * x1) + z_par
    return np.abs(z_tot), np.angle(z_tot)


@dataclass
class _ElectricalState:
    i_rms: np.ndarray
    phi: np.ndarray


def _electrical(
    slip: np.ndarray, v_rms: float, p: MotorParams, r_scale: float = 1.0
) -> _ElectricalState:
    z_mag, phi = _impedance(
        slip, p.r1_Ohm * r_scale, p.x1_Ohm, p.r2_Ohm * r_scale, p.x2_Ohm, p.rc_Ohm, p.xm_Ohm
    )
    i_rms = v_rms / z_mag
    return _ElectricalState(i_rms=i_rms, phi=phi)


def simulate(
    load_torque_Nm: np.ndarray,
    fs_hz: float,
    params: MotorParams | None = None,
    thermal_params: ThermalParams | None = None,
) -> MotorResult:
    """Simulate true waveforms for a shaft load-torque trace.

    Args:
        load_torque_Nm: shaft load torque [N·m], 1-D array.
        fs_hz: sample rate [samples/s].
        params: :class:`MotorParams` (grid drift via ``f_grid_offset_Hz``).
        thermal_params: :class:`ThermalParams` (defaults if None).

    Returns:
        :class:`MotorResult` with true v/i/torque/speed traces.
    """
    p = params or MotorParams()
    th = thermal_params or ThermalParams()
    n = int(load_torque_Nm.shape[0])
    t_s = np.arange(n) / fs_hz  # [s]
    f_grid = p.f_nom_Hz + p.f_grid_offset_Hz  # [Hz]
    pole_pairs = p.n_poles / 2.0  # [dimensionless]
    omega_sync = 2.0 * np.pi * f_grid / pole_pairs  # [rad/s]

    slip = np.clip(p.s_rated * (load_torque_Nm / p.t_rated_Nm), p.s_min, p.s_max)
    omega_target = omega_sync * (1.0 - slip)  # [rad/s]
    # First-order rotor-speed lag, initialized at the first target so the
    # run starts in quasi-steady operation (no startup inrush in Week 2).
    alpha = (1.0 / fs_hz) / p.tau_mech_s  # [dimensionless] Euler step factor
    speed = np.empty(n)
    w = float(omega_target[0])
    for k in range(n):
        w += alpha * (float(omega_target[k]) - w)
        speed[k] = w

    # Pass 1 at nominal resistance → temperature trace → rescale R by the
    # mean thermal rise → pass 2 (one fixed-point iteration; the rise over a
    # few seconds is < 1 K so this is converged for our purposes).
    elec = _electrical(slip, p.v_rms_nom_V, p)
    v_pk = p.v_rms_nom_V * np.sqrt(2.0)  # [V]
    phase = 2.0 * np.pi * f_grid * t_s  # [rad]
    i_pk = elec.i_rms * np.sqrt(2.0)  # [A]
    v_true = v_pk * np.sin(phase)  # [V]
    i_true = i_pk * np.sin(phase - elec.phi)  # [A]
    temp_C = simulate_temperature(i_true, fs_hz, th)  # [°C]
    r_scale = float(winding_resistance(np.mean(temp_C), th) / th.r_winding_Ohm)
    elec = _electrical(slip, p.v_rms_nom_V, p, r_scale=r_scale)
    i_pk = elec.i_rms * np.sqrt(2.0)  # [A]
    i_true = i_pk * np.sin(phase - elec.phi)  # [A]
    temp_C = simulate_temperature(i_true, fs_hz, th)  # [°C]

    p_in = p.v_rms_nom_V * elec.i_rms * np.cos(elec.phi)  # [W] input power
    p_cu = elec.i_rms**2 * p.r1_Ohm * r_scale  # [W] stator copper loss
    p_core = p.v_rms_nom_V**2 / p.rc_Ohm  # [W] core loss
    t_avg = (p_in - p_cu - p_core) / omega_sync  # [N·m] air-gap torque
    torque = t_avg * (1.0 + p.torque_ripple_frac * np.cos(2.0 * phase))  # [N·m]

    return MotorResult(
        t_s=t_s,
        v_true_V=v_true,
        i_true_A=i_true,
        torque_Nm=torque,
        speed_rad_s=speed,
        speed_rpm=speed * 60.0 / (2.0 * np.pi),
        slip=slip,
        temp_C=temp_C,
        f_grid_Hz=f_grid,
        i_rms_A=elec.i_rms,
        phi_rad=elec.phi,
    )
