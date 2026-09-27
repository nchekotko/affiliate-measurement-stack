import pandas as pd
import pytest

from affmeasure.contracts import (
    Contract,
    CPATerms,
    QFTDCriteria,
    RevShareTerms,
    Tier,
    allocate_revshare,
    cpa_lines,
    qualify_players,
    revshare_base,
    revshare_statement,
    run_contracts,
)

T0 = pd.Timestamp("2025-01-01 10:00")
AS_OF = pd.Timestamp("2025-12-31")


def _players(**kw):
    row = {
        "player_id": "P1",
        "reg_ts": T0,
        "kyc_ts": T0 + pd.Timedelta(hours=1),
        "ftd_ts": T0 + pd.Timedelta(hours=2),
    }
    row.update(kw)
    return pd.DataFrame([row])


def _act(rows):
    return pd.DataFrame(rows, columns=["player_id", "ts", "deposit", "stake"])


CRIT = QFTDCriteria(min_deposit=20, min_wagering=50, require_kyc=True, within_days=30)


def test_qualifies_exactly_at_boundaries():
    act = _act(
        [
            ("P1", T0 + pd.Timedelta(hours=2), 20.0, 0.0),
            ("P1", T0 + pd.Timedelta(days=1), 0.0, 50.0),
        ]
    )
    q = qualify_players(_players(), act, CRIT, AS_OF)
    assert bool(q.loc[0, "qualified"])
    assert q.loc[0, "reason"] == "OK"
    # qualification time is when the LAST condition was met
    assert q.loc[0, "qualified_ts"] == T0 + pd.Timedelta(days=1)


def test_min_deposit_just_below_fails():
    act = _act(
        [
            ("P1", T0 + pd.Timedelta(hours=2), 19.99, 0.0),
            ("P1", T0 + pd.Timedelta(days=1), 0.0, 80.0),
        ]
    )
    q = qualify_players(_players(), act, CRIT, AS_OF)
    assert not q.loc[0, "qualified"]
    assert "MIN_DEPOSIT" in q.loc[0, "reason"]


def test_cumulative_deposits_count():
    act = _act(
        [
            ("P1", T0 + pd.Timedelta(hours=2), 10.0, 0.0),
            ("P1", T0 + pd.Timedelta(days=3), 10.0, 0.0),
            ("P1", T0 + pd.Timedelta(days=4), 0.0, 60.0),
        ]
    )
    q = qualify_players(_players(), act, CRIT, AS_OF)
    assert bool(q.loc[0, "qualified"])
    assert q.loc[0, "qualified_ts"] == T0 + pd.Timedelta(days=4)


def test_wagering_after_window_does_not_count():
    act = _act(
        [
            ("P1", T0 + pd.Timedelta(hours=2), 25.0, 10.0),
            ("P1", T0 + pd.Timedelta(days=31), 0.0, 100.0),
        ]
    )
    q = qualify_players(_players(), act, CRIT, AS_OF)
    assert not q.loc[0, "qualified"]
    assert q.loc[0, "reason"] == "MIN_WAGERING"


def test_kyc_after_window_fails_and_missing_kyc_fails():
    act = _act([("P1", T0 + pd.Timedelta(hours=2), 25.0, 60.0)])
    late = qualify_players(_players(kyc_ts=T0 + pd.Timedelta(days=45)), act, CRIT, AS_OF)
    none = qualify_players(_players(kyc_ts=pd.NaT), act, CRIT, AS_OF)
    assert late.loc[0, "reason"] == "KYC"
    assert none.loc[0, "reason"] == "KYC"
    no_kyc_needed = qualify_players(
        _players(kyc_ts=pd.NaT), act, QFTDCriteria(20, 50, require_kyc=False, within_days=30), AS_OF
    )
    assert bool(no_kyc_needed.loc[0, "qualified"])


def test_no_ftd_and_pending():
    q = qualify_players(_players(ftd_ts=pd.NaT), _act([]), CRIT, AS_OF)
    assert q.loc[0, "reason"] == "NO_FTD"
    pending = qualify_players(_players(ftd_ts=pd.NaT), _act([]), CRIT, T0 + pd.Timedelta(days=5))
    assert pending.loc[0, "reason"] == "PENDING"


def test_cpa_monthly_cap_pays_first_in_time_order():
    q = pd.DataFrame(
        {
            "player_id": ["A", "B", "C", "D"],
            "qualified": [True, True, True, True],
            "qualified_ts": pd.to_datetime(
                ["2025-01-05", "2025-01-03", "2025-01-20", "2025-02-01"]
            ),
            "reason": "OK",
        }
    )
    lines, per = cpa_lines(q, CPATerms(100, CRIT, monthly_cap=2), ["2025-01", "2025-02"])
    jan = lines.set_index("month").loc["2025-01"]
    assert (jan["qftd"], jan["qftd_paid"], jan["qftd_over_cap"], jan["cpa_payable"]) == (
        3,
        2,
        1,
        200,
    )
    assert per.set_index("player_id").loc["C", "cpa_payout"] == 0.0  # latest in January
    assert lines.set_index("month").loc["2025-02", "cpa_payable"] == 100


MONTHS = ["2025-01", "2025-02", "2025-03"]


