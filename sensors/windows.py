"""Zero-crossing-aligned measurement windows (D-05 mitigation, golden model).

A window spans a FIXED number of whole grid cycles (``n_cycles``, default
10) with a VARIABLE sample count. Cycle boundaries come ONLY from rising
zero crossings detected on the measured, quantized voltage — the plant's
true frequency is never used anywhere in this module.

Detector (everything maps directly to FPGA comparators + a counter):
    integer core `find_rising_crossings_q15` is the single source of truth,
    operating on Q15 voltage codes with integer thresholds only —
    arm when ``code < ARM_Q15`` (-328 codes = -5.0049 V), trigger on the
    first ``code >= 0`` while armed. `find_rising_crossings` (volts) is a
    thin wrapper converting through the exact ADC→Q15 map; hysteresis
    magnitude is unchanged (5 V nominal; effective arm level on the ADC
    grid identical, proven by test). The detection sample index IS the
    window boundary (integer-bound sums, exactly what the RTL counter
    produces). A linear-interpolated fractional position is also reported
    for diagnostics only — it never decides edges (pinned by test).

Validity (a window failing any check is flagged invalid, never healed):
    - every inter-crossing gap within ``[gap_min_samples, gap_max_samples]``
      [samples] (catches missing/extra crossings from dropouts/distortion);
    - total sample count ``M`` within ``[m_min_samples, m_max_samples]``
      [samples], derived from the supported ``[f_min_Hz, f_max_Hz]`` [Hz]
      range: ``M ∈ [N*fs/f_max - 8, N*fs/f_min + 8]`` (margin covers
      jitter/noise boundary shifts of a few samples).

Variable-count mean for RTL (Week 4 target): the FPGA accumulates the
integer-bound ``sum(v*i)`` and multiplies once per window by ``1/M`` from
a reciprocal LUT (``lut_out_bits``-bit output, indexed by ``M - m_min``).
:func:`reciprocal_lut_inv` + :func:`lut_window_mean` model that path in
integer arithmetic; the measured accuracy cost is reported in
``results/metrics.json`` (far below the sensor error budget).

This module is the Python golden model for Week 4 RTL: integer-bound sums
are the bit-exact match target (1-LSB tolerance per AGENTS.md); float
division is the characterization reference.
"""

import math
from dataclasses import dataclass

import numpy as np

HYST_V = 5.0  # [V] nominal hysteresis arm level (magnitude unchanged since Week 2b)
V_FS_PK_V = 500.0  # [V] Q15 voltage full-scale (D-04, exact)
ARM_Q15 = int(round(-HYST_V / V_FS_PK_V * 32768.0))  # [counts] = -328; V_arm = -5.0049 V
TRIG_Q15 = 0  # [counts] rising trigger: first code >= 0 while armed


@dataclass(frozen=True)
class ZCParams:
    """Zero-crossing window parameters (units documented per field)."""

    n_cycles: int = 10  # [cycles] whole grid cycles per window (D-05 default)
    hyst_V: float = 5.0  # [V] hysteresis arm level (~14x noise rms; shifts nothing)
    f_min_Hz: float = 45.0  # [Hz] lower edge of supported grid-frequency range
    f_max_Hz: float = 55.0  # [Hz] upper edge of supported grid-frequency range
    fs_hz: float = 10_000.0  # [samples/s] telemetry sample rate (D-04)
    gap_min_samples: int = 150  # [samples] per-cycle gap floor (extra crossings)
    gap_max_samples: int = 260  # [samples] per-cycle gap ceiling (missing crossings)
    lut_out_bits: int = 24  # [bits] reciprocal-LUT output width (sized: 18 b cost 0.41 %)

    @property
    def m_min_samples(self) -> int:
        """[samples] window-count floor: N*fs/f_max minus boundary margin."""
        return int(self.n_cycles * self.fs_hz / self.f_max_Hz) - 8

    @property
    def m_max_samples(self) -> int:
        """[samples] window-count ceiling: N*fs/f_min plus boundary margin."""
        return int(self.n_cycles * self.fs_hz / self.f_min_Hz) + 8


@dataclass
class ZCWindow:
    """One candidate measurement window over integer sample bounds."""

    start_idx: int  # [samples] inclusive start (detection sample)
    end_idx: int  # [samples] exclusive end (detection sample N cycles later)
    valid: bool  # [flag] False → excluded from power means, reported upstream
    reason: str = ""  # [text] why invalid ("" when valid)

    @property
    def n_samples(self) -> int:
        """[samples] variable window count M."""
        return self.end_idx - self.start_idx


