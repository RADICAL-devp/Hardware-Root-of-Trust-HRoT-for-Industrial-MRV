"""12-bit ADC quantization model (per DECISIONS.md D-04).

Bipolar straight-binary coding with ``n_bits`` [bits] over a symmetric
full-scale range ``[-FS_pk, +FS_pk]`` ([V] for voltage, [A] for current):

    LSB = 2 * FS_pk / 2^n_bits = FS_pk / 2^(n_bits - 1)   ([V] or [A])
    code = clip(round((x + FS_pk) / LSB), 0, 2^n_bits - 1)  [counts]
    x_q  = code * LSB - FS_pk                              ([V] or [A])

Defaults: voltage FS ±500 Vpk (LSB ≈ 0.244 V), current FS ±100 Apk
(LSB ≈ 0.0488 A). Both comfortably contain the Week 2 motor operating
point (325 Vpk, ~31 Apk worst case). Out-of-range inputs saturate
(clip); there is no wrap-around. Quantization error is uniform ±0.5 LSB
by construction. Sensors always have noise from day one (AGENTS.md):
this module adds quantization; see :mod:`sensors.afe` for analog noise.
"""

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ADCParams:
    """ADC parameters (units documented per field)."""

    n_bits: int = 12  # [bits] resolution
    v_fullscale_pk_V: float = 500.0  # [V] positive full-scale, voltage channel
    i_fullscale_pk_A: float = 100.0  # [A] positive full-scale, current channel

    @property
    def n_codes(self) -> int:
        """[counts] distinct output codes (2^n_bits)."""
        return 1 << self.n_bits

    @property
    def lsb_V(self) -> float:
        """[V] voltage quantization step."""
        return self.v_fullscale_pk_V / float(1 << (self.n_bits - 1))

    @property
    def lsb_A(self) -> float:
        """[A] current quantization step."""
        return self.i_fullscale_pk_A / float(1 << (self.n_bits - 1))


def _quantize(x: np.ndarray, fs_pk: float, n_bits: int, lsb: float) -> np.ndarray:
    codes = np.round((np.asarray(x, dtype=float) + fs_pk) / lsb)
    codes = np.clip(codes, 0, (1 << n_bits) - 1)
    return codes * lsb - fs_pk


def quantize_voltage(v_V: np.ndarray, params: ADCParams) -> np.ndarray:
    """Quantize terminal voltage [V] through the 12-bit voltage channel."""
    return _quantize(v_V, params.v_fullscale_pk_V, params.n_bits, params.lsb_V)


def quantize_current(i_A: np.ndarray, params: ADCParams) -> np.ndarray:
    """Quantize phase current [A] through the 12-bit current channel."""
    return _quantize(i_A, params.i_fullscale_pk_A, params.n_bits, params.lsb_A)
