"""Sensors package: AFE + ADC. Single factored entry point, no bypass.

The ONLY route from true plant waveforms to measured signals is
:func:`measurement_chain` (AFE first, then ADC). There is no flag, branch,
or helper that returns measured-looking data without passing through the
:class:`~sensors.afe.AFE`. The plant packages never import this package
(enforced by ``test_no_perfect_world_path``).

Telemetry constants mirror DECISIONS.md D-04: 10 kHz sampling is exactly
200 samples per nominal 50 Hz cycle; an attestation window is 50 cycles
(10 000 samples, 1.0 s). Whole-cycle power/RMS helpers use the FIXED
nominal window length: they are never given the true grid frequency, so
grid drift leaks into the measurement error honestly (Week 2 reports it).
"""

import numpy as np

from sensors.adc import ADCParams, quantize_current, quantize_voltage
from sensors.afe import AFE

SAMPLE_RATE_HZ = 10_000.0  # [samples/s] telemetry sample rate (D-04)
NOMINAL_FREQ_HZ = 50.0  # [Hz] nominal grid frequency; windows assume exactly this
SAMPLES_PER_CYCLE = 200  # [samples] fixed window behind P = mean(v*i)
WINDOW_CYCLES = 50  # [cycles] attestation window length (D-04)
WINDOW_SAMPLES = 10_000  # [samples] 1.0 s attestation window (D-04)


def measurement_chain(
    v_clean_V: np.ndarray,
    i_clean_A: np.ndarray,
    t_s: np.ndarray,
    afe: AFE,
    adc: ADCParams,
) -> tuple[np.ndarray, np.ndarray]:
    """Convert true waveforms to measured waveforms (AFE → ADC).

    Args:
        v_clean_V: true terminal voltage [V].
        i_clean_A: true phase current [A].
        t_s: nominal sample instants [s].
        afe: seeded :class:`~sensors.afe.AFE` instance.
        adc: :class:`~sensors.adc.ADCParams`.

    Returns:
        Tuple ``(v_meas_V [V], i_meas_A [A])``. Always passes through both
        the AFE and the ADC; there is no bypass route.
    """
    v_noisy, i_noisy = afe.apply(v_clean_V, i_clean_A, t_s)
    return quantize_voltage(v_noisy, adc), quantize_current(i_noisy, adc)


def _reshape_cycles(x: np.ndarray, samples_per_cycle: int) -> np.ndarray:
    n = (x.shape[0] // samples_per_cycle) * samples_per_cycle
    return np.asarray(x[:n], dtype=float).reshape(-1, samples_per_cycle)


def cycle_mean_power(
    v_V: np.ndarray, i_A: np.ndarray, samples_per_cycle: int = SAMPLES_PER_CYCLE
) -> np.ndarray:
    """Real power ``P = mean(v*i)`` [W] per fixed nominal cycle window."""
    vv = _reshape_cycles(v_V, samples_per_cycle)
    ii = _reshape_cycles(i_A, samples_per_cycle)
    return np.mean(vv * ii, axis=1)


def cycle_rms(x: np.ndarray, samples_per_cycle: int = SAMPLES_PER_CYCLE) -> np.ndarray:
    """RMS per fixed nominal cycle window (same units as ``x``)."""
    xx = _reshape_cycles(x, samples_per_cycle)
    return np.sqrt(np.mean(xx * xx, axis=1))


def acceptance_bound_pct(
    afe_params,
    adc_params: ADCParams,
    v_rms_nom_V: float = 230.0,
    i_rms_typ_A: float = 15.0,
) -> tuple[float, str]:
    """Acceptance bound [%] on mean per-cycle P error, derived from the spec.

    Worst-case (linear-sum, NOT tuned) propagation of the fixed sensor spec
    at nominal voltage and typical mid-range current:
    gain terms add directly to P; offset terms enter as Voff/Vrms + Ioff/Irms;
    noise/quantization floors use 3σ of the per-cycle mean scatter
    (variance of ``mean(v*i)`` over ``SAMPLES_PER_CYCLE`` samples).

    Args:
        afe_params: :class:`~sensors.afe.AFEParams` (the fixed spec).
        adc_params: :class:`~sensors.adc.ADCParams`.
        v_rms_nom_V: nominal voltage [V rms] the bound is evaluated at.
        i_rms_typ_A: typical current [A rms] the bound is evaluated at.

    Returns:
        Tuple ``(bound_pct [%], derivation [str])``.
    """
    p = afe_params
    n = float(SAMPLES_PER_CYCLE)
    gain_pct = 100.0 * (abs(p.gain_err_v_frac) + abs(p.gain_err_i_frac))
    offset_pct = 100.0 * (abs(p.offset_v_V) / v_rms_nom_V + abs(p.offset_i_A) / i_rms_typ_A)
    noise_rel = float(
        np.sqrt((p.noise_std_v_V / v_rms_nom_V) ** 2 + (p.noise_std_i_A / i_rms_typ_A) ** 2)
    )
    noise_pct = 100.0 * 3.0 * noise_rel / np.sqrt(n)
    qv = adc_params.lsb_V / np.sqrt(12.0) / v_rms_nom_V
    qi = adc_params.lsb_A / np.sqrt(12.0) / i_rms_typ_A
    quant_pct = 100.0 * 3.0 * float(np.sqrt(qv * qv + qi * qi)) / np.sqrt(n)
    bound = gain_pct + offset_pct + noise_pct + quant_pct
    derivation = (
        f"bound = |gV|+|gI| ({gain_pct:.3f}%) + |Voff|/Vrms+|Ioff|/Irms "
        f"({offset_pct:.3f}%, Vrms={v_rms_nom_V}V Irms_typ={i_rms_typ_A}A) + "
        f"3-sigma per-cycle noise floor ({noise_pct:.3f}%, N={int(n)}) + "
        f"3-sigma quantization floor ({quant_pct:.3f}%). Total {bound:.3f}%."
    )
    return bound, derivation