def _volts_to_q15(v_V: np.ndarray) -> np.ndarray:
    """Exact ADC-volts→Q15 map replica (local: importing edge.framing cycles).

    For ADC-quantized inputs this is bit-exact: ``(code - 2048) * 16`` in
    integer arithmetic (LSB = 1000/4096 V is exact in binary FP and the
    products need < 53 bits). Pinned equal to ``edge.framing.q15_encode``
    by test across all 4096 ADC codes.
    """
    q = np.round(np.asarray(v_V, dtype=float) / V_FS_PK_V * 32768.0)
    return np.clip(q, -32768, 32767).astype(np.int64)


def find_rising_crossings_q15(
    v_q15: np.ndarray, arm_q15: int = ARM_Q15
) -> tuple[np.ndarray, np.ndarray]:
    """Integer-only rising-zero-crossing detector (single source of truth).

    Pure integer comparisons on Q15 codes — this exact logic maps to the
    Week 4b `zc_detect.v` comparators + counter: arm when ``code <
    arm_q15``, trigger on the first ``code >= TRIG_Q15`` (0) while armed.
    The returned detection indices ARE the window boundaries; the
    fractional positions are diagnostics-only (float division, never an
    edge input).
    """
    codes = np.asarray(v_q15, dtype=np.int64).ravel()
    det: list[int] = []
    frac: list[float] = []
    armed = bool(codes[0] < arm_q15)
    for k in range(1, codes.shape[0]):
        if codes[k - 1] < TRIG_Q15 and codes[k] >= TRIG_Q15 and armed:
            det.append(k)
            step = int(codes[k]) - int(codes[k - 1])
            frac.append((k - 1) + (-int(codes[k - 1]) / step if step > 0 else 0.0))
            armed = False
        elif codes[k] < arm_q15:
            armed = True
    return np.array(det, dtype=int), np.array(frac, dtype=float)


def find_rising_crossings(
    v_meas_V: np.ndarray, fs_hz: float, hyst_V: float
) -> tuple[np.ndarray, np.ndarray]:
    """Detect rising zero crossings on measured voltage (FPGA-equivalent logic).

    Thin wrapper: converts through the exact Q15 map and delegates to the
    integer core, so volts callers keep byte-identical behavior while all
    edge decisions are integer-exact. Arguments unchanged (``hyst_V`` kept
    for API stability; the live threshold is ``ARM_Q15``).

    Args:
        v_meas_V: measured, quantized voltage [V] (the ONLY detector input).
        fs_hz: sample rate [samples/s] (unused for detection; kept for API
            symmetry with the window builder — detection uses no frequency).
        hyst_V: hysteresis arm level [V] (nominal; integer core uses ARM_Q15).

    Returns:
        Tuple ``(detect_idx [samples], frac_pos [samples])``: integer
        detection indices (first ``v >= 0`` sample while armed) and
        linear-interpolated fractional crossing positions (diagnostics only).
    """
    _ = fs_hz  # detection is purely comparative; no frequency enters.
    if hyst_V != HYST_V:
        # The integer core pins the threshold at ARM_Q15; a non-default
        # volts threshold has no code-domain meaning — fail loud, never
        # silently substitute.
        raise ValueError(f"only the default hysteresis ({HYST_V} V) is supported")
    return find_rising_crossings_q15(_volts_to_q15(v_meas_V))


def build_windows(
    detect_idx: np.ndarray,
    frac_pos: np.ndarray,
    n_total_samples: int,
    params: ZCParams,
) -> list[ZCWindow]:
    """Stride non-overlapping N-cycle windows over detection indices.

    Integer bounds ``[start_idx, end_idx)`` match the RTL counter exactly.
    ``frac_pos`` is accepted (diagnostics/future refinement) but does not
    move the bounds, keeping the golden model bit-comparable with RTL.
    """
    _ = frac_pos
    det = np.asarray(detect_idx, dtype=int)
    n = params.n_cycles
    windows: list[ZCWindow] = []
    for j in range(0, det.shape[0] - n, n):
        s, e = int(det[j]), int(det[j + n])
        gaps = np.diff(det[j : j + n + 1])
        if np.any(gaps < params.gap_min_samples) or np.any(gaps > params.gap_max_samples):
            windows.append(ZCWindow(s, e, False, "gap"))
            continue
        m = e - s
        if m < params.m_min_samples or m > params.m_max_samples or e > n_total_samples:
            windows.append(ZCWindow(s, e, False, "range"))
            continue
        windows.append(ZCWindow(s, e, True))
    return windows


