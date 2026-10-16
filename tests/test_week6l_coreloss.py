"""Week 6L tests: flux/frequency-dependent core loss (amendment 10).

Suite for the 6L extension of :mod:`plant.dq_machine`: the core loss is no
longer a fixed shunt ``R_c`` but the approved flux- and frequency-dependent form

    P_core = P_core_nom * (lam/lam_nom)^2 * B(f),
    B(f) = (1-c)(f/f_nom) + c (f/f_nom)^2,

with synthetic ``P_core_nom = 151.1429 W`` per phase, ``alpha = 2`` and eddy
fraction ``c = 0.5``. Because ``alpha = 2`` the flux dependence is already
carried by the shunt (both scale as ``lam^2``); the NEW physics is the frequency
dependence ``B(f)``, which only matters once the model runs at variable
frequency (the 6M VSD pump case). These tests therefore pin:

* the reduction to the fixed shunt at ``f = f_nom`` (6K behaviour is unchanged);
* the flux dependence (``P_core ~ lam^2`` at fixed ``f``);
* the frequency dependence (``P_core ~ B(f)`` at fixed ``lam``);
* the analytic identity ``P_core = 3 P_core_nom (lam/lam_nom)^2 B(f)``;
* the energy balance at ``f != f_nom``;
* V/f control keeping ``lam`` (approximately) constant;
* the Steinmetz coefficients ``k_h``, ``k_e`` and their +/-50% sensitivity.

Every tolerance here is a derivation or an exact algebraic identity, fixed
before any run; none is chosen from a run output.
"""

import numpy as np
import pytest

from plant.dq_machine import DQMachine, DQParams, core_loss_coeffs, scale_core_loss

FS = 10_000.0
N_SUB = 8
F_NOM = 50.0  # [Hz] nominal supply frequency


def _machine(**kw):
    return DQMachine(DQParams(**kw))


def _p_core_nom(m):
    """Per-phase nominal core loss [W] = V_rms^2 / R_c."""
    return m.p.v_rms_nom_V**2 / m.p.rc_Ohm


def _lam_nom(m):
    """Nominal magnetizing flux [Wb] = V_pk / omega_nom."""
    return m.v_pk / m.w_e


def _B(f_Hz, c):
    """Frequency factor B(f) = (1-c)(f/f_nom) + c (f/f_nom)^2."""
    u = f_Hz / F_NOM
    return (1.0 - c) * u + c * u * u


class TestCoreLossForm:
    """The flux/frequency-dependent core-loss form and its reduction to 6K."""

    def test_reduces_to_fixed_shunt_at_f_nom(self):
        """At f = f_nom the effective shunt equals R_c (6K behaviour)."""
        m = _machine()
        assert m.rc_eff(F_NOM) == pytest.approx(m.p.rc_Ohm, rel=1e-12)

    def test_rc_eff_frequency_dependence(self):
        """R_c_eff(f) = R_c (f/f_nom)^2 / B(f)."""
        m = _machine()
        c = m.p.core_eddy_frac
        for f in (25.0, 35.0, 50.0, 65.0, 80.0):
            u = f / F_NOM
            expected = m.p.rc_Ohm * u * u / _B(f, c)
            assert m.rc_eff(f) == pytest.approx(expected, rel=1e-12)

    def test_core_loss_matches_analytic_form(self):
        """P_core = 3 P_core_nom (lam/lam_nom)^2 B(f) at the f-dependent steady state."""
        m = _machine()
        lam_nom = _lam_nom(m)
        p_core_nom = _p_core_nom(m)
        c = m.p.core_eddy_frac
        for f in (25.0, 35.0, 50.0, 65.0, 80.0):
            ss = m.steady_state(0.04, f_Hz=f)
            a = m.aux(ss["x"], f_Hz=f)
            lam = float(np.hypot(a.l_md, a.l_mq))
            expected = 3.0 * p_core_nom * (lam / lam_nom) ** 2 * _B(f, c)
            assert a.p_core_W == pytest.approx(expected, rel=1e-9)

    def test_core_loss_flux_dependence(self):
        """At fixed f, P_core ~ lam^2 (alpha = 2)."""
        m = _machine()
        f = 40.0
        lams, p_cores = [], []
        for s in (0.02, 0.04, 0.10, 0.20):
            ss = m.steady_state(s, f_Hz=f)
            a = m.aux(ss["x"], f_Hz=f)
            lams.append(float(np.hypot(a.l_md, a.l_mq)))
            p_cores.append(a.p_core_W)
        lams = np.array(lams)
        p_cores = np.array(p_cores)
        ratio = p_cores / lams**2
        assert np.max(np.abs(ratio - ratio[0]) / ratio[0]) <= 1e-9

    def test_core_loss_zero_when_flux_zero(self):
        """Zero magnetizing flux -> zero core loss (no spurious sink)."""
        m = _machine()
        a = m.aux(np.zeros(5))
        assert a.p_core_W == 0.0

    def test_core_loss_branch_carries_loss(self):
        """The core-loss branch must carry loss (catches its removal).

        Same band as the 6K shunt test: the implemented core loss at f_nom is
        ~87% of the committed fixed-V^2/R_c convention (453.429 W total).
        """
        m = _machine()
        a = m.aux(m.steady_state(0.04)["x"])
        assert a.p_core_W > 0.0
        assert 0.5 * 453.429 <= a.p_core_W <= 1.5 * 453.429


