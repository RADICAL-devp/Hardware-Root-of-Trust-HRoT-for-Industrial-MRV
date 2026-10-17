"""HRoT Carbon Verification dashboard (Week 8).

Reads ``results/metrics.json`` and presents the measured savings, attack-
detection, physics and gate results. Simulation only: this proves logic, math
and attack detection — NOT physical tamper resistance.

Run:  streamlit run dashboard/app.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

REPO = Path(__file__).resolve().parents[1]
METRICS_PATH = REPO / "results" / "metrics.json"


@st.cache_data
def load_metrics() -> dict:
    return json.loads(METRICS_PATH.read_text())


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def page_savings(m: dict) -> None:
    st.subheader("Week 6: savings (pre-registered coverage run)")
    r = m.get("week6m_coverage", {})
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("True savings", _pct(r.get("true_savings_frac", 0.0)))
    c2.metric("Estimated savings", _pct(r.get("est_savings_mean_frac", 0.0)))
    c3.metric("Coverage", f"{r.get('coverage', 0.0) * 100:.0f}/100")
    c4.metric("CI half-width", _pct(r.get("ci_half_width_frac", 0.0)))
    st.write(
        f"Legacy energy: {r.get('e_legacy_kwh', 0.0):.5f} kWh  |  "
        f"Retrofit: {r.get('e_retrofit_kwh', 0.0):.5f} kWh  |  "
        f"N seeds: {r.get('n_seeds', 0)}  |  "
        f"SE: {_pct(r.get('se_savings_frac', 0.0))}"
    )
    st.write(
        f"Coverage gate (>= 90/100): "
        f"{'PASS' if r.get('coverage_pass') else 'FAIL'}  |  "
        f"Cap (<= 7.79%): {'PASS' if r.get('cap_pass') else 'FAIL'}  |  "
        f"CIs excluding zero: {_pct(r.get('frac_ci_excludes_zero', 0.0))}"
    )
    st.bar_chart(
        pd.DataFrame(
            {"energy (kWh)": [r.get("e_legacy_kwh", 0.0), r.get("e_retrofit_kwh", 0.0)]},
            index=["legacy", "retrofit"],
        )
    )


def page_attacks(m: dict) -> None:
    st.subheader("Week 7: attack campaign (N = 128 trials / attack, fresh seeds)")
    d = m.get("week7_attacks", {}).get("detections", {})
    rows = []
    for name, row in d.items():
        rows.append(
            {
                "attack": name,
                "detected": row.get("detected", 0),
                "missed": row.get("missed", 0),
                "rate": row.get("rate", 0.0),
                "verdicts": ", ".join(f"{k}={v}" for k, v in row.get("verdicts", {}).items()),
            }
        )
    st.dataframe(rows, use_container_width=True)
    st.write(
        "False-reject rate (honest ledger): "
        f"{m.get('week7_attacks', {}).get('false_reject_rate', 0.0):.4f}"
    )
    st.subheader("Documented misses (honest)")
    for miss in m.get("week7_attacks", {}).get("misses", []):
        st.markdown(f"- **{miss['attack']}**: {miss['why']}")


def page_physics(m: dict) -> None:
    st.subheader("Week 6K: three-phase dq machine")
    k = m.get("week6k_dq", {})
    st.write(f"Eigenvalue basis (committed): {k.get('eigenvalues_committed_table_basis', 'n/a')}")
    st.write(f"Eigenvalue basis (implemented): {k.get('eigenvalues_implemented_shunt', 'n/a')}")
    tr = k.get("torque_residuals", {})
    st.write(
        "Torque residual (max rel gap, operating range): "
        f"{tr.get('max_rel_gap_operating_range', 'n/a')}"
    )
    bg = k.get("balances", {})
    st.write(
        f"Energy balance (1 s ripple): {bg.get('energy_ripple_rel', 'n/a')}  |  "
        f"step residual: {bg.get('energy_step_rel', 'n/a')}"
    )
    sg = k.get("step_gate", {})
    st.write(
        f"Step gate: lambda_max*dt = {sg.get('lambda_max_dt', 'n/a')} "
        f"(limit {sg.get('limit', 'n/a')}) -> "
        f"{'PASS' if sg.get('pass') else 'FAIL'}"
    )
    ub = k.get("unbalance", {})
    st.write(
        "Unbalance (sim, a_u=0.02): "
        f"{ub.get('sim_Nm', {}).get('0.02', 'n/a')} N*m  "
        "(committed 3.6440 N*m NOT reproduced; reported, not chased)"
    )
    st.subheader("Week 6L: flux/frequency-dependent core loss")
    core = m.get("week6l_coreloss", {})
    st.write(f"Core-loss form: {core.get('core_loss_form', {}).get('expression', 'n/a')}")
    st.write(
        "R_c_eff (ohm): "
        + ", ".join(f"{f}Hz={v:.1f}" for f, v in core.get("rc_eff_Ohm", {}).items())
    )
    cc = core.get("core_loss_checks", {})
    st.write(
        f"Flux dependence residual: {cc.get('flux_dependence_max_rel', 'n/a')}  |  "
        f"Freq dependence residual: {cc.get('frequency_dependence_max_rel', 'n/a')}  |  "
        f"V/f flux dev: {cc.get('vf_control_max_flux_rel_dev', 'n/a')}"
    )
    sens = core.get("sensitivity", {}).get("runs", {})
    if sens:
        st.write(
            "Sensitivity (+50% k_h): energy rel "
            f"{sens.get('kh+50', {}).get('energy_rel', 'n/a')}, "
            "torque rel "
            f"{sens.get('kh+50', {}).get('torque_rel', 'n/a')}"
        )


def page_gates() -> None:
    st.subheader("Gates (measured, committed)")
    st.table(
        [
            {"target": "make lint", "result": "clean (ruff check + format)"},
            {"target": "make test-quick", "result": "161 passed"},
            {"target": "make test-full", "result": "522 passed / 1:48:35"},
            {"target": "make repro (SEED=42)", "result": "522 passed / 1:30:20"},
            {"target": "make attacks-week7", "result": "5.47 s (128 trials/attack)"},
            {"target": "make mutants-week6k", "result": "6/6 caught"},
            {"target": "make mutants-week6l", "result": "5/5 caught"},
        ]
    )
    st.caption(
        "Gate numbers above are from the committed runs; re-run the targets "
        "to reproduce. `make test-full` / `make repro` take ~1.5 h each."
    )


def main() -> None:
    st.set_page_config(page_title="HRoT Carbon Verification", layout="wide")
    st.title("HRoT Carbon Verification Module")
    st.caption(
        "Honest scope: simulation proves logic, math and attack detection. "
        "It does NOT prove physical tamper resistance. "
        "No 'unspoofable' / 'tamper-proof' claims."
    )
    m = load_metrics()
    tabs = st.tabs(["Savings", "Attacks", "Physics", "Gates"])
    with tabs[0]:
        page_savings(m)
    with tabs[1]:
        page_attacks(m)
    with tabs[2]:
        page_physics(m)
    with tabs[3]:
        page_gates()


if __name__ == "__main__":
    main()
