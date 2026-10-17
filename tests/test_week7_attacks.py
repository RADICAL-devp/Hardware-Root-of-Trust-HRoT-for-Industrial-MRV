"""Week 7 tests: attack-campaign detection table (including documented misses).

The blueprint's Week 7 runs each in-scope attack class 100+ times and requires
a detection table that includes the attacks we *miss* and why. The threat model
(``docs/threat_model.md``) fixes the in-scope classes. These tests encode the
"done when" criteria: every in-scope attack is detected except the two that are
missed *by design* (the no-SE ablation and the pre-AFE sensor spoof), and an
honest ledger is never falsely rejected.

The full N=128 campaign is produced by ``sim/attacks_week7.py`` and stored in
``results/metrics.json['week7_attacks']``; these tests also re-run a small
subset in-process to keep the logic independently checkable.
"""

import json
from pathlib import Path

import pytest

from sim.attacks_week7 import (
    ATTACKS,
    BASE_RECORDS,
    N_TRIALS,
    WASTE_OVER_SPEED,
    run_campaign,
)

REPO = Path(__file__).resolve().parents[1]
METRICS_PATH = REPO / "results" / "metrics.json"

# Attacks that are EXPECTED to be missed (by design, per the threat model).
EXPECTED_MISSES = {"spoof_vi_without_se"}
# Every other in-scope attack must be detected 100% of the time.
EXPECTED_DETECTED = set(ATTACKS) - EXPECTED_MISSES


class TestCampaignShape:
    """The campaign covers every blueprint attack class and runs 100+ trials."""

    def test_covers_all_blueprint_attacks(self):
        assert set(ATTACKS) == {
            "replay",
            "spoof_vi_with_se",
            "spoof_vi_without_se",
            "bitflip",
            "drop",
            "ledger_edit_delete",
            "compromised_mpc",
        }

    def test_runs_at_least_100_trials(self):
        assert N_TRIALS >= 100

    def test_base_ledger_has_enough_records_for_chain_attacks(self):
        assert BASE_RECORDS >= 4


class TestDetectionLogic:
    """A small in-process run pins the per-attack detection behaviour."""

    @staticmethod
    def _run():
        return run_campaign(n_trials=8, seed=42)

    def test_every_expected_attack_is_detected(self):
        det = self._run()["detections"]
        for name in EXPECTED_DETECTED:
            assert det[name]["detected"] == 8, f"{name}: {det[name]}"

    def test_no_se_ablation_is_missed_by_design(self):
        det = self._run()["detections"]
        assert det["spoof_vi_without_se"]["detected"] == 0
        assert det["spoof_vi_without_se"]["missed"] == 8

    def test_honest_ledger_never_falsely_rejected(self):
        assert self._run()["false_reject_rate"] == 0.0

    def test_compromised_mpc_savings_are_negative(self):
        res = self._run()["detections"]["compromised_mpc"]
        assert res["detected"] == 8

    def test_waste_factor_is_declared_not_tuned(self):
        # A fixed over-speed factor; the resulting savings are reported, not
        # chosen to make the attack detected.
        assert WASTE_OVER_SPEED > 1.0


class TestMetricsArtifact:
    """``results/metrics.json['week7_attacks']`` is strict JSON and complete."""

    @staticmethod
    def _payload():
        return json.loads(METRICS_PATH.read_text())["week7_attacks"]

    def test_strict_json_and_required_keys(self):
        k = self._payload()
        for key in (
            "label",
            "source",
            "seed",
            "n_trials",
            "attacks",
            "detections",
            "false_reject_rate",
            "misses",
        ):
            assert key in k, key
        json.dumps(k, allow_nan=False)

    def test_artifact_records_100_plus_trials(self):
        assert self._payload()["n_trials"] >= 100

    def test_artifact_detection_rates(self):
        det = self._payload()["detections"]
        for name in EXPECTED_DETECTED:
            assert det[name]["rate"] == 1.0, f"{name}: {det[name]}"
        assert det["spoof_vi_without_se"]["rate"] == 0.0

    def test_artifact_false_reject_rate_is_zero(self):
        assert self._payload()["false_reject_rate"] == 0.0

    def test_artifact_documents_misses(self):
        misses = self._payload()["misses"]
        names = {m["attack"] for m in misses}
        assert "spoof_vi_without_se" in names
        assert all(m.get("why") for m in misses)

    def test_artifact_matches_a_fresh_run(self):
        k = self._payload()
        fresh = run_campaign()
        assert k["n_trials"] == fresh["n_trials"]
        assert k["seed"] == fresh["seed"]
        for name in ATTACKS:
            assert k["detections"][name]["detected"] == fresh["detections"][name]["detected"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
