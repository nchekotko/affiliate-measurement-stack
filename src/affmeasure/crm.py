"""CRM guardrails applied BEFORE a campaign audience is exported.

Order of checks (first failure is the suppression reason):
SELF_EXCLUDED -> COOL_OFF -> NO_CONSENT -> CHANNEL_CAP -> GLOBAL_CAP -> MIN_GAP.
Missing consent records fail closed (treated as no consent).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

CHANNELS = ("email", "sms", "push")
REASONS = ("SELF_EXCLUDED", "COOL_OFF", "NO_CONSENT", "CHANNEL_CAP", "GLOBAL_CAP", "MIN_GAP")


@dataclass(frozen=True)
class GuardrailPolicy:
    channel_caps_7d: dict[str, int] = field(
        default_factory=lambda: {"email": 3, "sms": 1, "push": 4}
    )
    global_cap_7d: int = 6
    min_hours_between_contacts: int = 12


@dataclass
class GuardrailResult:
    export: pd.DataFrame  # player_id, channel
    suppressed: pd.DataFrame  # player_id, channel, reason

    def summary(self) -> pd.Series:
        counts = self.suppressed["reason"].value_counts().reindex(list(REASONS), fill_value=0)
        counts["EXPORTED"] = len(self.export)
        return counts


def apply_guardrails(
    audience: pd.DataFrame,
    channel: str,
    as_of: pd.Timestamp,
    consents: pd.DataFrame,
    exclusions: pd.DataFrame,
    sends: pd.DataFrame,
    policy: GuardrailPolicy | None = None,
) -> GuardrailResult:
    """audience: player_id; consents: player_id + one bool column per channel;
    exclusions: player_id, exclusion_type (self_exclusion|cool_off), start_ts, end_ts (NaT = permanent);
    sends: player_id, channel, sent_ts."""
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel {channel!r}")
    policy = policy or GuardrailPolicy()
    as_of = pd.Timestamp(as_of)
    a = audience[["player_id"]].drop_duplicates().reset_index(drop=True)

    ex = exclusions[
        (exclusions["start_ts"] <= as_of)
        & (exclusions["end_ts"].isna() | (exclusions["end_ts"] > as_of))
    ]
    self_ex = set(ex.loc[ex["exclusion_type"] == "self_exclusion", "player_id"])
    cool = set(ex.loc[ex["exclusion_type"] == "cool_off", "player_id"])
    consent = consents.set_index("player_id")[channel].astype(bool).to_dict()

    window = sends[(sends["sent_ts"] > as_of - pd.Timedelta(days=7)) & (sends["sent_ts"] <= as_of)]
    ch_cnt = window[window["channel"] == channel].groupby("player_id").size().to_dict()
    all_cnt = window.groupby("player_id").size().to_dict()
    last = window.groupby("player_id")["sent_ts"].max().to_dict()
    gap = pd.Timedelta(hours=policy.min_hours_between_contacts)
    cap = policy.channel_caps_7d.get(channel, 0)

    reasons: list[str | None] = []
    for pid in a["player_id"]:
        if pid in self_ex:
            reasons.append("SELF_EXCLUDED")
        elif pid in cool:
            reasons.append("COOL_OFF")
        elif not consent.get(pid, False):
            reasons.append("NO_CONSENT")
        elif ch_cnt.get(pid, 0) >= cap:
            reasons.append("CHANNEL_CAP")
        elif all_cnt.get(pid, 0) >= policy.global_cap_7d:
            reasons.append("GLOBAL_CAP")
        elif pid in last and as_of - last[pid] < gap:
            reasons.append("MIN_GAP")
        else:
            reasons.append(None)
    a["reason"] = reasons
    a["channel"] = channel
    export = a[a["reason"].isna()][["player_id", "channel"]].reset_index(drop=True)
    suppressed = a[a["reason"].notna()][["player_id", "channel", "reason"]].reset_index(drop=True)
    return GuardrailResult(export, suppressed)
