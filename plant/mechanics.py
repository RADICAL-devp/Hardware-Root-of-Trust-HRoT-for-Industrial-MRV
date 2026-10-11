"""Stage A coupled mechanical dynamics (Week 6C).

Governing equation (rigid direct coupling, motor-shaft reference, SI units):

    J_eq * d(omega)/dt = T_e(omega, u, T) - T_load - T_friction(omega)
    T_friction(omega) = B * omega + Tf * sign(omega),  sign(0) = 0
    J_eq = J_motor + J_load_reflected  (gear ratio n = 1; see below)

Rotor inertia ``J_motor`` and reflected load inertia ``J_load_reflected``
are stored separately even though the single-inertia ODE uses their sum.
Any future gear ratio ``n:1`` (motor:load) reflects load quantities to the
motor shaft as ``J_load / n^2`` (speed ``omega_load = omega / n``, torque
``T_load / n``); the default ``gear_ratio = 1.0`` documents rigid direct
drive. No gearbox compliance, backlash, or belt dynamics are modelled.

Electrical torque ``T_e`` is produced ONLY by the existing equivalent
circuit in :mod:`plant.motor` (quasi-steady: ``T_avg = P_airgap / omega_sync``
evaluated at slip derived from actual rotor speed, plus the imposed 100 Hz
ripple). This module never alters the circuit; it only changes what slip is
fed into it. The ``tau_mech_s = 0.35`` follow-lag in :mod:`plant.motor` is
RETIRED as the default response model (see ``CoupledPlant.uses_legacy_tau``);
the legacy :func:`plant.motor.simulate` path remains untouched for
differential testing and its results are excluded from acceptance metrics.

Thermal treatment (Stage A): the existing two-pass mean-temperature
``r_scale`` approximation is preserved per integration segment; no
per-sample temperature-to-resistance feedback is introduced (Stage B scope).
This module is therefore mechanically coupled, electrically quasi-steady,
and approximately thermal -- NOT a fully coupled electro-thermal transient
model.

Parameter provenance (synthetic sensitivity study, NOT hardware calibration):
 ``J_MOTOR_GRID`` centres on 0.0064 kg*m^2, a catalogue rotor inertia for a
 same-class (3 kW, 2-pole, ~2900 rpm) machine, used ONLY as an
 order-of-magnitude anchor. It is not a calibrated parameter for this
 simulated single-phase motor (different construction, rotor-only, no load).
 All grid values are explicitly assumed sensitivity scenarios.
"""

from dataclasses import dataclass, field

import numpy as np

from plant.motor import MotorParams, _electrical, _harmonic_state
from plant.thermal import ThermalParams, simulate_temperature_ic, winding_resistance

# Approved Stage A sensitivity grid (synthetic scenarios, see module docstring).
J_MOTOR_GRID = (0.003, 0.0064, 0.015)  # [kg*m^2] rotor-only anchor sweep
J_LOAD_GRID = (0.0, 0.01, 0.05)  # [kg*m^2] shaft-referred driven-load sweep
B_GRID = (0.0004, 0.0008, 0.002)  # [N*m*s/rad] viscous friction sweep
TF_GRID = (0.0, 0.2)  # [N*m] Coulomb friction sweep

J_MOTOR_NOTE = (
    "0.0064 kg*m^2 is an order-of-magnitude catalogue reference for a "
    "same-class rotor, not a calibrated parameter for the simulated motor."
)

_LOOKUP_N = 4096  # [samples] uniform slip-grid density for circuit interpolation


@dataclass(frozen=True)
class MechParams:
    """Lumped mechanical parameters (SI units per field; all assumed synthetic)."""

    j_motor_kgm2: float = 0.0064  # [kg*m^2] rotor inertia (anchor, not calibrated)
    j_load_reflected_kgm2: float = 0.0  # [kg*m^2] driven load, shaft-referred
    gear_ratio: float = 1.0  # [dimensionless] motor:load, rigid direct drive
    b_Nms: float = 0.0008  # [N*m*s/rad] viscous friction coefficient
    tf_Nm: float = 0.0  # [N*m] Coulomb friction magnitude


