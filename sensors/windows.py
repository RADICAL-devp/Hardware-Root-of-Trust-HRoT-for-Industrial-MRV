"""Zero-crossing-aligned measurement windows (D-05 mitigation, golden model).

A window spans a FIXED number of whole grid cycles (``n_cycles``, default
10) with a VARIABLE sample count. Cycle boundaries come ONLY from rising
zero crossings detected on the measured, quantized voltage — the plant's
true frequency is never used anywhere in this module.

Detector (everything maps directly to FPGA comparators + a counter):
    - Arm when ``v < -hyst_V`` [V] (hysteresis; rejects noise chatter).
    - Trigger on the first sample with ``v >= 0`` [V] while armed.
    - The detection sample index IS the window boundary (integer-bound
      sums, exactly what the RTL counter produces). A linear-interpolated
      fractional position is also reported for diagnostics only.

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

from dataclasses import dataclass

import numpy as np


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


def find_rising_crossings(
    v_meas_V: np.ndarray, fs_hz: float, hyst_V: float
) -> tuple[np.ndarray, np.ndarray]:
    """Detect rising zero crossings on measured voltage (FPGA-equivalent logic).

    Args:
        v_meas_V: measured, quantized voltage [V] (the ONLY detector input).
        fs_hz: sample rate [samples/s] (unused for detection; kept for API
            symmetry with the window builder — detection uses no frequency).
        hyst_V: hysteresis arm level [V].

    Returns:
        Tuple ``(detect_idx [samples], frac_pos [samples])``: integer
        detection indices (first ``v >= 0`` sample while armed) and
        linear-interpolated fractional crossing positions (diagnostics only).
    """
    _ = fs_hz  # detection is purely comparative; no frequency enters.
    v = np.asarray(v_meas_V, dtype=float)
    det: list[int] = []
    frac: list[float] = []
    armed = bool(v[0] < -hyst_V)
    for k in range(1, v.shape[0]):
        if v[k - 1] < 0.0 and v[k] >= 0.0 and armed:
            det.append(k)
            step = v[k] - v[k - 1]
            frac.append((k - 1) + (-v[k - 1] / step if step > 0.0 else 0.0))
            armed = False
        elif v[k] < -hyst_V:
            armed = True
    return np.array(det, dtype=int), np.array(frac, dtype=float)


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
