import inspect

import pandas as pd
import pytest

from affmeasure.fraud import (
    RULES,
    review_queue,
    rule_flags,
    score_players,
    threshold_simulation,
)
from affmeasure.margin import attach_payouts, rollup


def _features():
    base = {
        "affiliate_id": "A",
        "sub_id": "A_s1",
        "click_to_reg_sec": 300,
        "players_per_device": 1,
        "players_per_payment": 1,
        "dep30": 100.0,
        "stake30": 1000.0,
        "bonus30": 50.0,
        "n_games": 5,
        "regs_same_sub_10min": 1,
        "coordinated_withdrawals": 1,
    }
    rows = [
        {**base, "player_id": "clean"},
        {
            **base,
            "player_id": "bot",
            "click_to_reg_sec": 3,
            "players_per_device": 6,
            "players_per_payment": 2,
            "regs_same_sub_10min": 12,
            "coordinated_withdrawals": 9,
            "n_games": 1,
            "stake30": 55,
            "dep30": 21,
            "bonus30": 36,
        },
        {**base, "player_id": "incent", "n_games": 1, "stake30": 55, "dep30": 22, "bonus30": 32},
        {**base, "player_id": "nodep", "dep30": 0.0, "stake30": 0.0, "bonus30": 0.0},
    ]
    return pd.DataFrame(rows)


def test_rules_fire_on_crafted_players():
    flags = rule_flags(_features()).set_index(_features()["player_id"])
    assert not flags.loc["clean"].any()
    assert flags.loc["bot"].all()
    assert flags.loc["incent", ["LOW_WAGER_DIVERSITY", "HIGH_BONUS_RATIO"]].all()
    assert not flags.loc["nodep", "HIGH_BONUS_RATIO"]  # no division by zero flag


def test_score_is_weighted_capped_and_explained():
    s = score_players(_features()).set_index("player_id")
    assert s.loc["clean", "score"] == 0
    assert s.loc["clean", "reason_codes"] == ""
    assert s.loc["bot", "score"] == 100  # capped
    w = {r.code: r.weight for r in RULES}
    assert s.loc["incent", "score"] == w["LOW_WAGER_DIVERSITY"] + w["HIGH_BONUS_RATIO"]
    assert s.loc["incent", "reason_codes"] == "LOW_WAGER_DIVERSITY|HIGH_BONUS_RATIO"


def test_scorer_has_no_access_to_labels():
    for fn in (score_players, rule_flags):
        params = inspect.signature(fn).parameters
        assert not any("label" in p or "fraud" in p for p in params)
    assert "is_fraud" not in score_players(_features()).columns


def _payouts():
    return pd.DataFrame(
        {
            "player_id": ["bot", "incent", "clean", "clean"],
            "affiliate_id": "A",
            "month": ["2025-01"] * 4,
            "cpa_payout": [100.0, 100.0, 100.0, 0.0],
            "rs_payout": [0.0, 0.0, 0.0, -30.0],
        }
    )


def test_threshold_simulation_counterfactuals():
    scores = score_players(_features())
    labels = pd.Series({"bot": True, "incent": True, "clean": False, "nodep": False})
    sim = threshold_simulation(scores, labels, _payouts(), [0, 40, 60]).set_index("threshold")
    assert sim.loc[0, "recall"] == 1.0
    assert sim.loc[0, "good_payout_held"] == pytest.approx(70.0)  # clean player's net payout
    assert sim.loc[40, "precision"] == 1.0
    assert sim.loc[40, "fraud_payout_avoided"] == 200.0
    assert sim.loc[60, "recall"] == 0.5
    assert sim.loc[60, "net_savings"] == 100.0


def test_review_queue_orders_by_expected_loss():
    q = review_queue(score_players(_features()), _payouts(), threshold=40)
    assert q["player_id"].tolist() == ["bot", "incent"]
    assert q.loc[0, "priority"] == pytest.approx(100.0)


def test_contribution_margin_formula():
    comp = pd.DataFrame(
        {
            "player_id": ["p1", "p2"],
            "affiliate_id": ["A", "A"],
            "ftd_month": ["2025-01"] * 2,
            "ngr": [200.0, 10.0],
            "bonus_cost": [20.0, 30.0],
            "payment_cost": [5.0, 1.0],
            "chargeback_cost": [0.0, 36.0],
            "variable_ops": [3.0, 1.5],
        }
    )
    pay = pd.DataFrame(
        {
            "player_id": ["p1", "p2"],
            "month": ["2025-01"] * 2,
            "cpa_payout": [0.0, 120.0],
            "rs_payout": [50.0, 0.0],
        }
    )
    cm = attach_payouts(comp, pay).set_index("player_id")
    assert cm.loc["p1", "cm"] == pytest.approx(200 - 50 - 20 - 5 - 0 - 3)
    assert cm.loc["p2", "cm"] == pytest.approx(10 - 120 - 30 - 1 - 36 - 1.5)
    r = rollup(cm.reset_index(), ["affiliate_id"]).iloc[0]
    assert r["ftd"] == 2
    assert r["cm"] == pytest.approx(cm["cm"].sum())
    assert r["cm_before_payout"] - r["cm"] == pytest.approx(170.0)
