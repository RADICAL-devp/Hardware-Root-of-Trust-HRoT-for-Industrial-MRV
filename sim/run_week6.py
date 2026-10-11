"""Week 6 Ground-truth Experiment: Legacy vs MPC Control Comparison.

Runs the motor plant under:
A. Legacy control (fixed throttle = 1.0)
B. MPC control (optimized throttle)

Uses identical load profiles, seeds, initial conditions.
Compares energy consumption, safety violations, and savings estimation.
"""

import json
import os
from pathlib import Path
from typing import Dict, List

import numpy as np

from baseline.emissions import EmissionsEstimator, EmissionsParams
from baseline.regression import BaselineParams, BaselineRegression
from control.mpc import MPCController, MPCParams
from ledger.verifier import classify
from plant.load_profiles import KINDS, make_profile
from plant.motor import MotorParams, MotorResult, simulate
from plant.thermal import ThermalParams
from sensors import SAMPLE_RATE_HZ, measurement_chain
from sensors.adc import ADCParams
from sensors.afe import AFE, AFEParams
from sensors.windows import ZCParams, build_windows, find_rising_crossings, window_mean_power

REPO = Path(__file__).resolve().parents[1]
RESULTS_DIR = REPO / "results"
FIG_DIR = RESULTS_DIR / "figures"
METRICS_PATH = RESULTS_DIR / "metrics.json"

SEED = int(os.environ.get("SEED", "42"))
FS_HZ = SAMPLE_RATE_HZ
DURATION_S = 60.0  # 60 seconds per profile
WINDOW_DURATION_S = 1.0  # 1 Hz attestation windows