def _as_mech(mech) -> MechParams:
    if isinstance(mech, MechParams):
        return mech
    if isinstance(mech, dict):
        return MechParams(**mech)
    raise TypeError(f"mech must be MechParams or kwargs dict, got {type(mech)!r}")


def j_eq(mech) -> float:
    """Equivalent shaft inertia [kg*m^2] (sum; representation keeps parts separate)."""
    m = _as_mech(mech)
    return float(m.j_motor_kgm2 + m.j_load_reflected_kgm2)


def friction_torque_Nm(omega_rad_s: float, b_Nms: float, tf_Nm: float) -> float:
    """Viscous + Coulomb friction torque [N*m], always opposing motion."""
    return float(b_Nms * omega_rad_s + tf_Nm * np.sign(omega_rad_s))


def stable_from_slope(dR_domega: float) -> bool:
    """Local stability of ``J*d(dw)/dt = R'(w*)*dw``: stable iff slope < 0."""
    return bool(dR_domega < 0.0)


def _sync(mp: MotorParams) -> tuple[float, float]:
    """Return (f_grid [Hz], omega_sync [rad/s])."""
    f_grid = mp.f_nom_Hz + mp.f_grid_offset_Hz
    return f_grid, 2.0 * np.pi * f_grid / (mp.n_poles / 2.0)


