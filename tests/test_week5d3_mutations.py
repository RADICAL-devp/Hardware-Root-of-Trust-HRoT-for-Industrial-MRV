"""Week 5d3 mutation suite: 7 source-level mutants, each caught by a named test.

Every mutant is a ONE-LINE change to a COPY of ledger/verifier.py (never
the repo file), written to results/mutation/ and imported as a separate
module under a unique name. Each scenario first runs against the REAL
verifier (must pass) and then against the mutant (must fail): the failing
assertion names the FIRST CATCHER (Amendment 9 — the catcher is the test
that must fail when the mutant is live). results/mutation/ keeps per-mutant
diffs for the gate report; `grep -rn "MUTANT" ledger` stays clean because
the repo file is never touched.
"""

import importlib
import importlib.util
import os
import re
from pathlib import Path

import pytest

from ledger import verifier as real_verifier
from ledger.writer import read_ledger
from sim.provision import derive_keys
from tests.week5d3_support import re_mint, signed_chain

REPO = Path(__file__).resolve().parents[1]
MUT_DIR = REPO / "results" / "mutation"
CLEAN = "tests/fixtures/ledger_rtl_clean.jsonl"
OVERFLOW = "tests/fixtures/ledger_rtl_overflow.jsonl"


def _todo(records, mod, anchor=None, check_zc_range=True):
    km = derive_keys(42)
    return mod.LedgerVerifier(km.verify_key, anchor=anchor, check_zc_range=check_zc_range).verify(
        records
    )


# ---------------------------------------------------------------- scenarios


def _s1_swapped_hmac(mod):
    """Signature check must NOT be removable: wrong key rejects EVERY row."""
    rows = read_ledger(CLEAN)
    wrong = derive_keys(43).verify_key  # different SE, wrong public key
    ver = mod.LedgerVerifier(wrong).verify(rows)
    assert all(o.first_verdict == "bad-signature" for o in ver.outcomes)


def _s2_gap_boundary(mod):
    """Bit-6 drop gap exactly == count*11 must be verified (>= semantics)."""
    rows = signed_chain(3, overruns=[2, 0, 0])
    assert all(o.verified for o in _todo(rows, mod).outcomes)


def _s3_overrun_iff(mod):
    """Count>0 with bit 6 clear must reject as bad-overrun (flag iff count)."""
    rows = signed_chain(2)
    rows[0] = re_mint(rows[0], overrun_cnt=1)
    assert _todo(rows, mod).outcomes[0].first_verdict == "bad-overrun"


def _s4_bit7(mod):
    """RTL overflow rows must reject (both) as bad-bit7 — nothing else fires."""
    rows = read_ledger(OVERFLOW)
    assert all(o.first_verdict == "bad-bit7" for o in _todo(rows, mod).outcomes)


def _s5_anchor(mod):
    """Anchor ahead of the tail must flag tail-truncation and refuse issuance."""
    rows = signed_chain(2)
    from ledger.verifier import Anchor

    ver = _todo(rows, mod, anchor=Anchor(expected_last_counter=3))
    assert ver.tail_truncated and not ver.accepted_for_issuance


def _s6_genesis(mod):
    """A head-truncated ledger (first counter != 0) must reject as bad-genesis."""
    rows = signed_chain(3)[1:]
    assert _todo(rows, mod).outcomes[0].first_verdict == "bad-genesis"


def _s7_classify_bit7(mod):
    """classify() must report bit-7 records REJECTED (Q1), never valid."""
    assert mod.classify({"window_flags": 0x1F | (1 << 7)}) == "rejected"


# ---------------------------------------------------------------- mutant loading


