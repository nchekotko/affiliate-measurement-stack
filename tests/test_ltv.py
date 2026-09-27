import numpy as np
import pandas as pd
import pytest

from affmeasure.ltv import (
    backtest,
    backtest_summary,
    chain_ladder_cum,
    development_factors,
    fit_power_decay,
    power_decay_cum,
)


def test_power_decay_recovers_parameters():
    k = np.arange(4)
    m = 50 * (k + 1) ** -0.7
    a, b = fit_power_decay(m)
    assert a == pytest.approx(50, rel=1e-6)
    assert b == pytest.approx(0.7, rel=1e-6)
    full = 50 * (np.arange(10) + 1) ** -0.7
    assert power_decay_cum(m[:3], 10) == pytest.approx(full.sum(), rel=1e-6)


def _curve(n_cohorts=12, max_obs=None):
    rows = []
    months = pd.period_range("2025-01", periods=n_cohorts, freq="M").astype(str)
    for i, c in enumerate(months):
        ages = (max_obs or n_cohorts) - i
        cum = 0.0
        for age in range(max(ages, 0)):
            m = 30 * 0.8**age
            cum += m
            rows.append(
                {
                    "segment": "ALL",
                    "ftd_month": c,
                    "age": age,
                    "n_ftd": 100,
                    "ngr_per_ftd": m,
                    "cum_ngr_per_ftd": cum,
                }
            )
    return pd.DataFrame(rows)


def test_chain_ladder_exact_on_geometric_curve():
    curve = _curve()
    f = development_factors(curve, None, 8)
    cum3 = sum(30 * 0.8**a for a in range(3))
    cum6 = sum(30 * 0.8**a for a in range(6))
    assert chain_ladder_cum(cum3, 2, 6, f) == pytest.approx(cum6)
    assert np.isnan(chain_ladder_cum(cum3, 2, 40, f))


def test_development_factors_respect_as_of():
    curve = _curve()
    # at an early date only young ages of the oldest cohorts are visible
    f = development_factors(curve, pd.Timestamp("2025-04-15"), 10)
    assert max(f) <= 1


def test_backtest_on_clean_data_is_exact_for_chain_ladder():
    bt = backtest(_curve(), h=3, horizon=6)
    cl = bt[(bt["model"] == "chain_ladder") & bt["predicted"].notna()]
    assert len(cl) > 0
    assert np.allclose(cl["pct_error"], 0, atol=1e-9)
    obs = bt[bt["model"] == "observed"]
    assert (obs["pct_error"] < 0).all()  # observed-so-far always underestimates growth
    summ = backtest_summary(bt)
    assert set(summ["model"]) == {"observed", "power_decay", "chain_ladder"}
