"""Week 2: ADC range and quantization step (12-bit, bipolar, per DECISIONS.md D-04)."""

import numpy as np

from sensors.adc import ADCParams, quantize_current, quantize_voltage

N_CODES = 4096


def test_adc_code_count_and_lsb():
    adc = ADCParams()  # V FS +-500 Vpk, I FS +-100 Apk
    assert adc.n_bits == 12
    assert adc.n_codes == N_CODES
    assert adc.lsb_V == adc.v_fullscale_pk_V / 2048.0
    assert adc.lsb_A == adc.i_fullscale_pk_A / 2048.0
    assert abs(adc.lsb_V - 1000.0 / 4096.0) < 1e-12
    assert abs(adc.lsb_A - 200.0 / 4096.0) < 1e-12


def test_adc_saturates_out_of_range():
    adc = ADCParams()
    over_v = np.array([10_000.0, -10_000.0])
    q = quantize_voltage(over_v, adc)
    assert np.all(q <= adc.v_fullscale_pk_V)
    assert np.all(q >= -adc.v_fullscale_pk_V)
    over_i = np.array([10_000.0, -10_000.0])
    qi = quantize_current(over_i, adc)
    assert np.all(qi <= adc.i_fullscale_pk_A)
    assert np.all(qi >= -adc.i_fullscale_pk_A)


def test_adc_quantization_step_is_one_lsb():
    adc = ADCParams()
    # A slow ramp must only ever change by multiples of 1 LSB (monotonic steps).
    ramp = np.linspace(-400.0, 400.0, 10001)
    q = quantize_voltage(ramp, adc)
    steps = np.unique(np.round(np.diff(q) / adc.lsb_V))
    assert set(steps.tolist()) <= {0.0, 1.0}, steps[:10]
    assert q.max() - q.min() > 0  # ramp actually spans codes


def test_adc_zero_maps_near_zero():
    adc = ADCParams()
    assert abs(quantize_voltage(np.array([0.0]), adc)[0]) <= adc.lsb_V
    assert abs(quantize_current(np.array([0.0]), adc)[0]) <= adc.lsb_A
