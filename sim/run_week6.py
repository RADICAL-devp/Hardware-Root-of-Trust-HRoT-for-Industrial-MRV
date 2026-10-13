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
from plant.mechanics import CoupledPlant, CoupledResult, solve_equilibrium
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


# ============================================================
# Week 6C Stage A: continuous coupled-plant experiment.
# Both arms share one stepper primitive with carried (omega, temp) state;
# the plant is never restarted at MPC boundaries. Legacy uses the same
# machinery with constant u = 1.0 so the comparison is exactly matched.
# Full evaluation horizon, no settling exclusion (frozen rule).
# ============================================================


def ground_truth_energy_kwh(v_true_V: np.ndarray, i_true_A: np.ndarray, fs_hz: float) -> float:
    """Independent ground-truth energy [kWh]: sum(v*i*dt)/3.6e6, both arms alike."""
    v = np.asarray(v_true_V, dtype=float)
    i = np.asarray(i_true_A, dtype=float)
    return float(np.sum(v * i * (1.0 / float(fs_hz))) / 3.6e6)


def apply_sensor_chain(
    v_true_V: np.ndarray, i_true_A: np.ndarray, t_s: np.ndarray, seed: int
) -> tuple[np.ndarray, np.ndarray]:
    """Whole-trace independent sensor path: fresh AFE(seed)+ADC per arm.

    Applied once over the entire trace (never per-window fragments), so both
    arms consume identical draw counts for equal horizons at equal seeds.
    """
    t = np.arange(np.asarray(v_true_V).size) / FS_HZ if t_s is None else np.asarray(t_s)
    return measurement_chain(
        np.asarray(v_true_V), np.asarray(i_true_A), t, AFE(AFEParams(seed=seed)), ADCParams()
    )


def measure_windows(
    v_meas: np.ndarray,
    i_meas: np.ndarray,
    load: np.ndarray,
    temp: np.ndarray,
) -> Dict:
    """Zero-crossing window energy + verifier counts over a full trace."""
    zc = ZCParams()
    det_idx, frac = find_rising_crossings(v_meas, FS_HZ, zc.hyst_V)
    windows = build_windows(det_idx, frac, v_meas.size, zc)
    p_windows = window_mean_power(v_meas, i_meas, windows)
    energy_windows_uwh = []
    window_load = []
    window_temp = []
    for w, p_win in zip(windows, p_windows):
        if w.valid:
            energy_windows_uwh.append(p_win * w.n_samples / FS_HZ / 3600.0 * 1_000_000.0)
        else:
            energy_windows_uwh.append(0.0)
        center = (w.start_idx + w.end_idx) // 2
        window_load.append(float(load[center]))
        window_temp.append(float(temp[center]))
    valid_count = excluded_count = rejected_count = 0
    for idx, w in enumerate(windows):
        rec = {
            "window_flags": 0x1F if w.valid else 0x00,
            "energy_uwh": int(energy_windows_uwh[idx]) if w.valid else 0,
        }
        cls = classify(rec)
        if cls == "valid":
            valid_count += 1
        elif cls == "excluded":
            excluded_count += 1
        elif cls == "rejected":
            rejected_count += 1
    return {
        "energy_windows_uwh": np.array(energy_windows_uwh),
        "window_load": np.array(window_load),
        "window_temp": np.array(window_temp),
        "valid_windows": valid_count,
        "excluded_windows": excluded_count,
        "rejected_windows": rejected_count,
    }


def stall_suspected(below_flags: np.ndarray, accel: np.ndarray, frac: float = 0.10) -> bool:
    """Collapse diagnostic: tail of trace below capability AND decelerating."""
    n = len(below_flags)
    k = max(1, int(n * frac))
    tail_below = np.asarray(below_flags[-k:], dtype=float)
    tail_accel = np.asarray(accel[-k:], dtype=float)
    return bool(np.mean(tail_below) > 0.5 and np.mean(tail_accel) < 0.0)


