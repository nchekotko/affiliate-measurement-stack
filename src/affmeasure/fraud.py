"""Interpretable traffic-quality scoring with reason codes.

The scorer only reads the operator-side warehouse (no ground truth). Evaluation
functions take labels as an explicit argument and are used only by the pipeline's
evaluation step on synthetic data.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb
import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Rule:
    code: str
    weight: int
    description: str


RULES: tuple[Rule, ...] = (
    Rule("DUP_DEVICE", 20, "device fingerprint shared by >= 2 registered accounts"),
    Rule("DUP_PAYMENT", 25, "payment fingerprint used by >= 2 accounts"),
    Rule("FAST_CLICK_REG", 20, "click -> registration in < 10 seconds"),
    Rule("BURST", 20, ">= 5 registrations from the same sub_id within +-10 minutes"),
    Rule("LOW_WAGER_DIVERSITY", 30, "one game only and stake < 3x deposits in first 30 days"),
    Rule(
        "COORD_WITHDRAWAL", 25, ">= 4 accounts of the same affiliate withdrew within +-30 minutes"
    ),
    Rule("HIGH_BONUS_RATIO", 20, "bonus / deposits >= 1.25 in first 30 days"),
)

FEATURES_SQL = """
WITH p AS (
    SELECT player_id, affiliate_id, sub_id, reg_ts, click_to_reg_sec, device_fp
    FROM fct_player WHERE ftd_ts IS NOT NULL
),
dev AS (SELECT device_fp, COUNT(*) AS n FROM players GROUP BY 1),
pay AS (SELECT payment_fp, COUNT(DISTINCT player_id) AS n FROM deposits GROUP BY 1),
pay_p AS (
    SELECT d.player_id, MAX(pay.n) AS players_per_payment
    FROM deposits d JOIN pay USING (payment_fp) GROUP BY 1
),
act AS (
    SELECT d.player_id, SUM(d.deposits) AS dep30, SUM(d.stake) AS stake30, SUM(d.bonus_cost) AS bonus30
    FROM fct_player_day d JOIN p USING (player_id)
    WHERE d.activity_date < CAST(p.reg_ts AS DATE) + 30
    GROUP BY 1
),
gms AS (
    SELECT b.player_id, COUNT(DISTINCT b.game_id) AS n_games
    FROM bets b JOIN p USING (player_id)
    WHERE CAST(b.bet_date AS DATE) < CAST(p.reg_ts AS DATE) + 30
    GROUP BY 1
),
regs AS (SELECT player_id, sub_id, reg_ts FROM fct_player WHERE sub_id IS NOT NULL),
burst AS (
    SELECT a.player_id, COUNT(*) AS regs_same_sub_10min
    FROM regs a JOIN regs b
      ON a.sub_id = b.sub_id
     AND b.reg_ts BETWEEN a.reg_ts - INTERVAL 10 MINUTE AND a.reg_ts + INTERVAL 10 MINUTE
    GROUP BY 1
),
wd AS (
    SELECT w.player_id, w.wd_ts, fp.affiliate_id
    FROM withdrawals w JOIN fct_player fp USING (player_id)
    WHERE fp.affiliate_id <> 'ORGANIC'
),
coord AS (
    SELECT a.player_id, MAX(cnt) AS coordinated_withdrawals
    FROM (
        SELECT a.player_id, a.wd_ts, COUNT(DISTINCT b.player_id) AS cnt
        FROM wd a JOIN wd b
          ON a.affiliate_id = b.affiliate_id
         AND b.wd_ts BETWEEN a.wd_ts - INTERVAL 30 MINUTE AND a.wd_ts + INTERVAL 30 MINUTE
        GROUP BY 1, 2
    ) a
    GROUP BY 1
)
SELECT
    p.player_id, p.affiliate_id, p.sub_id, p.click_to_reg_sec,
    dev.n AS players_per_device,
    COALESCE(pay_p.players_per_payment, 1) AS players_per_payment,
    COALESCE(act.dep30, 0) AS dep30,
    COALESCE(act.stake30, 0) AS stake30,
    COALESCE(act.bonus30, 0) AS bonus30,
    COALESCE(gms.n_games, 0) AS n_games,
    COALESCE(burst.regs_same_sub_10min, 0) AS regs_same_sub_10min,
    COALESCE(coord.coordinated_withdrawals, 0) AS coordinated_withdrawals
