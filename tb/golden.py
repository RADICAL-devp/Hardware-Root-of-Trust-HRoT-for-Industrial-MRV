"""Bit-exact Python golden model of the UART parse + power calculation.

Scope split (read before Week 4 RTL): this model covers everything the
Week 4 RTL must match — byte-stream parse, per-sample Q15×Q15 product,
window accumulations, and the window/descriptor outputs. Bit-level UART
line signaling (start/stop-bit sampling, baud error) is RTL-only; the
cocotb tests drive those bits and compare against the byte/frame/power
values produced here.

Fixed-point formats (DECISIONS.md D-04, pinned exact):
    V, I in  : i16 Q15, V_FS = 500 Vpk, I_FS = 100 Apk.
    P_inst   : i32 Q30 = v_q15 · i_q15 (exact; |P| ≤ 2^30 + 2^15 < 2^31).
    P_AVG    : i32 Q30 = round-half-away(sum / M).
    Vrms/Irms: u16 Q15 (FS = channel FS); saturated at 32767 — the single
               saturating corner is full-negative DC (-32768² mean → root
               32768 → 32767, 1 LSB).
    PF       : i16 Q15 signed = round-half-away(P_AVG·2^15 / (Vrms·Irms)),
               saturated to [-32768, 32767]; 0 when Vrms·Irms == 0.
               Contract: BIT-EXACT RTL == golden (pure integer formula, no
               LUT involved) — Week 4a2 decision, see DECISIONS.md.

Rounding mode (canonical, everywhere): ROUND HALF AWAY FROM ZERO.
Symmetric for ±quotients, so long-run averaging carries no DC bias
(unlike round-half-up); exact halves are the only cases where it differs
from Python banker's round, and only by 1 LSB. Tested explicitly on
negative values and exact halves in tests/test_week4a_golden.py.

Saturation rules: per-sample products are exact (no rounding, no
saturation — the full-scale product fits i32). Accumulators are
unbounded Python ints in the model; the minimum RTL widths are:
    per-window sum(v·i) : |sum| ≤ 2230·(2^30−2^15) < 2^42 → 43-bit signed.
    RECORD sum (P_AVG over up to 5 valid sub-windows, 11150 samples):
      |sum| ≤ 11150·32768·32767 = 11,973,085,974,400 < 2^44 (and ≥ 2^43)
      → 44 magnitude bits + sign = 45-bit signed minimum (RTL: 64-bit).
    sum(v²)  : same per-window bound as sum(v·i) → 43-bit signed, of which
               the mean is non-negative (RTL: 64-bit, 43 bits used).
    energy   : product p_sum·12500 < 2^56 per sub-window (RTL: 56-bit),
               then shift 30 + divide by 9; record ENERGY_UWH max
               15,485,165 µWh (~15.5 Wh), u64 headroom ≈ 1.2×10^12
               records (~37,700 yr at 1 Hz).
Means divide by the variable count M via the reciprocal LUT
(`sensors.windows.lut_window_mean`, 24-bit, error ≤ 0.0066 %); the exact
reference mean is `mean_q30_half_away`.

ENERGY_UWH increment per valid (sub-)window, integer-exact (no float):
    E = P_W·M/fs/3600·10^6, P_W = p_sum/M/2^30·50000, fs = 10000
      = round-half-away(p_sum · 12500 / (9 · 2^30))   [µWh].
Invalid sub-windows contribute samples to the window hash but zero energy
and are excluded from P_AVG (flagged, never healed — D-05 policy).

UART parse model: SOF hunt (0xA5), fixed 11-byte frames, CRC16-CCITT-FALSE
gate over bytes 1–8, +1-byte advance past false syncs, trailing partial
frames held back. Standalone implementation (independent of
edge.framing); cross-tested for exact equality on seeded streams.

Zero-crossing detector: golden logic lives in sensors/windows.py
(find_rising_crossings + build_windows); that module's docstring is the
detector design (hysteresis comparators, integer detection indices =
RTL counter values, fractional interpolation diagnostics-only, gap/M
validation, invalid flag). This module maps valid/invalid windows to the
descriptor fields (M or 0) exactly as edge/attestation.py does.
"""

