"""Week 5b: tb/golden.py arithmetic helpers ARE sensors/windows.py.

The FSM unit suites compare against `tb.golden` helpers; this test proves
those helpers are identically `sensors.windows` (import-identity), so the
units are checked against the single canonical implementation. Value pins
cover the exact-half rule (both signs), the 2^64-1 sqrt corner (rounds to
2^32 — the reason the RTL root port is 33 bits), and the energy form.
"""

import sensors.windows as sw
import tb.golden as gold


def test_helpers_are_sensors_windows():
    for name in (
        "div_round_half_away",
        "mean_q30_half_away",
        "isqrt_round_half_up",
        "energy_uwh_increment",
    ):
        assert getattr(gold, name) is getattr(sw, name), name


def test_half_away_both_signs():
    assert sw.div_round_half_away(7, 2) == 4
    assert sw.div_round_half_away(-7, 2) == -4
    assert sw.div_round_half_away(5, 2) == 3  # exact half, away from zero
    assert sw.div_round_half_away(-5, 2) == -3
    assert sw.div_round_half_away(0, 9) == 0
    assert sw.div_round_half_away(2**63 - 1, 1) == 2**63 - 1


def test_sqrt_corners():
    assert sw.isqrt_round_half_up(0) == 0
    assert sw.isqrt_round_half_up(1) == 1
    assert sw.isqrt_round_half_up(25) == 5  # perfect square, rem 0
    assert sw.isqrt_round_half_up(30) == 5  # rem 5 == root: no round-up
    assert sw.isqrt_round_half_up(31) == 6  # rem 6 == root+1: round-up
    assert sw.isqrt_round_half_up(2**64 - 1) == 2**32  # max: 33-bit result


def test_energy_form_spot():
    assert sw.energy_uwh_increment(0, 2000) == 0
    assert sw.energy_uwh_increment(2**40, 2000) == sw.div_round_half_away(2**40 * 12500, 9 * 2**30)
