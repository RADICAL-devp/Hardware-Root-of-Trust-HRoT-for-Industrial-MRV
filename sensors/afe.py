"""Analog front-end model: gain/offset error, Gaussian noise, sampling jitter.

Signal path per channel (units in brackets):
    1. Sampling jitter: the ADC actually samples at
       ``t_n = n * Ts [s] + N(0, jitter_std_s [s])``. Implemented by linearly
       interpolating the clean waveform at the jittered instants
       (``numpy.interp``; exact for our purposes since 1 µs << 20 ms period).
    2. Gain error: ``x *= (1 + gain_err_frac)`` [dimensionless fraction].
    3. Offset error: ``x += offset`` ([V] or [A]).
    4. Additive white Gaussian noise: ``x += N(0, noise_std)`` ([V] or [A]).

Default error magnitudes are plausible datasheet values, fixed BEFORE any
bound was computed (AGENTS.md: do not tune noise to make a bound pass):
gain within ±0.5 %, offsets 0.8 V / 0.05 A, noise ≈ 1.4 LSB rms per
channel, jitter 1 µs rms. All draws come from a seeded
``numpy.random.default_rng`` stream, so a fixed seed reproduces the exact
noisy trace. Sensors always have noise from day one (AGENTS.md).
"""

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class AFEParams:
    """Analog front-end error parameters (units documented per field)."""

    gain_err_v_frac: float = 0.004  # [dimensionless] voltage gain error (+0.4 %)
    gain_err_i_frac: float = -0.003  # [dimensionless] current gain error (-0.3 %)
    offset_v_V: float = 0.8  # [V] voltage offset error
    offset_i_A: float = 0.05  # [A] current offset error
    noise_std_v_V: float = 0.35  # [V rms] voltage Gaussian noise (~1.4 LSB rms)
    noise_std_i_A: float = 0.07  # [A rms] current Gaussian noise (~1.4 LSB rms)
    jitter_std_s: float = 1e-6  # [s rms] sampling-jitter standard deviation
    seed: int = 42  # [dimensionless] deterministic RNG seed


class AFE:
    """Seeded analog front-end; the ONLY route from clean to noisy signals."""

    def __init__(self, params: AFEParams | None = None) -> None:
        self.params = params or AFEParams()
        self._rng = np.random.default_rng(self.params.seed)

    def apply(
        self, v_clean_V: np.ndarray, i_clean_A: np.ndarray, t_s: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """Apply jitter + gain + offset + noise; return noisy (V [V], I [A]).

        Args:
            v_clean_V: true terminal voltage [V], 1-D array.
            i_clean_A: true phase current [A], 1-D array.
            t_s: nominal sample instants [s], 1-D array.

        Returns:
            Tuple ``(v_noisy_V, i_noisy_A)`` still in physical units
            (quantization happens downstream in :mod:`sensors.adc`).
        """
        p = self.params
        v = np.asarray(v_clean_V, dtype=float)
        i = np.asarray(i_clean_A, dtype=float)
        t = np.asarray(t_s, dtype=float)
        # Jittered sampling instants, clamped inside the clean record.
        t_j = t + self._rng.normal(0.0, p.jitter_std_s, size=t.shape)
        t_j = np.clip(t_j, t[0], t[-1])
        v_j = np.interp(t_j, t, v)  # [V] resampled at jittered instants
        i_j = np.interp(t_j, t, i)  # [A] resampled at jittered instants
        v_n = v_j * (1.0 + p.gain_err_v_frac) + p.offset_v_V
        i_n = i_j * (1.0 + p.gain_err_i_frac) + p.offset_i_A
        v_n = v_n + self._rng.normal(0.0, p.noise_std_v_V, size=v.shape)
        i_n = i_n + self._rng.normal(0.0, p.noise_std_i_A, size=i.shape)
        return v_n, i_n
