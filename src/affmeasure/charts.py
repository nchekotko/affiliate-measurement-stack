"""Static PNG charts for the report (matplotlib, Agg backend)."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from .pipeline import PipelineResult  # noqa: E402

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e4e3df"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
POS, NEG = "#2a78d6", "#eb6834"


def _style(ax: plt.Axes, title: str, xlabel: str = "", ylabel: str = "") -> None:
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", color=INK, fontsize=11, fontweight="bold")
    ax.set_xlabel(xlabel, color=INK2, fontsize=9)
    ax.set_ylabel(ylabel, color=INK2, fontsize=9)
    ax.tick_params(colors=INK2, labelsize=8)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(MUTED)
    ax.grid(axis="both", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def _fig(w: float = 8, h: float = 4.5, ncols: int = 1):
    fig, axes = plt.subplots(1, ncols, figsize=(w, h), facecolor=SURFACE)
    return fig, axes


def _save(fig, path: Path) -> None:
    fig.text(0.99, 0.01, "synthetic demo data", ha="right", va="bottom", fontsize=7, color=MUTED)
    fig.tight_layout()
    fig.savefig(path, dpi=130, facecolor=SURFACE)
    plt.close(fig)


def cm_by_affiliate(r: PipelineResult, path: Path) -> None:
    d = r.cm_affiliate[r.cm_affiliate["affiliate_id"] != "ORGANIC"].sort_values("cm")
    names = r.data.tables["affiliates"].set_index("affiliate_id")["name"]
    labels = [f"{a} {names.get(a, '')}" for a in d["affiliate_id"]]
    fig, (ax1, ax2) = _fig(11, 4.8, 2)
    colors = [POS if v >= 0 else NEG for v in d["cm"]]
    ax1.barh(labels, d["cm"] / 1000, color=colors, height=0.6)
    ax1.axvline(0, color=MUTED, linewidth=0.8)
    _style(ax1, "Contribution margin to date, kEUR", "kEUR")
    ax1.grid(axis="y", visible=False)
    ax2.barh(labels, d["ftd"], color=SERIES[0], height=0.6)
    _style(ax2, "FTD volume (last-click)", "FTDs")
    ax2.grid(axis="y", visible=False)
    ax2.set_yticklabels([])
    _save(fig, path)


def incrementality(r: PipelineResult, path: Path) -> None:
    ho, sw = r.holdout, r.switchback
    rows = [
        ("Last-click FTD\n(brand bidder)", ho.lastclick_per_geo_week, None, None),
        ("Geo holdout\n(diff in means)", ho.raw.estimate, ho.raw.ci_low, ho.raw.ci_high),
        ("Geo holdout\n(CUPED)", ho.cuped.estimate, ho.cuped.ci_low, ho.cuped.ci_high),
        ("Switchback\n(TWFE)", sw.effect.estimate, sw.effect.ci_low, sw.effect.ci_high),
    ]
    fig, ax = _fig(8.5, 4.5)
    x = np.arange(len(rows))
    vals = [v for _, v, _, _ in rows]
    colors = [MUTED] + [SERIES[0]] * 3
    ax.bar(x, vals, color=colors, width=0.55)
    for i, (_, v, lo, hi) in enumerate(rows):
        if lo is not None:
            ax.errorbar(i, v, yerr=[[v - lo], [hi - v]], color=INK, capsize=4, linewidth=1.2)
    truth = r.truth_incremental_share * ho.lastclick_per_geo_week
    ax.axhline(truth, color=SERIES[1], linestyle="--", linewidth=1.2)
    ax.text(
        0.5,
        truth,
        " true incremental\n (generator)",
        color=INK2,
        fontsize=8,
        va="bottom",
        ha="center",
    )
    ax.axhline(0, color=MUTED, linewidth=0.8)
    ax.set_xticks(x, [n for n, *_ in rows])
    _style(
        ax,
        "Brand bidder: FTDs per geo-week, last-click vs incremental (95% CI)",
        "",
        "FTDs per geo-week",
    )
    ax.grid(axis="x", visible=False)
    _save(fig, path)


def did_event_study(r: PipelineResult, path: Path) -> None:
    fig, ax = _fig(8, 4)
    es = r.did_log.event_study
    ax.errorbar(
        es["rel_bin"] * 2,
        es["estimate"],
        yerr=[es["estimate"] - es["ci_low"], es["ci_high"] - es["estimate"]],
        fmt="o",
        color=SERIES[0],
        ecolor=INK2,
        capsize=3,
        markersize=5,
    )
    ax.axhline(0, color=MUTED, linewidth=0.8)
    ax.axvline(-0.5, color=SERIES[1], linestyle="--", linewidth=1)
    ax.text(-0.3, ax.get_ylim()[1] * 0.9, "launch", color=INK2, fontsize=8)
    _style(
        ax,
        "DiD event study, log(1+FTD): pre-launch leads = parallel-trends check",
        "weeks relative to launch (2-week bins)",
        "coefficient",
    )
    _save(fig, path)


def ltv_curves(
    r: PipelineResult,
    path: Path,
    segments=("ORGANIC", "AFF03", "AFF06", "AFF08", "AFF09"),
    max_age: int = 8,
) -> None:
    c = r.ltv_curve
    fig, ax = _fig(8.5, 4.5)
    names = r.data.tables["affiliates"].set_index("affiliate_id")["name"]
    for i, seg in enumerate(segments):
        g = c[c["segment"] == seg]
        # balanced panel: only cohorts observed through age max_age, so the mix is fixed
        full = g.groupby("ftd_month")["age"].max()
        g = g[g["ftd_month"].isin(full[full >= max_age].index) & (g["age"] <= max_age)]
        tot = (
            g.assign(w=g["cum_ngr_per_ftd"] * g["n_ftd"])
            .groupby("age")
            .agg(w=("w", "sum"), n=("n_ftd", "sum"), k=("ftd_month", "nunique"))
        )
        ax.plot(
            tot.index,
            tot["w"] / tot["n"],
            color=SERIES[i],
            linewidth=2,
            marker="o",
            markersize=3,
            label=f"{seg} {names.get(seg, '')}".strip(),
        )
    ax.axhline(0, color=MUTED, linewidth=0.8)
    ax.legend(frameon=False, fontsize=8, labelcolor=INK2)
    _style(
        ax,
        f"Cumulative NGR per FTD, cohorts observed through M{max_age} (balanced)",
        "age, 30-day periods since FTD",
        "EUR per FTD",
    )
    _save(fig, path)


def fraud_thresholds(r: PipelineResult, path: Path, threshold: int) -> None:
    s = r.fraud_sim
    fig, (ax1, ax2) = _fig(11, 4.2, 2)
    ax1.plot(s["threshold"], s["precision"], color=SERIES[0], linewidth=2, label="precision")
    ax1.plot(s["threshold"], s["recall"], color=SERIES[1], linewidth=2, label="recall")
    ax1.axvline(threshold, color=MUTED, linestyle="--", linewidth=1)
    ax1.set_ylim(0, 1.05)
    ax1.legend(frameon=False, fontsize=8, labelcolor=INK2)
    _style(ax1, "Precision / recall vs score threshold", "threshold (score >= t is held)", "")
    ax2.plot(
        s["threshold"],
        s["fraud_payout_avoided"] / 1000,
        color=SERIES[2],
        linewidth=2,
        label="fraud payout avoided",
    )
    ax2.plot(
        s["threshold"],
        s["good_payout_held"] / 1000,
        color=SERIES[1],
        linewidth=2,
        label="good payout wrongly held",
    )
    ax2.axvline(threshold, color=MUTED, linestyle="--", linewidth=1)
    ax2.legend(frameon=False, fontsize=8, labelcolor=INK2)
    _style(ax2, "Counterfactual savings, kEUR", "threshold", "kEUR")
    _save(fig, path)


def make_all(r: PipelineResult, out_dir: Path, threshold: int) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "cm_by_affiliate": "cm_by_affiliate.png",
        "incrementality": "incrementality.png",
        "did_event_study": "did_event_study.png",
        "ltv_curves": "ltv_curves.png",
        "fraud_thresholds": "fraud_thresholds.png",
    }
    cm_by_affiliate(r, out_dir / files["cm_by_affiliate"])
    incrementality(r, out_dir / files["incrementality"])
    did_event_study(r, out_dir / files["did_event_study"])
    ltv_curves(r, out_dir / files["ltv_curves"])
    fraud_thresholds(r, out_dir / files["fraud_thresholds"], threshold)
    return files