def window_mean_power(v_V: np.ndarray, i_A: np.ndarray, windows: list[ZCWindow]) -> np.ndarray:
    """Real power [W] per window over integer bounds; NaN for invalid windows."""
    v = np.asarray(v_V, dtype=float)
    i = np.asarray(i_A, dtype=float)
    out = np.full(len(windows), np.nan)
    for k, w in enumerate(windows):
        if w.valid:
            out[k] = float(np.mean(v[w.start_idx : w.end_idx] * i[w.start_idx : w.end_idx]))
    return out


def window_rms(x: np.ndarray, windows: list[ZCWindow]) -> np.ndarray:
    """RMS [same units as x] per window over integer bounds; NaN if invalid."""
    xx = np.asarray(x, dtype=float)
    out = np.full(len(windows), np.nan)
    for k, w in enumerate(windows):
        if w.valid:
            seg = xx[w.start_idx : w.end_idx]
            out[k] = float(np.sqrt(np.mean(seg * seg)))
    return out


def reciprocal_lut_inv(m: int, m_min: int, out_bits: int) -> int:
    """Model the RTL reciprocal LUT: ``round(2^out_bits / M)`` [counts].

    Hardware: 9-bit-indexed table (``M - m_min`` covers the supported range
    in 512 entries) holding the ``out_bits``-bit fractional reciprocal; one
    multiply-shift per window replaces the divider. Sizing is measured, not
    assumed: 18 b output costs up to 0.41 % (material vs the sensor budget),
    24 b costs 0.0066 % (see D-05).
    """
    idx = m - m_min
    assert 0 <= idx < 512, f"M={m} outside supported LUT range"
    return int(round(float(1 << out_bits) / m))


def lut_window_mean(power_sum_q30: int, m: int, m_min: int, out_bits: int) -> int:
    """Model the RTL variable-count mean: ``(sum * lut(1/M)) >> out_bits``.

    Args:
        power_sum_q30: accumulated ``sum(v*i)`` [Q30 counts] over the window.
        m: window sample count [samples].
        m_min: LUT base index [samples].
        out_bits: reciprocal width [bits].

    Returns:
        Mean power [Q30 counts], directly comparable with the RTL register.
    """
    return (int(power_sum_q30) * reciprocal_lut_inv(m, m_min, out_bits)) >> out_bits


def div_round_half_away(num: int, den: int) -> int:
    """Integer division rounding half away from zero (canonical rounding mode).

    Exact halves (``2·|rem| == den``) round away from zero, so positive and
    negative quotients are symmetric and long-run averaging carries no DC
    bias — unlike round-half-up. ``den`` must be positive; the Week 4 RTL
    divider implements this exact rule (sign-magnitude + round injection).
    """
    num, den = int(num), int(den)
    if den <= 0:
        raise ValueError(f"denominator must be positive, got {den}")
    q, r = divmod(abs(num), den)
    if 2 * r >= den:
        q += 1
    return q if num >= 0 else -q


def mean_q30_half_away(power_sum_q30: int, m: int) -> int:
    """Window mean power [Q30 counts] = round-half-away(sum / M), ``m > 0``."""
    if int(m) <= 0:
        raise ValueError(f"window count must be positive, got {m}")
    return div_round_half_away(power_sum_q30, m)


def isqrt_round_half_up(x: int) -> int:
    """Integer square root [counts] rounding half up (``rem > root → +1``).

    Matches a non-restoring RTL square-root unit bit-for-bit: the unit
    yields ``(root, remainder)`` and rounds up iff ``remainder > root``
    (since ``(r+0.5)² = r²+r+0.25``, i.e. ``rem ≥ r+1`` means ≥ half).
    Negative inputs raise.
    """
    x = int(x)
    if x < 0:
        raise ValueError(f"isqrt of negative: {x}")
    root = math.isqrt(x)
    return root + 1 if (x - root * root) > root else root


def energy_uwh_increment(power_sum_q30: int, m: int) -> int:
    """Integer-exact energy increment [µWh] for one valid (sub-)window.

    ``E = P_W · M / fs / 3600 · 10^6`` with ``P_W = p_sum / M / 2^30 ·
    50000`` (Q30 full-scale 50 kW) and ``fs = 10000`` collapses to the
    integer form ``round-half-away(p_sum · 12500 / (9 · 2^30))`` — no
    float anywhere, so the Week 4 RTL (56-bit product, divide by the
    constant ``9·2^30`` = shift 30 + divide by 9) matches bit-for-bit.
    """
    _ = m  # count already inside p_sum; kept for call-site symmetry.
    return div_round_half_away(int(power_sum_q30) * 12500, 9 * (1 << 30))
