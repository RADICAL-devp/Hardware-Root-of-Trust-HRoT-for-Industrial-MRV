"""Shared Week 4b cocotb helpers: resets, signal conversion, stream builders.

Conventions (match the RTL contracts exactly):
- power_calc inputs are REGISTERED: set before an edge, read after (+1 ns).
- zc_detect outputs are COMBINATIONAL on the current sample: set inputs,
  wait 1 ns, read strobes BEFORE the next edge (state advances at the edge).
- Signed 16-bit codes driven masked (& 0xFFFF); read back via signed_integer.
"""

import cocotb
import numpy as np
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

CLK_NS = 10  # [ns] testbench clock period
SETTLE_NS = 1  # [ns] post-edge/combinational settle before sampling
ARM_LO = -500  # [counts] arming dip level (< ARM_Q15 = -328)
CROSS_HI = 100  # [counts] crossing sample level (>= 0, previous < 0)
BASE_V = 5000  # [counts] synthetic-stream baseline (positive half)


def mask16(v: int) -> int:
    """Wrap a signed code [counts] to 16-bit drive value."""
    return int(v) & 0xFFFF


def to_signed(val: int, bits: int) -> int:
    """Reinterpret an unsigned readout as signed [counts]."""
    val = int(val)
    return val - (1 << bits) if val >= (1 << (bits - 1)) else val


def read_sig(sig, bits: int, signed: bool = False) -> int:
    """Read a signal robustly (raises on X instead of propagating garbage)."""
    try:
        raw = int(sig.value)
    except ValueError as e:
        raise AssertionError(f"signal {sig._path} is X: {e}")
    return to_signed(raw, bits) if signed else raw


async def reset_dut(dut, cycles: int = 4) -> None:
    """Async reset: rst_n low, clocks running, inputs quiescent."""
    dut.rst_n.value = 0
    for name in ("sample_valid", "win_start_pulse", "win_end_strobe", "win_valid_in"):
        if hasattr(dut, name):
            getattr(dut, name).value = 0
    if hasattr(dut, "rx"):
        dut.rx.value = 1
    for _ in range(cycles):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


def start_clock(dut) -> None:
    """Start the 10 ns testbench clock."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, units="ns").start())


def synth_cross_stream(length: int, det: list[int]) -> list[int]:
    """Voltage codes [counts] with rising crossings at exactly `det`.

    Each detection index d carries the triple (lo, lo, hi) at d-2..d: the
    lo samples arm (< -328 with a negative predecessor at d-1), the hi
    sample triggers (first >= 0 while armed). Spacing between consecutive
    detections must be >= 4 and d >= 2. Matches find_rising_crossings_q15
    exactly (asserted by the zc tests, not assumed).
    """
    v = [BASE_V] * length
    for d in det:
        assert d >= 2 and v[d - 2] == BASE_V and v[d - 1] == BASE_V
        v[d - 2] = ARM_LO
        v[d - 1] = ARM_LO
        v[d] = CROSS_HI
    for a, b in zip(det, det[1:]):
        assert b - a >= 4, "detection spacing must be >= 4 samples"
    return v


def sine_codes(
    n: int,
    f_hz: float = 50.0,
    fs_hz: float = 10_000.0,
    vpk: float = 26000.0,
    ipk: float = 9000.0,
    phi_rad: float = 0.3,
    noise: float = 200.0,
    seed: int = 0,
) -> tuple[list[int], list[int]]:
    """Seeded sine V/I code lists [counts] (power-level vectors need no ZC)."""
    rng = np.random.default_rng(seed)
    t = np.arange(n) / fs_hz
    v = vpk * np.sin(2 * np.pi * f_hz * t) + rng.normal(0, noise, n)
    i = ipk * np.sin(2 * np.pi * f_hz * t - phi_rad) + rng.normal(0, noise / 3, n)

    def q(x):
        return int(np.clip(round(float(x)), -32768, 32767))

    return [q(x) for x in v], [q(x) for x in i]


async def settle() -> None:
    """Combinational settle delay."""
    await Timer(SETTLE_NS, units="ns")