MUTANTS = [
    (
        "M-5d3-1",
        "removes the signature check (wrong key must still reject)",
        ("if not self._sig_ok(rec):", "if False and not self._sig_ok(rec):"),
        _s1_swapped_hmac,
        "catcher: test_wrong_pubkey_detects_every_signature",
    ),
    (
        "M-5d3-2",
        "gap rule weakens >= to >",
        ("return [] if gap >= low else", "return [] if gap > low else"),
        _s2_gap_boundary,
        "catcher: test_gap_bit6_drop_below_min_span_fails_but_boundary_passes",
    ),
    (
        "M-5d3-3",
        "removes the bit6-iff-count check",
        (
            "if (_overrun_flag(rec)) != (1 if overrun > 0 else 0):",
            "if False and (_overrun_flag(rec)) != (1 if overrun > 0 else 0):",
        ),
        _s3_overrun_iff,
        "catcher: test_tamper_verdict[bit6 set, count 0]",
    ),
    (
        "M-5d3-4",
        "removes the bit-7 hard-reject rule",
        (
            "if (flags >> HASH_OVERFLOW_BIT) & 1:\n",
            "if (flags >> HASH_OVERFLOW_BIT) & 1 and False:\n",
        ),
        _s4_bit7,
        "catcher: test_overflow_fixture_rows_reject_as_bit7",
    ),
    (
        "M-5d3-5",
        "ignores the out-of-band anchor",
        ("if self.anchor is not None:", "if self.anchor is not None and False:"),
        _s5_anchor,
        "catcher: test_tail_truncation_caught_with_anchor",
    ),
    (
        "M-5d3-6",
        "removes the genesis check on the first record",
        (
            'elif counter != 0 or rec["prev_hash_hex"] != GENESIS_HASH32.hex():',
            'elif False and (counter != 0 or rec["prev_hash_hex"] != GENESIS_HASH32.hex()):',
        ),
        _s6_genesis,
        "catcher: test_tamper_verdict[head truncation]",
    ),
    (
        "M-5d3-7",
        "classify() treats bit-7 as a valid record",
        ('(int(record.get("window_flags", 0)) >> HASH_OVERFLOW_BIT) & 1:', "(0):"),
        _s7_classify_bit7,
        "catcher: test_classify_classes",
    ),
]


def _load_mutant(mutant_id: str, substitution) -> "module":  # noqa: F821
    os.makedirs(MUT_DIR, exist_ok=True)
    src = Path(real_verifier.__file__).read_text()
    old, new = substitution
    assert src.count(old) == 1, f"mutant anchor for {mutant_id} not unique in source"
    assert old in src
    mutated = src.replace(old, new)
    path = MUT_DIR / f"{mutant_id}.py"
    path.write_text(mutated)
    (MUT_DIR / f"{mutant_id}.diff.txt").write_text(f"- {old}\n+ {new}\n", encoding="ascii")
    spec = importlib.util.spec_from_file_location(f"verifier_{mutant_id}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "mutant_id,what,substitution,scenario,catcher",
    [m for m in MUTANTS],
    ids=[m[0] for m in MUTANTS],
)
def test_mutant_is_caught_by_named_test(mutant_id, what, substitution, scenario, catcher):
    scenario(real_verifier)  # good code passes the scenario
    with pytest.raises(AssertionError):
        scenario(_load_mutant(mutant_id, substitution))
    print(f"\n{mutant_id} ({what}) -> {catcher}")


def test_repo_verifier_has_no_mutant_markers():
    src = Path(real_verifier.__file__).read_text()
    assert "MUTANT" not in src
    assert "mutant" not in src.lower()


def test_mutation_report_names_every_catcher():
    # Provenance: each named catcher is a real test node in the repo —
    # either a @tamper("...") event id (rendered as its parametrized node)
    # or a plain test function name.
    import tests.test_week5d3_ledger as ledger_mod
    import tests.test_week5d3_tamper as tamper_mod

    ids = set()
    for path in (tamper_mod.__file__, ledger_mod.__file__):
        src = open(path).read()
        for fn in re.findall(r"^def (test_\w+)\(name", src, re.M):
            for ev in re.findall(r'@tamper\("([^"]+)"', src):
                ids.add(f"{fn}[{ev}]")
        ids |= set(re.findall(r"^def (test_[A-Za-z0-9_]+)\(", src, re.M))

    for mutant_id, _, _, _, catcher in MUTANTS:
        label = catcher.split("catcher: ", 1)[-1]
        assert label in ids, f"{mutant_id}: catcher {label!r} is not a test node"
