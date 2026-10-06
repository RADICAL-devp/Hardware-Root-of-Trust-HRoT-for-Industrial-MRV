"""Load torque profiles for the Week 2 motor plant.

Every parameter below carries SI units. All profiles are deterministic in
``(kind, seed)``: the same arguments always return the bit-identical array,
so ``make repro`` (SEED=42) reproduces every figure and metric.

Profile definitions (output: shaft load torque, newton-metre [N·m]):
- ``steady``: constant ``STEADY_TORQUE_NM`` [N·m].
- ``cyclic``: 50 %-duty square wave alternating ``CYCLIC_LOW_NM`` /
  ``CYCLIC_HIGH_NM`` [N·m] with period ``CYCLIC_PERIOD_S`` [s].
- ``bursty``: constant base ``BURSTY_BASE_NM`` [N·m] plus intermittent
  bursts. Burst start times are drawn uniformly over the run and burst
  durations / amplitudes from seeded ``numpy.random.default_rng(seed)``
  streams, so the shape is fully reproducible for a fixed seed.

Units: ``duration_s`` [s], ``fs_hz`` [samples/s], ``seed`` [dimensionless].
"""

from dataclasses import dataclass

import numpy as np

KINDS = ("steady", "cyclic", "bursty")

STEADY_TORQUE_NM = 10.0  # [N·m] constant shaft load
CYCLIC_LOW_NM = 6.0  # [N·m] off-peak shaft load
CYCLIC_HIGH_NM = 14.0  # [N·m] on-peak shaft load
CYCLIC_PERIOD_S = 4.0  # [s] square-wave period (50 % duty)
BURSTY_BASE_NM = 6.0  # [N·m] background shaft load between bursts
BURSTY_PEAK_NM = 17.0  # [N·m] burst plateau shaft load
BURSTY_N_BURSTS = 6  # [count] bursts per run (scaled by duration)
BURSTY_MIN_DUR_S = 0.15  # [s] shortest burst plateau
BURSTY_MAX_DUR_S = 0.60  # [s] longest burst plateau
TORQUE_LIMIT_NM = 20.0  # [N·m] hard clip: motor pull-out / current limit


@dataclass(frozen=True)
class ProfileConfig:
    """Tunable profile constants (units documented per field)."""

    steady_torque_Nm: float = STEADY_TORQUE_NM  # [N·m]
    cyclic_low_Nm: float = CYCLIC_LOW_NM  # [N·m]
    cyclic_high_Nm: float = CYCLIC_HIGH_NM  # [N·m]
    cyclic_period_s: float = CYCLIC_PERIOD_S  # [s]
    bursty_base_Nm: float = BURSTY_BASE_NM  # [N·m]
    bursty_peak_Nm: float = BURSTY_PEAK_NM  # [N·m]
    torque_limit_Nm: float = TORQUE_LIMIT_NM  # [N·m]


def make_profile(
    kind: str,
    duration_s: float = 5.0,
    fs_hz: float = 10_000.0,
    seed: int = 42,
    config: ProfileConfig | None = None,
) -> np.ndarray:
    """Return shaft load torque [N·m] sampled at ``fs_hz`` [samples/s].

    Args:
        kind: one of ``"steady"``, ``"cyclic"``, ``"bursty"``.
        duration_s: profile length [s].
        fs_hz: sample rate [samples/s].
        seed: deterministic seed [dimensionless]; only ``"bursty"`` draws
            random numbers, but all kinds accept it for a uniform API.
        config: optional :class:`ProfileConfig` overrides.

    Returns:
        1-D ``float64`` array of length ``round(duration_s * fs_hz)`` [N·m].
    """
    if kind not in KINDS:
        raise ValueError(f"unknown profile kind {kind!r}; expected one of {KINDS}")
    cfg = config or ProfileConfig()
    n = int(round(duration_s * fs_hz))
    if kind == "steady":
        return np.full(n, cfg.steady_torque_Nm)
    if kind == "cyclic":
        period = int(round(cfg.cyclic_period_s * fs_hz))
        idx = np.arange(n) % period
        high = idx >= period // 2
        out = np.full(n, cfg.cyclic_low_Nm)
        out[high] = cfg.cyclic_high_Nm
        return out
    # bursty: seeded intermittent plateaus with short linear ramps.
    rng = np.random.default_rng(seed)
    out = np.full(n, cfg.bursty_base_Nm)
    n_bursts = max(1, int(round(BURSTY_N_BURSTS * duration_s / 8.0)))
    starts = rng.uniform(0.0, max(duration_s - BURSTY_MAX_DUR_S, 0.0), size=n_bursts)
    durations = rng.uniform(BURSTY_MIN_DUR_S, BURSTY_MAX_DUR_S, size=n_bursts)
    ramps = int(0.02 * fs_hz)  # [samples] 20 ms linear edges
    for start_s, dur_s in zip(starts.tolist(), durations.tolist()):
        s0 = int(start_s * fs_hz)
        s1 = min(n, s0 + int(dur_s * fs_hz))
        if s1 <= s0:
            continue
        out[s0:s1] = cfg.bursty_peak_Nm
        a = max(0, s0 - ramps)  # linear ramp up into the burst
        if s0 > a:
            out[a:s0] = np.linspace(cfg.bursty_base_Nm, cfg.bursty_peak_Nm, s0 - a, endpoint=False)
        b = min(n, s1 + ramps)  # linear ramp down out of the burst
        if b > s1:
            out[s1:b] = np.linspace(cfg.bursty_peak_Nm, cfg.bursty_base_Nm, b - s1, endpoint=False)
    return np.clip(out, 0.0, cfg.torque_limit_Nm)
