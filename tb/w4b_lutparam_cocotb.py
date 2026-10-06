"""Week 4b2: power_calc with LUT_MEAN_ENABLE=0 (exact-only build).

p_avg_lut must tie to 0 while every other output stays bit-exact and the
latency stays 3. Run with parameters={"LUT_MEAN_ENABLE": 0}.
"""

import cocotb

from tb.golden import window_power
from tb.w4b_common import reset_dut, start_clock
from tb.w4b_power_cocotb import drive_window


@cocotb.test()
async def test_lut_disabled(dut):
    start_clock(dut)
    await reset_dut(dut)
    m = 2000
    v = [20000] * m
    i = [15000] * m
    out, lat = await drive_window(dut, v, i, m, True)
    assert out["p_avg_lut"] == 0, "LUT register not gated by LUT_MEAN_ENABLE=0"
    g = window_power(v, i)
    assert out["p_avg_exact"] == g.p_avg_q30
    assert out["vrms"] == g.vrms_q15 and out["irms"] == g.irms_q15
    assert out["pf"] == g.pf_q15 and out["energy"] == g.energy_uwh_inc
    assert out["m"] == m and out["valid"] == 1 and out["p_sum"] == g.p_sum_q30
    assert lat == 3
    cocotb.log.info("LUT_MEAN_ENABLE=0: lut tied 0, exact path intact")
