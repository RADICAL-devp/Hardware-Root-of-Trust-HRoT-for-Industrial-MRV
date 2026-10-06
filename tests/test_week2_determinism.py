"""Week 2: determinism under a fixed seed.

Fails until plant + sensors are implemented with seeded, deterministic RNG.
"""

import numpy as np

from plant.load_profiles import make_profile
from plant.motor import MotorParams, simulate
from sensors import measurement_chain
from sensors.adc import ADCParams
from sensors.afe import AFE, AFEParams

FS_HZ = 10_000.0
DURATION_S = 1.0


def test_profiles_deterministic_same_seed():
    a = make_profile("bursty", duration_s=DURATION_S, fs_hz=FS_HZ, seed=42)
    b = make_profile("bursty", duration_s=DURATION_S, fs_hz=FS_HZ, seed=42)
    np.testing.assert_array_equal(a, b)


def test_profiles_differ_across_seeds():
    a = make_profile("bursty", duration_s=DURATION_S, fs_hz=FS_HZ, seed=42)
    b = make_profile("bursty", duration_s=DURATION_S, fs_hz=FS_HZ, seed=43)
    assert not np.array_equal(a, b)


def test_motor_deterministic_given_load():
    load = make_profile("cyclic", duration_s=DURATION_S, fs_hz=FS_HZ, seed=42)
    r1 = simulate(load, fs_hz=FS_HZ, params=MotorParams())
    r2 = simulate(load, fs_hz=FS_HZ, params=MotorParams())
    np.testing.assert_array_equal(r1.v_true_V, r2.v_true_V)
    np.testing.assert_array_equal(r1.i_true_A, r2.i_true_A)


def test_afe_deterministic_same_seed():
    load = make_profile("steady", duration_s=DURATION_S, fs_hz=FS_HZ, seed=42)
    res = simulate(load, fs_hz=FS_HZ, params=MotorParams())
    t = np.arange(res.v_true_V.size) / FS_HZ
    v1, i1 = measurement_chain(res.v_true_V, res.i_true_A, t, AFE(AFEParams(seed=42)), ADCParams())
    v2, i2 = measurement_chain(res.v_true_V, res.i_true_A, t, AFE(AFEParams(seed=42)), ADCParams())
    np.testing.assert_array_equal(v1, v2)
    np.testing.assert_array_equal(i1, i2)


def test_afe_differs_across_seeds():
    load = make_profile("steady", duration_s=DURATION_S, fs_hz=FS_HZ, seed=42)
    res = simulate(load, fs_hz=FS_HZ, params=MotorParams())
    t = np.arange(res.v_true_V.size) / FS_HZ
    v1, _ = measurement_chain(res.v_true_V, res.i_true_A, t, AFE(AFEParams(seed=42)), ADCParams())
    v2, _ = measurement_chain(res.v_true_V, res.i_true_A, t, AFE(AFEParams(seed=43)), ADCParams())
    assert not np.array_equal(v1, v2)
