"""Week 6K: three-phase dq induction-machine transient plant (simulation only).

Scope: a fixed-step transient dq model of a balanced (optionally unbalanced)
three-phase induction machine with an iron-loss (core-loss) shunt, its
mechanical rotor dynamics, and the small-signal linearisation used by the
physics tests. This is a SIMULATION model of model-checking quality: it proves
the dq equations, the integrator and the attack-detection logic are internally
consistent. It does NOT prove anything about a real machine.

Model (synchronous reference frame, amplitude-invariant dq, SI units):

    States y = [lambda_ds, lambda_qs, lambda_dr, lambda_qr, omega_m]
    (4 electrical flux linkages + 1 mechanical rotor speed).

    Leakage/magnetizing split:
        lambda_ds = L_ls i_ds + lambda_md ,  lambda_qs = L_ls i_qs + lambda_mq
        lambda_dr = L_lr i_dr + lambda_md ,  lambda_qr = L_lr i_qr + lambda_mq

    Core loss is a shunt resistance R_c in parallel with the magnetizing
    inductance L_m in BOTH axes. The magnetizing (node) flux lambda_m is NOT
    integrated: it is eliminated algebraically from the node current balance.
    The core-loss current is driven by the magnetizing-branch voltage, which
    in the synchronous frame is the rotational EMF j*omega_e*lambda_m (the
    frame-derivative term d(lambda_m)/dt is the fast branch transient and is
    dropped; see APPROXIMATIONS). This gives a QUASI-STATIC reduction:

        i_s + i_r = lambda_m/L_m + i_c ,   i_c = j*omega_e*lambda_m / R_c
        i_s = (lambda_s - lambda_m)/L_ls ,  i_r = (lambda_r - lambda_m)/L_lr

        => [ K  -g ] [lambda_md]   [ lambda_ds/L_ls + lambda_dr/L_lr ]
           [ g  K  ] [lambda_mq] = [ lambda_qs/L_ls + lambda_qr/L_lr ]
        with K = 1/L_ls + 1/L_lr + 1/L_m and g = omega_e/R_c.

    State equations (omega_e = 2*pi*f_s, omega_r = pole_pairs*omega_m):
        d(lambda_ds)/dt = v_ds - R_s i_ds + omega_e lambda_qs
        d(lambda_qs)/dt = v_qs - R_s i_qs - omega_e lambda_ds
        d(lambda_dr)/dt = -R_r i_dr + (omega_e - omega_r) lambda_qr
        d(lambda_qr)/dt = -R_r i_qr - (omega_e - omega_r) lambda_dr
        J d(omega_m)/dt = T_e - T_load - B omega_m - Tf sign(omega_m)

    Electromagnetic torque: the core-loss current produces no shaft torque, so
    the air-gap torque is taken from the ROTOR current and the magnetizing flux
        T_e = (3/2) pole_pairs (lambda_mq i_dr - lambda_md i_qr).
    (The stator-current form (3/2)p(lambda_md i_qs - lambda_mq i_ds) is the
    no-core-loss identity and OVERSTATES torque once i_c != 0 by the core-loss
    cross term; see the module tests. This is a found correction to the plan.)

Integration: classical RK4 on all 5 states with N_sub sub-steps per output
sample (default N_sub=8 at 10 kHz -> 80 kHz). The committed step gate
|lambda|max*dt <= 5e-3 is evaluated on the 4x4 electrical matrix; see
:mod:`sim.run_week6k` and the committed tolerance table.

Honest limits: every parameter is reused from the committed :class:`plant.
motor.MotorParams` equivalent circuit (Rc included); nothing is re-fit. The
model is NOT validated against a real machine.
"""

from dataclasses import dataclass

import numpy as np