def simulate_control(
    profile_kind: str,
    controller_type: str,  # "legacy" or "mpc"
    mpc_controller: MPCController = None,
    seed: int = SEED,
) -> Dict:
    """Simulate plant with given control strategy.

    Returns dict with energy, temperatures, violations, etc.
    """
    # Generate load profile
    load = make_profile(profile_kind, duration_s=DURATION_S, fs_hz=FS_HZ, seed=seed)

    # Plant parameters
    motor_params = MotorParams()
    thermal_params = ThermalParams()

    # Simulate plant (open-loop for legacy, closed-loop for MPC)
    # For MPC, we need to step the simulation and apply control at each window
    if controller_type == "legacy":
        # Legacy: constant throttle = 1.0 (full voltage)
        res = simulate(load, fs_hz=FS_HZ, params=motor_params, thermal_params=thermal_params)
        throttle_trace = np.ones_like(load)
    else:
        # MPC: closed-loop simulation with control updates at window boundaries
        res, throttle_trace = simulate_mpc_closed_loop(
            load, motor_params, thermal_params, mpc_controller, seed
        )

    # Apply sensor chain
    t = np.arange(res.v_true_V.size) / FS_HZ
    v_meas, i_meas = measurement_chain(
        res.v_true_V, res.i_true_A, t, AFE(AFEParams(seed=seed)), ADCParams()
    )

    # Zero-crossing windows
    zc = ZCParams()
    det_idx, frac = find_rising_crossings(v_meas, FS_HZ, zc.hyst_V)
    windows = build_windows(det_idx, frac, v_meas.size, zc)
    p_windows = window_mean_power(v_meas, i_meas, windows)

    # Energy per window [µWh]
    energy_windows_uwh = []
    window_load = []
    window_temp = []
    for w, p_win in zip(windows, p_windows):
        if w.valid:
            m = w.n_samples
            e_uwh = p_win * m / FS_HZ / 3600.0 * 1_000_000.0
            energy_windows_uwh.append(e_uwh)
        else:
            energy_windows_uwh.append(0.0)
        # Sample load and temp at window center for all windows
        center = (w.start_idx + w.end_idx) // 2
        window_load.append(float(load[center]))
        window_temp.append(float(res.temp_C[center]))

    energy_windows_uwh = np.array(energy_windows_uwh)
    window_load = np.array(window_load)
    window_temp = np.array(window_temp)

    # Total energy [kWh]
    total_energy_kwh = np.sum(energy_windows_uwh) / 1_000_000_000.0

    # Ground-truth energy from plant (true mean power * time) [kWh]
    true_avg_power = np.mean(res.v_true_V * res.i_true_A)  # [W]
    gt_energy_kwh = true_avg_power * DURATION_S / 3_600_000.0  # [kWh] = W * s / 3.6e6

    # Safety violations
    temp_violations = np.sum(res.temp_C > 120.0)
    # Torque margin violations (1-second average):
    # average motor torque over each window < 0.9 * load
    # (Matches MPC's torque_margin=0.9 and 1 Hz control interval)
    torque_margin_violations = 0
    # Instantaneous torque violations:
    # instantaneous motor torque < 0.9 * load
    # With 30% torque ripple, instantaneous torque can dip
    # below 0.9 * load even when average meets requirement
    instantaneous_torque_violations = np.sum(res.torque_Nm < 0.9 * load)
    window_samples = int(FS_HZ * WINDOW_DURATION_S)
    n_windows = len(load) // window_samples
    for w_idx in range(n_windows):
        w_start = w_idx * int(FS_HZ * WINDOW_DURATION_S)
        w_end = min((w_idx + 1) * int(FS_HZ * WINDOW_DURATION_S), len(load))
        if w_start >= w_end:
            continue
        avg_torque = np.mean(res.torque_Nm[w_start:w_end])
        avg_load = np.mean(load[w_start:w_end])
        if avg_torque < 0.9 * avg_load:
            torque_margin_violations += w_end - w_start
    # Throttle violations (for MPC)
    if controller_type == "mpc":
        throttle_violations = np.sum((throttle_trace < 0.4) | (throttle_trace > 1.0))
    else:
        throttle_violations = 0

    # Verifier classification for each window
    valid_count = 0
    excluded_count = 0
    rejected_count = 0
    for w in windows:
        # Mock record for classification
        rec = {
            "window_flags": 0x1F if w.valid else 0x00,
            "energy_uwh": int(energy_windows_uwh[windows.index(w)]) if w.valid else 0,
        }
        cls = classify(rec)
        if cls == "valid":
            valid_count += 1
        elif cls == "excluded":
            excluded_count += 1
        elif cls == "rejected":
            rejected_count += 1

    return {
        "profile": profile_kind,
        "controller": controller_type,
        "measured_energy_kwh": float(total_energy_kwh),
        "gt_energy_kwh": float(gt_energy_kwh),
        "max_temp_C": float(np.max(res.temp_C)),
        "min_torque_margin": float(np.min(res.torque_Nm / np.maximum(load, 0.1))),
        "temp_violations": int(temp_violations),
        "torque_margin_violations": int(torque_margin_violations),
        "instantaneous_torque_violations": int(instantaneous_torque_violations),
        "throttle_violations": int(throttle_violations),
        "failsafe_count": 0,  # Will be filled from MPC
        "valid_windows": valid_count,
        "excluded_windows": excluded_count,
        "rejected_windows": rejected_count,
        "energy_windows_uwh": energy_windows_uwh.tolist(),
        "window_load": window_load.tolist(),
        "window_temp": window_temp.tolist(),
        "throttle_trace": throttle_trace.tolist() if controller_type == "mpc" else None,
    }