def _window_torque_stats(torque: np.ndarray, load: np.ndarray, window_samples: int) -> Dict:
    """Per-fixed-window min/max/mean torque (diagnostics, not pass criteria)."""
    n = len(torque)
    n_windows = n // window_samples
    out = {"min": [], "max": [], "mean": []}
    for w_idx in range(n_windows):
        seg = torque[w_idx * window_samples : (w_idx + 1) * window_samples]
        out["min"].append(float(np.min(seg)))
        out["max"].append(float(np.max(seg)))
        out["mean"].append(float(np.mean(seg)))
    return out


def _summarize_arm(
    coupled: CoupledResult,
    load: np.ndarray,
    u_schedule: np.ndarray,
    gt_energy_kwh: float,
) -> Dict:
    """Per-arm diagnostics. Instantaneous torque margin is electromagnetic torque
    (NOT validated shaft torque); 1 s averages are reported, not pass criteria."""
    window_samples = int(FS_HZ * WINDOW_DURATION_S)
    inst = coupled.torque_Nm < 0.9 * load
    n_windows = len(load) // window_samples
    avg_viol_samples = 0
    for w_idx in range(n_windows):
        seg = coupled.torque_Nm[w_idx * window_samples : (w_idx + 1) * window_samples]
        lseg = load[w_idx * window_samples : (w_idx + 1) * window_samples]
        if float(np.mean(seg)) < 0.9 * float(np.mean(lseg)):
            avg_viol_samples += len(seg)
    stats = _window_torque_stats(coupled.torque_Nm, load, window_samples)
    speed = coupled.speed_rad_s
    return {
        "coupled": coupled,
        "load": np.asarray(load, dtype=float),
        "u_schedule": np.asarray(u_schedule, dtype=float),
        "gt_energy_kwh": float(gt_energy_kwh),
        "max_temp_C": float(np.max(coupled.temp_C)),
        "temp_violations": int(np.sum(coupled.temp_C > 120.0)),
        "instantaneous_violations": int(np.sum(inst)),
        "avg_torque_violation_samples": int(avg_viol_samples),
        "window_torque_min": stats["min"],
        "window_torque_max": stats["max"],
        "window_torque_mean": stats["mean"],
        "min_torque_margin": float(np.min(coupled.torque_Nm / np.maximum(load, 0.1))),
        "speed_min": float(np.min(speed)),
        "speed_max": float(np.max(speed)),
        "speed_drop_from_start": float(speed[0] - np.min(speed)),
        "max_abs_accel": float(np.max(np.abs(coupled.accel_rad_s2))),
        "slip_clamp_samples": int(np.sum(coupled.slip_clamp_flags)),
        "below_capability_samples": int(np.sum(coupled.below_capability_flags)),
        "stall_suspected": stall_suspected(coupled.below_capability_flags, coupled.accel_rad_s2),
        "throttle_violations": int(
            np.sum((np.asarray(u_schedule) < 0.4) | (np.asarray(u_schedule) > 1.0))
        ),
    }


