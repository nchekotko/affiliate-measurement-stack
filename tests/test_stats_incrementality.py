import numpy as np
import pandas as pd
import pytest

from affmeasure.config import SimConfig
from affmeasure.incrementality import (
    did_readout,
    holdout_readout,
    lastclick_vs_incremental,
    switchback_readout,
)
from affmeasure.stats import cuped, fe_design, ols_cluster, welch_diff


def test_welch_matches_manual():
    a, b = np.array([3.0, 4.0, 5.0, 6.0]), np.array([1.0, 2.0, 2.0, 3.0])
    e = welch_diff(a, b)
    assert e.estimate == pytest.approx(2.5)
    se = np.sqrt(a.var(ddof=1) / 4 + b.var(ddof=1) / 4)
    assert e.se == pytest.approx(se)
    assert e.ci_low < 2.5 < e.ci_high


def test_cuped_reduces_variance_and_keeps_mean():
    rng = np.random.default_rng(0)
    x = rng.normal(10, 3, 500)
    y = x + rng.normal(0, 1, 500)
    adj = cuped(y, x)
    assert adj.mean() == pytest.approx(y.mean())
    assert adj.var() < 0.2 * y.var()


def test_ols_cluster_recovers_coefficient():
    rng = np.random.default_rng(1)
    unit = np.repeat(np.arange(30), 10)
    time = np.tile(np.arange(10), 30)
    d = rng.random(300) < 0.5
    y = 2.0 * d + unit * 0.5 + time * 0.1 + rng.normal(0, 1, 300)
    X, names = fe_design({"d": d.astype(float)}, unit, time)
    res = ols_cluster(y, X, names, unit)
    e = res.estimate("d")
    assert abs(e.estimate - 2.0) < 0.3
    assert e.ci_low < 2.0 < e.ci_high


def _panel(effect: float, trend_gap: float = 0.0, seed: int = 0) -> pd.DataFrame:
    """Synthetic geo x week panel mimicking geo_week_panel output with a known effect."""
    cfg = SimConfig()
    rng = np.random.default_rng(seed)
    rows = []
    geos = [f"G{g:02d}" for g in range(40)]
    base = rng.uniform(5, 15, 40)
    holdout = set(rng.choice(40, 12, replace=False))
    early = set(np.argsort(-base)[:12])
    for g, geo in enumerate(geos):
        for w in range(cfg.n_weeks):
            if w < cfg.did_launch_week:
                phase, on = "did_pre", False
            elif w < cfg.holdout_start_week:
                phase, on = "did_post", g in early
            elif w < cfg.switchback_start_week:
                phase, on = "holdout", g not in holdout
            else:
                phase, on = "switchback", bool(rng.random() < 0.5)
            trend = trend_gap * w if g in early else 0.0
            y = base[g] + trend + effect * on + rng.normal(0, 1.0)
            rows.append(
                {
                    "geo": geo,
                    "week": w,
                    "bb_on": on,
                    "phase": phase,
                    "did_group": "early" if g in early else "late",
                    "holdout_group": "holdout" if g in holdout else "control",
                    "ftd_brand": y,
                    "ftd_bb_lastclick": 4.0 * on,
                    "ftd_total": y,
                }
            )
    return pd.DataFrame(rows)


def test_holdout_switchback_did_recover_known_effect():
    p = _panel(effect=1.5)
    ho = holdout_readout(p)
    assert ho.cuped.ci_low < 1.5 < ho.cuped.ci_high
    assert ho.cuped.se < ho.raw.se  # pre-period covariate helps
    sw = switchback_readout(p)
    assert sw.effect.ci_low < 1.5 < sw.effect.ci_high
    assert sw.incremental_share == pytest.approx(sw.effect.estimate / 4.0)
    d = did_readout(p, SimConfig())
    assert d.att.ci_low < 1.5 < d.att.ci_high
    assert d.parallel_trends_ok


def test_parallel_trends_violation_is_detected():
    d = did_readout(_panel(effect=1.5, trend_gap=0.15, seed=3), SimConfig())
    assert not d.parallel_trends_ok
    assert d.pretrend_slope.p_value < 0.05


def test_lastclick_vs_incremental_math():
    r = lastclick_vs_incremental(1000, 60_000, 0.25, 80.0)
    assert r["incremental_ftd"] == 250
    assert r["cost_per_lastclick_ftd"] == 60
    assert r["cost_per_incremental_ftd"] == 240
    assert r["lastclick_cm"] == 20_000
    assert r["incremental_cm"] == -40_000