def mean_torque_direct(
    slip: np.ndarray, v_rms_V: float, mp: MotorParams, r_scale: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Direct circuit evaluation: (T_avg [N*m], I_rms [A], phi [rad]) at slip.

    Uses the actual :mod:`plant.motor` equivalent circuit (never the affine
    MPC surrogate). Air-gap torque ``T_avg = P_airgap / omega_sync``: the
    synchronous (not rotor) speed appears because the circuit expresses
    air-gap power; shaft output power would use rotor speed instead.
    """
    slip = np.asarray(slip, dtype=float)
    _, omega_sync = _sync(mp)
    elec = _electrical(slip, float(v_rms_V), mp, r_scale=r_scale)
    p_in = float(v_rms_V) * elec.i_rms * np.cos(elec.phi)  # [W]
    p_cu = elec.i_rms**2 * mp.r1_Ohm * r_scale  # [W]
    p_core = float(v_rms_V) ** 2 / mp.rc_Ohm  # [W]
    return (p_in - p_cu - p_core) / omega_sync, elec.i_rms, elec.phi


class SlipLookup:
    """Interpolated circuit table over a uniform slip grid (numerics only).

    Built from the actual vectorized circuit; :meth:`eval` reproduces it to
    interpolation tolerance (pinned by test). Rebuilt whenever (V_rms,
    r_scale) changes; harmonic tables are built only when harmonics are on.
    """

    def __init__(self, mp: MotorParams, v_rms_V: float, r_scale: float):
        self.v_rms_V = float(v_rms_V)
        self.r_scale = float(r_scale)
        self.grid = np.linspace(mp.s_min, mp.s_max, _LOOKUP_N)
        t_avg, i_rms, phi = mean_torque_direct(self.grid, self.v_rms_V, mp, r_scale)
        self.t_avg = t_avg
        self.i_rms = i_rms
        self.phi = phi
        harm3 = mp.v_harm3_frac * mp.v_rms_nom_V
        harm5 = mp.v_harm5_frac * mp.v_rms_nom_V
        if harm3 or harm5:
            h3, ph3 = _harmonic_state(self.grid, harm3, 3, mp, r_scale)
            h5, ph5 = _harmonic_state(self.grid, harm5, 5, mp, r_scale)
        else:
            h3 = ph3 = h5 = ph5 = np.zeros_like(self.grid)
        self.h3_rms, self.ph3, self.h5_rms, self.ph5 = h3, ph3, h5, ph5
        self._s0 = float(mp.s_min)
        self._ds = float((mp.s_max - mp.s_min) / (_LOOKUP_N - 1))

    def eval(self, s: float) -> tuple[float, float, float]:
        """Lerp (T_avg [N*m], I_rms [A], phi [rad]) at slip s (clamped)."""
        x = (float(s) - self._s0) / self._ds
        x = min(max(x, 0.0), float(_LOOKUP_N - 1))
        i0 = int(x)
        i1 = min(i0 + 1, _LOOKUP_N - 1)
        f = x - i0
        return (
            float(self.t_avg[i0] + f * (self.t_avg[i1] - self.t_avg[i0])),
            float(self.i_rms[i0] + f * (self.i_rms[i1] - self.i_rms[i0])),
            float(self.phi[i0] + f * (self.phi[i1] - self.phi[i0])),
        )


def build_slip_lookup(mp: MotorParams, v_rms_V: float, r_scale: float) -> SlipLookup:
    """Build a :class:`SlipLookup` for (V_rms [V], r_scale [-])."""
    return SlipLookup(mp, v_rms_V, r_scale)


@dataclass
class EquilibriumPoint:
    """One torque-balance root (mean torque; ripple excluded from fixed point)."""

    omega: float  # [rad/s] equilibrium rotor speed
    slip: float  # [dimensionless] operating slip at root
    t_avg: float  # [N*m] circuit mean torque at root
    residual: float  # [N*m] R(omega*) ~ 0
    slope: float  # [N*m*s/rad] dR/domega at root
    stable: bool  # local stability (slope < 0)
    slip_clamped: bool  # root at a slip-bound edge


@dataclass
class EquilibriumResult:
    """Equilibrium analysis for one (load, voltage, temperature-scale) condition."""

    load_Nm: float
    u: float
    r_scale: float
    status: str  # OK | OK_SLIP_CLAMP | NO_EQUILIBRIUM | BEYOND_CAPABILITY | UNSTABLE_ONLY
    selected: EquilibriumPoint | None
    points: list = field(default_factory=list)
    rejected: list = field(default_factory=list)  # [{omega, reason}]


def _residual(
    omega: float,
    load: float,
    v_rms: float,
    mp: MotorParams,
    mech: MechParams,
    r_scale: float,
    omega_sync: float,
) -> tuple[float, float, bool]:
    """Return (R [N*m], slip [-], clamped [bool]) at rotor speed omega [rad/s]."""
    s_raw = (omega_sync - float(omega)) / omega_sync
    clamped = bool(s_raw < mp.s_min or s_raw > mp.s_max)
    s = min(max(s_raw, mp.s_min), mp.s_max)
    (t_avg,) = mean_torque_direct(np.array([s]), v_rms, mp, r_scale)[:1]
    t_avg = float(t_avg[0])
    r = t_avg - load - friction_torque_Nm(float(omega), mech.b_Nms, mech.tf_Nm)
    return r, s, clamped


def solve_equilibrium(
    load_Nm: float,
    u: float,
    mp: MotorParams | None = None,
    mech=None,
    r_scale: float = 1.0,
    n_scan: int = 2001,
) -> EquilibriumResult:
    """Find torque-balance roots using the actual circuit (never the surrogate).

    Solves ``T_avg(omega, u, r_scale) = T_load + B*omega + Tf*sign(omega)``
    over ``omega in [wsync*(1-smax), wsync*(1-smin)]`` by dense scan +
    bisection of sign-change brackets. Stability from the central-difference
    slope (stable iff ``dR/domega < 0``). Selection prefers the highest-speed
    interior stable root; edge roots are flagged ``slip_clamped``.
    """
    mp = mp or MotorParams()
    mech = _as_mech(MechParams() if mech is None else mech)
    load = float(load_Nm)
    v_rms = float(mp.v_rms_nom_V * u)
    _, omega_sync = _sync(mp)
    w_lo = omega_sync * (1.0 - mp.s_max)
    w_hi = omega_sync * (1.0 - mp.s_min)
    grid = np.linspace(w_lo, w_hi, int(n_scan))
    s_raw = (omega_sync - grid) / omega_sync
    t_avg, _, _ = mean_torque_direct(s_raw, v_rms, mp, float(r_scale))
    fric = mech.b_Nms * grid + mech.tf_Nm * np.sign(grid)
    res = t_avg - load - fric

    points: list[EquilibriumPoint] = []
    n = len(grid)

    def _refine(a: float, b: float) -> float:
        ra, _, _ = _residual(a, load, v_rms, mp, mech, float(r_scale), omega_sync)
        for _ in range(60):
            m = 0.5 * (a + b)
            rm, _, _ = _residual(m, load, v_rms, mp, mech, float(r_scale), omega_sync)
            if ra * rm <= 0.0:
                b = m
            else:
                a, ra = m, rm
        return 0.5 * (a + b)

    for i in range(n - 1):
        bracket = False
        if res[i] == 0.0:
            w_star = float(grid[i])
            bracket = True
        elif res[i] * res[i + 1] < 0.0:
            w_star = _refine(float(grid[i]), float(grid[i + 1]))
            bracket = True
        if not bracket:
            continue
        h = max(1e-6 * omega_sync, 1e-9)
        rp, _, _ = _residual(w_star + h, load, v_rms, mp, mech, float(r_scale), omega_sync)
        rm, _, _ = _residual(w_star - h, load, v_rms, mp, mech, float(r_scale), omega_sync)
        slope = (rp - rm) / (2.0 * h)
        r0, s0, cl = _residual(w_star, load, v_rms, mp, mech, float(r_scale), omega_sync)
        (t0,) = mean_torque_direct(np.array([s0]), v_rms, mp, float(r_scale))[:1]
        edge = w_star <= grid[0] + (grid[1] - grid[0]) or w_star >= grid[-1] - (grid[1] - grid[0])
        points.append(
            EquilibriumPoint(
                omega=w_star,
                slip=s0,
                t_avg=float(t0[0]),
                residual=r0,
                slope=slope,
                stable=stable_from_slope(slope),
                slip_clamped=bool(cl or edge),
            )
        )

    rejected: list[dict] = []
    interior_stable = [pt for pt in points if pt.stable and not pt.slip_clamped]
    edge_stable = [pt for pt in points if pt.stable and pt.slip_clamped]
    for pt in points:
        if not pt.stable:
            rejected.append({"omega": pt.omega, "reason": "unstable"})
    if interior_stable:
        selected = max(interior_stable, key=lambda pt: pt.omega)
        status = "OK"
    elif edge_stable:
        selected = max(edge_stable, key=lambda pt: pt.omega)
        status = "OK_SLIP_CLAMP"
        rejected.append({"omega": selected.omega, "reason": "slip_clamp_edge"})
    elif points:
        selected = None
        status = "UNSTABLE_ONLY"
    else:
        selected = None
        # Distinguish demand-beyond-capability from an empty interior.
        if float(np.max(res)) < 0.0:
            status = "BEYOND_CAPABILITY"
        elif float(np.min(res)) > 0.0:
            status = "SLIP_CLAMP"
        else:
            status = "NO_EQUILIBRIUM"
    return EquilibriumResult(
        load_Nm=load,
        u=float(u),
        r_scale=float(r_scale),
        status=status,
        selected=selected,
        points=points,
        rejected=rejected,
    )


@dataclass
class CoupledResult:
    """Sample-level traces from one continuous coupled integration segment/run."""

    t_s: np.ndarray  # [s] global simulation time (never reset at window bounds)
    v_true_V: np.ndarray  # [V]
    i_true_A: np.ndarray  # [A]
    t_avg_Nm: np.ndarray  # [N*m] circuit mean electromagnetic torque
    torque_Nm: np.ndarray  # [N*m] instantaneous electromagnetic torque (with ripple)
    speed_rad_s: np.ndarray  # [rad/s] rotor speed (post-update)
    speed_rpm: np.ndarray  # [rev/min]
    slip: np.ndarray  # [dimensionless] clamped operating slip
    slip_clamp_flags: np.ndarray  # [bool] slip bound reached (explicit diagnostic)
    below_capability_flags: np.ndarray  # [bool] raw slip above s_max (demand too high)
    temp_C: np.ndarray  # [°C] Stage A approximate thermal trace
    accel_rad_s2: np.ndarray  # [rad/s^2] angular acceleration (ODE right-hand side / J)
    net_accel_torque_Nm: np.ndarray  # [N*m] T_e - T_load - T_friction (identity)
    load_Nm: np.ndarray  # [N*m] demand input
    u_schedule: np.ndarray  # [pu] applied throttle per sample
    init_status: str  # equilibrium status at segment start
    init_omega: float  # [rad/s] starting rotor speed
    r_scale: float  # [-] Stage A per-segment temperature rescale
    f_grid_Hz: float  # [Hz]


def concat_coupled(parts: list[CoupledResult]) -> CoupledResult:
    """Concatenate window segments into one continuous result (shared primitive)."""
    if len(parts) == 1:
        return parts[0]
    return CoupledResult(
        t_s=np.concatenate([p.t_s for p in parts]),
        v_true_V=np.concatenate([p.v_true_V for p in parts]),
        i_true_A=np.concatenate([p.i_true_A for p in parts]),
        t_avg_Nm=np.concatenate([p.t_avg_Nm for p in parts]),
        torque_Nm=np.concatenate([p.torque_Nm for p in parts]),
        speed_rad_s=np.concatenate([p.speed_rad_s for p in parts]),
        speed_rpm=np.concatenate([p.speed_rpm for p in parts]),
        slip=np.concatenate([p.slip for p in parts]),
        slip_clamp_flags=np.concatenate([p.slip_clamp_flags for p in parts]),
        below_capability_flags=np.concatenate([p.below_capability_flags for p in parts]),
        temp_C=np.concatenate([p.temp_C for p in parts]),
        accel_rad_s2=np.concatenate([p.accel_rad_s2 for p in parts]),
        net_accel_torque_Nm=np.concatenate([p.net_accel_torque_Nm for p in parts]),
        load_Nm=np.concatenate([p.load_Nm for p in parts]),
        u_schedule=np.concatenate([p.u_schedule for p in parts]),
        init_status=parts[0].init_status,
        init_omega=parts[0].init_omega,
        r_scale=parts[0].r_scale,
        f_grid_Hz=parts[0].f_grid_Hz,
    )


class CoupledPlant:
    """Continuous coupled electro-mechanical plant (Stage A).

    State carried across calls: rotor speed ``omega`` [rad/s] and temperature
    ``temp`` [°C]. :meth:`step_window` advances one control window with
    two-pass Stage A thermal handling and a continuous global time base, so
    the ripple phase never resets at MPC boundaries. Missing static
    equilibrium never truncates integration: the transient is always
    integrated where numerically valid while flags record the condition.
    """

    uses_legacy_tau = False  # retired default; legacy path lives in plant.motor

    def __init__(
        self,
        motor_params: MotorParams | None = None,
        mech=None,
        thermal_params: ThermalParams | None = None,
        fs_hz: float = 10_000.0,
    ):
        self.mp = motor_params or MotorParams()
        self.mech = _as_mech(MechParams() if mech is None else mech)
        self.th = thermal_params or ThermalParams()
        self.fs_hz = float(fs_hz)
        self.j_equiv = j_eq(self.mech)
        self.omega: float | None = None
        self.temp: float = self.th.t_amb_C
        self._tables: dict[tuple[float, float], SlipLookup] = {}

    def solve_init(self, load0: float, u0: float = 1.0) -> tuple[float, str]:
        """Solve a defensible stable start speed; NO_EQUILIBRIUM reported, never hidden."""
        eq = solve_equilibrium(float(load0), float(u0), self.mp, self.mech, r_scale=1.0)
        if eq.selected is not None:
            self.omega = float(eq.selected.omega)
            return self.omega, eq.status
        self.omega = None
        return float("nan"), eq.status

    def _table(self, v_rms: float, r_scale: float) -> SlipLookup:
        key = (round(float(v_rms), 9), round(float(r_scale), 9))
        table = self._tables.get(key)
        if table is None:
            table = build_slip_lookup(self.mp, float(v_rms), float(r_scale))
            self._tables[key] = table
        return table

    def _integrate(
        self,
        load: np.ndarray,
        u_arr: np.ndarray,
        t_start: float,
        omega0: float,
        r_scale: float,
    ) -> dict:
        """Single-pass semi-implicit Euler integration at full sample rate."""
        mp, mech = self.mp, self.mech
        fs = self.fs_hz
        dt = 1.0 / fs
        f_grid, omega_sync = _sync(mp)
        n = int(load.shape[0])
        v = np.empty(n)
        i = np.empty(n)
        t_avg = np.empty(n)
        te = np.empty(n)
        speed = np.empty(n)
        slip = np.empty(n)
        clamp = np.zeros(n, dtype=bool)
        below = np.zeros(n, dtype=bool)
        accel = np.empty(n)
        net = np.empty(n)
        v_pk_base = mp.v_rms_nom_V * np.sqrt(2.0)
        w = float(omega0)
        last_table = None
        last_vrms = float("nan")
        for k in range(n):
            u_k = float(u_arr[k]) if u_arr.ndim else float(u_arr)
            v_rms = float(mp.v_rms_nom_V * u_k)
            if last_table is None or v_rms != last_vrms:
                last_table = self._table(v_rms, r_scale)
                last_vrms = v_rms
            s_raw = (omega_sync - w) / omega_sync
            below[k] = bool(s_raw > mp.s_max)
            s = mp.s_min if s_raw < mp.s_min else (mp.s_max if s_raw > mp.s_max else s_raw)
            clamp[k] = bool(s != s_raw)
            t_k, ir_k, ph_k = last_table.eval(s)
            phase = 2.0 * np.pi * f_grid * (t_start + k * dt)  # global time: no reset
            te_k = t_k * (1.0 + mp.torque_ripple_frac * np.cos(2.0 * phase))
            fr_k = friction_torque_Nm(w, mech.b_Nms, mech.tf_Nm)
            net_k = te_k - float(load[k]) - fr_k
            a_k = net_k / self.j_equiv
            w = w + dt * a_k
            speed[k] = w
            accel[k] = a_k
            net[k] = net_k
            slip[k] = s
            t_avg[k] = t_k
            te[k] = te_k
            v[k] = v_pk_base * u_k * np.sin(phase) + mp.v_dc_V
            i[k] = ir_k * np.sqrt(2.0) * np.sin(phase - ph_k)
            if mp.v_harm3_frac or mp.v_harm5_frac:
                x = (s - last_table._s0) / last_table._ds
                x = min(max(x, 0.0), float(_LOOKUP_N - 1))
                i0 = int(x)
                i1 = min(i0 + 1, _LOOKUP_N - 1)
                f = x - i0
                h3 = last_table.h3_rms[i0] + f * (last_table.h3_rms[i1] - last_table.h3_rms[i0])
                p3 = last_table.ph3[i0] + f * (last_table.ph3[i1] - last_table.ph3[i0])
                h5 = last_table.h5_rms[i0] + f * (last_table.h5_rms[i1] - last_table.h5_rms[i0])
                p5 = last_table.ph5[i0] + f * (last_table.ph5[i1] - last_table.ph5[i0])
                if mp.v_harm3_frac:
                    v[k] += (
                        mp.v_harm3_frac
                        * v_pk_base
                        * u_k
                        * np.sin(3.0 * phase + mp.v_harm3_phase_rad)
                    )
                    i[k] += h3 * np.sqrt(2.0) * np.sin(3.0 * phase + mp.v_harm3_phase_rad - p3)
                if mp.v_harm5_frac:
                    v[k] += (
                        mp.v_harm5_frac
                        * v_pk_base
                        * u_k
                        * np.sin(5.0 * phase + mp.v_harm5_phase_rad)
                    )
                    i[k] += h5 * np.sqrt(2.0) * np.sin(5.0 * phase + mp.v_harm5_phase_rad - p5)
        return {
            "v": v,
            "i": i,
            "t_avg": t_avg,
            "te": te,
            "speed": speed,
            "slip": slip,
            "clamp": clamp,
            "below": below,
            "accel": accel,
            "net": net,
            "omega_end": w,
        }

    def step_window(self, w_load: np.ndarray, u: float, t_start: float) -> CoupledResult:
        """Advance one control window; carry (omega, temp) forward.

        Stage A thermal handling: pass 1 at nominal resistance from the
        carried state gives the window current, whose mean temperature sets a
        frozen per-window ``r_scale``; pass 2 re-integrates from the same
        carried state. No per-sample electro-thermal feedback (Stage B).
        """
        load = np.asarray(w_load, dtype=float).ravel()
        n = load.shape[0]
        if self.omega is None:
            _, status = self.solve_init(float(load[0]), float(u))
            if self.omega is None:
                # No equilibrium anywhere near start: begin at synchronous
                # speed and let flags record the collapse (never silently
                # substitute the legacy load-derived target).
                _, omega_sync = _sync(self.mp)
                self.omega = float(omega_sync * (1.0 - self.mp.s_min))
                status = "NO_EQUILIBRIUM_FORCED_START"
        else:
            status = "CONTINUED"
        u_arr = np.full(n, float(u))
        omega0 = float(self.omega)
        p1 = self._integrate(load, u_arr, t_start, omega0, r_scale=1.0)
        temp1 = simulate_temperature_ic(p1["i"], self.fs_hz, self.th, t_init_C=self.temp)
        r_scale = float(winding_resistance(float(np.mean(temp1)), self.th) / self.th.r_winding_Ohm)
        p2 = self._integrate(load, u_arr, t_start, omega0, r_scale=r_scale)
        temp2 = simulate_temperature_ic(p2["i"], self.fs_hz, self.th, t_init_C=self.temp)
        self.omega = float(p2["omega_end"])
        self.temp = float(temp2[-1])
        t_s = t_start + np.arange(n) / self.fs_hz
        _, f_grid = _sync(self.mp)
        return CoupledResult(
            t_s=t_s,
            v_true_V=p2["v"],
            i_true_A=p2["i"],
            t_avg_Nm=p2["t_avg"],
            torque_Nm=p2["te"],
            speed_rad_s=p2["speed"],
            speed_rpm=p2["speed"] * 60.0 / (2.0 * np.pi),
            slip=p2["slip"],
            slip_clamp_flags=p2["clamp"],
            below_capability_flags=p2["below"],
            temp_C=temp2,
            accel_rad_s2=p2["accel"],
            net_accel_torque_Nm=p2["net"],
            load_Nm=load,
            u_schedule=u_arr,
            init_status=status,
            init_omega=omega0,
            r_scale=r_scale,
            f_grid_Hz=f_grid,
        )

    def run(
        self, load: np.ndarray, u, t_start: float = 0.0, window_samples: int | None = None
    ) -> CoupledResult:
        """Continuous run over ``load``; optionally chunked into windows.

        Chunked and single-shot runs share the identical primitive
        (:meth:`step_window`) and carried state; chunking exists so the
        harness can interleave MPC control updates at window boundaries.
        """
        load = np.asarray(load, dtype=float).ravel()
        if np.ndim(u) == 0:
            u_arr = np.full(load.shape[0], float(u))
        else:
            u_arr = np.asarray(u, dtype=float).ravel()
        if window_samples is None:
            window_samples = load.shape[0]
        parts: list[CoupledResult] = []
        for start in range(0, load.shape[0], int(window_samples)):
            end = min(start + int(window_samples), load.shape[0])
            seg_u = u_arr[start:end]
            u0 = float(seg_u[0])
            if not np.all(seg_u == u0):
                raise ValueError("step_window requires constant u per window")
            parts.append(self.step_window(load[start:end], u0, t_start + start / self.fs_hz))
        return concat_coupled(parts)