def _window_records(
    load: np.ndarray,
    mp: MotorParams,
    mech,
    window_samples: int,
    legacy_parts: list[CoupledResult],
    mpc_parts: list[CoupledResult],
    mpc_u: list[float],
    mpc_window_events: list[dict | None],
) -> list[Dict]:
    """Per-control-window record: load, temperature, mechanical state, solver
    outcome, capability prediction (fit and true), feasibility classification,
    requested/applied control, provisional-criterion satisfaction, event."""
    from control.capability import CAP_OFFSET, capability_floor, meets_demand, peak_torque_Nm

    records: list[Dict] = []
    n_windows = len(mpc_parts)
    for w_idx in range(n_windows):
        seg_load = load[w_idx * window_samples : (w_idx + 1) * window_samples]
        mean_load = float(np.mean(seg_load))
        u_app = float(np.asarray(mpc_u)[w_idx])
        part = mpc_parts[w_idx]
        r = float(part.r_scale)
        cap_fit = float(capability_floor(u_app))
        cap_true = float(peak_torque_Nm(u_app, mp, r) * 0.7)
        demand = float(0.9 * mean_load + CAP_OFFSET)
        feasible = bool(meets_demand(mean_load, u_app))
        event = mpc_window_events[w_idx]
        if event is None:
            solver_status, requested, reason = "optimal", u_app, None
        else:
            solver_status, requested, reason = (
                event["solver_status"],
                event["u_requested"],
                event["reason"],
            )
        # Plant-side check of the PROVISIONAL criterion on produced torque
        # (acceptance metric; distinct from capability feasibility).
        satisfies = bool(np.min(part.torque_Nm) >= 0.9 * mean_load + CAP_OFFSET)
        records.append(
            {
                "window": w_idx,
                "load_mean_Nm": mean_load,
                "temp_mean_C": float(np.mean(part.temp_C)),
                "speed_end_rad_s": float(part.speed_rad_s[-1]),
                "u_requested": None if requested is None else float(requested),
                "u_applied": u_app,
                "solver_status": solver_status,
                "failsafe_reason": reason,
                "capability_fit_derated_Nm": cap_fit,
                "capability_true_derated_Nm": cap_true,
                "demand_Nm": demand,
                "feasible": feasible,
                "satisfies_provisional": satisfies,
                "window_torque_min_Nm": float(np.min(part.torque_Nm)),
            }
        )
    return records


def default_stageA_mpc_params() -> MPCParams:
    """MPC configuration for the Stage A experiment (criterion unchanged)."""
    return MPCParams(
        prediction_horizon=10,
        dt=WINDOW_DURATION_S,
        u_min=0.4,
        u_max=1.0,
        du_max=0.1,
        temp_max=120.0,
        torque_margin=0.9,
        R_u=1.0,
        Q_T=0.1,
        solver_timeout_ms=0,  # production: iteration limit only (Week 6F)
        warm_start=True,
    )


