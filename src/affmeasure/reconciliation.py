"""Invoice reconciliation: partner-claimed CPA conversions vs internal tracking + contract logic."""

from __future__ import annotations

import pandas as pd

from .contracts import Contract

# Evaluation order matters: the first matching reason is reported.
REASONS = (
    "DUPLICATE_CLAIM",
    "UNKNOWN_CLICK",
    "NOT_LAST_CLICK",
    "NO_REGISTRATION",
    "NO_FTD",
    "PENDING",
    "NOT_QUALIFIED",
    "FRAUD_HOLD",
    "OVER_CAP",
    "PERIOD_MISMATCH",
    "AMOUNT_MISMATCH",
    "MATCHED",
)
PAYABLE_STATUSES = {"PERIOD_MISMATCH", "AMOUNT_MISMATCH", "MATCHED"}


def reconcile(
    claims: pd.DataFrame,
    clicks: pd.DataFrame,
    players: pd.DataFrame,
    qualification: pd.DataFrame,
    cpa_payouts: pd.DataFrame,
    held_players: set[str],
    contracts: dict[str, Contract],
    stitch_days: int = 30,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Classify each claim; also list internal paid QFTDs the partner did not claim.

    claims:        claim_id, affiliate_id, report_month, click_id, claimed_ftd_ts, claimed_commission
    clicks:        click_id, affiliate_id, click_ts, device_fp
    players:       player_id, click_id, affiliate_id, reg_ts, device_fp, ftd_ts
    qualification: player_id, affiliate_id, qualified, qualified_ts, reason
    cpa_payouts:   player_id, affiliate_id, month, cpa_payout (internal CPA accruals)
    """
    c = claims.copy()
    c["dup_rank"] = c.groupby(["affiliate_id", "click_id"]).cumcount()
    c = c.merge(
        clicks[["click_id", "affiliate_id", "click_ts", "device_fp"]].rename(
            columns={"affiliate_id": "click_aff"}
        ),
        on="click_id",
        how="left",
    )
    known = c["click_aff"].notna() & (c["click_aff"] == c["affiliate_id"])

    # last-click owner of the click (direct tracking link)
    direct = players[["player_id", "click_id"]].rename(columns={"player_id": "direct_player"})
    c = c.merge(direct, on="click_id", how="left")

    # device stitching for clicks that were not the tracked last click
    stitch = c[known & c["direct_player"].isna()][["claim_id", "device_fp", "click_ts"]].merge(
        players[["player_id", "device_fp", "reg_ts", "affiliate_id"]].rename(
            columns={"affiliate_id": "owner_aff"}
        ),
        on="device_fp",
    )
    stitch = stitch[
        (stitch["click_ts"] <= stitch["reg_ts"])
        & (stitch["reg_ts"] <= stitch["click_ts"] + pd.Timedelta(days=stitch_days))
    ]
    stitch = stitch.sort_values("reg_ts").drop_duplicates("claim_id")[["claim_id", "player_id"]]
    c = c.merge(stitch.rename(columns={"player_id": "stitched_player"}), on="claim_id", how="left")

    pl = players.set_index("player_id")
    qual = qualification.set_index("player_id")
    paid = cpa_payouts[cpa_payouts["cpa_payout"] > 0].set_index("player_id")
    accrued = cpa_payouts.set_index("player_id")

    statuses, detail, internal_month, payable, player_ids = [], [], [], [], []
    for row in c.itertuples(index=False):
        pid = row.direct_player if isinstance(row.direct_player, str) else None
        status, info, month, amt = "", "", None, 0.0
        cpa_amount = (
            contracts[row.affiliate_id].cpa.amount if contracts[row.affiliate_id].cpa else 0.0
        )
        if row.dup_rank > 0:
            status = "DUPLICATE_CLAIM"
        elif not (isinstance(row.click_aff, str) and row.click_aff == row.affiliate_id):
            status = "UNKNOWN_CLICK"
        elif pid is None:
            if isinstance(row.stitched_player, str):
                status = "NOT_LAST_CLICK"
                pid = row.stitched_player
                info = f"attributed to {pl.at[pid, 'affiliate_id']}"
            else:
                status = "NO_REGISTRATION"
        elif pd.isna(pl.at[pid, "ftd_ts"]):
            status = "NO_FTD"
        elif pid in qual.index and qual.at[pid, "reason"] == "PENDING":
            status = "PENDING"
        elif pid in qual.index and not bool(qual.at[pid, "qualified"]):
            status = "NOT_QUALIFIED"
            info = str(qual.at[pid, "reason"])
        elif pid in held_players:
            status = "FRAUD_HOLD"
        elif pid in accrued.index and pid not in paid.index:
            status = "OVER_CAP"
        else:
            month = str(paid.at[pid, "month"]) if pid in paid.index else None
            amt = cpa_amount
            if month != row.report_month:
                status = "PERIOD_MISMATCH"
                info = f"internal month {month}"
            elif abs(row.claimed_commission - cpa_amount) > 1e-9:
                status = "AMOUNT_MISMATCH"
                info = f"contract {cpa_amount:.0f} vs claimed {row.claimed_commission:.0f}"
            else:
                status = "MATCHED"
        statuses.append(status)
        detail.append(info)
        internal_month.append(month)
        payable.append(amt)
        player_ids.append(pid)
    c["player_id"] = player_ids
    c["status"] = statuses
    c["detail"] = detail
    c["internal_month"] = internal_month
    c["internal_payable"] = payable
    c["disputed_amount"] = c["claimed_commission"] - c["internal_payable"]

    claimed_players = set(c.loc[c["status"].isin(PAYABLE_STATUSES), "player_id"].dropna())
    unclaimed = cpa_payouts[
        (cpa_payouts["cpa_payout"] > 0)
        & ~cpa_payouts["player_id"].isin(claimed_players)
        & ~cpa_payouts["player_id"].isin(held_players)
        & cpa_payouts["affiliate_id"].isin(claims["affiliate_id"].unique())
    ].assign(status="UNCLAIMED_BY_PARTNER")
    cols = [
        "claim_id",
        "affiliate_id",
        "report_month",
        "click_id",
        "player_id",
        "status",
        "detail",
        "internal_month",
        "claimed_commission",
        "internal_payable",
        "disputed_amount",
    ]
    return c[cols], unclaimed[["player_id", "affiliate_id", "month", "cpa_payout", "status"]]


def reconciliation_summary(recon: pd.DataFrame, unclaimed: pd.DataFrame) -> pd.DataFrame:
    s = (
        recon.groupby(["affiliate_id", "status"])
        .agg(
            claims=("claim_id", "size"),
            claimed=("claimed_commission", "sum"),
            payable=("internal_payable", "sum"),
        )
        .reset_index()
    )
    u = (
        unclaimed.groupby("affiliate_id")
        .agg(claims=("player_id", "size"), payable=("cpa_payout", "sum"))
        .reset_index()
        .assign(status="UNCLAIMED_BY_PARTNER", claimed=0.0)
    )
    return (
        pd.concat([s, u], ignore_index=True)
        .sort_values(["affiliate_id", "status"])
        .reset_index(drop=True)
    )