def test_negative_carryover_on_offsets_future_months():
    terms = RevShareTerms(tiers=(Tier(0, 0.3),), negative_carryover=True)
    st = revshare_statement(
        pd.Series({"2025-01": -100.0, "2025-02": 50.0, "2025-03": 80.0}),
        pd.Series(dtype=float),
        terms,
        MONTHS,
    )
    assert st["rs_payable"].tolist() == pytest.approx([0.0, 0.0, 9.0])
    assert st["carryover_out"].tolist() == pytest.approx([-100.0, -50.0, 0.0])


def test_negative_carryover_off_resets_each_month():
    terms = RevShareTerms(tiers=(Tier(0, 0.3),), negative_carryover=False)
    st = revshare_statement(
        pd.Series({"2025-01": -100.0, "2025-02": 50.0, "2025-03": 80.0}),
        pd.Series(dtype=float),
        terms,
        MONTHS,
    )
    assert st["rs_payable"].tolist() == pytest.approx([0.0, 15.0, 24.0])
    assert (st["carryover_out"] == 0).all()


def test_admin_fee_only_on_positive_months():
    terms = RevShareTerms(tiers=(Tier(0, 0.5),), admin_fee=0.1)
    st = revshare_statement(
        pd.Series({"2025-01": 100.0, "2025-02": -40.0}), pd.Series(dtype=float), terms, MONTHS[:2]
    )
    assert st.loc[0, "admin_fee"] == pytest.approx(10.0)
    assert st.loc[0, "rs_payable"] == pytest.approx(45.0)
    assert st.loc[1, "admin_fee"] == 0.0


def test_tier_boundaries_are_inclusive():
    terms = RevShareTerms(tiers=(Tier(0, 0.25), Tier(60, 0.30), Tier(120, 0.35)))
    assert terms.rate_for(59) == 0.25
    assert terms.rate_for(60) == 0.30
    assert terms.rate_for(119.999) == 0.30
    assert terms.rate_for(120) == 0.35
    st = revshare_statement(
        pd.Series({"2025-01": 1000.0}), pd.Series({"2025-01": 60}), terms, MONTHS[:1]
    )
    assert st.loc[0, "rs_payable"] == pytest.approx(300.0)


def test_tier_basis_net_revenue():
    terms = RevShareTerms(tiers=(Tier(0, 0.3), Tier(5000, 0.35)), tier_basis="net_revenue")
    st = revshare_statement(
        pd.Series({"2025-01": 4999.0, "2025-02": 5000.0}), pd.Series(dtype=float), terms, MONTHS[:2]
    )
    assert st["rs_rate"].tolist() == [0.3, 0.35]


def _pm():
    return pd.DataFrame(
        {
            "player_id": ["A", "A", "B"],
            "month": ["2025-01", "2025-03", "2025-01"],
            "months_since_reg": [0, 13, 0],
            "ggr": [100.0, 100.0, 50.0],
            "gaming_tax": [10.0, 10.0, 5.0],
            "bonus_cost": [20.0, 0.0, 0.0],
            "chargeback_amount": [0.0, 0.0, 5.0],
            "provider_fee": [10.0, 10.0, 0.0],
        }
    )


def test_revshare_base_lifetime_and_deductions():
    base = revshare_base(_pm(), RevShareTerms(tiers=(Tier(0, 0.3),), player_lifetime_months=12))
    assert len(base) == 2  # month 13 of player A is beyond lifetime
    assert base.set_index("player_id").loc["A", "net_revenue"] == pytest.approx(60.0)
    no_ded = revshare_base(_pm(), RevShareTerms(tiers=(Tier(0, 0.3),), deduct_provider_fees=False))
    assert no_ded.iloc[0]["net_revenue"] == pytest.approx(70.0)


def test_allocation_sums_to_invoice():
    base = pd.DataFrame(
        {
            "player_id": ["A", "B", "C"],
            "month": ["2025-01"] * 3,
            "net_revenue": [100.0, -20.0, 40.0],
        }
    )
    st = pd.DataFrame({"month": ["2025-01"], "rs_payable": [36.0]})
    alloc = allocate_revshare(base, st)
    assert alloc["rs_payout"].sum() == pytest.approx(36.0)


def test_run_contracts_hybrid_equals_cpa_plus_revshare():
    players = pd.DataFrame(
        {
            "player_id": ["A"],
            "affiliate_id": ["X"],
            "reg_ts": [T0],
            "kyc_ts": [T0],
            "ftd_ts": [T0 + pd.Timedelta(hours=1)],
        }
    )
    act = _act(
        [("A", T0 + pd.Timedelta(hours=1), 30.0, 0.0), ("A", T0 + pd.Timedelta(days=1), 0.0, 60.0)]
    )
    pm = _pm()[_pm()["player_id"] == "A"].assign(player_id="A")
    contracts = {
        "X": Contract("X", cpa=CPATerms(50, CRIT), revshare=RevShareTerms(tiers=(Tier(0, 0.2),)))
    }
    run = run_contracts(contracts, players, act, pm, MONTHS, AS_OF)
    inv = run.invoices.set_index("month")
    assert run.invoices["contract"].iloc[0] == "Hybrid"
    assert inv.loc["2025-01", "cpa_payable"] == 50
    assert inv.loc["2025-01", "rs_payable"] == pytest.approx(0.2 * 60.0)
    assert (
        run.invoices["total_payable"] == run.invoices["cpa_payable"] + run.invoices["rs_payable"]
    ).all()
    assert run.player_payouts[["cpa_payout", "rs_payout"]].sum().sum() == pytest.approx(
        run.invoices["total_payable"].sum()
    )