def simulate_continuous_pair(
    profile_kind: str,
    seconds: float,
    mech,
    seed: int = SEED,
    motor_params: MotorParams | None = None,
    thermal_params: ThermalParams | None = None,
    mpc_params: MPCParams | None = None,
    controller_id: str = "mpc",
) -> Dict:
    """Matched legacy-vs-MPC continuous coupled simulation over one profile.

    Both arms start from the same solved equilibrium speed and ambient
    temperature, run the same load trace over the same horizon with carried
    (omega, temp) state and a continuous global time base. The MPC arm
    updates control at 1 s boundaries; the plant never restarts.
    """
    mp = motor_params or MotorParams()
    th = thermal_params or ThermalParams()
    mpc_p = mpc_params or default_stageA_mpc_params()
    load = make_profile(profile_kind, duration_s=seconds, fs_hz=FS_HZ, seed=seed)
    window_samples = int(FS_HZ * WINDOW_DURATION_S)
    n_windows = len(load) // window_samples

    # Common initialization: solved stable equilibrium at initial load, u = 1.
    probe = CoupledPlant(mp, mech, th, FS_HZ)
    omega0, init_status = probe.solve_init(float(load[0]), 1.0)

    legacy_plant = CoupledPlant(mp, mech, th, FS_HZ)
    mpc_plant = CoupledPlant(mp, mech, th, FS_HZ)
    if omega0 is not None and np.isfinite(omega0):
        legacy_plant.omega = float(omega0)
        mpc_plant.omega = float(omega0)
    # else: both plants take the identical documented forced start inside
    # step_window (NO_EQUILIBRIUM reported, never an inconsistent silent target).
    legacy_plant.temp = th.t_amb_C
    mpc_plant.temp = th.t_amb_C

    mpc = MPCController(mpc_p, profile=profile_kind, controller_id=controller_id)

    legacy_u: list[float] = []
    mpc_u: list[float] = []
    legacy_parts: list[CoupledResult] = []
    mpc_parts: list[CoupledResult] = []
    mpc_window_events: list[dict | None] = []
    for w_idx in range(n_windows):
        w_start = w_idx * window_samples
        w_end = w_start + window_samples
        w_load = load[w_start:w_end]
        sim_time = w_idx * WINDOW_DURATION_S
        legacy_u.append(1.0)
        legacy_parts.append(legacy_plant.step_window(w_load, 1.0, w_start / FS_HZ))
        delta_T = mpc_plant.temp - th.t_amb_C
        u_prev = mpc_u[-1] if mpc_u else 1.0
        n_logs_before = len(mpc.get_failsafe_logs())
        u = mpc.solve(
            np.array([delta_T]),
            float(np.mean(w_load)),
            u_prev,
            step=w_idx,
            sim_time=sim_time,
        )
        new_events = mpc.get_failsafe_logs()[n_logs_before:]
        mpc_window_events.append(new_events[-1] if new_events else None)
        mpc_u.append(float(u))
        mpc_parts.append(mpc_plant.step_window(w_load, float(u), w_start / FS_HZ))

    from plant.mechanics import concat_coupled as _concat

    legacy_coupled = _concat(legacy_parts)
    mpc_coupled = _concat(mpc_parts)

    legacy_gt = ground_truth_energy_kwh(legacy_coupled.v_true_V, legacy_coupled.i_true_A, FS_HZ)
    mpc_gt = ground_truth_energy_kwh(mpc_coupled.v_true_V, mpc_coupled.i_true_A, FS_HZ)

    # Per-window equilibrium status at applied (mean load, u, window r_scale).
    eq_statuses: list[str] = []
    for w_idx in range(n_windows):
        seg_u = float(np.asarray(mpc_u)[w_idx])
        seg_load = float(np.mean(load[w_idx * window_samples : (w_idx + 1) * window_samples]))
        eq = solve_equilibrium(seg_load, seg_u, mp, mech, r_scale=mpc_parts[w_idx].r_scale)
        eq_statuses.append(eq.status)

    logs = [e for e in mpc.get_failsafe_logs() if e["profile"] == profile_kind]
    window_records = _window_records(
        load, mp, mech, window_samples, legacy_parts, mpc_parts, mpc_u, mpc_window_events
    )
    return {
        "profile": profile_kind,
        "seconds": float(seconds),
        "seed": int(seed),
        "init_omega": float(omega0) if omega0 is not None and np.isfinite(omega0) else None,
        "init_status": init_status,
        "load": load,
        "legacy": _summarize_arm(legacy_coupled, load, np.ones(n_windows), legacy_gt),
        "mpc": _summarize_arm(
            mpc_coupled,
            load,
            np.asarray(mpc_u, dtype=float),
            mpc_gt,
        ),
        "gt_savings_kwh": float(legacy_gt - mpc_gt),
        "failsafe_logs": logs,
        "failsafe_count": len(logs),
        "window_equilibrium_statuses": eq_statuses,
        "window_records": window_records,
    }


def estimation_error_fields(
    gt_savings_kwh: float, est_savings_kwh: float
) -> tuple[float | None, bool, str | None]:
    """Relative-error triple with standards-compliant undefined handling.

    Returns (pct_or_None, meaningful, reason_or_None). A zero-denominator
    relative error is recorded as None (JSON null) with an explicit reason;
    no epsilon substitution is applied. A null/undefined value must never be
    counted as passing the <3% target.
    """
    gt = float(gt_savings_kwh)
    est = float(est_savings_kwh)
    if abs(gt) > 1e-6:
        return 100.0 * (est - gt) / gt, True, None
    if abs(est) > 1e-6:
        return (
            None,
            False,
            "undefined: ground-truth savings is zero (|GT| <= 1e-6 kWh); "
            "see signed_error_kwh and abs_error_kwh",
        )
    return None, False, "undefined: both ground-truth and estimated savings are zero"


