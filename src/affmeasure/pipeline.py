"""End-to-end pipeline: generate -> warehouse -> contracts -> fraud -> reconciliation ->
contribution margin -> incrementality -> LTV -> CRM guardrails."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import duckdb
import pandas as pd

from .config import BRAND_BIDDER, CONTRACTS, FRAUD_THRESHOLD, SimConfig
from .contracts import ContractRun, month_of, revshare_base, revshare_statement, run_contracts
from .crm import GuardrailResult, apply_guardrails
from .fraud import (
    affiliate_rollup,
    fraud_features,
    review_queue,
    rule_precision,
    score_players,
    threshold_simulation,
)
from .generator import SyntheticData, generate
from .incrementality import (
    DiDReadout,
    HoldoutReadout,
    SwitchbackReadout,
    channel_overlap,
    did_readout,
    geo_week_panel,
    holdout_readout,
    lastclick_vs_incremental,
    switchback_readout,
)
from .ltv import backtest, backtest_summary, forecast_segments
from .margin import attach_payouts, player_components, rollup
from .reconciliation import reconcile, reconciliation_summary
from .warehouse import build_warehouse, export_parquet


@dataclass
class PipelineResult:
    data: SyntheticData
    con: duckdb.DuckDBPyConnection
    contracts: ContractRun
    funnel: pd.DataFrame
    cohort_windows: pd.DataFrame
    chargebacks: pd.DataFrame
    scores: pd.DataFrame
    queue: pd.DataFrame
    fraud_sim: pd.DataFrame
    rule_stats: pd.DataFrame
    fraud_rollup: pd.DataFrame
    recon: pd.DataFrame
    unclaimed: pd.DataFrame
    recon_summary: pd.DataFrame
    player_cm: pd.DataFrame
    cm_affiliate: pd.DataFrame
    cm_affiliate_d90: pd.DataFrame
    cm_cohort: pd.DataFrame
    panel: pd.DataFrame
    holdout: HoldoutReadout
    switchback: SwitchbackReadout
    did_level: DiDReadout
    did_log: DiDReadout
    overlap: pd.DataFrame
    bb_contrast: pd.DataFrame
    truth_incremental_share: float
    ltv_curve: pd.DataFrame
    ltv_backtest: pd.DataFrame
    ltv_backtest_summary: pd.DataFrame
    ltv_forecast: pd.DataFrame
    carryover_whatif: pd.DataFrame = field(default_factory=pd.DataFrame)
    crm: dict[str, GuardrailResult] = field(default_factory=dict)
    crm_audience_size: int = 0


def months_between(start: str, end: str) -> list[str]:
    return [
        str(p)
        for p in pd.period_range(
            pd.Timestamp(start).to_period("M"), pd.Timestamp(end).to_period("M"), freq="M"
        )
    ]


def contract_activity(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return con.sql(
        """
        SELECT player_id, dep_ts AS ts, amount AS deposit, 0.0 AS stake FROM deposits
        UNION ALL
        SELECT player_id, CAST(bet_date AS TIMESTAMP) + INTERVAL 86399 SECOND AS ts, 0.0 AS deposit, stake
        FROM bets
        """
    ).df()


WHATIF_AFFILIATE = "AFF05"


def carryover_whatif(
    players: pd.DataFrame, player_month: pd.DataFrame, months: list[str], affiliate_id: str
) -> pd.DataFrame:
    """RevShare statement of one affiliate with negative carryover as contracted vs toggled."""
    terms = CONTRACTS[affiliate_id].revshare
    assert terms is not None
    ids = set(players.loc[players["affiliate_id"] == affiliate_id, "player_id"])
    ftd = month_of(
        players.loc[players["affiliate_id"] == affiliate_id, "ftd_ts"].dropna()
    ).value_counts()
    base = revshare_base(player_month[player_month["player_id"].isin(ids)], terms)
    net = base.groupby("month")["net_revenue"].sum()
    a = revshare_statement(net, ftd, terms, months)
    b = revshare_statement(
        net, ftd, replace(terms, negative_carryover=not terms.negative_carryover), months
    )
    tag_a = "on" if terms.negative_carryover else "off"
    tag_b = "off" if terms.negative_carryover else "on"
    return pd.DataFrame(
        {
            "month": months,
            "net_revenue_gross": a["net_revenue_gross"],
            f"payable_carryover_{tag_a}": a["rs_payable"],
            f"payable_carryover_{tag_b}": b["rs_payable"],
            f"carryover_out_{tag_b}": b["carryover_out"],
        }
    )


def run_pipeline(
    cfg: SimConfig | None = None,
    data_dir: str | Path | None = None,
    db_path: str | Path = ":memory:",
) -> PipelineResult:
    cfg = cfg or SimConfig()
    data = generate(cfg)
    if data_dir is not None:
        export_parquet(data, data_dir)
    con = build_warehouse(data, db_path)
    obs_end = pd.Timestamp(cfg.obs_end)
    months = months_between(cfg.start, cfg.obs_end)

    # --- contracts
    players = con.sql("SELECT player_id, affiliate_id, reg_ts, kyc_ts, ftd_ts FROM fct_player").df()
    player_month = con.sql("SELECT * FROM fct_player_month").df()
    run = run_contracts(CONTRACTS, players, contract_activity(con), player_month, months, obs_end)
    whatif = carryover_whatif(players, player_month, months, WHATIF_AFFILIATE)

    # --- fraud scoring (no ground truth) + evaluation (ground truth only here)
    scores = score_players(fraud_features(con))
    queue = review_queue(scores, run.player_payouts, FRAUD_THRESHOLD)
    held = set(queue["player_id"])
    labels = data.ground_truth["player_truth"].set_index("player_id")["is_fraud"]
    sim = threshold_simulation(scores, labels, run.player_payouts)
    rules = rule_precision(scores, labels)

    # --- reconciliation
    fp = con.sql(
        "SELECT player_id, click_id, affiliate_id, reg_ts, device_fp, ftd_ts FROM fct_player"
    ).df()
    recon, unclaimed = reconcile(
        data.tables["partner_reports"],
        data.tables["clicks"],
        fp,
        run.qualification,
        run.cpa_accruals,
        held,
        CONTRACTS,
    )

    # --- contribution margin
    pcm = attach_payouts(player_components(con), run.player_payouts)
    held_pay = (
        pcm[pcm["player_id"].isin(held)]
        .groupby("affiliate_id")["affiliate_payout"]
        .apply(lambda s: s.clip(lower=0).sum())
    )
    cm_aff = rollup(pcm, ["affiliate_id"])
    cm_aff["payout_held_by_fraud_rules"] = cm_aff["affiliate_id"].map(held_pay).fillna(0.0)
    cm_aff["cm_after_fraud_hold"] = cm_aff["cm"] + cm_aff["payout_held_by_fraud_rules"]
    ftd_ts = (
        con.sql("SELECT player_id, ftd_ts FROM fct_player WHERE ftd_ts IS NOT NULL")
        .df()
        .set_index("player_id")["ftd_ts"]
    )
    complete = ftd_ts[ftd_ts + pd.Timedelta(days=90) <= obs_end].index
    comp90 = player_components(con, 90)
    comp90 = comp90[comp90["player_id"].isin(complete)]
    cm90 = rollup(attach_payouts(comp90, run.player_payouts, ftd_ts, 90), ["affiliate_id"])
    cm_cohort = rollup(pcm, ["affiliate_id", "ftd_month"])

    # --- incrementality
    panel = geo_week_panel(con)
    ho = holdout_readout(panel)
    sw = switchback_readout(panel)
    did_l = did_readout(panel, cfg, log=False)
    did_g = did_readout(panel, cfg, log=True)
    overlap = channel_overlap(con)
    truth = data.ground_truth["player_truth"]
    bb_ftd = truth.merge(fp[["player_id", "ftd_ts"]], on="player_id")
    bb_ftd = bb_ftd[
        bb_ftd["origin"].isin(["bb_intercepted", "bb_incremental"]) & bb_ftd["ftd_ts"].notna()
    ]
    truth_share = float((bb_ftd["origin"] == "bb_incremental").mean())
    bb = cm_aff.set_index("affiliate_id").loc[BRAND_BIDDER]
    contrast = []
    for label, share in (
        ("last-click (tracking)", 1.0),
        ("geo holdout, CUPED", ho.incremental_share),
        ("switchback, TWFE", sw.incremental_share),
    ):
        row = lastclick_vs_incremental(
            bb["ftd"], bb["affiliate_payout"], share, bb["cm_before_payout"] / bb["ftd"]
        )
        row["view"] = label
        contrast.append(row)
    bb_contrast = pd.DataFrame(contrast)

    # --- LTV
    curve = con.sql("SELECT * FROM mart_ltv_curve").df()
    bt = backtest(curve[curve["segment"] == "ALL"], h=3, horizon=6)
    fc = forecast_segments(curve, horizon=12)

    # --- CRM guardrails: reactivation campaign for lapsed depositors
    last_act = con.sql(
        "SELECT player_id, MAX(activity_date) AS last_active FROM fct_player_day WHERE stake > 0 GROUP BY 1"
    ).df()
    last_act["last_active"] = pd.to_datetime(last_act["last_active"])
    lapsed = last_act[
        (last_act["last_active"] <= obs_end - pd.Timedelta(days=30))
        & (last_act["last_active"] > obs_end - pd.Timedelta(days=120))
    ]
    crm = {
        ch: apply_guardrails(
            lapsed[["player_id"]],
            ch,
            obs_end,
            data.tables["crm_consents"],
            data.tables["self_exclusions"],
            data.tables["crm_sends"],
        )
        for ch in ("email", "sms", "push")
    }

    return PipelineResult(
        data=data,
        con=con,
        contracts=run,
        funnel=con.sql("SELECT * FROM mart_funnel").df(),
        cohort_windows=con.sql("SELECT * FROM mart_cohort_windows").df(),
        chargebacks=con.sql("SELECT * FROM mart_chargebacks").df(),
        scores=scores,
        queue=queue,
        fraud_sim=sim,
        rule_stats=rules,
        fraud_rollup=affiliate_rollup(scores, FRAUD_THRESHOLD),
        recon=recon,
        unclaimed=unclaimed,
        recon_summary=reconciliation_summary(recon, unclaimed),
        player_cm=pcm,
        cm_affiliate=cm_aff,
        cm_affiliate_d90=cm90,
        cm_cohort=cm_cohort,
        panel=panel,
        holdout=ho,
        switchback=sw,
        did_level=did_l,
        did_log=did_g,
        overlap=overlap,
        bb_contrast=bb_contrast,
        truth_incremental_share=truth_share,
        ltv_curve=curve,
        ltv_backtest=bt,
        ltv_backtest_summary=backtest_summary(bt),
        ltv_forecast=fc,
        carryover_whatif=whatif,
        crm=crm,
        crm_audience_size=len(lapsed),
    )