APPROXIMATIONS = (
    "1. Lumped dq model; no spatial harmonics, no saturation, no skin effect.",
    "2. Core loss enters as a single constant shunt R_c in the magnetizing "
    "branch of both axes (no hysteresis/eddy split, no flux or frequency "
    "dependence; the frequency/flux-dependent form is a Week 6L proposal).",
    "3. Quasi-static core-loss branch: i_c is driven by the rotational EMF "
    "j*omega_e*lambda_m; the branch's own fast transient (pole ~1.86e5 rad/s, "
    "measured from the 6-state linearisation) is not integrated, which keeps "
    "the committed 4-state step gate. The reduction is exact at electrical "
    "steady state and reproduces the committed eigenvalue table.",
    "4. Constant parameters (no temperature feedback in 6K); winding "
    "resistance is the cold committed value.",
    "5. Rigid single inertia, viscous + Coulomb friction; no shaft compliance or gear dynamics.",
    "6. Torque from the rotor current and magnetizing flux (the core-loss "
    "current makes no torque); see the module docstring.",
)


@dataclass(frozen=True)
class DQParams:
    """Three-phase dq machine parameters (units documented per field).

    Electrical values reuse the committed :class:`plant.motor.MotorParams`
    equivalent circuit so the dq model and the committed per-phase circuit
    speak about the SAME machine. Where the committed circuit is a per-phase
    (single-phase) model, this module is the three-phase machine with the same
    per-phase parameters.
    """

    v_rms_nom_V: float = 230.0  # [V rms] per-phase nominal voltage
    f_nom_Hz: float = 50.0  # [Hz] stator supply frequency
    n_poles: int = 2  # [count] pole number (2 -> pole pairs = 1)
    r1_Ohm: float = 0.4  # [Ohm] stator resistance
    x1_Ohm: float = 1.2  # [Ohm] stator leakage reactance at f_nom
    r2_Ohm: float = 0.48  # [Ohm] rotor resistance referred to stator
    x2_Ohm: float = 1.2  # [Ohm] rotor leakage reactance referred to stator
    rc_Ohm: float = 350.0  # [Ohm] core-loss shunt resistance
    xm_Ohm: float = 45.0  # [Ohm] magnetizing reactance at f_nom
    j_kgm2: float = 0.0064  # [kg*m^2] rotor inertia (catalogue anchor, synthetic)
    b_Nms: float = 0.0008  # [N*m*s/rad] viscous friction
    tf_Nm: float = 0.0  # [N*m] Coulomb friction magnitude
    n_sub: int = 8  # [count] RK4 sub-steps per output sample


@dataclass(frozen=True)
class _Inductances:
    l_ls: float  # [H] stator leakage
    l_lr: float  # [H] rotor leakage
    l_m: float  # [H] magnetizing


def inductances(p: DQParams) -> _Inductances:
    """Derive leakage/magnetizing inductances [H] from the reactances at f_nom."""
    w_e = 2.0 * np.pi * p.f_nom_Hz
    return _Inductances(
        l_ls=p.x1_Ohm / w_e,
        l_lr=p.x2_Ohm / w_e,
        l_m=p.xm_Ohm / w_e,
    )


def pole_pairs(p: DQParams) -> float:
    """Number of pole pairs (2 poles -> 1)."""
    return p.n_poles / 2.0


def _magnetizing_flux(li: _Inductances, p: DQParams, w_e: float, x):
    """Eliminate lambda_m from the node balance; return (lambda_md, lambda_mq).

    Solves [[K, -g], [g, K]] [lmd, lmq]^T = [a_d, a_q]^T with
    K = 1/L_ls + 1/L_lr + 1/L_m, g = w_e/R_c (quasi-static core-loss branch).
    """
    lds, lqs, ldr, lqr = x[0], x[1], x[2], x[3]
    k = 1.0 / li.l_ls + 1.0 / li.l_lr + 1.0 / li.l_m
    g = w_e / p.rc_Ohm
    a_d = lds / li.l_ls + ldr / li.l_lr
    a_q = lqs / li.l_ls + lqr / li.l_lr
    det = k * k + g * g
    lmd = (k * a_d + g * a_q) / det
    lmq = (-g * a_d + k * a_q) / det
    return lmd, lmq


