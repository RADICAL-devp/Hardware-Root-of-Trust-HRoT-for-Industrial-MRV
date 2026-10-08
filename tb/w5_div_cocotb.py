"""Week 5b cocotb: div_fsm unit (toplevel=div_fsm).

Contract: start sampled at edge E => done rises at edge E+65 (66 inclusive
cycles: 1 load + 64 iterate + 1 finalize), quot == div_round_half_away
(signed) for den > 0. Sign-magnitude core: magnitude path proven 0..2^64-1;
signed contract |N| < 2^63 (controller guarantees < 2^56 on every path).
"""

import cocotb
from cocotb.triggers import RisingEdge

from sensors.windows import div_round_half_away
from tb.w4b_common import read_sig, reset_dut, settle, start_clock

EDGE_LAT = 65  # [cycles] start-edge -> done-edge (66 inclusive)

DIRECTED: list[tuple[bool, int, int]] = [
    (False, 0, 9),  # zero numerator
    (False, 1, 1),  # identity
    (False, 2**64 - 1, 1),  # max magnitude
    (False, 2**64 - 1, 2**64 - 1),  # max/max
    (False, 7, 2),  # exact half -> away (4)
    (True, 7, 2),  # exact half -> away (-4)
    (False, 5, 2),  # half -> 3
    (True, 5, 2),  # half -> -3
    (False, 2**63 - 1, 1),  # max signed
    (True, 2**63 - 1, 3),  # max-magnitude negative
    (False, 10**18, 7),
    (True, 10**18, 7),
    (False, 1, 2**64 - 1),  # tiny / huge
    (False, 2**63 - 1, 2),
    (True, 1, 9),
]


async def run_div(dut, neg: bool, mag: int, den: int) -> tuple[int, int]:
    """Run one division; return (signed quotient, edge latency)."""
    dut.neg.value = int(neg)
    dut.mag.value = mag
    dut.den.value = den
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0
    # Edges after the start edge until done (timeout 200).
    for k in range(1, 201):
        await RisingEdge(dut.clk)
        await settle()
        if read_sig(dut.done, 1):
            # Unsigned read for non-negative results (magnitudes reach
            # 2^64-1, which a signed read would show as -1); signed read
            # for negative ones (contract |N| < 2^63, always fits).
            if neg:
                q = read_sig(dut.quot, 64, signed=True)
            else:
                q = read_sig(dut.quot, 64)
            return q, k
    raise AssertionError(f"div never done: neg={neg} mag={mag} den={den}")


@cocotb.test()
async def test_div_directed(dut):
    """Directed edge cases incl. max magnitude and exact halves both signs."""
    start_clock(dut)
    dut.start.value = 0
    await reset_dut(dut)
    await settle()
    for neg, mag, den in DIRECTED:
        num = -mag if neg else mag
        q, lat = await run_div(dut, neg, mag, den)
        assert lat == EDGE_LAT, f"({neg},{mag},{den}): latency {lat} != {EDGE_LAT}"
        assert q == div_round_half_away(num, den), f"({neg},{mag},{den}): {q}"
    cocotb.log.info(f"div_directed: {len(DIRECTED)} vectors exact, latency {EDGE_LAT}")


@cocotb.test()
async def test_div_random(dut):
    """200 seeded vectors across magnitudes, dens incl. 1 and 2^64-1."""
    import random

    start_clock(dut)
    dut.start.value = 0
    await reset_dut(dut)
    await settle()
    rng = random.Random(7)
    for v in range(200):
        mag = rng.randrange(0, 2**63)  # signed contract |N| < 2^63
        den = rng.randrange(1, 2**64)
        neg = bool(rng.getrandbits(1))
        if v % 20 == 0:  # pin the extremes periodically
            mag, den = (2**63 - 1, 1) if v % 40 == 0 else (0, 2**64 - 1)
        num = -mag if neg else mag
        q, lat = await run_div(dut, neg, mag, den)
        assert lat == EDGE_LAT, f"v{v}: latency {lat}"
        assert q == div_round_half_away(num, den), f"v{v}: ({neg},{mag},{den}) -> {q}"
    cocotb.log.info("div_random: 200 vectors exact")
