"""Command line entry point: ``affmeasure run``."""

from __future__ import annotations

import argparse
import time
from dataclasses import replace
from pathlib import Path

from .config import SimConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="affmeasure", description="Affiliate measurement stack (synthetic demo data)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser(
        "run", help="generate data, build DuckDB models, run analyses, write reports"
    )
    run.add_argument("--seed", type=int, default=SimConfig.seed)
    run.add_argument(
        "--scale", type=float, default=SimConfig.scale, help="multiplier for arrival volumes"
    )
    run.add_argument("--out", type=Path, default=Path("reports"))
    run.add_argument(
        "--data-dir", type=Path, default=Path("data"), help="parquet export (raw + ground_truth)"
    )
    run.add_argument("--db", type=Path, default=Path("data/warehouse.duckdb"))
    run.add_argument(
        "--no-export", action="store_true", help="skip parquet export and use in-memory DuckDB"
    )
    args = parser.parse_args(argv)

    from .pipeline import run_pipeline
    from .report import write_report

    cfg = replace(SimConfig(), seed=args.seed, scale=args.scale)
    t0 = time.perf_counter()
    result = run_pipeline(
        cfg,
        data_dir=None if args.no_export else args.data_dir,
        db_path=":memory:" if args.no_export else args.db,
    )
    path = write_report(result, args.out)
    result.con.close()
    print(f"report: {path}  ({time.perf_counter() - t0:.1f}s, SYNTHETIC DEMO DATA)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
