import pandas as pd
import pytest

from affmeasure.crm import GuardrailPolicy, apply_guardrails

AS_OF = pd.Timestamp("2026-03-31 12:00")


def _setup():
    ids = ["ok", "se", "se_expired", "cool", "noconsent", "missing", "chcap", "globalcap", "gap"]
    consents = pd.DataFrame(
        {
            "player_id": [i for i in ids if i != "missing"],
            "email": [i != "noconsent" for i in ids if i != "missing"],
            "sms": False,
            "push": True,
        }
    )
    exclusions = pd.DataFrame(
        {
            "player_id": ["se", "se_expired", "cool"],
            "exclusion_type": ["self_exclusion", "self_exclusion", "cool_off"],
            "start_ts": pd.to_datetime(["2025-06-01", "2025-01-01", "2026-03-20"]),
            "end_ts": pd.to_datetime([None, "2025-07-01", "2026-04-20"]),
        }
    )
    d = pd.Timedelta
    sends = pd.DataFrame(
        [
            ("chcap", "email", AS_OF - d(days=1)),
            ("chcap", "email", AS_OF - d(days=2)),
            ("chcap", "email", AS_OF - d(days=3)),
            ("globalcap", "push", AS_OF - d(days=1)),
            ("globalcap", "push", AS_OF - d(days=2)),
            ("globalcap", "push", AS_OF - d(days=3)),
            ("globalcap", "push", AS_OF - d(days=4)),
            ("globalcap", "email", AS_OF - d(days=5)),
            ("globalcap", "email", AS_OF - d(days=6)),
            ("gap", "push", AS_OF - d(hours=3)),
            ("ok", "email", AS_OF - d(days=10)),  # outside the 7-day window
        ],
        columns=["player_id", "channel", "sent_ts"],
    )
    return pd.DataFrame({"player_id": ids}), consents, exclusions, sends


def test_each_suppression_reason():
    aud, consents, ex, sends = _setup()
    res = apply_guardrails(aud, "email", AS_OF, consents, ex, sends)
    reasons = res.suppressed.set_index("player_id")["reason"].to_dict()
    assert reasons == {
        "se": "SELF_EXCLUDED",
        "cool": "COOL_OFF",
        "noconsent": "NO_CONSENT",
        "missing": "NO_CONSENT",  # fail closed
        "chcap": "CHANNEL_CAP",
        "globalcap": "GLOBAL_CAP",
        "gap": "MIN_GAP",
    }
    assert set(res.export["player_id"]) == {"ok", "se_expired"}
    assert res.summary()["EXPORTED"] == 2


def test_self_exclusion_takes_precedence_over_everything():
    aud = pd.DataFrame({"player_id": ["p"]})
    consents = pd.DataFrame({"player_id": ["p"], "email": [False], "sms": [False], "push": [False]})
    ex = pd.DataFrame(
        {
            "player_id": ["p", "p"],
            "exclusion_type": ["cool_off", "self_exclusion"],
            "start_ts": [AS_OF - pd.Timedelta(days=1)] * 2,
            "end_ts": [pd.NaT, pd.NaT],
        }
    )
    sends = pd.DataFrame(columns=["player_id", "channel", "sent_ts"]).astype(
        {"sent_ts": "datetime64[ns]"}
    )
    res = apply_guardrails(aud, "email", AS_OF, consents, ex, sends)
    assert res.suppressed["reason"].tolist() == ["SELF_EXCLUDED"]


def test_policy_caps_are_configurable_and_duplicates_dropped():
    aud, consents, ex, sends = _setup()
    loose = GuardrailPolicy(
        channel_caps_7d={"email": 10, "sms": 1, "push": 4},
        global_cap_7d=100,
        min_hours_between_contacts=0,
    )
    res = apply_guardrails(pd.concat([aud, aud]), "email", AS_OF, consents, ex, sends, loose)
    assert {"chcap", "globalcap", "gap"} <= set(res.export["player_id"])
    assert res.export["player_id"].is_unique


def test_unknown_channel_rejected():
    aud, consents, ex, sends = _setup()
    with pytest.raises(ValueError):
        apply_guardrails(aud, "telegram", AS_OF, consents, ex, sends)
