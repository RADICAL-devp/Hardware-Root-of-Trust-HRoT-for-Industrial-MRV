"""Week 5b cocotb: sqrt_fsm unit (toplevel=sqrt_fsm).

Contract: start sampled at edge E => done rises at edge E+33 (34 inclusive
cycles: 1 load + 32 iterate + 1 finalize), root == isqrt_round_half_up
(rem > root -> +1) on a 33-bit port (2^64-1 rounds to 2^32, which a
32-bit port would wrap to 0).
"""

import cocotb
from cocotb.triggers import RisingEdge

from sensors.windows import isqrt_round_half_up
from tb.w4b_common import read_sig, reset_dut, settle, start_clock

EDGE_LAT = 33  # [cycles] start-edge -> done-edge (34 inclusive)

DIRECTED: list[int] = [
    0,
    1,
    2,
    3,
    4,
    9,
    15,
    16,
    24,
    25,  # perfect square, rem 0
    30,  # rem 5 == root: no round-up
    31,  # rem 6 == root+1: round-up
    36,
    2**32 - 1,
    2**32,
    2**62,
    2**64 - 2,
    2**64 - 1,  # max: root 2^32 (33 bits)
    255**2,
    256**2,
    65535**2,
    65536**2,
    (2**32 - 1) ** 2,  # max perfect square < 2^64
]


async def run_sqrt(dut, x: int) -> tuple[int, int]:
    """Run one square root; return (root, edge latency)."""
    dut.x.value = x
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0
    for k in range(1, 101):
        await RisingEdge(dut.clk)
        await settle()
        if read_sig(dut.done, 1):
            return read_sig(dut.root, 33), k
    raise AssertionError(f"sqrt never done: x={x}")


@cocotb.test()
async def test_sqrt_directed(dut):
    """Directed corners incl. rem==root / root+1 and 2^64-1."""
    start_clock(dut)
    dut.start.value = 0
    await reset_dut(dut)
    await settle()
    for x in DIRECTED:
        r, lat = await run_sqrt(dut, x)
        assert lat == EDGE_LAT, f"x={x}: latency {lat} != {EDGE_LAT}"
        assert r == isqrt_round_half_up(x), f"x={x}: {r}"
    cocotb.log.info(f"sqrt_directed: {len(DIRECTED)} vectors exact, latency {EDGE_LAT}")


@cocotb.test()
async def test_sqrt_random(dut):
    """200 seeded vectors over the full 64-bit input range."""
    import random

    start_clock(dut)
    dut.start.value = 0
    await reset_dut(dut)
    await settle()
    rng = random.Random(8)
    for v in range(200):
        x = rng.randrange(0, 2**64)
        if v % 25 == 0:  # sprinkle perfect squares (rem 0 path)
            k = rng.randrange(0, 2**32)
            x = k * k
        r, lat = await run_sqrt(dut, x)
        assert lat == EDGE_LAT, f"v{v}: latency {lat}"
        assert r == isqrt_round_half_up(x), f"v{v}: x={x} -> {r}"
    cocotb.log.info("sqrt_random: 200 vectors exact")
