"""Week 6L: flux/frequency-dependent core-loss experiment runner.

Produces ``results/metrics.json['week6l_coreloss']`` from ACTUAL runs of
:mod:`plant.dq_machine` (amendment 10). Nothing here is fabricated; every number
is measured or reused from a committed constant, and each field says which.

The approved core-loss form is
    P_core = P_core_nom (lam/lam_nom)^2 B(f),
    B(f) = (1-c)(f/f_nom) + c (f/f_nom)^2,
with synthetic ``P_core_nom = 151.1429 W`` per phase, ``alpha = 2`` and eddy
fraction ``c = 0.5``. With ``alpha = 2`` the flux dependence is already carried
by the shunt; the new physics is ``B(f)``, which only matters at variable
frequency (the 6M VSD pump case). The runner therefore:

* refreshes ``week6k_dq`` (the declared approximation list changed in 6L);
* pins the reduction to the fixed shunt at ``f = f_nom``;
* measures the flux dependence (``P_core ~ lam^2``) and the frequency
  dependence (``P_core ~ B(f)``) against the analytic form;
* measures the V/f-control flux deviation and the energy balance at ``f != f_nom``;
* runs the required +/-50% sensitivity on the Steinmetz coefficients
  ``k_h``, ``k_e`` and reports the effect on torque and energy.

Run:  uv run python sim/run_week6l.py
"""

import json
from pathlib import Path

import numpy as np

from plant.dq_machine import (
    APPROXIMATIONS,
    DQMachine,
    core_loss_coeffs,
    scale_core_loss,
)
from sim.run_week6k import FS_HZ, N_SUB, SEED, run_week6k, save_week6k_metrics

REPO = Path(__file__).resolve().parents[1]
METRICS_PATH = REPO / "results" / "metrics.json"

F_NOM = 50.0
# Frequencies at which the frequency dependence is pinned [Hz].
FREQS = (25.0, 35.0, 40.0, 50.0, 65.0, 80.0)
# Slips at which the flux dependence is pinned.
FLUX_SLIPS = (0.02, 0.04, 0.10, 0.20)
# Sensitivity factors on (k_h, k_e).
SENS = {
    "baseline": (1.0, 1.0),
    "kh+50": (1.5, 1.0),
    "kh-50": (0.5, 1.0),
    "ke+50": (1.0, 1.5),
    "ke-50": (1.0, 0.5),
}


def _p_core_nom(m: DQMachine) -> float:
    return m.p.v_rms_nom_V**2 / m.p.rc_Ohm


def _lam_nom(m: DQMachine) -> float:
    return m.v_pk / m.w_e


def _B(f_Hz: float, c: float) -> float:
    u = f_Hz / F_NOM
    return (1.0 - c) * u + c * u * u


def _flux_dependence(m: DQMachine) -> float:
    """Max relative spread of P_core/lam^2 over FLUX_SLIPS at fixed f (alpha=2)."""
    f = 40.0
    ratio = []
    for s in FLUX_SLIPS:
        a = m.aux(m.steady_state(s, f_Hz=f)["x"], f_Hz=f)
        lam = float(np.hypot(a.l_md, a.l_mq))
        ratio.append(a.p_core_W / lam**2)
    ratio = np.array(ratio)
    return float(np.max(np.abs(ratio - ratio[0]) / ratio[0]))


def _frequency_dependence(m: DQMachine) -> float:
    """Max relative deviation of P_core from 3 P_core_nom (lam/lam_nom)^2 B(f)."""
    lam_nom = _lam_nom(m)
    p_core_nom = _p_core_nom(m)
    c = m.p.core_eddy_frac
    worst = 0.0
    for f in FREQS:
        a = m.aux(m.steady_state(0.04, f_Hz=f)["x"], f_Hz=f)
        lam = float(np.hypot(a.l_md, a.l_mq))
        expected = 3.0 * p_core_nom * (lam / lam_nom) ** 2 * _B(f, c)
        worst = max(worst, abs(a.p_core_W - expected) / expected)
    return worst