from dataclasses import dataclass

from sensors.windows import (
    build_windows,
    div_round_half_away,
    energy_uwh_increment,
    find_rising_crossings,
    isqrt_round_half_up,
    mean_q30_half_away,
)

# Re-exported so tb/golden.py is the single entry point for cocotb Week 4:
# detection golden logic lives in sensors/windows.py (one source only).
__all__ = [
    "find_rising_crossings",
    "build_windows",
]

SOF = 0xA5  # [byte] start-of-frame marker (D-01)
FRAME_LEN = 11  # [bytes] fixed L0 frame length (D-01)
Q15_SCALE = 1 << 15  # [counts] Q15 scale factor
Q30_SCALE = 1 << 30  # [counts] Q30 scale factor
P_FS_W = 500.0 * 100.0  # [W] Q30 power full-scale
I16_MAX = (1 << 15) - 1  # [counts] i16/u16 positive saturation corner
ACCUM_BITS = 43  # [bits] minimum signed per-window accumulator width
RECORD_ACCUM_BITS = 45  # [bits] minimum signed record-total accumulator width


@dataclass(frozen=True)
class L0Sample:
    """One parsed L0 sample (host units per field)."""

    counter: int  # [counts] monotonic u32 sample counter
    v_q15: int  # [counts] i16 Q15 voltage code
    i_q15: int  # [counts] i16 Q15 current code