def simulate_mpc_closed_loop(
    load: np.ndarray,
    motor_params: MotorParams,
    thermal_params: ThermalParams,
    mpc: MPCController,
    seed: int,
) -> tuple[MotorResult, np.ndarray]:
    """Run closed-loop MPC simulation with 1 Hz control updates."""
    # Simulate step by step, updating control at each attestation window
    n_total = len(load)
    window_samples = int(FS_HZ * WINDOW_DURATION_S)  # 10000 samples per window
    n_windows = n_total // window_samples

    # Pre-allocate arrays
    v_true = np.zeros(len(load))
    i_true = np.zeros(len(load))
    torque = np.zeros(len(load))
    speed = np.zeros(len(load))
    temp = np.zeros(len(load))
    throttle_trace = np.zeros(n_windows)

    # Initial state
    all_v = []
    all_i = []
    all_torque = []
    all_speed = []
    all_temp = []
    all_throttle = []

    # Initial thermal state
    current_temp = thermal_params.t_amb_C

    for w_idx in range(n_windows):
        w_start = w_idx * int(FS_HZ * WINDOW_DURATION_S)
        w_end = min((w_idx + 1) * int(FS_HZ * WINDOW_DURATION_S), len(load))
        w_len = w_end - w_start
        if w_len == 0:
            break

        w_load = load[w_start:w_end]

        # Get MPC control action at window start
        if w_idx == 0:
            # Initial state estimate
            delta_T = current_temp - thermal_params.t_amb_C
            x0 = np.array([delta_T])  # [ΔT] only
            u_prev = 1.0
        else:
            # Update state estimate from previous window
            delta_T = current_temp - thermal_params.t_amb_C
            x0 = np.array([delta_T])
            u_prev = all_throttle[-1]

        sim_time = w_idx * WINDOW_DURATION_S
        u = mpc.solve(x0, float(np.mean(w_load)), u_prev, step=w_idx, sim_time=sim_time)
        all_throttle.append(u)

        # Simulate this window with throttle u
        # Scale voltage by throttle
        scaled_params = MotorParams(
            v_rms_nom_V=motor_params.v_rms_nom_V * u,
            f_grid_offset_Hz=motor_params.f_grid_offset_Hz,
        )

        w_res = simulate(w_load, fs_hz=FS_HZ, params=scaled_params, thermal_params=thermal_params)

        all_v.append(w_res.v_true_V)
        all_i.append(w_res.i_true_A)
        all_torque.append(w_res.torque_Nm)
        all_speed.append(w_res.speed_rad_s)
        all_temp.append(w_res.temp_C)

        current_temp = w_res.temp_C[-1]

    # Concatenate results
    v_true = np.concatenate(all_v)
    i_true = np.concatenate(all_i)
    torque = np.concatenate(all_torque)
    speed = np.concatenate(all_speed)
    temp = np.concatenate(all_temp)
    throttle_trace = np.array(all_throttle)

    # Trim to exact length
    v_true = v_true[: len(load)]
    i_true = i_true[: len(load)]
    torque = torque[: len(load)]
    speed = speed[: len(load)]
    temp = temp[: len(load)]
    throttle_trace = np.array(all_throttle)

    res = MotorResult(
        t_s=np.arange(len(load)) / FS_HZ,
        v_true_V=v_true,
        i_true_A=i_true,
        torque_Nm=torque,
        speed_rad_s=speed,
        speed_rpm=speed * 60.0 / (2.0 * np.pi),
        slip=np.zeros(len(load)),  # placeholder
        temp_C=temp,
        f_grid_Hz=motor_params.f_nom_Hz + motor_params.f_grid_offset_Hz,
        i_rms_A=np.zeros(len(load)),
        phi_rad=np.zeros(len(load)),
        i_h3_rms_A=np.zeros(len(load)),
        phi_h3_rad=np.zeros(len(load)),
        i_h5_rms_A=np.zeros(len(load)),
        phi_h5_rad=np.zeros(len(load)),
    )

    return res, throttle_trace


def run_baseline_training(legacy_results: List[Dict]) -> BaselineRegression:
    """Train baseline on legacy (pre-retrofit) data."""
    # Collect all valid windows from legacy runs
    all_load = []
    all_temp = []
    all_energy = []

    for r in legacy_results:
        if r["controller"] != "legacy":
            continue
        n_windows = len(r["energy_windows_uwh"])
        for i in range(n_windows):
            if r["energy_windows_uwh"][i] > 0:  # valid window
                all_load.append(r["window_load"][i] if i < len(r["window_load"]) else 10.0)
                all_temp.append(r["window_temp"][i] if i < len(r["window_temp"]) else 40.0)
                all_energy.append(r["energy_windows_uwh"][i])

    baseline = BaselineRegression(BaselineParams(random_state=SEED))
    baseline.fit(np.array(all_load), np.array(all_temp), np.array(all_energy))
    return baseline