def _vf_control(m: DQMachine) -> float:
    """Max |lam/lam_nom - 1| under V/f control (V ~ f) at slip 0.04."""
    lam_nom = _lam_nom(m)
    worst = 0.0
    for f in (30.0, 40.0, 50.0):
        v_rms = m.p.v_rms_nom_V * f / F_NOM
        ss = m.steady_state(0.04, v_rms=v_rms, f_Hz=f)
        a = m.aux(ss["x"], v_dq=(v_rms * np.sqrt(2.0), 0.0), f_Hz=f)
        lam = float(np.hypot(a.l_md, a.l_mq))
        worst = max(worst, abs(lam / lam_nom - 1.0))
    return worst


def _energy_balance_at_f(m: DQMachine, f: float) -> float:
    """Integral energy-balance residual over a 1 s run at frequency f."""
    ss = m.steady_state(0.04, f_Hz=f)
    ld = ss["torque_Nm"] - m.p.b_Nms * ss["omega_r"] - m.p.tf_Nm
    n = int(FS_HZ)
    t = np.arange(n) / FS_HZ
    load = ld + 0.02 * ld * np.sin(2.0 * np.pi * 1.0 * t)
    r = m.simulate(load_Nm=load, fs_hz=FS_HZ, n_sub=N_SUB, x0=ss["x"], f_Hz=f)
    tt = r["t"]
    p_in = float(np.trapezoid(r["p_in"], tt))
    sinks = float(
        np.trapezoid(r["p_cu_s"] + r["p_cu_r"] + r["p_core"] + r["torque"] * r["speed"], tt)
    )
    d_w = r["w_store"][-1] - r["w_store"][0]
    return abs(p_in - (sinks + d_w)) / abs(p_in)


def _sens_run(m: DQMachine) -> dict:
    """1 s run at the mechanical equilibrium: average torque, input energy, core energy."""
    ss = m.steady_state(0.04)
    ld = ss["torque_Nm"] - m.p.b_Nms * ss["omega_r"] - m.p.tf_Nm
    r = m.simulate(load_Nm=ld, fs_hz=FS_HZ, duration_s=1.0, n_sub=N_SUB, x0=ss["x"])
    t = r["t"]
    return {
        "torque_avg_Nm": float(np.mean(r["torque"])),
        "energy_in_J": float(np.trapezoid(r["p_in"], t)),
        "energy_core_J": float(np.trapezoid(r["p_core"], t)),
    }


def _sensitivity() -> dict:
    """+/-50% sensitivity on (k_h, k_e); effect on torque and energy."""
    base_m = DQMachine()
    base = _sens_run(base_m)
    runs = {
        "baseline": {
            "torque_rel": 0.0,
            "energy_rel": 0.0,
            **{
                "torque_avg_Nm": base["torque_avg_Nm"],
                "energy_in_J": base["energy_in_J"],
                "energy_core_J": base["energy_core_J"],
                "p_core_W": _p_core_nom(base_m),
            },
        }
    }
    for name, (kh, ke) in SENS.items():
        if name == "baseline":
            continue
        m = DQMachine(scale_core_loss(base_m.p, kh_scale=kh, ke_scale=ke))
        got = _sens_run(m)
        runs[name] = {
            "torque_rel": (got["torque_avg_Nm"] - base["torque_avg_Nm"])
            / abs(base["torque_avg_Nm"]),
            "energy_rel": (got["energy_in_J"] - base["energy_in_J"]) / abs(base["energy_in_J"]),
            "torque_avg_Nm": got["torque_avg_Nm"],
            "energy_in_J": got["energy_in_J"],
            "energy_core_J": got["energy_core_J"],
            "p_core_W": _p_core_nom(m),
        }
    return {
        "runs": runs,
        "definition": "torque_rel / energy_rel are the relative change vs baseline "
        "of the 1 s average torque and total input energy at the slip-0.04 "
        "mechanical equilibrium; energy_core_J is the integrated core-loss energy.",
        "note": "the core loss enters the torque only through lambda_m (g << K), so "
        "the torque effect is small; the energy effect is the direct core-loss "
        "change. Both are measured, not derived. At f_nom with c=0.5 the "
        "hysteresis and eddy contributions are equal, so kh+50 == ke+50 and "
        "kh-50 == ke-50: the f_nom sensitivity cannot distinguish k_h from k_e "
        "(that distinction needs f != f_nom).",
    }


