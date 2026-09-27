import pandas as pd

from affmeasure.contracts import Contract, CPATerms
from affmeasure.reconciliation import reconcile, reconciliation_summary

T = pd.Timestamp("2025-03-10 12:00")
CONTRACTS = {"A": Contract("A", cpa=CPATerms(100)), "B": Contract("B", cpa=CPATerms(80))}


def _fixture():
    clicks = pd.DataFrame(
        [
            ("c_ok", "A", T, "d_ok"),
            ("c_period", "A", T, "d_period"),
            ("c_amount", "A", T, "d_amount"),
            ("c_noftd", "A", T, "d_noftd"),
            ("c_nq", "A", T, "d_nq"),
            ("c_fraud", "A", T, "d_fraud"),
            ("c_cap", "A", T, "d_cap"),
            ("c_pending", "A", T, "d_pending"),
            ("c_stolen_a", "A", T - pd.Timedelta(days=2), "d_stolen"),
            ("c_stolen_b", "B", T, "d_stolen"),
            ("c_noreg", "A", T, "d_nobody"),
            ("c_unclaimed", "A", T, "d_unclaimed"),
        ],
        columns=["click_id", "affiliate_id", "click_ts", "device_fp"],
    )
    pl = [
        ("p_ok", "c_ok", "A", "d_ok", T),
        ("p_period", "c_period", "A", "d_period", T),
        ("p_amount", "c_amount", "A", "d_amount", T),
        ("p_noftd", "c_noftd", "A", "d_noftd", pd.NaT),
        ("p_nq", "c_nq", "A", "d_nq", T),
        ("p_fraud", "c_fraud", "A", "d_fraud", T),
        ("p_cap", "c_cap", "A", "d_cap", T),
        ("p_pending", "c_pending", "A", "d_pending", T),
        ("p_stolen", "c_stolen_b", "B", "d_stolen", T),
        ("p_unclaimed", "c_unclaimed", "A", "d_unclaimed", T),
    ]
    players = pd.DataFrame(
        pl, columns=["player_id", "click_id", "affiliate_id", "device_fp", "ftd_ts"]
    )
    players["reg_ts"] = T + pd.Timedelta(minutes=5)
    qual = pd.DataFrame(
        [
            ("p_ok", True, "OK"),
            ("p_period", True, "OK"),
            ("p_amount", True, "OK"),
            ("p_noftd", False, "NO_FTD"),
            ("p_nq", False, "MIN_WAGERING"),
            ("p_fraud", True, "OK"),
            ("p_cap", True, "OK"),
            ("p_pending", False, "PENDING"),
            ("p_unclaimed", True, "OK"),
        ],
        columns=["player_id", "qualified", "reason"],
    )
    acc = pd.DataFrame(
        [
            ("p_ok", "A", "2025-03", 100.0),
            ("p_period", "A", "2025-04", 100.0),
            ("p_amount", "A", "2025-03", 100.0),
            ("p_fraud", "A", "2025-03", 100.0),
            ("p_cap", "A", "2025-03", 0.0),
            ("p_unclaimed", "A", "2025-03", 100.0),
        ],
        columns=["player_id", "affiliate_id", "month", "cpa_payout"],
    )
    claims = pd.DataFrame(
        [
            ("k1", "A", "2025-03", "c_ok", 100.0),
            ("k2", "A", "2025-03", "c_ok", 100.0),
            ("k3", "A", "2025-03", "c_period", 100.0),
            ("k4", "A", "2025-03", "c_amount", 120.0),
            ("k5", "A", "2025-03", "c_noftd", 100.0),
            ("k6", "A", "2025-03", "c_nq", 100.0),
            ("k7", "A", "2025-03", "c_fraud", 100.0),
            ("k8", "A", "2025-03", "c_cap", 100.0),
            ("k9", "A", "2025-03", "c_pending", 100.0),
            ("k10", "A", "2025-03", "c_stolen_a", 100.0),
            ("k11", "A", "2025-03", "c_noreg", 100.0),
            ("k12", "A", "2025-03", "X_fabricated", 100.0),
            ("k13", "A", "2025-03", "c_stolen_b", 100.0),  # click belongs to B, not A
        ],
        columns=["claim_id", "affiliate_id", "report_month", "click_id", "claimed_commission"],
    )
    claims["claimed_ftd_ts"] = T
    return claims, clicks, players, qual, acc


def test_every_reason_is_classified():
    claims, clicks, players, qual, acc = _fixture()
    recon, unclaimed = reconcile(claims, clicks, players, qual, acc, {"p_fraud"}, CONTRACTS)
    status = recon.set_index("claim_id")["status"].to_dict()
    assert status == {
        "k1": "MATCHED",
        "k2": "DUPLICATE_CLAIM",
        "k3": "PERIOD_MISMATCH",
        "k4": "AMOUNT_MISMATCH",
        "k5": "NO_FTD",
        "k6": "NOT_QUALIFIED",
        "k7": "FRAUD_HOLD",
        "k8": "OVER_CAP",
        "k9": "PENDING",
        "k10": "NOT_LAST_CLICK",
        "k11": "NO_REGISTRATION",
        "k12": "UNKNOWN_CLICK",
        "k13": "UNKNOWN_CLICK",
    }
    r = recon.set_index("claim_id")
    assert r.loc["k6", "detail"] == "MIN_WAGERING"
    assert r.loc["k10", "detail"] == "attributed to B"
    assert r.loc["k4", "internal_payable"] == 100 and r.loc["k4", "disputed_amount"] == 20
    assert unclaimed["player_id"].tolist() == ["p_unclaimed"]


def test_summary_totals():
    claims, clicks, players, qual, acc = _fixture()
    recon, unclaimed = reconcile(claims, clicks, players, qual, acc, {"p_fraud"}, CONTRACTS)
    s = reconciliation_summary(recon, unclaimed)
    assert s["claims"].sum() == len(claims) + 1
    assert s.loc[s["status"] == "MATCHED", "payable"].sum() == 100
    assert s.loc[s["status"] == "UNCLAIMED_BY_PARTNER", "payable"].sum() == 100
