"""Affiliate contract engine: CPA (QFTD), RevShare (tiers, negative carryover), Hybrid.

The engine is deliberately data-source agnostic: it consumes plain DataFrames
(player facts, player activity, player-month revenue base) and produces
monthly invoice lines plus a per-player payout allocation that downstream
modules (contribution margin, fraud counterfactuals) can use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- terms


@dataclass(frozen=True)
class QFTDCriteria:
    """Qualified-FTD rule: all conditions must be met within ``within_days`` of registration."""

    min_deposit: float = 20.0  # cumulative deposits since registration
    min_wagering: float = 0.0  # cumulative real-money stake since registration
    require_kyc: bool = True
    within_days: int = 30


@dataclass(frozen=True)
class CPATerms:
    amount: float
    criteria: QFTDCriteria = field(default_factory=QFTDCriteria)
    monthly_cap: int | None = None  # max paid QFTDs per calendar month


@dataclass(frozen=True)
class Tier:
    threshold: float  # inclusive lower bound of the tier basis
    rate: float


@dataclass(frozen=True)
class RevShareTerms:
    tiers: tuple[Tier, ...]
    tier_basis: Literal["ftd", "net_revenue"] = "ftd"
    negative_carryover: bool = True
    admin_fee: float = 0.0  # share of positive net revenue kept by the operator
    deduct_provider_fees: bool = True  # game/provider deductions
    player_lifetime_months: int | None = None  # months since registration that earn share

    def rate_for(self, basis_value: float) -> float:
        rate = 0.0
        for tier in sorted(self.tiers, key=lambda t: t.threshold):
            if basis_value >= tier.threshold:
                rate = tier.rate
        return rate


@dataclass(frozen=True)
class Contract:
    affiliate_id: str
    cpa: CPATerms | None = None
    revshare: RevShareTerms | None = None

    @property
    def kind(self) -> str:
        if self.cpa and self.revshare:
            return "Hybrid"
        if self.cpa:
            return "CPA"
        if self.revshare:
            return "RevShare"
        return "None"


# --------------------------------------------------------------------------- QFTD

QFTD_COLUMNS = ["player_id", "qualified", "qualified_ts", "reason"]


def qualify_players(
    players: pd.DataFrame,
    activity: pd.DataFrame,
    criteria: QFTDCriteria,
    as_of: pd.Timestamp,
) -> pd.DataFrame:
    """Evaluate QFTD status per player.

    players:  player_id, reg_ts, kyc_ts (NaT if KYC not passed), ftd_ts (NaT if no deposit)
    activity: player_id, ts, deposit, stake  (event-level or day-level; ts = when amount accrued)

    Returns player_id, qualified (bool), qualified_ts, reason.
    Reason is ``OK`` or a ``|``-joined list of failed conditions:
    NO_FTD, MIN_DEPOSIT, MIN_WAGERING, KYC, PENDING (window still open at ``as_of``).
    """
    as_of = pd.Timestamp(as_of)
    p = players[["player_id", "reg_ts", "kyc_ts", "ftd_ts"]].copy()
    p["window_end"] = p["reg_ts"] + pd.to_timedelta(criteria.within_days, unit="D")

    act = (
        activity[["player_id", "ts", "deposit", "stake"]]
        .astype({"ts": "datetime64[ns]", "deposit": float, "stake": float})
        .merge(p[["player_id", "reg_ts", "window_end"]], on="player_id", how="inner")
    )
    act = act[(act["ts"] >= act["reg_ts"]) & (act["ts"] <= act["window_end"])]
    act = act[act["ts"] <= as_of].sort_values(["player_id", "ts"], kind="mergesort")
    act["cum_dep"] = act.groupby("player_id")["deposit"].cumsum()
    act["cum_stake"] = act.groupby("player_id")["stake"].cumsum()

    dep_hit = (
        act[(act["cum_dep"] >= criteria.min_deposit) & (act["deposit"] > 0)]
        .groupby("player_id")["ts"]
        .min()
        .rename("t_dep")
    )
    if criteria.min_wagering > 0:
        wag_hit = (
            act[act["cum_stake"] >= criteria.min_wagering]
            .groupby("player_id")["ts"]
            .min()
            .rename("t_wag")
        )
    else:
        wag_hit = pd.Series(dtype="datetime64[ns]", name="t_wag")

    p = p.merge(dep_hit, on="player_id", how="left").merge(wag_hit, on="player_id", how="left")
    if criteria.min_wagering <= 0:
        p["t_wag"] = p["t_dep"]
    kyc_ok = p["kyc_ts"].notna() & (p["kyc_ts"] <= p["window_end"]) & (p["kyc_ts"] <= as_of)
    p["t_kyc"] = p["kyc_ts"].where(kyc_ok) if criteria.require_kyc else p["t_dep"]

    stamps = p[["t_dep", "t_wag", "t_kyc"]]
    all_met = stamps.notna().all(axis=1)
    p["qualified_ts"] = stamps.max(axis=1).where(all_met)
    p["qualified"] = all_met

    window_open = p["window_end"] > as_of
    reasons: list[str] = []
    for row in p.itertuples(index=False):
        if row.qualified:
            reasons.append("OK")
            continue
        failed: list[str] = []
        if pd.isna(row.ftd_ts) or row.ftd_ts > row.window_end:
            failed.append("NO_FTD")
        elif pd.isna(row.t_dep):
            failed.append("MIN_DEPOSIT")
        if criteria.min_wagering > 0 and pd.isna(row.t_wag) and "NO_FTD" not in failed:
            failed.append("MIN_WAGERING")
        if criteria.require_kyc and pd.isna(row.t_kyc):
            failed.append("KYC")
        reasons.append("|".join(failed) if failed else "UNKNOWN")
    p["reason"] = reasons
    pending = ~p["qualified"] & window_open.to_numpy()
    p.loc[pending, "reason"] = "PENDING"
    return p[QFTD_COLUMNS].reset_index(drop=True)


def month_of(ts: pd.Series) -> pd.Series:
    return ts.dt.to_period("M").astype(str)


def cpa_lines(
    qualified: pd.DataFrame, terms: CPATerms, months: list[str]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Monthly CPA lines and per-player CPA payout (with monthly cap applied in time order)."""
    q = qualified[qualified["qualified"]].copy()
    q["month"] = month_of(q["qualified_ts"])
    q = q[q["month"].isin(months)].sort_values("qualified_ts", kind="mergesort")
    q["rank_in_month"] = q.groupby("month").cumcount() + 1
    q["paid"] = True if terms.monthly_cap is None else q["rank_in_month"] <= terms.monthly_cap
    q["cpa_payout"] = np.where(q["paid"], terms.amount, 0.0)
    lines = (
        q.groupby("month")
        .agg(
            qftd=("player_id", "size"), qftd_paid=("paid", "sum"), cpa_payable=("cpa_payout", "sum")
        )
        .reindex(months, fill_value=0)
        .rename_axis("month")
        .reset_index()
    )
    lines["qftd_over_cap"] = lines["qftd"] - lines["qftd_paid"]
    per_player = q[["player_id", "month", "cpa_payout"]]
    return lines, per_player