def _findings() -> list:
    return [
        {
            "id": "core-loss-form",
            "title": "flux/frequency-dependent core loss applied (amendment 10)",
            "detail": "The fixed shunt R_c is replaced by the approved form "
            "P_core = P_core_nom (lam/lam_nom)^2 [(1-c)(f/f_nom) + c (f/f_nom)^2] "
            "(alpha=2, c=0.5), applied as a frequency-dependent effective shunt "
            "R_c_eff(f) = R_c (f/f_nom)^2/B(f). With alpha=2 the flux dependence "
            "is already carried by the shunt; the new physics is B(f). At f=f_nom "
            "the model reduces to the 6K fixed shunt (R_c_eff = R_c).",
        },
        {
            "id": "variable-frequency-support",
            "title": "the dq model now runs at variable supply frequency",
            "detail": "f_Hz is threaded through supply/aux/derivatives/simulate/"
            "steady_state/electrical_matrix so the frequency-dependent core loss "
            "is exercised; this is what the 6M VSD pump case needs. The inductances "
            "stay at the committed values (reactances scale with f automatically).",
        },
    ]


def _mutation_check() -> dict:
    """Recorded Week 6L mutation result (observed via ``make mutants-week6l``).

    Reproduce with ``uv run python sim/mutants_week6l.py``; the first catcher
    is the first test that fails under ``pytest -x`` in file order.
    """
    return {
        "tool": "sim/mutants_week6l.py",
        "observed": "2026-10-16",
        "first_catchers": [
            {
                "id": "M-6L-1",
                "mutation": "no frequency dependence (R_c_eff = R_c)",
                "first_catcher": "TestCoreLossForm::test_rc_eff_frequency_dependence",
            },
            {
                "id": "M-6L-2",
                "mutation": "wrong eddy fraction (c = 0)",
                "first_catcher": "TestSteinmetzCoefficients::test_coeffs_sum_to_nominal_core_loss",
            },
            {
                "id": "M-6L-3",
                "mutation": "core loss removed (R_c = 1e12)",
                "first_catcher": "TestCoreLossForm::test_core_loss_branch_carries_loss",
            },
            {
                "id": "M-6L-4",
                "mutation": "wrong frequency scaling (u not u^2)",
                "first_catcher": "TestCoreLossForm::test_rc_eff_frequency_dependence",
            },
            {
                "id": "M-6L-5",
                "mutation": "fixed core loss (no flux dependence)",
                "first_catcher": "TestCoreLossForm::test_core_loss_matches_analytic_form",
            },
        ],
        "note": "all five mutants were caught; the tool rewrites plant/dq_machine.py "
        "then restores it and is NOT part of any gate.",
    }