def _crc16(data: bytes) -> int:
    """CRC16-CCITT-FALSE [u16] (independent re-implementation for the golden)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def parse_l0_stream(data: bytes) -> list[L0Sample]:
    """Parse a UART byte stream into L0 samples (CRC-gated, resynchronising).

    Standalone golden re-implementation of the D-01 framing: hunt SOF,
    require 11 bytes, check CRC over bytes 1–8, advance one byte past
    false syncs, drop trailing partial frames. Returns samples in arrival
    order; CRC-bad frames vanish (the receiver flags the resulting gap).
    """
    import struct

    out: list[L0Sample] = []
    buf = bytearray(data)
    while True:
        try:
            sof = buf.index(SOF)
        except ValueError:
            return out
        del buf[:sof]
        if len(buf) < FRAME_LEN:
            return out
        body = bytes(buf[1:9])
        (crc,) = struct.unpack("<H", bytes(buf[9:11]))
        if crc != _crc16(body):
            del buf[:1]
            continue
        (counter, v_q15, i_q15) = struct.unpack("<Ihh", body)
        out.append(L0Sample(counter=counter, v_q15=v_q15, i_q15=i_q15))
        del buf[:FRAME_LEN]


def mult_q15_q15(v_q15: int, i_q15: int) -> int:
    """Exact per-sample power [Q30 counts]; asserts i32 range (never saturates)."""
    p = int(v_q15) * int(i_q15)
    assert -(1 << 31) <= p < (1 << 31), f"Q30 product out of i32 range: {p}"
    return p


@dataclass(frozen=True)
class WindowPower:
    """Bit-exact per-window power results (units per field)."""

    m: int  # [samples] variable window count
    p_sum_q30: int  # [Q30 counts] exact integer sum of v·i
    p_avg_q30: int  # [Q30 counts] i32 mean, round-half-away
    vrms_q15: int  # [counts] u16 Q15 RMS voltage (saturated corner documented)
    irms_q15: int  # [counts] u16 Q15 RMS current (saturated corner documented)
    pf_q15: int  # [counts] i16 Q15 power factor (0 when Vrms·Irms == 0)
    energy_uwh_inc: int  # [µWh] integer-exact energy increment


def window_power(
    v_q15: list[int] | tuple[int, ...], i_q15: list[int] | tuple[int, ...]
) -> WindowPower:
    """Compute all power outputs for one window of Q15 code lists (equal length)."""
    v = [int(x) for x in v_q15]
    i = [int(x) for x in i_q15]
    if len(v) != len(i) or not v:
        raise ValueError("V/I windows must be non-empty and equal length")
    m = len(v)
    p_sum = sum(a * b for a, b in zip(v, i))  # [Q30 counts] exact
    assert abs(p_sum) < (1 << 42), "accumulator exceeds 43-bit signed minimum"
    sv_sum = sum(a * a for a in v)  # [counts] exact sum of squares
    si_sum = sum(b * b for b in i)  # [counts] exact sum of squares
    p_avg = mean_q30_half_away(p_sum, m)  # [Q30 counts]
    vrms = min(isqrt_round_half_up(div_round_half_away(sv_sum, m)), I16_MAX)
    irms = min(isqrt_round_half_up(div_round_half_away(si_sum, m)), I16_MAX)
    den = vrms * irms  # [Q30 counts]
    if den == 0:
        pf = 0
    else:
        pf = div_round_half_away(p_avg * Q15_SCALE, den)
        pf = max(-(1 << 15), min(I16_MAX, pf))
    return WindowPower(
        m=m,
        p_sum_q30=p_sum,
        p_avg_q30=p_avg,
        vrms_q15=vrms,
        irms_q15=irms,
        pf_q15=pf,
        energy_uwh_inc=energy_uwh_increment(p_sum, m),
    )


@dataclass(frozen=True)
class DescriptorFields:
    """Exact values for the 36-byte descriptor (units per field)."""

    p_avg_q30: int  # [Q30 counts] i32 mean over valid samples only
    energy_uwh: int  # [µWh] u64 cumulative over valid sub-windows only
    zc_samples: tuple[int, ...]  # [samples] 5× u16 M, 0 when invalid
    window_flags: int  # [u8] bit k = sub-window k valid


def fractional_window_mean(
    v_q15: list[int] | tuple[int, ...],
    i_q15: list[int] | tuple[int, ...],
    start_frac: float,
    end_frac: float,
) -> float:
    """Diagnostics-only mean over fractional bounds [Q30 counts, float].

    Trapezoid weights: end samples contribute their covered fraction,
    interior samples weight 1, divided by (end_frac - start_frac). Used to
    MEASURE the accuracy cost of integer-boundary truncation (Week 4a2):
    it is explicitly NOT the RTL contract — the RTL sums integer bounds.
    """
    v = [int(x) for x in v_q15]
    i = [int(x) for x in i_q15]
    span = float(end_frac) - float(start_frac)
    if span <= 0 or len(v) != len(i):
        raise ValueError("need end_frac > start_frac and equal-length inputs")
    total, k0, k1 = 0.0, int(start_frac), int(end_frac)
    for k in range(max(k0, 0), min(k1 + 1, len(v))):
        w = min(k + 1, end_frac) - max(k, start_frac)
        if w > 0:
            total += w * v[k] * i[k]
    return total / span


def record_fields(
    sub_windows: list[tuple[list[int], list[int], bool]], energy_prev_uwh: int = 0
) -> DescriptorFields:
    """Compose descriptor fields from 5 (v_codes, i_codes, valid) sub-windows.

    Mirrors edge/attestation.build_record exactly (same helpers): invalid
    sub-windows hash their samples upstream but contribute M = 0 here,
    zero energy, and no samples to P_AVG.
    """
    if len(sub_windows) != 5:
        raise ValueError("need exactly 5 sub-windows")
    zc: list[int] = []
    p_sum, p_count, energy = 0, 0, int(energy_prev_uwh)
    for v_codes, i_codes, valid in sub_windows:
        if not valid:
            zc.append(0)
            continue
        v = [int(x) for x in v_codes]
        w = window_power(v, [int(x) for x in i_codes])
        zc.append(w.m)
        p_sum += w.p_sum_q30
        p_count += w.m
        energy += w.energy_uwh_inc
    flags = 0
    for k, (_, _, valid) in enumerate(sub_windows):
        flags |= (1 if valid else 0) << k
    return DescriptorFields(
        p_avg_q30=mean_q30_half_away(p_sum, p_count) if p_count else 0,
        energy_uwh=energy,
        zc_samples=tuple(zc),
        window_flags=flags,
    )
