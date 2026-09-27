"""Simulation, affiliate roster, contracts and unit-cost assumptions.

All numbers here parameterise SYNTHETIC DEMO DATA. They are plausible-looking
assumptions chosen to exercise the analytics, not estimates of any real market.
"""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import Contract, CPATerms, QFTDCriteria, RevShareTerms, Tier


@dataclass(frozen=True)
class SimConfig:
    seed: int = 42
    start: str = "2025-01-06"  # Monday; week 0
    n_weeks: int = 52  # acquisition window
    obs_end: str = "2026-03-31 23:59:59"  # last observed activity
    n_geos: int = 40
    scale: float = 1.0  # multiplies every arrival rate (tests use small values)
    organic_daily: float = 70.0
    brand_search_share: float = 0.6  # share of organic arrivals that search the brand
    bb_intercept_rate: float = 0.45  # share of brand searchers that click the brand bidder
    bb_incremental_ratio: float = 0.35  # truly new regs per intercepted reg
    # experiment design for the brand-bidding channel
    did_launch_week: int = 13
    did_early_geo_share: float = 0.3
    holdout_start_week: int = 26
    switchback_start_week: int = 39
    holdout_share: float = 0.3
    coupon_steal_organic: float = 0.06
    coupon_steal_seo: float = 0.10
    bonus_abuse_share: float = 0.015


@dataclass(frozen=True)
class KindProfile:
    """Behaviour of *legitimate* players from a traffic kind."""

    click_to_reg: float
    kyc_rate: float
    ftd_rate: float
    base_deposit: float
    retention: float  # monthly survival after FTD
    days_per_month: float
    vip_share: float
    casino_share: float
    welcome_optin: float


KIND_PROFILES: dict[str, KindProfile] = {
    "organic": KindProfile(1.0, 0.86, 0.46, 40, 0.64, 5.0, 0.03, 0.6, 0.5),
    "seo": KindProfile(0.12, 0.85, 0.42, 40, 0.62, 5.0, 0.03, 0.6, 0.55),
    "brand_bidding": KindProfile(0.35, 0.86, 0.46, 40, 0.64, 5.0, 0.03, 0.6, 0.5),
    "coupon": KindProfile(0.20, 0.80, 0.42, 30, 0.50, 4.0, 0.02, 0.6, 0.95),
    "streamer": KindProfile(0.06, 0.80, 0.33, 70, 0.58, 6.0, 0.12, 0.92, 0.6),
    "tipster": KindProfile(0.10, 0.85, 0.38, 35, 0.60, 6.0, 0.03, 0.1, 0.4),
    "incentivised": KindProfile(0.40, 0.75, 0.25, 20, 0.30, 2.0, 0.0, 0.7, 0.9),
    "bot_farm": KindProfile(0.05, 0.70, 0.20, 25, 0.35, 2.0, 0.0, 0.6, 0.8),
    "media": KindProfile(0.08, 0.80, 0.30, 30, 0.50, 4.0, 0.02, 0.6, 0.6),
}


@dataclass(frozen=True)
class AffiliateSpec:
    affiliate_id: str
    name: str
    kind: str
    daily_regs: float  # expected registrations/day (brand bidder: driven by mechanism)
    n_subs: int
    fraud_share: float = 0.0  # share of registrations that are fraudulent (by design)
    bursts_per_day: float = 0.0  # bot-farm registration bursts


AFFILIATES: tuple[AffiliateSpec, ...] = (
    AffiliateSpec("AFF01", "ReviewHub", "seo", 14, 6),
    AffiliateSpec("AFF02", "BetGuide", "seo", 10, 4),
    AffiliateSpec("AFF03", "BrandSearch Pro", "brand_bidding", 0, 3),
    AffiliateSpec("AFF04", "PromoCodes", "coupon", 6, 4),
    AffiliateSpec("AFF05", "HighRollerTV", "streamer", 4, 2),
    AffiliateSpec("AFF06", "SlotNights", "streamer", 6, 3),
    AffiliateSpec("AFF07", "TelegramTips", "tipster", 5, 5),
    AffiliateSpec("AFF08", "CashbackClub", "incentivised", 22, 6, fraud_share=0.65),
    AffiliateSpec("AFF09", "TrafficMax", "bot_farm", 3, 5, fraud_share=0.0, bursts_per_day=0.6),
    AffiliateSpec("AFF10", "AdNet Media", "media", 12, 8),
)

BRAND_BIDDER = "AFF03"
COUPON_AFFILIATE = "AFF04"
SEO_AFFILIATES = ("AFF01", "AFF02")

STANDARD_QFTD = QFTDCriteria(min_deposit=20, min_wagering=50, require_kyc=True, within_days=30)

CONTRACTS: dict[str, Contract] = {
    "AFF01": Contract(
        "AFF01",
        revshare=RevShareTerms(
            tiers=(Tier(0, 0.25), Tier(60, 0.30), Tier(120, 0.35)),
            tier_basis="ftd",
            negative_carryover=True,
            admin_fee=0.10,
            player_lifetime_months=24,
        ),
    ),
    "AFF02": Contract(
        "AFF02",
        cpa=CPATerms(50, QFTDCriteria(20, 100, True, 14)),
        revshare=RevShareTerms(tiers=(Tier(0, 0.20),), admin_fee=0.05),
    ),
    "AFF03": Contract("AFF03", cpa=CPATerms(75, STANDARD_QFTD)),
    "AFF04": Contract("AFF04", cpa=CPATerms(60, STANDARD_QFTD)),
    "AFF05": Contract(
        "AFF05",
        revshare=RevShareTerms(
            tiers=(Tier(0, 0.40),), negative_carryover=False, player_lifetime_months=12
        ),
    ),
    "AFF06": Contract(
        "AFF06",
        cpa=CPATerms(60, STANDARD_QFTD),
        revshare=RevShareTerms(tiers=(Tier(0, 0.25),), admin_fee=0.10),
    ),
    "AFF07": Contract(
        "AFF07",
        revshare=RevShareTerms(
            tiers=(Tier(0, 0.30), Tier(5000, 0.35)), tier_basis="net_revenue", admin_fee=0.05
        ),
    ),
    "AFF08": Contract("AFF08", cpa=CPATerms(120, STANDARD_QFTD)),
    "AFF09": Contract("AFF09", cpa=CPATerms(110, STANDARD_QFTD, monthly_cap=250)),
    "AFF10": Contract("AFF10", cpa=CPATerms(60, QFTDCriteria(30, 150, True, 14))),
}


@dataclass(frozen=True)
class CostConfig:
    gaming_tax_rate: float = 0.10  # on GGR (linear, negative GGR gives credit)
    card_fee_pct: float = 0.025
    card_fee_fixed: float = 0.20
    ewallet_fee_pct: float = 0.030
    open_banking_fee_fixed: float = 0.50
    withdrawal_fee: float = 1.00
    chargeback_fee: float = 15.0
    kyc_check_cost: float = 1.50
    support_cost_per_active_month: float = 2.00


COSTS = CostConfig()

# fraud operating threshold (score >= threshold -> hold payout, manual review)
FRAUD_THRESHOLD = 50