@dataclass
class DQAux:
    """Per-sample secondary quantities derived from the state (SI units)."""

    v_ds: float  # [V] stator d-axis voltage
    v_qs: float  # [V] stator q-axis voltage
    l_md: float  # [Wb] magnetizing flux, d axis
    l_mq: float  # [Wb] magnetizing flux, q axis
    i_ds: float  # [A] stator d current
    i_qs: float  # [A] stator q current
    i_dr: float  # [A] rotor d current (stator-referred)
    i_qr: float  # [A] rotor q current (stator-referred)
    v_md: float  # [V] magnetizing-branch d voltage (rotational EMF)
    v_mq: float  # [V] magnetizing-branch q voltage (rotational EMF)
    i_cd: float  # [A] core-loss d current
    i_cq: float  # [A] core-loss q current
    torque_Nm: float  # [N*m] air-gap electromagnetic torque
    p_in_W: float  # [W] air-gap-frame input power (3-phase)
    p_cu_s_W: float  # [W] stator copper loss (3-phase)
    p_cu_r_W: float  # [W] rotor copper loss (3-phase)
    p_core_W: float  # [W] core loss (3-phase)
    w_store_J: float  # [J] stored magnetic energy (3-phase dq convention)
    # state derivatives (filled by :meth:`DQMachine.derivatives`)
    dl_ds: float = 0.0
    dl_qs: float = 0.0
    dl_dr: float = 0.0
    dl_qr: float = 0.0
    d_wm: float = 0.0


