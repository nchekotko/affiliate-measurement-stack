"""Contribution margin per player, affiliate and FTD cohort.

CM = NGR - affiliate payout - bonus cost - payment cost - chargebacks - variable ops
(NGR here = GGR - gaming tax - provider/game fees; bonuses are a separate line.)
"""

from __future__ import annotations

import duckdb
import pandas as pd

from .config import COSTS, CostConfig

COMPONENTS = [
    "ngr",
    "affiliate_payout",
    "bonus_cost",
    "payment_cost",
    "chargeback_cost",
    "variable_ops",
]


def player_components(
    con: duckdb.DuckDBPyConnection, window_days: int | None = None, costs: CostConfig = COSTS
) -> pd.DataFrame:
    """Per-FTD-player money lines, lifetime-to-date or within ``window_days`` after FTD."""
    cond = f"AND d.activity_date < CAST(p.ftd_ts AS DATE) + {window_days}" if window_days else ""
    q = f"""
    SELECT p.player_id, p.affiliate_id, p.ftd_month, p.kyc_attempts,
           COALESCE(SUM(d.ngr), 0) AS ngr,
           COALESCE(SUM(d.bonus_cost), 0) AS bonus_cost,
           COALESCE(SUM(d.payment_cost), 0) AS payment_cost,
           COALESCE(SUM(d.chargeback_cost), 0) AS chargeback_cost,
           COUNT(DISTINCT CASE WHEN d.stake > 0 THEN strftime(d.activity_date, '%Y-%m') END) AS active_months
    FROM fct_player p
    LEFT JOIN fct_player_day d ON d.player_id = p.player_id {cond}
    WHERE p.ftd_ts IS NOT NULL
    GROUP BY ALL
    """
    df = con.sql(q).df()
    df["variable_ops"] = (
        df["kyc_attempts"] * costs.kyc_check_cost
        + df["active_months"] * costs.support_cost_per_active_month
    )
    return df


def attach_payouts(
    comp: pd.DataFrame,
    payouts: pd.DataFrame,
    ftd_ts: pd.Series | None = None,
    window_days: int | None = None,
) -> pd.DataFrame:
    """Add affiliate payout per player. With a window, only payout months starting before
    FTD + window are counted (RevShare allocation is monthly, so this is an approximation)."""
    pay = payouts.copy()
    if window_days and ftd_ts is not None:
        pay = pay.merge(ftd_ts.rename("ftd_ts"), left_on="player_id", right_index=True, how="left")
        month_start = pd.to_datetime(pay["month"] + "-01")
        pay = pay[month_start < pay["ftd_ts"] + pd.Timedelta(days=window_days)]
    per = (
        pay.groupby("player_id")[["cpa_payout", "rs_payout"]]
        .sum()
        .sum(axis=1)
        .rename("affiliate_payout")
    )
    out = comp.merge(per, on="player_id", how="left")
    out["affiliate_payout"] = out["affiliate_payout"].fillna(0.0)
    out["cm"] = (
        out["ngr"]
        - out["affiliate_payout"]
        - out["bonus_cost"]
        - out["payment_cost"]
        - out["chargeback_cost"]
        - out["variable_ops"]
    )
    out["cm_before_payout"] = out["cm"] + out["affiliate_payout"]
    return out


def rollup(player_cm: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    g = player_cm.groupby(by)
    out = g[COMPONENTS + ["cm", "cm_before_payout"]].sum()
    out.insert(0, "ftd", g.size())
    out["cm_per_ftd"] = out["cm"] / out["ftd"]
    out["payout_per_ftd"] = out["affiliate_payout"] / out["ftd"]
    return out.reset_index()


def cm_waterfall(row: pd.Series) -> pd.DataFrame:
    """Waterfall lines for one affiliate (for reports)."""
    steps = [("NGR", row["ngr"])]
    for name, col in (
        ("affiliate payout", "affiliate_payout"),
        ("bonus cost", "bonus_cost"),
        ("payment cost", "payment_cost"),
        ("chargebacks", "chargeback_cost"),
        ("variable ops", "variable_ops"),
    ):
        steps.append((f"- {name}", -row[col]))
    steps.append(("= contribution margin", row["cm"]))
    return pd.DataFrame(steps, columns=["line", "amount"])