# --------------------------------------------------------------------------- RevShare


def revshare_base(player_month: pd.DataFrame, terms: RevShareTerms) -> pd.DataFrame:
    """Per player-month net revenue that earns revenue share.

    player_month needs: player_id, month, months_since_reg, ggr, gaming_tax,
    bonus_cost, chargeback_amount, provider_fee.
    """
    pm = player_month.copy()
    if terms.player_lifetime_months is not None:
        pm = pm[pm["months_since_reg"] < terms.player_lifetime_months]
    provider = pm["provider_fee"] if terms.deduct_provider_fees else 0.0
    pm["net_revenue"] = (
        pm["ggr"] - pm["gaming_tax"] - pm["bonus_cost"] - pm["chargeback_amount"] - provider
    )
    return pm[["player_id", "month", "net_revenue"]]


def revshare_statement(
    monthly_net: pd.Series,
    monthly_ftd: pd.Series,
    terms: RevShareTerms,
    months: list[str],
) -> pd.DataFrame:
    """Walk months in order applying admin fee, carryover and tiers.

    monthly_net: net revenue by month (before admin fee); monthly_ftd: FTD count by month.
    """
    rows = []
    carry = 0.0
    for m in months:
        gross = float(monthly_net.get(m, 0.0))
        ftd = float(monthly_ftd.get(m, 0.0))
        admin = terms.admin_fee * gross if gross > 0 else 0.0
        after_admin = gross - admin
        carry_in = carry
        net = after_admin + carry_in
        basis = ftd if terms.tier_basis == "ftd" else after_admin
        rate = terms.rate_for(basis)
        if net > 0:
            payable = rate * net
            carry = 0.0
        else:
            payable = 0.0
            carry = net if terms.negative_carryover else 0.0
        rows.append(
            {
                "month": m,
                "net_revenue_gross": gross,
                "admin_fee": admin,
                "carryover_in": carry_in,
                "net_revenue": net,
                "tier_basis_value": basis,
                "rs_rate": rate,
                "rs_payable": payable,
                "carryover_out": carry,
            }
        )
    return pd.DataFrame(rows)


