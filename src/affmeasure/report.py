"""Write reports/summary.md, CSV exports and charts from a PipelineResult."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .charts import make_all
from .config import BRAND_BIDDER, CONTRACTS, FRAUD_THRESHOLD
from .fraud import RULES
from .pipeline import WHATIF_AFFILIATE, PipelineResult
from .stats import Estimate


def md_table(
    df: pd.DataFrame,
    floatfmt: str = ",.1f",
    pct_cols: tuple[str, ...] = (),
    formats: dict[str, str] | None = None,
) -> str:
    """Render a DataFrame as a GitHub markdown table (no tabulate dependency)."""
    formats = formats or {}

    def fmt(col: str, kind: str, v) -> str:
        if v is None or (not isinstance(v, str) and pd.isna(v)):
            return "—"
        if col in pct_cols:
            return f"{100 * float(v):.1f}%"
        if col in formats:
            return format(float(v), formats[col])
        if kind in "iub" or isinstance(v, (int, np.integer)):
            return f"{int(v):,}"
        if isinstance(v, (float, np.floating)):
            return format(float(v), floatfmt)
        return str(v)

    cols = list(df.columns)
    kinds = {c: df[c].dtype.kind for c in cols}
    values = {c: df[c].tolist() for c in cols}
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for i in range(len(df)):
        lines.append("| " + " | ".join(fmt(c, kinds[c], values[c][i]) for c in cols) + " |")
    return "\n".join(lines)


def _ci(e: Estimate, digits: int = 2) -> str:
    return f"{e.estimate:.{digits}f} [{e.ci_low:.{digits}f}; {e.ci_high:.{digits}f}], p={e.p_value:.3f}"


def _eur(v: float) -> str:
    return f"{v:,.0f} EUR".replace(",", " ")


def cohort_window_summary(cw: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for aff, g in cw.groupby("affiliate_id"):
        row = {"affiliate_id": aff}
        for col in (
            "ngr_d7",
            "ngr_d30",
            "ngr_d90",
            "retention_d8_30",
            "retention_d31_90",
            "chargeback_rate_d90",
        ):
            ok = g[g[col].notna()]
            row[col] = np.average(ok[col], weights=ok["n_ftd"]) if len(ok) else np.nan
        row["cohorts_d90"] = int(g["ngr_d90"].notna().sum())
        rows.append(row)
    return pd.DataFrame(rows)


def write_report(r: PipelineResult, out_dir: str | Path = "reports") -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    charts = make_all(r, out / "charts", FRAUD_THRESHOLD)
    names = r.data.tables["affiliates"].set_index("affiliate_id")["name"].to_dict()
    names["ORGANIC"] = "(organic)"
    cfg = r.data.config

    # ------------------------------------------------------------------ CSV exports
    inv = r.contracts.invoices
    inv.round(2).to_csv(out / "invoices.csv", index=False)
    r.cm_affiliate.round(2).to_csv(out / "contribution_margin_affiliate.csv", index=False)
    r.cm_cohort.round(2).to_csv(out / "contribution_margin_cohort.csv", index=False)
    r.cohort_windows.round(4).to_csv(out / "cohort_windows.csv", index=False)
    r.fraud_sim.round(4).to_csv(out / "fraud_threshold_simulation.csv", index=False)
    r.queue.round(2).to_csv(out / "review_queue.csv", index=False)
    r.recon_summary.round(2).to_csv(out / "reconciliation_summary.csv", index=False)
    r.recon[r.recon["status"] != "MATCHED"].round(2).to_csv(
        out / "reconciliation_mismatches.csv", index=False
    )
    r.ltv_backtest.round(4).to_csv(out / "ltv_backtest.csv", index=False)
    crm_rows = pd.DataFrame({ch: g.summary() for ch, g in r.crm.items()})
    crm_rows.to_csv(out / "crm_suppression_summary.csv")
    r.crm["email"].export.to_csv(out / "crm_audience_export_email.csv", index=False)

    # ------------------------------------------------------------------ sections
    t = r.data.tables
    n_players = len(t["players"])
    s: list[str] = []
    s.append("# Affiliate measurement stack — summary\n")
    s.append(
        "> **SYNTHETIC DEMO DATA.** Все числа ниже получены на сгенерированном датасете "
        f"(seed={cfg.seed}) и описывают только механику симуляции, а не реальный рынок, "
        "оператора или партнёров.\n"
    )
    s.append(
        f"Период привлечения: {cfg.n_weeks} нед. с {cfg.start}; активность наблюдается до {cfg.obs_end[:10]}. "
        f"Игроков: {n_players:,}, кликов: {len(t['clicks']):,}, депозитов: {len(t['deposits']):,}, "
        f"player-day ставок: {len(t['bets']):,}, гео: {cfg.n_geos}.\n"
    )

    # 1 funnel
    f = r.funnel.copy()
    f.insert(1, "name", f["affiliate_id"].map(names))
    s.append("## 1. Воронка click → registration → KYC → FTD\n")
    s.append(
        md_table(
            f[
                [
                    "affiliate_id",
                    "name",
                    "clicks",
                    "registrations",
                    "kyc_passed",
                    "ftd",
                    "click_to_reg",
                    "reg_to_kyc",
                    "reg_to_ftd",
                    "avg_ftd_amount",
                ]
            ],
            pct_cols=("click_to_reg", "reg_to_kyc", "reg_to_ftd"),
        )
    )
    s.append("")

    # 2 cohorts
    cws = cohort_window_summary(r.cohort_windows)
    cb = r.chargebacks.set_index("affiliate_id")["cb_rate_count"]
    cws["cb_rate_all"] = cws["affiliate_id"].map(cb)
    s.append("## 2. FTD-когорты с фиксированными окнами\n")
    s.append(
        "NGR на FTD за D7/D30/D90 от даты FTD; окно учитывается только для когорт, у которых оно полностью "
        "наблюдено. Retention = доля FTD с активностью (ставкой) в днях 8–30 и 31–90. Среднее по когортам, веса = число FTD.\n"
    )
    s.append(
        md_table(
            cws,
            pct_cols=("retention_d8_30", "retention_d31_90", "chargeback_rate_d90", "cb_rate_all"),
        )
    )
    s.append("")

    # 3 contracts
    tot = (
        inv.groupby("affiliate_id")
        .agg(
            contract=("contract", "first"),
            ftd=("ftd", "sum"),
            qftd=("qftd", "sum"),
            qftd_over_cap=("qftd_over_cap", "sum"),
            cpa_payable=("cpa_payable", "sum"),
            rs_payable=("rs_payable", "sum"),
            total_payable=("total_payable", "sum"),
        )
        .reset_index()
    )
    tot["qftd_rate"] = tot["qftd"] / tot["ftd"].replace(0, np.nan)
    tot.loc[tot["contract"] == "RevShare", "qftd_rate"] = np.nan
    s.append("## 3. Контракты и инвойсы\n")
    s.append(
        f"Месячные инвойсы: `invoices.csv` ({len(inv)} строк, {inv['month'].nunique()} месяцев).\n"
    )
    s.append(md_table(tot, pct_cols=("qftd_rate",)))
    s.append("")
    neg = inv[(inv["net_revenue_gross"] < 0) & (inv["contract"] != "CPA")]
    if len(neg):
        s.append("Отрицательные месяцы RevShare (net revenue < 0) и перенос:\n")
        s.append(
            md_table(
                neg[
                    [
                        "affiliate_id",
                        "month",
                        "contract",
                        "net_revenue_gross",
                        "carryover_in",
                        "net_revenue",
                        "rs_payable",
                        "carryover_out",
                    ]
                ]
            )
        )
        s.append("")
    w = r.carryover_whatif
    pay_cols = [c for c in w.columns if c.startswith("payable_")]
    terms = CONTRACTS[WHATIF_AFFILIATE].revshare
    contracted = "on" if terms and terms.negative_carryover else "off"
    alt = "off" if contracted == "on" else "on"
    diff = w[f"payable_carryover_{contracted}"].sum() - w[f"payable_carryover_{alt}"].sum()
    s.append(
        f"What-if для {WHATIF_AFFILIATE} ({names[WHATIF_AFFILIATE]}, RevShare, negative carryover по контракту = **{contracted}**): "
        f"выплаты за период {_eur(w[f'payable_carryover_{contracted}'].sum())} против "
        f"{_eur(w[f'payable_carryover_{alt}'].sum())} при carryover={alt}; разница {_eur(diff)}.\n"
    )
    wm = w[(w["net_revenue_gross"] < 0) | (w[pay_cols[0]] != w[pay_cols[1]])]
    if len(wm):
        s.append(md_table(wm))
        s.append("")

    # 4 CM
    cm = r.cm_affiliate.copy()
    cm.insert(1, "name", cm["affiliate_id"].map(names))
    s.append("## 4. Contribution margin\n")
    s.append(
        "`CM = NGR − affiliate payout − bonus cost − payment cost − chargebacks − variable ops`, "
        "где NGR = GGR − gaming tax − provider fees (бонусы — отдельной строкой). Всё время наблюдения (to date).\n"
    )
    s.append(
        md_table(
            cm[
                [
                    "affiliate_id",
                    "name",
                    "ftd",
                    "ngr",
                    "affiliate_payout",
                    "bonus_cost",
                    "payment_cost",
                    "chargeback_cost",
                    "variable_ops",
                    "cm",
                    "cm_per_ftd",
                    "payout_per_ftd",
                    "cm_after_fraud_hold",
                ]
            ],
            floatfmt=",.0f",
        )
    )
    s.append("")
    aff_only = cm[cm["affiliate_id"] != "ORGANIC"]
    top = aff_only.sort_values("ftd", ascending=False).iloc[0]
    s.append(
        f"**Высокий FTD volume ≠ прибыль.** {top['affiliate_id']} ({top['name']}) — крупнейший партнёр по FTD "
        f"({int(top['ftd']):,}), но CM = {_eur(top['cm'])} ({top['cm_per_ftd']:.0f} EUR на FTD): NGR на FTD "
        f"{top['ngr'] / top['ftd']:.1f} EUR при выплате {top['payout_per_ftd']:.0f} EUR на FTD и бонусах "
        f"{top['bonus_cost'] / top['ftd']:.0f} EUR на FTD. Даже если удержать выплаты по игрокам, отмеченным "
        f"fraud-правилами, CM остаётся {_eur(top['cm_after_fraud_hold'])}.\n"
    )
    s.append(f"![CM by affiliate](charts/{charts['cm_by_affiliate']})\n")
    d90 = r.cm_affiliate_d90[
        ["affiliate_id", "ftd", "ngr", "affiliate_payout", "cm", "cm_per_ftd", "payout_per_ftd"]
    ].copy()
    s.append(
        "CM в фиксированном окне D90 (только игроки с полностью наблюдённым окном; RevShare-выплаты — по месяцам, начавшимся до FTD+90):\n"
    )
    s.append(md_table(d90, floatfmt=",.1f"))
    s.append("")

    # 5 incrementality
    ho, sw = r.holdout, r.switchback
    s.append(f"## 5. Incrementality: brand bidding ({BRAND_BIDDER} {names[BRAND_BIDDER]})\n")
    s.append(
        "Outcome — FTD (в течение 14 дней после регистрации) по brand-search пути: ORGANIC + brand bidder + coupon, "
        "на уровне geo × week. Эффект = сколько FTD в неделю в гео добавляет включённый brand bidder.\n"
    )
    s.append(
        f"- Last-click: brand bidder получает **{ho.lastclick_per_geo_week:.2f}** FTD на geo-week (в гео, где он включён)."
    )
    s.append(
        f"- Geo holdout ({ho.n_on} гео on / {ho.n_off} off, 13 недель): diff in means {_ci(ho.raw)}; CUPED (ковариата — pre-period): {_ci(ho.cuped)} → incremental share ≈ {ho.incremental_share:.0%}."
    )
    s.append(
        f"- Switchback (рандомизация geo × week, {sw.n_cells} ячеек, TWFE, cluster SE по гео): {_ci(sw.effect)} → incremental share ≈ {sw.incremental_share:.0%}."
    )
    for d in (r.did_level, r.did_log):
        ok = "не отвергается" if d.parallel_trends_ok else "ОТВЕРГАЕТСЯ"
        s.append(
            f"- DiD ({d.scale}; ранний запуск в {round(cfg.n_geos * cfg.did_early_geo_share)} крупнейших гео — неслучайно): ATT {_ci(d.att, 3)}; "
            f"pre-trend slope {_ci(d.pretrend_slope, 3)}; joint test leads p={d.leads_wald_p:.3f} → parallel trends {ok}."
        )
    s.append(
        f"- Истинная доля инкрементальных FTD brand bidder в генераторе (ground truth, только для проверки): **{r.truth_incremental_share:.0%}**.\n"
    )
    s.append(f"![incrementality](charts/{charts['incrementality']})\n")
    s.append(f"![event study](charts/{charts['did_event_study']})\n")
    s.append(
        "Last-click vs incremental экономика brand bidder (CM до выплаты на FTD берётся из раздела 4):\n"
    )
    s.append(
        md_table(
            r.bb_contrast[
                [
                    "view",
                    "lastclick_ftd",
                    "incremental_share",
                    "incremental_ftd",
                    "payout",
                    "cost_per_lastclick_ftd",
                    "cost_per_incremental_ftd",
                    "lastclick_cm",
                    "incremental_cm",
                ]
            ],
            floatfmt=",.0f",
            pct_cols=("incremental_share",),
            formats={"cost_per_lastclick_ftd": ",.1f", "cost_per_incremental_ftd": ",.1f"},
        )
    )
    s.append("")
    s.append(
        "Overlap: доля FTD партнёра, у которых за 30 дней до регистрации был organic/brand-search touchpoint или клик другого партнёра (device stitching):\n"
    )
    ov = r.overlap.copy()
    ov.insert(1, "name", ov["affiliate_id"].map(names))
    s.append(
        md_table(
            ov,
            pct_cols=(
                "share_organic_touch",
                "share_brand_search",
                "share_other_affiliate",
                "share_any_overlap",
            ),
        )
    )
    s.append("")

    # 6 LTV
    s.append("## 6. LTV forecast и backtest\n")
    s.append(
        "Backtest без look-ahead: для каждой когорты (сегмент ALL) прогноз cumulative NGR/FTD на возраст M5 по первым 3 месяцам; "
        "chain ladder использует только ячейки других когорт, доступные на дату прогноза.\n"
    )
    s.append(md_table(r.ltv_backtest_summary, floatfmt=".3f", pct_cols=("mape", "bias")))
    s.append("")
    fc = r.ltv_forecast.copy()
    cpa = {k: (c.cpa.amount if c.cpa else np.nan) for k, c in CONTRACTS.items()}
    fc["cpa_amount"] = fc["segment"].map(cpa)
    fc["ftd"] = fc["ftd"].astype(int)
    fc.insert(1, "name", fc["segment"].map(names).fillna(""))
    s.append(
        "Прогноз LTV12 (NGR на FTD, chain ladder; факторы сегмента, если за ними ≥3 когорт, иначе ALL):\n"
    )
    s.append(md_table(fc))
    s.append("")
    s.append(f"![LTV curves](charts/{charts['ltv_curves']})\n")

    # 7 fraud
    s.append("## 7. Fraud scoring (интерпретируемый)\n")
    s.append("Правила и веса (score = сумма весов сработавших правил, максимум 100):\n")
    rs = r.rule_stats.copy()
    rs["description"] = rs["rule"].map({x.code: x.description for x in RULES})
    s.append(
        md_table(
            rs[["rule", "weight", "description", "fires", "precision", "recall"]], floatfmt=".3f"
        )
    )
    s.append("")
    s.append(
        "Precision/recall считаются по ground truth генератора; scorer к меткам доступа не имеет.\n"
    )
    sim = r.fraud_sim[r.fraud_sim["threshold"].isin([20, 30, 40, 50, 60, 80])]
    s.append(
        md_table(
            sim,
            floatfmt=",.0f",
            formats={"precision": ".3f", "recall": ".3f", "fraud_payout_share_caught": ".3f"},
        )
    )
    s.append("")
    op = r.fraud_sim.set_index("threshold").loc[FRAUD_THRESHOLD]
    s.append(
        f"Рабочий порог {FRAUD_THRESHOLD}: в manual review queue {len(r.queue):,} игроков (`review_queue.csv`, сортировка по score × payout at stake); "
        f"precision {op['precision']:.3f}, recall {op['recall']:.3f}; удержано выплат по fraud {_eur(op['fraud_payout_avoided'])}, "
        f"ошибочно удержано у честных игроков {_eur(op['good_payout_held'])}.\n"
    )
    s.append(f"![fraud thresholds](charts/{charts['fraud_thresholds']})\n")
    roll = r.fraud_rollup[r.fraud_rollup["ftd"] >= 30].head(8)
    s.append("Sub-ID с наибольшей долей отмеченных FTD:\n")
    s.append(md_table(roll, pct_cols=("flagged_share",)))
    s.append("")

    # 8 reconciliation
    s.append("## 8. Сверка инвойсов (partner report vs internal)\n")
    rsum = (
        r.recon_summary.groupby("status")
        .agg(claims=("claims", "sum"), claimed=("claimed", "sum"), payable=("payable", "sum"))
        .reset_index()
    )
    rsum["disputed"] = rsum["claimed"] - rsum["payable"]
    s.append(
        "Партнёрский отчёт (синтетический) считает FTD по своему трекингу, режет месяц по UTC+3 и содержит дубликаты, "
        "неизвестные click_id и устаревшие ставки. Каждая претензия получает первую сработавшую причину.\n"
    )
    s.append(md_table(rsum, floatfmt=",.0f"))
    s.append("")
    by_aff = (
        r.recon.groupby("affiliate_id")
        .agg(
            claims=("claim_id", "size"),
            claimed=("claimed_commission", "sum"),
            payable=("internal_payable", "sum"),
        )
        .reset_index()
    )
    by_aff["disputed_share"] = 1 - by_aff["payable"] / by_aff["claimed"]
    s.append(md_table(by_aff, floatfmt=",.0f", pct_cols=("disputed_share",)))
    s.append("")

    # 9 CRM
    s.append("## 9. CRM guardrails\n")
    s.append(
        f"Кампания реактивации: {r.crm_audience_size:,} депозиторов без ставок 30–120 дней на {cfg.obs_end[:10]}. "
        "Перед экспортом аудитории применяются self-exclusion, cool-off, согласие по каналу (fail closed), частотные лимиты за 7 дней и минимальный интервал.\n"
    )
    s.append(md_table(crm_rows.reset_index().rename(columns={"index": "reason"})))
    s.append("")
    s.append("Файлы: `crm_suppression_summary.csv`, `crm_audience_export_email.csv`.\n")

    path = out / "summary.md"
    path.write_text("\n".join(s), encoding="utf-8")
    return path