def run_week6_experiment():
    """Run full Week 6 experiment and return metrics."""
    print(f"Week 6 Experiment: SEED={SEED}, DURATION={DURATION_S}s per profile")

    # MPC controller
    mpc_params = MPCParams(
        prediction_horizon=10,
        dt=WINDOW_DURATION_S,
        u_min=0.4,
        u_max=1.0,
        du_max=0.1,
        temp_max=120.0,
        torque_margin=0.9,  # calibrated: feasible up to 17 Nm with model
        R_u=1.0,
        Q_T=0.1,
        solver_timeout_ms=100,
        warm_start=True,
    )
    mpc = MPCController(mpc_params)

    # Results storage
    all_results = []

    # Run for each profile
    for profile in KINDS:
        print(f"\n--- Profile: {profile} ---")

        # Legacy control
        print("  Running legacy control...")
        legacy_result = simulate_control(profile, "legacy", seed=SEED)
        all_results.append(legacy_result)
        print(f"    Energy: {legacy_result['measured_energy_kwh']:.4f} kWh (measured)")
        print(f"    GT Energy: {legacy_result['gt_energy_kwh']:.4f} kWh")
        print(f"    Max temp: {legacy_result['max_temp_C']:.1f}°C")
        print(
            "    Violations: "
            f"T={legacy_result['temp_violations']}, "
            f"Torque={legacy_result['torque_margin_violations']}"
        )

        # MPC control
        print("  Running MPC control...")
        mpc_result = simulate_control(profile, "mpc", mpc_controller=mpc, seed=SEED)
        mpc_result["failsafe_count"] = len(mpc.get_failsafe_logs())
        all_results.append(mpc_result)
        print(f"    Energy: {mpc_result['measured_energy_kwh']:.4f} kWh (measured)")
        print(f"    GT Energy: {mpc_result['gt_energy_kwh']:.4f} kWh")
        print(f"    Max temp: {mpc_result['max_temp_C']:.1f}°C")
        print(
            "    Violations: "
            f"T={mpc_result['temp_violations']}, "
            f"Torque={mpc_result['torque_margin_violations']}, "
            f"Throttle={mpc_result['throttle_violations']}"
        )
        print(f"    Failsafe count: {mpc_result['failsafe_count']}")

    # Train baseline on legacy (pre-retrofit) data
    print("\n--- Training baseline on legacy data ---")
    baseline = run_baseline_training(all_results)
    print(f"  CV(RMSE): {baseline.cv_rmse_:.2f}%")
    print(f"  NMBE: {baseline.nmbe_:.2f}%")
    print(f"  Val RMSE: {baseline.val_rmse_:.0f} µWh")

    # Estimate savings for MPC (post-retrofit) periods
    print("\n--- Savings estimation ---")
    emissions = EmissionsEstimator(EmissionsParams())

    week6_metrics = {
        "profiles": {},
        "summary": {},
    }

    for profile in KINDS:
        legacy = [
            r for r in all_results if r["profile"] == profile and r["controller"] == "legacy"
        ][0]
        mpc = [r for r in all_results if r["profile"] == profile and r["controller"] == "mpc"][0]

        # Ground-truth savings
        gt_savings_kwh = legacy["gt_energy_kwh"] - mpc["gt_energy_kwh"]
        gt_savings_pct = (
            100.0 * gt_savings_kwh / legacy["gt_energy_kwh"] if legacy["gt_energy_kwh"] > 0 else 0
        )

        # Estimated savings (baseline + measured post-retrofit)
        post_load = np.array(mpc["window_load"])
        post_temp = np.array(mpc["window_temp"])
        post_measured = np.array(mpc["energy_windows_uwh"])

        est_savings_kwh, ci_low, ci_high = baseline.estimate_savings(
            post_load, post_temp, post_measured
        )
        est_savings_pct = (
            100.0 * est_savings_kwh / legacy["measured_energy_kwh"]
            if legacy["measured_energy_kwh"] > 0
            else 0
        )

        # Estimation error
        if abs(gt_savings_kwh) > 1e-6:
            est_error_pct = 100.0 * (est_savings_kwh - gt_savings_kwh) / gt_savings_kwh
        else:
            est_error_pct = float("inf") if abs(est_savings_kwh) > 1e-6 else 0.0

        # Emissions
        avoided_emissions = emissions.aggregate_emissions(
            [est_savings_kwh],  # single aggregate value
            [int(DURATION_S / 3600)] if DURATION_S >= 3600 else [0],
        )

        week6_metrics["profiles"][profile] = {
            "legacy": {
                "measured_energy_kwh": legacy["measured_energy_kwh"],
                "gt_energy_kwh": legacy["gt_energy_kwh"],
                "max_temp_C": legacy["max_temp_C"],
                "temp_violations": legacy["temp_violations"],
                "torque_margin_violations": legacy["torque_margin_violations"],
                "valid_windows": legacy["valid_windows"],
                "excluded_windows": legacy["excluded_windows"],
                "rejected_windows": legacy["rejected_windows"],
            },
            "mpc": {
                "measured_energy_kwh": mpc["measured_energy_kwh"],
                "gt_energy_kwh": mpc["gt_energy_kwh"],
                "max_temp_C": mpc["max_temp_C"],
                "temp_violations": mpc["temp_violations"],
                "torque_margin_violations": mpc["torque_margin_violations"],
                "throttle_violations": mpc["throttle_violations"],
                "failsafe_count": mpc["failsafe_count"],
                "valid_windows": mpc["valid_windows"],
                "excluded_windows": mpc["excluded_windows"],
                "rejected_windows": mpc["rejected_windows"],
            },
            "ground_truth_savings_kwh": gt_savings_kwh,
            "ground_truth_savings_pct": gt_savings_pct,
            "estimated_savings_kwh": est_savings_kwh,
            "estimated_savings_pct": est_savings_pct,
            "estimation_error_pct": est_error_pct,
            "ci_lower_kwh": ci_low,
            "ci_upper_kwh": ci_high,
            "avoided_emissions_kg": avoided_emissions,
            "baseline_cv_rmse_pct": baseline.cv_rmse_,
            "baseline_nmbe_pct": baseline.nmbe_,
        }

        print(f"\n  {profile} summary:")
        print(f"    Legacy energy: {legacy['measured_energy_kwh']:.4f} kWh")
        print(f"    MPC energy: {mpc['measured_energy_kwh']:.4f} kWh")
        print(f"    GT savings: {gt_savings_kwh:.4f} kWh ({gt_savings_pct:.1f}%)")
        print(f"    Est savings: {est_savings_kwh:.4f} kWh ({est_savings_pct:.1f}%)")
        print(f"    Est error: {est_error_pct:.1f}%")
        print(f"    CI: [{ci_low:.4f}, {ci_high:.4f}] kWh")
        print(f"    Avoided CO2: {avoided_emissions:.4f} kg")

    # Overall summary
    total_legacy = sum(
        r["legacy"]["measured_energy_kwh"] for r in week6_metrics["profiles"].values()
    )
    total_mpc = sum(r["mpc"]["measured_energy_kwh"] for r in week6_metrics["profiles"].values())
    total_gt_savings = sum(
        r["ground_truth_savings_kwh"] for r in week6_metrics["profiles"].values()
    )
    total_est_savings = sum(r["estimated_savings_kwh"] for r in week6_metrics["profiles"].values())
    total_emissions = sum(r["avoided_emissions_kg"] for r in week6_metrics["profiles"].values())

    week6_metrics["summary"] = {
        "total_legacy_energy_kwh": total_legacy,
        "total_mpc_energy_kwh": total_mpc,
        "total_gt_savings_kwh": total_gt_savings,
        "total_est_savings_kwh": total_est_savings,
        "total_avoided_emissions_kg": total_emissions,
        "profiles_with_positive_savings": sum(
            1 for r in week6_metrics["profiles"].values() if r["ground_truth_savings_kwh"] > 0
        ),
        "profiles_with_positive_est_savings": sum(
            1 for r in week6_metrics["profiles"].values() if r["estimated_savings_kwh"] > 0
        ),
        "max_est_error_pct": max(
            abs(r["estimation_error_pct"])
            for r in week6_metrics["profiles"].values()
            if np.isfinite(r["estimation_error_pct"])
        ),
    }

    return week6_metrics


def save_metrics(week6_metrics: Dict):
    """Save Week 6 metrics to metrics.json."""
    existing = {}
    if METRICS_PATH.exists():
        existing = json.loads(METRICS_PATH.read_text())

    existing["week6"] = week6_metrics

    METRICS_PATH.write_text(json.dumps(existing, indent=2) + "\n")
    print(f"\nSaved metrics to {METRICS_PATH}")


def main():
    """Main entry point."""
    FIG_DIR.mkdir(parents=True, exist_ok=True)

    metrics = run_week6_experiment()
    save_metrics(metrics)

    print("\n=== Week 6 Experiment Complete ===")
    print(f"Summary: {metrics['summary']}")

    return metrics


if __name__ == "__main__":
    main()