def run_stageA_experiment(mech=None, duration_s: float = DURATION_S, seed: int = SEED) -> Dict:
    """Full-length Stage A experiment: matched pairs + baseline estimation."""

    mech = _as_mech_arg(mech)
    pairs: dict[str, Dict] = {}
    for profile in KINDS:
        pairs[profile] = simulate_continuous_pair(
            profile,
            duration_s,
            mech,
            seed=seed,
            controller_id=f"mpc-{profile}",
        )

    # Baseline training on legacy measured windows (unchanged counterfactual).
    legacy_like = []
    for profile in KINDS:
        p = pairs[profile]
        m = measure_windows(
            *apply_sensor_chain(
                p["legacy"]["coupled"].v_true_V,
                p["legacy"]["coupled"].i_true_A,
                p["legacy"]["coupled"].t_s,
                seed,
            ),
            p["load"],
            p["legacy"]["coupled"].temp_C,
        )
        p["legacy"]["measured"] = m
        legacy_like.append(
            {
                "controller": "legacy",
                "energy_windows_uwh": m["energy_windows_uwh"].tolist(),
                "window_load": m["window_load"].tolist(),
                "window_temp": m["window_temp"].tolist(),
            }
        )
    baseline = run_baseline_training(legacy_like)

    emissions = EmissionsEstimator(EmissionsParams())
    out: Dict = {"profiles": {}, "summary": {}, "mech": _mech_provenance(mech)}
    for profile in KINDS:
        p = pairs[profile]
        m_mpc = measure_windows(
            *apply_sensor_chain(
                p["mpc"]["coupled"].v_true_V,
                p["mpc"]["coupled"].i_true_A,
                p["mpc"]["coupled"].t_s,
                seed,
            ),
            p["load"],
            p["mpc"]["coupled"].temp_C,
        )
        p["mpc"]["measured"] = m_mpc
        gt_savings = p["gt_savings_kwh"]
        legacy_gt = p["legacy"]["gt_energy_kwh"]
        est_kwh, ci_low, ci_high = baseline.estimate_savings(
            m_mpc["window_load"], m_mpc["window_temp"], m_mpc["energy_windows_uwh"]
        )
        meas_legacy_kwh = float(np.sum(p["legacy"]["measured"]["energy_windows_uwh"]) / 1e9)
        meas_mpc_kwh = float(np.sum(m_mpc["energy_windows_uwh"]) / 1e9)
        err_pct, rel_meaningful, err_reason = estimation_error_fields(gt_savings, est_kwh)
        avoided = emissions.aggregate_emissions(
            [est_kwh], [int(duration_s / 3600)] if duration_s >= 3600 else [0]
        )
        out["profiles"][profile] = {
            "legacy_gt_kwh": legacy_gt,
            "mpc_gt_kwh": p["mpc"]["gt_energy_kwh"],
            "measured_legacy_kwh": meas_legacy_kwh,
            "measured_mpc_kwh": meas_mpc_kwh,
            "ground_truth_savings_kwh": gt_savings,
            "ground_truth_savings_pct": (100.0 * gt_savings / legacy_gt if legacy_gt > 0 else 0.0),
            "estimated_savings_kwh": est_kwh,
            "estimated_savings_pct": (
                100.0 * est_kwh / meas_legacy_kwh if meas_legacy_kwh > 0 else 0.0
            ),
            "signed_error_kwh": float(est_kwh - gt_savings),
            "abs_error_kwh": float(abs(est_kwh - gt_savings)),
            "estimation_error_pct": err_pct,
            "estimation_error_undefined_reason": err_reason,
            "rel_error_meaningful": rel_meaningful,
            "rel_error_denominator_kwh": float(gt_savings),
            "ci_lower_kwh": ci_low,
            "ci_upper_kwh": ci_high,
            "avoided_emissions_kg": avoided,
            "baseline_cv_rmse_pct": baseline.cv_rmse_,
            "baseline_nmbe_pct": baseline.nmbe_,
            "init_status": p["init_status"],
            "failsafe_count": p["failsafe_count"],
            "window_equilibrium_statuses": p["window_equilibrium_statuses"],
            "legacy": _diag_summary(p["legacy"]),
            "mpc": _diag_summary(p["mpc"]),
        }
    finite_errs = [
        abs(v["estimation_error_pct"])
        for v in out["profiles"].values()
        if v["estimation_error_pct"] is not None and np.isfinite(v["estimation_error_pct"])
    ]
    if finite_errs:
        max_abs_err: float | None = max(finite_errs)
        max_abs_reason: str | None = None
    else:
        max_abs_err = None
        max_abs_reason = (
            "undefined: no profile has a finite relative error "
            "(all ground-truth savings are zero); see abs errors"
        )
    out["summary"] = {
        "total_legacy_gt_kwh": sum(v["legacy_gt_kwh"] for v in out["profiles"].values()),
        "total_mpc_gt_kwh": sum(v["mpc_gt_kwh"] for v in out["profiles"].values()),
        "total_gt_savings_kwh": sum(
            v["ground_truth_savings_kwh"] for v in out["profiles"].values()
        ),
        "total_est_savings_kwh": sum(v["estimated_savings_kwh"] for v in out["profiles"].values()),
        "profiles_with_positive_savings": sum(
            1 for v in out["profiles"].values() if v["ground_truth_savings_kwh"] > 0
        ),
        "max_abs_est_error_pct": max_abs_err,
        "max_abs_est_error_undefined_reason": max_abs_reason,
        "n_finite_est_errors": len(finite_errs),
    }
    return out


