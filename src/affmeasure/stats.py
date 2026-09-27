"""Small, dependency-light statistical helpers (Welch, OLS with cluster-robust SE)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import stats


@dataclass(frozen=True)
class Estimate:
    estimate: float
    se: float
    ci_low: float
    ci_high: float
    p_value: float
    df: float

    def as_dict(self) -> dict[str, float]:
        return {
            "estimate": self.estimate,
            "se": self.se,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "p_value": self.p_value,
        }


def _from_t(est: float, se: float, df: float, alpha: float) -> Estimate:
    if se <= 0 or not np.isfinite(se):
        return Estimate(est, float("nan"), float("nan"), float("nan"), float("nan"), df)
    t = stats.t.ppf(1 - alpha / 2, df)
    p = 2 * (1 - stats.t.cdf(abs(est / se), df))
    return Estimate(est, se, est - t * se, est + t * se, float(p), df)


def welch_diff(treat: np.ndarray, control: np.ndarray, alpha: float = 0.05) -> Estimate:
    """Difference in means (treat - control) with Welch-Satterthwaite CI."""
    a, b = np.asarray(treat, float), np.asarray(control, float)
    va, vb = a.var(ddof=1) / len(a), b.var(ddof=1) / len(b)
    se = float(np.sqrt(va + vb))
    df = (va + vb) ** 2 / (va**2 / (len(a) - 1) + vb**2 / (len(b) - 1))
    return _from_t(float(a.mean() - b.mean()), se, df, alpha)


def cuped(y: np.ndarray, x: np.ndarray) -> np.ndarray:
    """CUPED adjustment of outcome y by pre-period covariate x (variance reduction)."""
    y, x = np.asarray(y, float), np.asarray(x, float)
    theta = np.cov(y, x, ddof=1)[0, 1] / x.var(ddof=1)
    return y - theta * (x - x.mean())


@dataclass
class OLSResult:
    params: np.ndarray
    cov: np.ndarray
    names: list[str]
    n_clusters: int

    def estimate(self, name: str, alpha: float = 0.05) -> Estimate:
        i = self.names.index(name)
        return _from_t(
            float(self.params[i]), float(np.sqrt(self.cov[i, i])), self.n_clusters - 1, alpha
        )

    def wald(self, names: list[str]) -> tuple[float, float]:
        """Joint test that the named coefficients are all zero (F approx, df=(q, G-1))."""
        idx = [self.names.index(n) for n in names]
        b = self.params[idx]
        v = self.cov[np.ix_(idx, idx)]
        stat = float(b @ np.linalg.pinv(v) @ b) / len(idx)
        p = 1 - stats.f.cdf(stat, len(idx), self.n_clusters - 1)
        return stat, float(p)


def ols_cluster(y: np.ndarray, X: np.ndarray, names: list[str], clusters: np.ndarray) -> OLSResult:
    """OLS with CR1 cluster-robust covariance."""
    y = np.asarray(y, float)
    X = np.asarray(X, float)
    n, k = X.shape
    xtx_inv = np.linalg.pinv(X.T @ X)
    beta = xtx_inv @ X.T @ y
    u = y - X @ beta
    meat = np.zeros((k, k))
    groups = np.unique(clusters)
    for g in groups:
        m = clusters == g
        s = X[m].T @ u[m]
        meat += np.outer(s, s)
    G = len(groups)
    corr = G / (G - 1) * (n - 1) / max(n - k, 1)
    cov = corr * xtx_inv @ meat @ xtx_inv
    return OLSResult(beta, cov, names, G)


def fe_design(
    regressors: dict[str, np.ndarray], unit: np.ndarray, time: np.ndarray
) -> tuple[np.ndarray, list[str]]:
    """Design matrix: named regressors + unit and time fixed effects (first level dropped)."""
    cols = [np.asarray(v, float) for v in regressors.values()]
    names = list(regressors)
    for label, codes in (("unit", unit), ("time", time)):
        levels = np.unique(codes)
        for lv in levels[1:] if label == "time" else levels:
            cols.append((codes == lv).astype(float))
            names.append(f"{label}={lv}")
    return np.column_stack(cols), names