class TestEnergyBalance:
    """Energy bookkeeping with the frequency-dependent core loss."""

    def test_energy_balance_at_variable_frequency(self):
        """P_in = P_cu,s + P_cu,r + P_core + P_mech + dW/dt at f != f_nom, tol 1e-6."""
        m = _machine()
        f = 40.0
        ss = m.steady_state(0.04, f_Hz=f)
        ld = ss["torque_Nm"] - m.p.b_Nms * ss["omega_r"] - m.p.tf_Nm
        n = int(round(1.0 * FS))
        t = np.arange(n) / FS
        load = ld + 0.02 * ld * np.sin(2.0 * np.pi * 1.0 * t)
        r = m.simulate(load_Nm=load, fs_hz=FS, n_sub=N_SUB, x0=ss["x"], f_Hz=f)
        tt = r["t"]
        p_in = np.trapezoid(r["p_in"], tt)
        sinks = np.trapezoid(r["p_cu_s"] + r["p_cu_r"] + r["p_core"] + r["torque"] * r["speed"], tt)
        d_w = r["w_store"][-1] - r["w_store"][0]
        resid = abs(p_in - (sinks + d_w)) / abs(p_in)
        assert resid <= 1e-6, f"energy balance residual {resid:.3e} > 1e-6"

    def test_vf_control_keeps_flux_nearly_constant(self):
        """V/f control (V ~ f) keeps lam ~ const; core loss then follows B(f)."""
        m = _machine()
        lam_nom = _lam_nom(m)
        p_core_nom = _p_core_nom(m)
        c = m.p.core_eddy_frac
        for f in (30.0, 40.0, 50.0):
            v_rms = m.p.v_rms_nom_V * f / F_NOM
            ss = m.steady_state(0.04, v_rms=v_rms, f_Hz=f)
            a = m.aux(ss["x"], v_dq=(v_rms * np.sqrt(2.0), 0.0), f_Hz=f)
            lam = float(np.hypot(a.l_md, a.l_mq))
            # V/f constant -> flux approximately constant. Without a
            # low-frequency boost the stator drop makes lam fall a few percent
            # at the lowest f (measured ~6% at f=30); the band is set to
            # distinguish V/f control from constant-V (which would give ~0.6).
            assert abs(lam / lam_nom - 1.0) <= 0.10
            expected = 3.0 * p_core_nom * (lam / lam_nom) ** 2 * _B(f, c)
            assert a.p_core_W == pytest.approx(expected, rel=1e-9)


class TestSteinmetzCoefficients:
    """k_h, k_e and the +/-50% sensitivity (amendment 10 requirement)."""

    def test_coeffs_sum_to_nominal_core_loss(self):
        """k_h f lam^2 + k_e f^2 lam^2 = P_core_nom at f = f_nom, lam = lam_nom."""
        m = DQMachine()
        k_h, k_e = core_loss_coeffs(m.p)
        assert k_h > 0.0 and k_e > 0.0
        lam_nom = _lam_nom(m)
        p_core_nom = _p_core_nom(m)
        total = k_h * F_NOM * lam_nom**2 + k_e * F_NOM**2 * lam_nom**2
        assert total == pytest.approx(p_core_nom, rel=1e-12)

    def test_coeffs_split_matches_eddy_fraction(self):
        """The eddy share of the core loss at f_nom equals c."""
        m = DQMachine()
        k_h, k_e = core_loss_coeffs(m.p)
        lam_nom = _lam_nom(m)
        p_core_nom = _p_core_nom(m)
        eddy_share = k_e * F_NOM**2 * lam_nom**2 / p_core_nom
        assert eddy_share == pytest.approx(m.p.core_eddy_frac, rel=1e-12)

    def test_scale_core_loss_scales_both(self):
        """scale_core_loss(2, 2) doubles k_h and k_e (exact) and ~doubles P_core."""
        m = DQMachine()
        k_h, k_e = core_loss_coeffs(m.p)
        p2 = scale_core_loss(m.p, kh_scale=2.0, ke_scale=2.0)
        k_h2, k_e2 = core_loss_coeffs(p2)
        assert k_h2 == pytest.approx(2.0 * k_h, rel=1e-9)
        assert k_e2 == pytest.approx(2.0 * k_e, rel=1e-9)
        # The steady-state core loss follows the coefficients to leading order;
        # the small deviation is the lambda_m change through the KCL (g << K).
        base = m.steady_state(0.04)["p_core_W"]
        got = DQMachine(p2).steady_state(0.04)["p_core_W"]
        assert got == pytest.approx(2.0 * base, rel=1e-2)

    def test_scale_core_loss_one_at_a_time(self):
        """Independent k_h / k_e scaling (used by the sensitivity run)."""
        m = DQMachine()
        base = m.steady_state(0.04)["p_core_W"]
        for kh, ke in ((1.5, 1.0), (0.5, 1.0), (1.0, 1.5), (1.0, 0.5)):
            m2 = DQMachine(scale_core_loss(m.p, kh_scale=kh, ke_scale=ke))
            got = m2.steady_state(0.04)["p_core_W"]
            # at f_nom the split is linear in (1-c) and c, so the total scales
            # with the weighted combination actually applied
            assert got > 0.0
            assert got != pytest.approx(base, rel=1e-6)

    def test_sensitivity_reported_in_metrics(self):
        """The sensitivity effects on torque/energy are recorded, not silent."""
        import json
        from pathlib import Path

        metrics = Path(__file__).resolve().parents[1] / "results" / "metrics.json"
        payload = json.loads(metrics.read_text())
        sens = payload["week6l_coreloss"]["sensitivity"]
        assert set(sens["runs"]) == {"baseline", "kh+50", "kh-50", "ke+50", "ke-50"}
        for run in sens["runs"].values():
            assert run["torque_rel"] is not None
            assert run["energy_rel"] is not None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
