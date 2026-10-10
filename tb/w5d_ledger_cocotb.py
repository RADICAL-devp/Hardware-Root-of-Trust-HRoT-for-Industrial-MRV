"""Week 5d3 cocotb: generate the RTL ledger fixtures used by the verifier tests.

Two generators, run through the existing crack harness (tb/test_week5d3_ledger_fixtures.py):

- ``test_top_ledger_clean_fixture`` — SKID_DEPTH 16, 50 Hz sine (M ~= 2000, in
  the [1810, 2230] golden range), 4 consecutive all-valid records. Exercises the
  REAL seam: DUT field outputs + published hash + FPGA HMAC are packed by
  ``ledger.writer.write_ledger``, which re-packs the descriptor, verifies the
  HMAC via ``SecureElement.attest`` (refuses mismatches) and appends the
  Ed25519 signature. Output: ``tests/fixtures/ledger_rtl_clean.jsonl``.

- ``test_top_ledger_overflow_fixture`` — SKID_DEPTH 4 (test-only override,
  production/CI/synth run 16, repo-pinned), identical shape to the 5d2c
  overflow E2E: two bit-7 records with valid bits cleared, zc detected intact.
  Output: ``tests/fixtures/ledger_rtl_overflow.jsonl``.

Reproducibility: the sim is seeded and the writer is deterministic, so a
regeneration must be byte-identical to the committed fixture; both tests
assert that (bootstrap: if the fixture is absent on a fresh clone they write
it once, then byte-compare on every later run).

Telemetry (sidecar + skid peak + sticky stamps) rides the ledger marked
``telemetry_attested: false`` — bound to nothing, keyed by counter.
"""

import hashlib
from pathlib import Path

import cocotb

from edge.secure_element import SecureElement
from ledger.verifier import LedgerVerifier
from ledger.writer import read_ledger, write_ledger
from sim.provision import derive_keys
from tb.w5d_top_cocotb import (
    BASE_COUNTER,
    POST_CLOSE_WAIT,
    REPO,
    check_record,
    check_window,
    golden_windows,
    meas_line,
    meas_open,
    provision,
    quiesce_top,
    read_record,
    read_sig,
    read_window,
    reset_dut,
    send_frames,
    send_to_close,
    sine_frames,
    start_clock,
    wait_edges,
)

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
RESULTS = REPO / "results"

CLEAN_PATH = FIXTURES / "ledger_rtl_clean.jsonl"
OVERFLOW_PATH = FIXTURES / "ledger_rtl_overflow.jsonl"

DEVICE = 1
SEED = 42


def _stage_record(r: dict, counter: int) -> dict:
    """Map a DUT record to a writer record dict (descriptor re-packed by writer)."""
    return {
        "counter": counter,
        "window_start": r["wstart"],
        "zc_samples": [(r["zc"] >> (12 * k)) & 0xFFF for k in range(5)],
        "window_flags": r["flags"],
        "overrun_cnt": r["overrun"],
        "p_avg_q30": r["p_avg"],
        "energy_uwh": r["energy"],
        "window_hash": bytes(r["hash"]),
        "hmac": bytes(r["hmac"]),
    }


def _telemetry(r: dict) -> dict:
    return {
        "sidecar": list(r["sc"]),
        "skid_peak": r["skid_peak"],
        "stamps": list(r["stamps"]),
    }


def _publish(fixture_path: Path, staged, telemetry, expected_rejected: int) -> None:
    """Sign + write to results/, check the verifier contract, then the oracle.

    Oracle rule: if a committed fixture already exists it is the SOURCE OF
    TRUTH — a regeneration must be byte-identical (deterministic sim +
    writer). If the file is absent (fresh clone before the first commit)
    the generator BOOTSTRAPS it by writing the committed copy, which is
    then reviewed in git; every later run is a byte-compare.
    """
    km = derive_keys(SEED)
    gen_path = RESULTS / f"gen_{fixture_path.name}"
    n = write_ledger(
        staged,
        gen_path,
        SecureElement.from_seed(SEED),
        device_id=DEVICE,
        telemetry=telemetry,
    )
    assert n == len(staged), "writer refused part of the chain"
    verdict = LedgerVerifier(km.verify_key).verify(read_ledger(gen_path))
    if expected_rejected == 0:
        assert verdict.counts["valid"] == len(staged), [
            (o.first_verdict, o.failures) for o in verdict.outcomes
        ]
        assert verdict.counts["rejected"] == 0
    else:
        assert verdict.counts["rejected"] == expected_rejected
        assert all(o.first_verdict == "bad-bit7" for o in verdict.outcomes)

    if fixture_path.exists():
        ours = gen_path.read_bytes()
        assert ours == fixture_path.read_bytes(), (
            f"regenerated {fixture_path.name} != committed fixture (sim drift?); "
            "see results/gen_*.jsonl"
        )
    else:  # bootstrap (documented in this module's docstring)
        fixture_path.write_bytes(gen_path.read_bytes())
    sha = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    path = RESULTS / "ledger_fixtures.sha256"
    lines = []
    for line in path.read_text(encoding="ascii").splitlines() if path.exists() else []:
        if not line.endswith(f"  {fixture_path.name}"):
            lines.append(line)
    lines.append(f"{sha}  {fixture_path.name}")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