def allocate_revshare(base: pd.DataFrame, statement: pd.DataFrame) -> pd.DataFrame:
    """Allocate each month's RS payable to players pro rata to their net revenue.

    The effective rate = payable / sum(player net revenue of the month), so the
    allocation sums exactly to the invoice even when carryover changed the base.
    """
    tot = base.groupby("month")["net_revenue"].sum().rename("month_net")
    b = base.merge(tot, on="month").merge(statement[["month", "rs_payable"]], on="month")
    eff = np.where(
        b["month_net"] > 0, b["rs_payable"] / b["month_net"].where(b["month_net"] > 0, 1), 0.0
    )
    b["rs_payout"] = b["net_revenue"] * eff
    return b[["player_id", "month", "rs_payout"]]


# --------------------------------------------------------------------------- invoices

INVOICE_COLUMNS = [
    "affiliate_id",
    "month",
    "contract",
    "ftd",
    "qftd",
    "qftd_paid",
    "qftd_over_cap",
    "cpa_payable",
    "net_revenue_gross",
    "admin_fee",
    "carryover_in",
    "net_revenue",
    "rs_rate",
    "rs_payable",
    "carryover_out",
    "total_payable",
]


@dataclass
class ContractRun:
    invoices: pd.DataFrame
    player_payouts: pd.DataFrame  # player_id, affiliate_id, month, cpa_payout, rs_payout
    qualification: pd.DataFrame  # player_id, affiliate_id, qualified, qualified_ts, reason
    cpa_accruals: pd.DataFrame  # player_id, affiliate_id, month, cpa_payout (0 when over cap)


def run_contracts(
    contracts: dict[str, Contract],
    players: pd.DataFrame,
    activity: pd.DataFrame,
    player_month: pd.DataFrame,
    months: list[str],
    as_of: pd.Timestamp,
) -> ContractRun:
    """players: player_id, affiliate_id, reg_ts, kyc_ts, ftd_ts."""
    invoices, payouts, quals, accruals = [], [], [], []
    for aff_id, contract in contracts.items():
        ap = players[players["affiliate_id"] == aff_id]
        ids = set(ap["player_id"])
        ftd_month = month_of(ap["ftd_ts"].dropna()).value_counts()
        inv = pd.DataFrame({"month": months})
        inv["ftd"] = inv["month"].map(ftd_month).fillna(0).astype(int)
        pp = []
        if contract.cpa:
            q = qualify_players(
                ap, activity[activity["player_id"].isin(ids)], contract.cpa.criteria, as_of
            )
            q["affiliate_id"] = aff_id
            quals.append(q)
            lines, per_player = cpa_lines(q, contract.cpa, months)
            inv = inv.merge(lines, on="month", how="left")
            pp.append(per_player.assign(rs_payout=0.0))
            accruals.append(per_player.assign(affiliate_id=aff_id))
        else:
            inv[["qftd", "qftd_paid", "qftd_over_cap", "cpa_payable"]] = 0
        if contract.revshare:
            base = revshare_base(
                player_month[player_month["player_id"].isin(ids)], contract.revshare
            )
            base = base[base["month"].isin(months)]
            stmt = revshare_statement(
                base.groupby("month")["net_revenue"].sum(), ftd_month, contract.revshare, months
            )
            inv = inv.merge(stmt.drop(columns=["tier_basis_value"]), on="month", how="left")
            pp.append(allocate_revshare(base, stmt).assign(cpa_payout=0.0))
        else:
            for c in [
                "net_revenue_gross",
                "admin_fee",
                "carryover_in",
                "net_revenue",
                "rs_rate",
                "rs_payable",
                "carryover_out",
            ]:
                inv[c] = 0.0
        inv["affiliate_id"] = aff_id
        inv["contract"] = contract.kind
        inv["total_payable"] = inv["cpa_payable"] + inv["rs_payable"]
        invoices.append(inv[INVOICE_COLUMNS])
        if pp:
            payouts.append(pd.concat(pp, ignore_index=True).assign(affiliate_id=aff_id))
    pay = (
        pd.concat(payouts, ignore_index=True)
        .groupby(["player_id", "affiliate_id", "month"], as_index=False)[
            ["cpa_payout", "rs_payout"]
        ]
        .sum()
    )
    qual = (
        pd.concat(quals, ignore_index=True)
        if quals
        else pd.DataFrame(columns=[*QFTD_COLUMNS, "affiliate_id"])
    )
    acc = (
        pd.concat(accruals, ignore_index=True)
        if accruals
        else pd.DataFrame(columns=["player_id", "month", "cpa_payout", "affiliate_id"])
    )
    return ContractRun(pd.concat(invoices, ignore_index=True), pay, qual, acc)
