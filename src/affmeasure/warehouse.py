"""Load public synthetic tables into DuckDB and build the SQL models.

Ground-truth labels are intentionally NOT loaded: every model and the fraud
scorer run against the same information an operator would have.
"""

from __future__ import annotations

from dataclasses import asdict
from importlib import resources
from pathlib import Path

import duckdb
import pandas as pd

from .config import COSTS, CostConfig
from .generator import SyntheticData

MODEL_FILES = [
    "01_fct_player.sql",
    "02_fct_player_day.sql",
    "03_fct_player_month.sql",
    "04_mart_funnel.sql",
    "05_mart_cohort_windows.sql",
    "06_mart_ltv_curve.sql",
    "07_mart_chargebacks.sql",
]


def _sql(name: str) -> str:
    return resources.files("affmeasure").joinpath("sql", name).read_text(encoding="utf-8")


def load_raw(con: duckdb.DuckDBPyConnection, tables: dict[str, pd.DataFrame]) -> None:
    for name, df in tables.items():
        con.register("_tmp_df", df)
        con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM _tmp_df")
        con.unregister("_tmp_df")


def write_meta(
    con: duckdb.DuckDBPyConnection, start: str, obs_end: str, costs: CostConfig = COSTS
) -> None:
    meta = pd.DataFrame(
        [{"start_ts": pd.Timestamp(start), "obs_end": pd.Timestamp(obs_end), **asdict(costs)}]
    )
    con.register("_meta", meta)
    con.execute("CREATE OR REPLACE TABLE sim_meta AS SELECT * FROM _meta")
    con.unregister("_meta")


def build_models(con: duckdb.DuckDBPyConnection) -> None:
    for f in MODEL_FILES:
        con.execute(_sql(f))


def build_warehouse(
    data: SyntheticData, db_path: str | Path = ":memory:"
) -> duckdb.DuckDBPyConnection:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(db_path).unlink(missing_ok=True)
    con = duckdb.connect(str(db_path))
    load_raw(con, data.tables)
    write_meta(con, data.config.start, data.config.obs_end)
    build_models(con)
    return con


def export_parquet(data: SyntheticData, out_dir: str | Path) -> None:
    """Write public tables and (separately) ground truth to parquet."""
    out = Path(out_dir)
    (out / "raw").mkdir(parents=True, exist_ok=True)
    (out / "ground_truth").mkdir(parents=True, exist_ok=True)
    for name, df in data.tables.items():
        df.to_parquet(out / "raw" / f"{name}.parquet", index=False)
    for name, df in data.ground_truth.items():
        df.to_parquet(out / "ground_truth" / f"{name}.parquet", index=False)
