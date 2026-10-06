"""Week 2: load-profile shapes (steady, cyclic, bursty)."""

import numpy as np

from plant.load_profiles import KINDS, make_profile

FS_HZ = 10_000.0
DURATION_S = 8.0  # long enough to see cyclic periods and bursts


def test_all_three_kinds_exist():
    assert set(KINDS) == {"steady", "cyclic", "bursty"}
    for kind in KINDS:
        p = make_profile(kind, duration_s=1.0, fs_hz=FS_HZ, seed=42)
        assert p.shape == (int(1.0 * FS_HZ),)
        assert np.all(np.isfinite(p))
        assert np.all(p >= 0.0)  # passive load: no negative torque


def test_steady_is_constant():
    p = make_profile("steady", duration_s=DURATION_S, fs_hz=FS_HZ, seed=42)
    assert np.ptp(p) == 0.0


def test_cyclic_is_periodic_two_level():
    p = make_profile("cyclic", duration_s=DURATION_S, fs_hz=FS_HZ, seed=42)
    levels = np.unique(np.round(p, 9))
    assert len(levels) == 2  # low and high torque only
    period_s = 4.0
    n_period = int(period_s * FS_HZ)
    np.testing.assert_array_equal(p[:n_period], p[n_period : 2 * n_period])


def test_bursty_has_bursts_above_base():
    p = make_profile("bursty", duration_s=DURATION_S, fs_hz=FS_HZ, seed=42)
    base = p.min()
    assert (p > base).mean() < 0.5  # bursts are intermittent, not the norm
    assert p.max() > base + 5.0  # bursts are material torque events
    assert p.max() <= 20.0  # within motor pull-out / current-limit range
