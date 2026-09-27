"""End-to-end checks on a small synthetic run (scale=0.12)."""

from dataclasses import replace

import pandas as pd
import pytest

from affmeasure.config import SimConfig
from affmeasure.generator import generate
from affmeasure.pipeline import run_pipeline
from affmeasure.report import write_report

SMALL = replace(SimConfig(), scale=0.12, seed=7)


@pytest.fixture(scope="module")
def result():
    r = run_pipeline(SMALL)
    yield r
    r.con.close()


def test_generator_is_deterministic():
    a = generate(replace(SMALL, scale=0.05))
    b = generate(replace(SMALL, scale=0.05))
    for name in ("players", "deposits", "bets", "partner_reports"):
        pd.testing.assert_frame_equal(a.tables[name], b.tables[name])
    c = generate(replace(SMALL, scale=0.05, seed=8))
    assert not a.tables["deposits"].equals(c.tables["deposits"])


def test_ground_truth_is_not_in_warehouse(result):
    tables = set(
        result.con.sql("SELECT table_name FROM information_schema.tables").df()["table_name"]
    )
    assert "player_truth" not in tables
    cols = result.con.sql("SELECT column_name FROM information_schema.columns").df()["column_name"]
    assert not cols.isin(["is_fraud", "fraud_type", "origin"]).any()


def test_public_affiliate_types_do_not_leak_labels(result):
    declared = set(result.data.tables["affiliates"]["declared_type"])
    assert "bot_farm" not in declared and "incentivised" not in declared


def test_funnel_is_monotone(result):
    f = result.funnel
    assert (f["registrations"] >= f["kyc_passed"]).all()
    assert (f["registrations"] >= f["ftd"]).all()
    aff = f[f["affiliate_id"] != "ORGANIC"]
    assert (aff["clicks"] >= aff["registrations"]).all()


def test_ngr_identity_in_player_day(result):
    d = result.con.sql(
        "SELECT SUM(ggr) g, SUM(gaming_tax) t, SUM(provider_fee) p, SUM(ngr) n FROM fct_player_day"
    ).fetchone()
    assert d[3] == pytest.approx(d[0] - d[1] - d[2], rel=1e-9)


def test_cohort_windows_null_when_incomplete(result):
    cw = result.cohort_windows
    obs_end = pd.Timestamp(SMALL.obs_end)
    start = pd.to_datetime(cw["ftd_month"] + "-01")
    incomplete = start + pd.DateOffset(months=1) + pd.Timedelta(days=90) > obs_end
    assert cw.loc[incomplete, "ngr_d90"].isna().all()
    assert cw.loc[~incomplete, "ngr_d90"].notna().all()


def test_ltv_curve_only_fully_observed_ages(result):
    c = result.ltv_curve
    avail = (
        pd.to_datetime(c["ftd_month"] + "-01")
        + pd.DateOffset(months=1)
        + pd.to_timedelta(30 * (c["age"] + 1), unit="D")
    )
    assert (avail <= pd.Timestamp(SMALL.obs_end)).all()


def test_invoices_match_player_allocation(result):
    inv_total = result.contracts.invoices["total_payable"].sum()
    alloc = result.contracts.player_payouts[["cpa_payout", "rs_payout"]].sum().sum()
    assert alloc == pytest.approx(inv_total, rel=1e-9)


def test_incentivised_affiliate_has_negative_margin(result):
    cm = result.cm_affiliate.set_index("affiliate_id")
    assert cm.loc["AFF08", "cm"] < 0
    assert cm.loc["AFF08", "ftd"] > cm.loc["AFF01", "ftd"]


def test_report_written(result, tmp_path):
    path = write_report(result, tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "SYNTHETIC DEMO DATA" in text
    for chart in (
        "cm_by_affiliate.png",
        "incrementality.png",
        "ltv_curves.png",
        "fraud_thresholds.png",
    ):
        assert (tmp_path / "charts" / chart).exists()
    assert (tmp_path / "review_queue.csv").exists()