def run_week6l() -> dict:
    """Run the Week 6L core-loss checks and return the metrics payload."""
    m = DQMachine()
    k_h, k_e = core_loss_coeffs(m.p)
    dt_int = 1.0 / FS_HZ / N_SUB

    payload = {
        "label": "Week 6L flux/frequency-dependent core loss (amendment 10)",
        "source": "sim/run_week6l.py:run_week6l() via plant.dq_machine.DQMachine",
        "approach": "Steinmetz+eddy flux/frequency-dependent core loss applied as a "
        "frequency-dependent effective shunt; RK4, N_sub=8 at 10 kHz",
        "seed": SEED,
        "fs_hz": FS_HZ,
        "n_sub": N_SUB,
        "dt_int_s": dt_int,
        "run_identity": {
            "label": "Week 6L core-loss checks",
            "source": "sim/run_week6l.py:run_week6l()",
            "seed": SEED,
            "sample_rate_hz": FS_HZ,
            "n_sub": N_SUB,
            "note": "implementation checks of the core-loss form and its +/-50% "
            "(k_h, k_e) sensitivity; NOT validation against a real machine",
        },
        "core_loss_form": {
            "expression": "P_core = P_core_nom (lam/lam_nom)^2 [(1-c)(f/f_nom) + c (f/f_nom)^2]",
            "p_core_nom_W_per_phase": _p_core_nom(m),
            "alpha": 2,
            "eddy_fraction_c": m.p.core_eddy_frac,
            "total_core_loss_W": 3.0 * _p_core_nom(m),
            "implementation": "frequency-dependent effective shunt "
            "R_c_eff(f) = R_c (f/f_nom)^2 / B(f)",
            "reduction_at_f_nom": "R_c_eff(f_nom) = R_c = 350 ohm (6K behaviour)",
        },
        "rc_eff_Ohm": {str(f): m.rc_eff(f) for f in FREQS},
        "steinmetz": {
            "k_h_W_per_Hz_Wb2": k_h,
            "k_e_W_per_Hz2_Wb2": k_e,
            "eddy_share_at_f_nom": m.p.core_eddy_frac,
        },
        "core_loss_checks": {
            "flux_dependence_max_rel": _flux_dependence(m),
            "frequency_dependence_max_rel": _frequency_dependence(m),
            "vf_control_max_flux_rel_dev": _vf_control(m),
            "energy_balance_rel_at_40Hz": _energy_balance_at_f(m, 40.0),
        },
        "sensitivity": _sensitivity(),
        "approximations": list(APPROXIMATIONS),
        "findings": _findings(),
        "mutation_check": _mutation_check(),
        "measured_vs_derived": "rc_eff, the core-loss checks and the sensitivity "
        "effects are MEASURED from this run; the Steinmetz coefficients and the "
        "nominal core loss are DERIVED from the committed R_c and c",
    }
    return payload


def save_week6l_metrics(payload: dict) -> None:
    """Persist under ``results/metrics.json['week6l_coreloss']`` (strict JSON)."""
    existing = {}
    if METRICS_PATH.exists():
        existing = json.loads(METRICS_PATH.read_text())
    existing["week6l_coreloss"] = payload
    existing.setdefault("run_labels", {})["week6l_coreloss"] = payload["run_identity"]
    json.dumps(existing, allow_nan=False)  # strict gate: raises on NaN/Infinity
    METRICS_PATH.write_text(json.dumps(existing, indent=2) + "\n")
    print(f"\nSaved week6l_coreloss metrics to {METRICS_PATH}")


def main() -> None:
    # The declared approximation list changed in 6L; refresh week6k_dq so the
    # recorded approximations stay identical to plant.dq_machine.APPROXIMATIONS.
    save_week6k_metrics(run_week6k())
    payload = run_week6l()
    save_week6l_metrics(payload)
    chk = payload["core_loss_checks"]
    print(f"flux dependence max rel: {chk['flux_dependence_max_rel']:.3e}")
    print(f"frequency dependence max rel: {chk['frequency_dependence_max_rel']:.3e}")
    print(f"V/f control max flux rel dev: {chk['vf_control_max_flux_rel_dev']:.3e}")
    print(f"energy balance rel @40 Hz: {chk['energy_balance_rel_at_40Hz']:.3e}")
    for name, run in payload["sensitivity"]["runs"].items():
        print(
            f"sensitivity {name}: torque_rel={run['torque_rel']:+.3e} "
            f"energy_rel={run['energy_rel']:+.3e}"
        )


if __name__ == "__main__":
    main()
