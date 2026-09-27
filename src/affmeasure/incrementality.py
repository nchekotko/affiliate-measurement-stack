"""Incrementality of the brand-bidding channel vs last-click attribution.

Three designs run on the same synthetic geo x week panel:

* geo holdout (randomised): difference in means across geos, plus CUPED;
* geo/time switchback (randomised per geo-week): two-way fixed effects;
* difference-in-differences for a NON-random early launch in the largest geos,
  with an explicit parallel-trends check (pre-period slope test + event-study leads).

Outcome = FTDs (within 14 days of registration) from registrations that arrive through
the brand-search path: ORGANIC + brand bidder + coupon site (the coupon site competes for
the same last click, so leaving it out would count displacement as lift). Other
affiliates are unaffected by the brand bidder in this simulation; including them only
adds noise.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb
import numpy as np
import pandas as pd

from .config import BRAND_BIDDER, COUPON_AFFILIATE, SimConfig
from .stats import Estimate, cuped, fe_design, ols_cluster, welch_diff

FTD_WINDOW_DAYS = 14


def geo_week_panel(
    con: duckdb.DuckDBPyConnection, bb: str = BRAND_BIDDER, coupon: str = COUPON_AFFILIATE
) -> pd.DataFrame:
    q = f"""
    WITH f AS (
        SELECT geo, reg_week AS week,
               COUNT(*) FILTER (WHERE affiliate_id IN ('ORGANIC', '{bb}', '{coupon}') AND ftd_ok) AS ftd_brand,
               COUNT(*) FILTER (WHERE affiliate_id = '{bb}' AND ftd_ok) AS ftd_bb_lastclick,
               COUNT(*) FILTER (WHERE ftd_ok) AS ftd_total
        FROM (
            SELECT *, ftd_ts IS NOT NULL AND ftd_ts <= reg_ts + INTERVAL {FTD_WINDOW_DAYS} DAY AS ftd_ok
            FROM fct_player
        )
        GROUP BY 1, 2
    )
    SELECT d.*, COALESCE(f.ftd_brand, 0) AS ftd_brand,
           COALESCE(f.ftd_bb_lastclick, 0) AS ftd_bb_lastclick,
           COALESCE(f.ftd_total, 0) AS ftd_total
    FROM geo_design d LEFT JOIN f USING (geo, week)
    ORDER BY d.geo, d.week
    """
    return con.sql(q).df()


# --------------------------------------------------------------------------- holdout


@dataclass
class HoldoutReadout:
    raw: Estimate
    cuped: Estimate
    lastclick_per_geo_week: float
    n_on: int
    n_off: int

    @property
    def incremental_share(self) -> float:
        return self.cuped.estimate / self.lastclick_per_geo_week


def holdout_readout(panel: pd.DataFrame, y: str = "ftd_brand") -> HoldoutReadout:
    per_geo = (
        panel[panel["phase"] == "holdout"]
        .groupby("geo")
        .agg(
            y=(y, "mean"), lastclick=("ftd_bb_lastclick", "mean"), group=("holdout_group", "first")
        )
    )
    pre = panel[panel["phase"] == "did_pre"].groupby("geo")[y].mean().rename("x")
    per_geo = per_geo.join(pre)
    on = per_geo["group"] == "control"  # control geos keep the brand bidder switched on
    raw = welch_diff(per_geo.loc[on, "y"].to_numpy(), per_geo.loc[~on, "y"].to_numpy())
    adj = cuped(per_geo["y"].to_numpy(), per_geo["x"].to_numpy())
    cup = welch_diff(adj[on.to_numpy()], adj[~on.to_numpy()])
    return HoldoutReadout(
        raw, cup, float(per_geo.loc[on, "lastclick"].mean()), int(on.sum()), int((~on).sum())
    )


# --------------------------------------------------------------------------- switchback


@dataclass
class SwitchbackReadout:
    effect: Estimate
    lastclick_per_on_cell: float
    n_cells: int

    @property
    def incremental_share(self) -> float:
        return self.effect.estimate / self.lastclick_per_on_cell


def switchback_readout(panel: pd.DataFrame, y: str = "ftd_brand") -> SwitchbackReadout:
    p = panel[panel["phase"] == "switchback"]
    X, names = fe_design(
        {"bb_on": p["bb_on"].astype(float).to_numpy()}, p["geo"].to_numpy(), p["week"].to_numpy()
    )
    res = ols_cluster(p[y].to_numpy(), X, names, p["geo"].to_numpy())
    lc = float(p.loc[p["bb_on"], "ftd_bb_lastclick"].mean())
    return SwitchbackReadout(res.estimate("bb_on"), lc, len(p))


# --------------------------------------------------------------------------- DiD


@dataclass
class DiDReadout:
    scale: str
    att: Estimate
    pretrend_slope: Estimate  # treated x week slope difference in the pre-period
    leads_wald_p: float  # joint test of event-study leads == 0
    event_study: pd.DataFrame  # rel_bin, estimate, ci_low, ci_high
    treated_pre_mean: float
    lastclick_post_treated: float

    @property
    def parallel_trends_ok(self) -> bool:
        return self.pretrend_slope.p_value > 0.05 and self.leads_wald_p > 0.05


def did_readout(
    panel: pd.DataFrame, cfg: SimConfig, y: str = "ftd_brand", log: bool = False
) -> DiDReadout:
    p = panel[panel["week"] < cfg.holdout_start_week].copy()
    p["yv"] = np.log1p(p[y]) if log else p[y].astype(float)
    treated = (p["did_group"] == "early").to_numpy(float)
    post = (p["week"] >= cfg.did_launch_week).to_numpy(float)
    geo, week = p["geo"].to_numpy(), p["week"].to_numpy()

    X, names = fe_design({"treated_x_post": treated * post}, geo, week)
    att = ols_cluster(p["yv"].to_numpy(), X, names, geo).estimate("treated_x_post")

    pre = p["week"] < cfg.did_launch_week
    wk = (p.loc[pre, "week"] - p.loc[pre, "week"].mean()).to_numpy(float)
    Xp, names_p = fe_design({"treated_x_week": treated[pre] * wk}, geo[pre], week[pre])
    slope = ols_cluster(p.loc[pre, "yv"].to_numpy(), Xp, names_p, geo[pre]).estimate(
        "treated_x_week"
    )

    # event study with 2-week bins relative to launch; reference bin = -1 (the 2 weeks before)
    rel = (p["week"].to_numpy() - cfg.did_launch_week) // 2
    bins = [b for b in np.unique(rel) if b != -1]
    regs = {f"rel_{b}": treated * (rel == b) for b in bins}
    Xe, names_e = fe_design(regs, geo, week)
    es = ols_cluster(p["yv"].to_numpy(), Xe, names_e, geo)
    leads = [f"rel_{b}" for b in bins if b < -1]
    _, lead_p = es.wald(leads)
    rows = [{"rel_bin": -1, "estimate": 0.0, "ci_low": 0.0, "ci_high": 0.0}]
    for b in bins:
        e = es.estimate(f"rel_{b}")
        rows.append(
            {"rel_bin": int(b), "estimate": e.estimate, "ci_low": e.ci_low, "ci_high": e.ci_high}
        )
    event = pd.DataFrame(rows).sort_values("rel_bin").reset_index(drop=True)

    tp = p[(p["did_group"] == "early") & (p["week"] < cfg.did_launch_week)][y].mean()
    lc = p[(p["did_group"] == "early") & (p["week"] >= cfg.did_launch_week)][
        "ftd_bb_lastclick"
    ].mean()
    return DiDReadout("log" if log else "level", att, slope, lead_p, event, float(tp), float(lc))


# --------------------------------------------------------------------------- overlap


def channel_overlap(con: duckdb.DuckDBPyConnection, lookback_days: int = 30) -> pd.DataFrame:
    """Share of each affiliate's FTDs that also had organic/brand or other-affiliate touchpoints."""
    q = f"""
    WITH fp AS (
        SELECT player_id, affiliate_id, reg_ts, device_fp
        FROM fct_player WHERE ftd_ts IS NOT NULL AND affiliate_id <> 'ORGANIC'
    ),
    org AS (
        SELECT DISTINCT fp.player_id,
               MAX(CASE WHEN t.channel = 'organic_brand_search' THEN 1 ELSE 0 END) AS brand
        FROM fp JOIN touchpoints t USING (player_id)
        WHERE t.touch_ts BETWEEN fp.reg_ts - INTERVAL {lookback_days} DAY AND fp.reg_ts
        GROUP BY 1
    ),
    oth AS (
        SELECT DISTINCT fp.player_id
        FROM fp JOIN clicks c ON c.device_fp = fp.device_fp AND c.affiliate_id <> fp.affiliate_id
        WHERE c.click_ts BETWEEN fp.reg_ts - INTERVAL {lookback_days} DAY AND fp.reg_ts
    )
    SELECT fp.affiliate_id,
           COUNT(*) AS ftd,
           AVG(CASE WHEN org.player_id IS NOT NULL THEN 1 ELSE 0 END) AS share_organic_touch,
           AVG(COALESCE(org.brand, 0)) AS share_brand_search,
           AVG(CASE WHEN oth.player_id IS NOT NULL THEN 1 ELSE 0 END) AS share_other_affiliate,
           AVG(CASE WHEN org.player_id IS NOT NULL OR oth.player_id IS NOT NULL THEN 1 ELSE 0 END) AS share_any_overlap
    FROM fp LEFT JOIN org USING (player_id) LEFT JOIN oth USING (player_id)
    GROUP BY 1 ORDER BY 1
    """
    return con.sql(q).df()


# --------------------------------------------------------------------------- summary


def lastclick_vs_incremental(
    lastclick_ftd: float,
    payout: float,
    incremental_share: float,
    margin_before_payout_per_ftd: float,
) -> dict[str, float]:
    """Contrast last-click economics with the incremental view for one channel."""
    inc_ftd = max(lastclick_ftd * incremental_share, 0.0)
    return {
        "lastclick_ftd": lastclick_ftd,
        "incremental_share": incremental_share,
        "incremental_ftd": inc_ftd,
        "payout": payout,
        "cost_per_lastclick_ftd": payout / lastclick_ftd if lastclick_ftd else float("nan"),
        "cost_per_incremental_ftd": payout / inc_ftd if inc_ftd else float("inf"),
        "lastclick_cm": lastclick_ftd * margin_before_payout_per_ftd - payout,
        "incremental_cm": inc_ftd * margin_before_payout_per_ftd - payout,
    }