@cocotb.test()
async def test_top_ledger_clean_fixture(dut):
    """Produce + verify the 4-record clean fixture at wire rate (slow, 50 Hz)."""
    n_records = 4
    start_clock(dut)
    await quiesce_top(dut)
    await reset_dut(dut)
    km = derive_keys(SEED)
    await provision(dut, km.hmac_key)
    frames = sine_frames(47_000, 50.0, BASE_COUNTER, seed=11)
    v_all = [v for _, v, _ in frames]
    i_all = [i for _, _, i in frames]
    wins = golden_windows(v_all)
    assert len(wins) >= 5 * n_records + 2, f"only {len(wins)} golden windows"
    assert all(w[2] for w in wins[: 5 * n_records]), "clean fixture needs valid windows"

    with meas_open("ledger_fixture_clean") as _:
        await send_frames(dut, frames[: wins[0][0]])  # preamble (hashes, windowless)
        cur = wins[0][0]
        staged, telemetry = [], {}
        prev_tag = bytes(32)
        energy_prev = 0
        hs = 0  # record 0 hashes the preamble too
        for k in range(n_records):
            group = wins[5 * k : 5 * k + 5]
            for s, e, ok in group:
                cur = await send_to_close(dut, frames, cur, e)
                check_window(read_window(dut), v_all, i_all, (s, e, ok))
            for _ in range(25):
                await wait_edges(dut, 200)
                if read_sig(dut.u_cnt.count_out, 32) == k + 1:
                    break
            assert read_sig(dut.u_cnt.count_out, 32) == k + 1, "record never published"
            await wait_edges(dut, POST_CLOSE_WAIT)
            r = read_record(dut)
            assert r["counter"] == k, r["counter"]
            assert r["flags"] & 0x80 == 0, hex(r["flags"])
            he = group[-1][1]
            tag = check_record(
                r,
                v_all,
                i_all,
                group,
                energy_prev,
                k,
                DEVICE,
                km.hmac_key,
                prev_tag,
                BASE_COUNTER,
                hash_span=(hs, he),
            )
            assert tag == r["hash"], "DUT hash != golden"
            staged.append(_stage_record(r, k))
            telemetry[k] = _telemetry(r)
            prev_tag, energy_prev = tag, r["energy"]
            hs = he
            meas_line(
                "ledger_fixture_clean",
                f"rec{k} M={[(r['zc'] >> (12 * s)) & 0xFFF for s in range(5)]} "
                f"flags={r['flags']:#04x} wstart={r['wstart']}",
            )

    _publish(CLEAN_PATH, staged, telemetry, expected_rejected=0)
    assert CLEAN_PATH.exists()
    meas_line("ledger_fixture_clean", f"{n_records} records -> {CLEAN_PATH.name}")


@cocotb.test()
async def test_top_ledger_overflow_fixture(dut):
    """Produce + verify the 2-record bit-7 fixture at SKID_DEPTH=4 (slow, 50 Hz)."""
    start_clock(dut)
    await quiesce_top(dut)
    await reset_dut(dut)
    km = derive_keys(SEED)
    await provision(dut, km.hmac_key)
    frames = sine_frames(21_000, 50.0, BASE_COUNTER, seed=11)
    v_all = [v for _, v, _ in frames]
    i_all = [i for _, _, i in frames]
    wins = golden_windows(v_all)
    assert len(wins) >= 10, f"only {len(wins)} golden windows"
    assert all(w[2] for w in wins[:10])

    with meas_open("ledger_fixture_overflow") as _:
        await send_frames(dut, frames[: wins[0][0]])
        cur = wins[0][0]
        for s, e, ok in wins[:4]:
            cur = await send_to_close(dut, frames, cur, e)
            check_window(read_window(dut), v_all, i_all, (s, e, ok))
        s, e, ok = wins[4]
        await send_frames(dut, frames[cur : e + 1])
        cur = e + 1
        seen = 0
        for _ in range(8):
            await wait_edges(dut, 50)
            seen |= read_sig(dut.skid_overflow_out, 1)
        assert seen == 1, "skid overflow never raised at depth 4"
        await wait_edges(dut, 200)
        check_window(read_window(dut), v_all, i_all, (s, e, ok))
        for _ in range(25):
            await wait_edges(dut, 200)
            if read_sig(dut.u_cnt.count_out, 32) == 1:
                break
        assert read_sig(dut.u_cnt.count_out, 32) == 1, "record 0 never published"
        await wait_edges(dut, POST_CLOSE_WAIT)
        r0 = read_record(dut)
        assert r0["counter"] == 0 and r0["flags"] & 0x80 == 0x80
        for k, (ws, we, ok) in enumerate(wins[:5]):
            assert ((r0["zc"] >> (12 * k)) & 0xFFF) == we - ws, "detection not intact"
        staged = [_stage_record(r0, 0), None]
        telemetry = {0: _telemetry(r0)}
        for s, e, ok in wins[5:10]:
            cur = await send_to_close(dut, frames, cur, e)
            check_window(read_window(dut), v_all, i_all, (s, e, ok))
        await wait_edges(dut, POST_CLOSE_WAIT)
        r1 = read_record(dut)
        assert r1["counter"] == 1 and r1["flags"] & 0x80 == 0x80
        assert r1["energy"] == r0["energy"]
        staged[1] = _stage_record(r1, 1)
        telemetry[1] = _telemetry(r1)

    _publish(OVERFLOW_PATH, staged, telemetry, expected_rejected=2)
    meas_line("ledger_fixture_overflow", f"rec0 flags={r0['flags']:#04x} peak={r0['skid_peak']}")
    meas_line("ledger_fixture_overflow", f"rec1 flags={r1['flags']:#04x} peak={r1['skid_peak']}")
    assert OVERFLOW_PATH.exists()
