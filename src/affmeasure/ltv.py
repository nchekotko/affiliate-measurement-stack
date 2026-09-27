"""Cohort LTV forecasting with an as-of (no look-ahead) backtest.

Models of cumulative NGR per FTD at age H-1 (30-day ages since FTD), given the first h ages:

* ``observed``      - naive: LTV = what has been observed so far (lower bound);
* ``power_decay``   - fit m(k) = a * (k+1)^(-b) on the cohort's own first h monthly values;
* ``chain_ladder``  - development factors from other cohorts' cells observable at the as-of date.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

MODELS = ("observed", "power_decay", "chain_ladder")


def cell_available_at(ftd_month: pd.Series, age: pd.Series) -> pd.Series:
    """Date when cell (cohort, age) is fully observed (same rule as mart_ltv_curve)."""
    start = pd.to_datetime(ftd_month + "-01")
    return start + pd.DateOffset(months=1) + pd.to_timedelta(30 * (age + 1), unit="D")


def fit_power_decay(monthly: np.ndarray) -> tuple[float, float]:
    """Least squares on log m(k) = log a - b log(k+1). Non-positive values are floored."""
    m = np.asarray(monthly, float)
    k = np.arange(len(m))
    floor = max(1e-3, 0.02 * np.abs(m).max()) if len(m) else 1e-3
    y = np.log(np.maximum(m, floor))
    X = np.column_stack([np.ones_like(k, dtype=float), -np.log(k + 1)])
    (log_a, b), *_ = np.linalg.lstsq(X, y, rcond=None)
    return float(np.exp(log_a)), float(b)


def power_decay_cum(monthly_obs: np.ndarray, horizon: int) -> float:
    a, b = fit_power_decay(monthly_obs)
    h = len(monthly_obs)
    tail = sum(a * (k + 1) ** (-b) for k in range(h, horizon))
    return float(np.sum(monthly_obs) + tail)


def development_factors(
    curve: pd.DataFrame, as_of: pd.Timestamp | None, max_age: int
) -> dict[int, float]:
    """Volume-weighted age-to-age factors f_k = sum cum(k+1) / sum cum(k), using cells known at as_of."""
    c = curve.copy()
    if as_of is not None:
        c = c[cell_available_at(c["ftd_month"], c["age"]) <= as_of]
    c["cum_total"] = c["cum_ngr_per_ftd"] * c["n_ftd"]
    wide = c.pivot_table(index="ftd_month", columns="age", values="cum_total")
    factors: dict[int, float] = {}
    for k in range(max_age):
        if k not in wide.columns or k + 1 not in wide.columns:
            break
        both = wide[[k, k + 1]].dropna()
        if len(both) == 0 or both[k].sum() <= 0:
            break
        factors[k] = float(both[k + 1].sum() / both[k].sum())
    return factors


def chain_ladder_cum(
    cum_last: float, last_age: int, horizon: int, factors: dict[int, float]
) -> float:
    val = cum_last
    for k in range(last_age, horizon - 1):
        if k not in factors:
            return float("nan")
        val *= factors[k]
    return float(val)


def backtest(curve: pd.DataFrame, h: int = 3, horizon: int = 6) -> pd.DataFrame:
    """For each cohort observed to age horizon-1, predict cum NGR/FTD at horizon-1 from ages < h."""
    rows = []
    for cohort, g in curve.groupby("ftd_month"):
        g = g.sort_values("age")
        if g["age"].max() < horizon - 1:
            continue
        actual = float(g.loc[g["age"] == horizon - 1, "cum_ngr_per_ftd"].iloc[0])
        early = g[g["age"] < h]
        as_of = cell_available_at(pd.Series([cohort]), pd.Series([h - 1])).iloc[0]
        others = curve[curve["ftd_month"] != cohort]
        factors = development_factors(others, as_of, horizon)
        cum_h = float(early["cum_ngr_per_ftd"].iloc[-1])
        preds = {
            "observed": cum_h,
            "power_decay": power_decay_cum(early["ngr_per_ftd"].to_numpy(), horizon),
            "chain_ladder": chain_ladder_cum(cum_h, h - 1, horizon, factors),
        }
        for model, pred in preds.items():
            rows.append(
                {
                    "ftd_month": cohort,
                    "model": model,
                    "actual": actual,
                    "predicted": pred,
                    "pct_error": (pred - actual) / actual if actual else float("nan"),
                }
            )
    return pd.DataFrame(rows)


def backtest_summary(bt: pd.DataFrame) -> pd.DataFrame:
    """MAPE and bias per model on the cohorts where every model produced a prediction."""
    ok = bt.dropna(subset=["predicted"])
    common = ok.groupby("ftd_month")["model"].nunique()
    common = common[common == ok["model"].nunique()].index
    sub = ok[ok["ftd_month"].isin(common)]
    out = sub.groupby("model").agg(
        cohorts=("ftd_month", "nunique"),
        mape=("pct_error", lambda s: float(np.mean(np.abs(s)))),
        bias=("pct_error", "mean"),
    )
    return out.reindex([m for m in MODELS if m in out.index]).reset_index()


def forecast_segments(curve: pd.DataFrame, horizon: int = 12) -> pd.DataFrame:
    """LTV(horizon) per FTD by segment with chain ladder; segment factors fall back to ALL."""
    all_f = development_factors(curve[curve["segment"] == "ALL"], None, horizon)
    rows = []
    for seg, g in curve.groupby("segment"):
        seg_f = development_factors(g, None, horizon)
        # require >=3 cohorts behind a segment factor; otherwise borrow the ALL factor
        depth = g.groupby("age")["ftd_month"].nunique()
        factors = {
            k: (seg_f[k] if k in seg_f and depth.get(k + 1, 0) >= 3 else all_f.get(k, float("nan")))
            for k in range(horizon - 1)
        }
        preds, weights, m3 = [], [], []
        for _, c in g.groupby("ftd_month"):
            c = c.sort_values("age")
            last = c.iloc[-1]
            if last["age"] >= horizon - 1:
                pred = float(c.loc[c["age"] == horizon - 1, "cum_ngr_per_ftd"].iloc[0])
            else:
                pred = chain_ladder_cum(
                    float(last["cum_ngr_per_ftd"]), int(last["age"]), horizon, factors
                )
            if np.isfinite(pred):
                preds.append(pred)
                weights.append(float(last["n_ftd"]))
            if (c["age"] == 2).any():
                m3.append(
                    (float(c.loc[c["age"] == 2, "cum_ngr_per_ftd"].iloc[0]), float(last["n_ftd"]))
                )
        rows.append(
            {
                "segment": seg,
                "ftd": float(g.groupby("ftd_month")["n_ftd"].first().sum()),
                "ngr_m3_per_ftd": np.average([v for v, _ in m3], weights=[w for _, w in m3])
                if m3
                else np.nan,
                f"ltv{horizon}_ngr_per_ftd": np.average(preds, weights=weights)
                if preds
                else np.nan,
            }
        )
    return pd.DataFrame(rows)