def _as_mech_arg(mech):
    from plant.mechanics import MechParams as _MechParams

    if mech is None:
        return _MechParams(
            j_motor_kgm2=0.0064,
            j_load_reflected_kgm2=0.01,
            gear_ratio=1.0,
            b_Nms=0.0008,
            tf_Nm=0.0,
        )
    if isinstance(mech, dict):
        return _MechParams(**mech)
    return mech


def _mech_provenance(mech) -> Dict:
    return {
        "j_motor_kgm2": mech.j_motor_kgm2,
        "j_load_reflected_kgm2": mech.j_load_reflected_kgm2,
        "gear_ratio": mech.gear_ratio,
        "b_Nms": mech.b_Nms,
        "tf_Nm": mech.tf_Nm,
        "status": (
            "synthetic sensitivity scenario; catalogue anchor 0.0064 is an "
            "order-of-magnitude reference, not a calibrated parameter"
        ),
    }


def _diag_summary(arm: Dict) -> Dict:
    c = arm["coupled"]
    return {
        "max_temp_C": arm["max_temp_C"],
        "temp_violations": arm["temp_violations"],
        "instantaneous_violations": arm["instantaneous_violations"],
        "avg_torque_violation_samples": arm["avg_torque_violation_samples"],
        "min_torque_margin": arm["min_torque_margin"],
        "global_torque_min": float(np.min(c.torque_Nm)),
        "global_torque_max": float(np.max(c.torque_Nm)),
        "speed_min": arm["speed_min"],
        "speed_max": arm["speed_max"],
        "speed_drop_from_start": arm["speed_drop_from_start"],
        "max_abs_accel": arm["max_abs_accel"],
        "slip_clamp_samples": arm["slip_clamp_samples"],
        "below_capability_samples": arm["below_capability_samples"],
        "stall_suspected": arm["stall_suspected"],
        "throttle_violations": arm["throttle_violations"],
        "init_status": c.init_status,
        "r_scale_first": float(c.r_scale),
    }


def save_stageA_metrics(stageA_metrics: Dict):
    """Persist Stage A metrics under results/metrics.json['week6c_stageA'] (actual runs only).

    Strict-JSON gate: the payload must serialize with allow_nan=False, so
    NaN/Infinity can never reach the committed file again.
    """
    existing = {}
    if METRICS_PATH.exists():
        existing = json.loads(METRICS_PATH.read_text())
    existing["week6c_stageA"] = stageA_metrics
    payload = json.dumps(existing, indent=2) + "\n"
    json.dumps(existing, allow_nan=False)  # strict gate: raises on NaN/Infinity
    METRICS_PATH.write_text(payload)
    print(f"\nSaved Stage A metrics to {METRICS_PATH}")


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
        solver_timeout_ms=0,  # production: iteration limit only (Week 6F)
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