class DQMachine:
    """Fixed-step three-phase dq machine with a quasi-static core-loss shunt."""

    def __init__(self, params: DQParams | None = None):
        self.p = params or DQParams()
        self.li = inductances(self.p)
        self.w_e = 2.0 * np.pi * self.p.f_nom_Hz  # [rad/s] synchronous (electrical)
        self.v_pk = self.p.v_rms_nom_V * np.sqrt(2.0)  # [V] peak phase voltage

    # ------------------------------------------------------------------ supply
    def supply(
        self, t: float, v_rms: float | None = None, a_unb: float = 0.0, phi_unb: float = 0.0
    ) -> tuple[float, float]:
        """Balanced + optional negative-sequence dq supply voltage.

        The balanced part is the DC (d, q) = (V_pk, 0). A negative-sequence
        component of amplitude ``a_unb*V_pk`` appears at 2*omega_e in the
        synchronous frame:
            v_ds = V_pk + V_pk a_unb cos(2 w_e t + phi)
            v_qs =        V_pk a_unb sin(2 w_e t + phi)
        """
        vpk = self.v_pk if v_rms is None else v_rms * np.sqrt(2.0)
        if a_unb == 0.0:
            return vpk, 0.0
        ang = 2.0 * self.w_e * t + phi_unb
        return vpk + vpk * a_unb * np.cos(ang), vpk * a_unb * np.sin(ang)

    def unbalance_phasor(
        self, v_rms: float | None = None, a_unb: float = 0.0, phi_unb: float = 0.0
    ) -> np.ndarray:
        """Complex dq input phasor U for the 2*omega_e negative-sequence term.

        With u(t) = u_c cos(2 w_e t) + u_s sin(2 w_e t), U = u_c - j u_s.
        """
        vpk = self.v_pk if v_rms is None else v_rms * np.sqrt(2.0)
        u_c = np.array([vpk * a_unb * np.cos(phi_unb), vpk * a_unb * np.sin(phi_unb)])
        u_s = np.array([-vpk * a_unb * np.sin(phi_unb), vpk * a_unb * np.cos(phi_unb)])
        return u_c - 1j * u_s

    # ------------------------------------------------------------------- rhs
    def _aux(self, x, w_e: float, v_dq) -> DQAux:
        p, li = self.p, self.li
        lmd, lmq = _magnetizing_flux(li, p, w_e, x)
        i_ds = (x[0] - lmd) / li.l_ls
        i_qs = (x[1] - lmq) / li.l_ls
        i_dr = (x[2] - lmd) / li.l_lr
        i_qr = (x[3] - lmq) / li.l_lr
        # magnetizing-branch (rotational) voltage and core-loss current
        v_md, v_mq = -w_e * lmq, w_e * lmd
        i_cd, i_cq = v_md / p.rc_Ohm, v_mq / p.rc_Ohm
        i_md = i_ds + i_dr - i_cd
        i_mq = i_qs + i_qr - i_cq
        torque = 1.5 * pole_pairs(p) * (lmq * i_dr - lmd * i_qr)
        p_in = 1.5 * (v_dq[0] * i_ds + v_dq[1] * i_qs)
        p_cu_s = 1.5 * p.r1_Ohm * (i_ds * i_ds + i_qs * i_qs)
        p_cu_r = 1.5 * p.r2_Ohm * (i_dr * i_dr + i_qr * i_qr)
        p_core = 1.5 * (v_md * i_cd + v_mq * i_cq)
        w_store = (
            1.5
            * 0.5
            * (
                li.l_ls * (i_ds * i_ds + i_qs * i_qs)
                + li.l_lr * (i_dr * i_dr + i_qr * i_qr)
                + li.l_m * (i_md * i_md + i_mq * i_mq)
            )
        )
        return DQAux(
            v_ds=v_dq[0],
            v_qs=v_dq[1],
            l_md=lmd,
            l_mq=lmq,
            i_ds=i_ds,
            i_qs=i_qs,
            i_dr=i_dr,
            i_qr=i_qr,
            v_md=v_md,
            v_mq=v_mq,
            i_cd=i_cd,
            i_cq=i_cq,
            torque_Nm=torque,
            p_in_W=p_in,
            p_cu_s_W=p_cu_s,
            p_cu_r_W=p_cu_r,
            p_core_W=p_core,
            w_store_J=w_store,
        )

    def aux(self, x, v_dq=(None, None), t: float = 0.0) -> DQAux:
        """Secondary quantities at state ``x`` (v defaults to the balanced supply)."""
        if v_dq[0] is None:
            v_dq = self.supply(t)
        return self._aux(x, self.w_e, v_dq)

    def _electrical_rhs(self, x4, v_dq, w_r: float, w_e: float) -> np.ndarray:
        """4-vector electrical flux derivative. SINGLE SOURCE OF TRUTH.

        Used by both :meth:`derivatives` (time-domain) and
        :meth:`electrical_matrix` (linearisation); keeping one implementation
        prevents the linearisation from silently drifting from the nonlinear
        model (a duplicated RHS hid a sign error from the steady-state tests
        during Week 6K mutant checking).
        """
        p = self.p
        a = self._aux(np.array([x4[0], x4[1], x4[2], x4[3], 0.0]), w_e, v_dq)
        return np.array(
            [
                a.v_ds - p.r1_Ohm * a.i_ds + w_e * x4[1],
                a.v_qs - p.r1_Ohm * a.i_qs - w_e * x4[0],
                -p.r2_Ohm * a.i_dr + (w_e - w_r) * x4[3],
                -p.r2_Ohm * a.i_qr - (w_e - w_r) * x4[2],
            ]
        )

    def derivatives(
        self,
        x,
        t: float,
        load_Nm: float,
        v_rms=None,
        a_unb: float = 0.0,
        phi_unb: float = 0.0,
        free_rotor: bool = True,
    ) -> DQAux:
        """State derivative (via the returned aux + internal d/dt terms).

        Returns a :class:`DQAux`; use :meth:`dy_dt` for the raw 5-vector.
        """
        p = self.p
        v_dq = self.supply(t, v_rms=v_rms, a_unb=a_unb, phi_unb=phi_unb)
        a = self._aux(x, self.w_e, v_dq)
        w_r = pole_pairs(p) * x[4]
        dl_ds, dl_qs, dl_dr, dl_qr = self._electrical_rhs(np.asarray(x), v_dq, w_r, self.w_e)
        if free_rotor:
            fr = p.b_Nms * x[4] + p.tf_Nm * np.sign(x[4])
            d_wm = (a.torque_Nm - load_Nm - fr) / p.j_kgm2
        else:
            d_wm = 0.0
        a.dl_ds, a.dl_qs, a.dl_dr, a.dl_qr, a.d_wm = dl_ds, dl_qs, dl_dr, dl_qr, d_wm
        return a

    def dy_dt(self, x, t: float, load_Nm: float, **kw) -> np.ndarray:
        a = self.derivatives(x, t, load_Nm, **kw)
        return np.array([a.dl_ds, a.dl_qs, a.dl_dr, a.dl_qr, a.d_wm])

    def rk4_step(self, x, t: float, dt: float, load_Nm: float, **kw) -> np.ndarray:
        """One classical RK4 step of the 5-state (r, v, k1..k4) update."""
        k1 = self.dy_dt(x, t, load_Nm, **kw)
        k2 = self.dy_dt(x + 0.5 * dt * k1, t + 0.5 * dt, load_Nm, **kw)
        k3 = self.dy_dt(x + 0.5 * dt * k2, t + 0.5 * dt, load_Nm, **kw)
        k4 = self.dy_dt(x + dt * k3, t + dt, load_Nm, **kw)
        return x + (dt / 6.0) * (k1 + 2.0 * k2 + 2.0 * k3 + k4)

    # -------------------------------------------------------- steady state
    def electrical_matrix(self, x_ref, w_e: float | None = None) -> tuple[np.ndarray, np.ndarray]:
        """Linear 4x4 electrical A and 4x2 input B about the given rotor speed.

        The electrical subsystem is linear for fixed omega_r, so A and B are
        exact (no operating point needed). ``x_ref`` supplies omega_m.
        """
        w_e = self.w_e if w_e is None else w_e
        p = self.p
        w_r = pole_pairs(p) * x_ref[4]
        A = np.zeros((4, 4))
        for i in range(4):
            e = np.zeros(4)
            e[i] = 1.0
            A[:, i] = self._electrical_rhs(e, (0.0, 0.0), w_r, w_e)
        z = np.zeros(4)
        B = np.zeros((4, 2))
        for j in range(2):
            ev = np.zeros(2)
            ev[j] = 1.0
            B[:, j] = self._electrical_rhs(z, tuple(ev), w_r, w_e)
        return A, B

    def torque_gradient(self, x0, h: float = 1e-7) -> np.ndarray:
        """dT_e/dx (1x5) at the operating point x0 (finite difference)."""
        t0 = self.aux(x0).torque_Nm
        c = np.zeros(5)
        for i in range(5):
            xp = np.array(x0, dtype=float)
            xp[i] += h
            c[i] = (self.aux(xp).torque_Nm - t0) / h
        return c

    def steady_state(self, slip: float, v_rms: float | None = None) -> dict:
        """Solve the electrical fixed point at a given slip (exact linear solve)."""
        p = self.p
        v_rms = p.v_rms_nom_V if v_rms is None else v_rms
        w_r = self.w_e * (1.0 - slip)
        x_ref = np.array([0.0, 0.0, 0.0, 0.0, w_r / pole_pairs(p)])
        A, B = self.electrical_matrix(x_ref)
        v_dq = np.array([v_rms * np.sqrt(2.0), 0.0])
        x4 = np.linalg.solve(A, -B @ v_dq)
        x = np.array([x4[0], x4[1], x4[2], x4[3], x_ref[4]])
        a = self.aux(x, v_dq=tuple(v_dq))
        return {
            "slip": float(slip),
            "omega_r": float(w_r),
            "omega_m": float(x_ref[4]),
            "x": x,
            "torque_Nm": a.torque_Nm,
            "i_ds": a.i_ds,
            "i_qs": a.i_qs,
            "i_dr": a.i_dr,
            "i_qr": a.i_qr,
            "l_md": a.l_md,
            "l_mq": a.l_mq,
            "p_core_W": a.p_core_W,
            "p_in_W": a.p_in_W,
        }

    def analytic_shunt_torque(self, slip: float, v_rms: float | None = None) -> float:
        """Exact per-phase circuit air-gap torque with the R_c shunt at the node.

        Reference for tests 1b/2/3: total 3-phase torque
        3*|I2|^2*(R2/s)/omega_e with I2 the rotor-branch current of the
        equivalent circuit (R_c and j*Xm in parallel with R2/s + j*X2).
        """
        p = self.p
        v = p.v_rms_nom_V if v_rms is None else v_rms
        z_s = p.r1_Ohm + 1j * p.x1_Ohm
        z_r = p.r2_Ohm / slip + 1j * p.x2_Ohm
        y = 1.0 / z_r + 1.0 / p.rc_Ohm + 1.0 / (1j * p.xm_Ohm)
        z_n = 1.0 / y
        v_n = v * z_n / (z_s + z_n)
        i2 = v_n / z_r
        return 3.0 * abs(i2) ** 2 * (p.r2_Ohm / slip) / self.w_e

    def analytic_thevenin(self) -> dict:
        """Thevenin constants and pull-out/starting torque with the R_c shunt."""
        p = self.p
        z_m = (p.rc_Ohm * 1j * p.xm_Ohm) / (p.rc_Ohm + 1j * p.xm_Ohm)
        z_s = p.r1_Ohm + 1j * p.x1_Ohm
        v_th = abs(p.v_rms_nom_V * z_m / (z_s + z_m))
        z_th = z_s * z_m / (z_s + z_m)
        r_th, x_th = z_th.real, z_th.imag
        denom = r_th + np.sqrt(r_th**2 + (x_th + p.x2_Ohm) ** 2)
        t_max = 3.0 * v_th**2 / (2.0 * self.w_e * denom)
        s_po = p.r2_Ohm / np.sqrt(r_th**2 + (x_th + p.x2_Ohm) ** 2)
        t_start = (
            3.0
            * v_th**2
            * p.r2_Ohm
            / (self.w_e * ((r_th + p.r2_Ohm) ** 2 + (x_th + p.x2_Ohm) ** 2))
        )
        return {
            "v_th_V": v_th,
            "r_th_Ohm": r_th,
            "x_th_Ohm": x_th,
            "t_max_Nm": t_max,
            "slip_pull_out": s_po,
            "t_start_Nm": t_start,
        }

    def eigenvalues(self, slip: float) -> np.ndarray:
        """Complex eigenvalues of the 4x4 electrical matrix at a slip."""
        w_r = self.w_e * (1.0 - slip)
        x_ref = np.array([0.0, 0.0, 0.0, 0.0, w_r / pole_pairs(self.p)])
        A, _ = self.electrical_matrix(x_ref)
        return np.linalg.eigvals(A)

    def max_abs_eigenvalue(self, slips) -> float:
        return max(float(np.max(np.abs(self.eigenvalues(s)))) for s in slips)

    def electromechanical_eigenvalues(self, slip: float) -> np.ndarray:
        """Full 5x5 electromechanical eigenvalues at a slip (linearised)."""
        p = self.p
        x0 = self.steady_state(slip)["x"]

        def f5(x):
            return self.dy_dt(x, 0.0, 0.0, v_rms=p.v_rms_nom_V)

        f0 = f5(x0)
        h = 1e-7
        A = np.zeros((5, 5))
        for i in range(5):
            e = np.zeros(5)
            e[i] = 1.0
            A[:, i] = (f5(x0 + h * e) - f0) / h
        return np.linalg.eigvals(A)

    def unbalance_torque_amplitude(self, slip: float, a_unb: float = 0.02) -> float:
        """|T_2we| from the linearised formula C (j2we I - A)^-1 B U_n.

        Derived (not a run): the negative-sequence dq input U_n drives the
        linear electrical system at 2*omega_e; the torque ripple amplitude is
        |C X|. C is the torque gradient, A,B the electrical matrices of the
        implemented (quasi-static shunt) model.
        """
        s = self.steady_state(slip)
        x0 = s["x"]
        A, B = self.electrical_matrix(x0)
        C = self.torque_gradient(x0)
        U = self.unbalance_phasor(a_unb=a_unb)
        X = np.linalg.solve(1j * 2.0 * self.w_e * np.eye(4) - A, B @ U)
        return float(abs(C[:4] @ X))

    # -------------------------------------------------------------- simulate
    def simulate(
        self,
        load_Nm,
        fs_hz: float,
        duration_s: float | None = None,
        n_sub: int | None = None,
        v_rms=None,
        a_unb: float = 0.0,
        phi_unb: float = 0.0,
        x0=None,
        t0: float = 0.0,
        free_rotor: bool = True,
        init_slip: float = 0.04,
    ) -> dict:
        """Fixed-step RK4 run over a scalar or per-sample load trace.

        Args:
            load_Nm: scalar or 1-D shaft-load torque [N*m].
            fs_hz: output sample rate [samples/s].
            duration_s: run length [s]; required when ``load_Nm`` is scalar,
                ignored when it is an array (length sets the horizon).
            n_sub: RK4 sub-steps per output sample (default ``p.n_sub``).
            x0: initial 5-state (defaults to the balanced fixed point at
                ``init_slip``).
            free_rotor: integrate the mechanical state (False holds speed).
            init_slip: slip used to build the default initial state.

        Returns a dict of arrays sampled at ``fs_hz`` (global time base ``t0``).
        """
        p = self.p
        n_sub = p.n_sub if n_sub is None else int(n_sub)
        load = np.asarray(load_Nm, dtype=float)
        if load.ndim == 0:
            if duration_s is None:
                raise ValueError("duration_s is required for a scalar load")
            n = int(round(float(duration_s) * fs_hz))
            load_arr = np.full(n, float(load))
        else:
            load_arr = load.ravel()
            n = load_arr.shape[0]
        if x0 is None:
            ld0 = float(load_arr[0]) if free_rotor else 0.0
            x0 = self._default_state(init_slip, v_rms, a_unb, ld0, free_rotor)
        x = np.array(x0, dtype=float)
        dt = 1.0 / fs_hz
        h = dt / n_sub
        out = {
            k: np.empty(n)
            for k in (
                "t",
                "torque",
                "speed",
                "i_ds",
                "i_qs",
                "i_dr",
                "i_qr",
                "l_md",
                "l_mq",
                "v_ds",
                "v_qs",
                "p_in",
                "p_core",
                "p_cu_s",
                "p_cu_r",
                "w_store",
                "v_md",
                "v_mq",
                "i_cd",
                "i_cq",
            )
        }
        for k in range(n):
            tk = t0 + k * dt
            a = self.aux(x, t=tk)
            out["t"][k] = tk
            out["torque"][k] = a.torque_Nm
            out["speed"][k] = x[4]
            out["i_ds"][k] = a.i_ds
            out["i_qs"][k] = a.i_qs
            out["i_dr"][k] = a.i_dr
            out["i_qr"][k] = a.i_qr
            out["l_md"][k] = a.l_md
            out["l_mq"][k] = a.l_mq
            out["v_ds"][k] = a.v_ds
            out["v_qs"][k] = a.v_qs
            out["p_in"][k] = a.p_in_W
            out["p_core"][k] = a.p_core_W
            out["p_cu_s"][k] = a.p_cu_s_W
            out["p_cu_r"][k] = a.p_cu_r_W
            out["w_store"][k] = a.w_store_J
            out["v_md"][k] = a.v_md
            out["v_mq"][k] = a.v_mq
            out["i_cd"][k] = a.i_cd
            out["i_cq"][k] = a.i_cq
            for j in range(n_sub):
                x = self.rk4_step(
                    x,
                    tk + j * h,
                    h,
                    float(load_arr[k]),
                    v_rms=v_rms,
                    a_unb=a_unb,
                    phi_unb=phi_unb,
                    free_rotor=free_rotor,
                )
        out["x_final"] = x
        out["n_sub"] = int(n_sub)
        out["dt_int_s"] = h
        return out

    def _default_state(self, slip, v_rms, a_unb, load_Nm, free_rotor):
        """Balanced electrical fixed point at the slip (speed = synchronous*(1-s))."""
        base = self.steady_state(max(min(slip, 0.2), 1e-3), v_rms=v_rms)
        return np.array(base["x"], dtype=float)


def finite_difference_jacobian(
    m: DQMachine, x, t: float, load_Nm: float, h: float = 1e-8, **kw
) -> np.ndarray:
    """5x5 numerical Jacobian of the full state derivative (diagnostics only)."""
    f0 = m.dy_dt(x, t, load_Nm, **kw)
    J = np.zeros((5, 5))
    for i in range(5):
        xp = np.array(x, dtype=float)
        xp[i] += h
        J[:, i] = (m.dy_dt(xp, t, load_Nm, **kw) - f0) / h
    return J