FROM p
LEFT JOIN dev USING (device_fp)
LEFT JOIN pay_p USING (player_id)
LEFT JOIN act USING (player_id)
LEFT JOIN gms USING (player_id)
LEFT JOIN burst USING (player_id)
LEFT JOIN coord USING (player_id)
ORDER BY p.player_id
"""


def fraud_features(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return con.sql(FEATURES_SQL).df()


def rule_flags(f: pd.DataFrame) -> pd.DataFrame:
    """Boolean flag per rule (columns = rule codes)."""
    dep = f["dep30"].replace(0, np.nan)
    flags = pd.DataFrame(index=f.index)
    flags["DUP_DEVICE"] = f["players_per_device"] >= 2
    flags["DUP_PAYMENT"] = f["players_per_payment"] >= 2
    flags["FAST_CLICK_REG"] = f["click_to_reg_sec"].notna() & (f["click_to_reg_sec"] < 10)
    flags["BURST"] = f["regs_same_sub_10min"] >= 5
    flags["LOW_WAGER_DIVERSITY"] = (f["n_games"] <= 1) & (f["stake30"] < 3 * f["dep30"])
    flags["COORD_WITHDRAWAL"] = f["coordinated_withdrawals"] >= 4
    flags["HIGH_BONUS_RATIO"] = (f["bonus30"] / dep >= 1.25).fillna(False)
    return flags.astype(bool)


def score_players(features: pd.DataFrame, rules: tuple[Rule, ...] = RULES) -> pd.DataFrame:
    """Weighted additive score (capped at 100) with human-readable reason codes."""
    flags = rule_flags(features)
    weights = pd.Series({r.code: r.weight for r in rules})
    score = (flags[weights.index].astype(int) * weights).sum(axis=1).clip(upper=100)
    codes = flags[weights.index].to_numpy()
    names = np.array(weights.index)
    reasons = ["|".join(names[row]) for row in codes]
    out = features[["player_id", "affiliate_id", "sub_id"]].copy()
    out["score"] = score.astype(int)
    out["reason_codes"] = reasons
    return pd.concat([out, flags.add_prefix("flag_")], axis=1)


def player_payout(payouts: pd.DataFrame) -> pd.Series:
    """Payout at stake per player; negative RevShare allocations are floored at 0."""
    return (
        payouts.groupby("player_id")[["cpa_payout", "rs_payout"]].sum().sum(axis=1).clip(lower=0.0)
    )


def review_queue(scores: pd.DataFrame, payouts: pd.DataFrame, threshold: int) -> pd.DataFrame:
    """Manual review queue: held players ordered by expected loss (score/100 x payout at stake)."""
    pay = player_payout(payouts).rename("payout_at_stake")
    q = scores[scores["score"] >= threshold].merge(pay, on="player_id", how="left")
    q["payout_at_stake"] = q["payout_at_stake"].fillna(0.0)
    q["priority"] = q["score"] / 100 * q["payout_at_stake"]
    cols = [
        "player_id",
        "affiliate_id",
        "sub_id",
        "score",
        "reason_codes",
        "payout_at_stake",
        "priority",
    ]
    return q.sort_values(["priority", "score"], ascending=False)[cols].reset_index(drop=True)


def affiliate_rollup(scores: pd.DataFrame, threshold: int) -> pd.DataFrame:
    s = scores.assign(flagged=scores["score"] >= threshold)
    return (
        s.groupby(["affiliate_id", "sub_id"], dropna=False)
        .agg(ftd=("player_id", "size"), flagged=("flagged", "sum"), avg_score=("score", "mean"))
        .assign(flagged_share=lambda d: d["flagged"] / d["ftd"])
        .reset_index()
        .sort_values("flagged_share", ascending=False)
    )


# --------------------------------------------------------------------------- evaluation


def threshold_simulation(
    scores: pd.DataFrame,
    labels: pd.Series,
    payouts: pd.DataFrame,
    thresholds: list[int] | None = None,
) -> pd.DataFrame:
    """Precision/recall and counterfactual savings of holding payouts at each threshold.

    labels: boolean Series indexed by player_id (ground truth, evaluation only).
    fraud_payout_avoided = payouts on fraudulent players that would be held;
    good_payout_held     = payouts on legitimate players wrongly held (partner friction cost).
    """
    thresholds = thresholds or list(range(0, 101, 5))
    pay = player_payout(payouts)
    df = scores[["player_id", "score"]].copy()
    df["is_fraud"] = df["player_id"].map(labels).fillna(False).astype(bool)
    df["payout"] = df["player_id"].map(pay).fillna(0.0)
    total_fraud = int(df["is_fraud"].sum())
    total_fraud_pay = float(df.loc[df["is_fraud"], "payout"].sum())
    rows = []
    for t in thresholds:
        held = df["score"] >= t
        tp = int((held & df["is_fraud"]).sum())
        fp = int((held & ~df["is_fraud"]).sum())
        avoided = float(df.loc[held & df["is_fraud"], "payout"].sum())
        wrong = float(df.loc[held & ~df["is_fraud"], "payout"].sum())
        rows.append(
            {
                "threshold": t,
                "flagged": tp + fp,
                "precision": tp / (tp + fp) if tp + fp else float("nan"),
                "recall": tp / total_fraud if total_fraud else float("nan"),
                "fraud_payout_avoided": avoided,
                "good_payout_held": wrong,
                "net_savings": avoided - wrong,
                "fraud_payout_share_caught": avoided / total_fraud_pay
                if total_fraud_pay
                else float("nan"),
            }
        )
    return pd.DataFrame(rows)


def rule_precision(scores: pd.DataFrame, labels: pd.Series) -> pd.DataFrame:
    y = scores["player_id"].map(labels).fillna(False).astype(bool)
    rows = []
    for r in RULES:
        f = scores[f"flag_{r.code}"]
        rows.append(
            {
                "rule": r.code,
                "weight": r.weight,
                "fires": int(f.sum()),
                "precision": float(y[f].mean()) if f.any() else float("nan"),
                "recall": float(f[y].mean()) if y.any() else float("nan"),
            }
        )
    return pd.DataFrame(rows)
