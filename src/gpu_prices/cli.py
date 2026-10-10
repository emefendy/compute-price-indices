"""Command-line entry point: gpu-prices {collect,build,validate,report,run,demo}."""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
from pathlib import Path

import polars as pl

from . import collectors
from .catalog import load_catalog
from .pipeline import build, load_issues, load_prices, load_rejections, save_raw
from .report import write_report
from .validate import run_checks

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "raw"


def cmd_collect(args) -> int:
    catalog = load_catalog()
    date = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(dt.timezone.utc).date()
    jobs = {
        "azure": lambda: collectors.collect_azure(catalog),
        "aws": lambda: collectors.collect_aws(catalog),
        "gcp": lambda: collectors.collect_gcp(),
    }
    ok = 0
    for provider in [p.strip() for p in args.providers.split(",") if p.strip()]:
        try:
            path = save_raw(args.data_dir, date, provider, jobs[provider]())
            print(f"[collect] {provider}: saved {path}")
            ok += 1
        except Exception as exc:  # one provider failing shouldn't lose the others
            print(f"[collect] {provider}: skipped ({exc})", file=sys.stderr)
    return 0 if ok else 1


def cmd_build(args) -> int:
    prices, rejections = build(args.data_dir, load_catalog())
    print(f"[build] {prices.height} quotes kept, {rejections['count'].sum()} raw records filtered")
    return 0


def cmd_validate(args) -> int:
    issues = run_checks(load_prices(args.data_dir))
    issues.write_csv(args.data_dir / "clean" / "issues.csv")
    with pl.Config(tbl_rows=50, fmt_str_lengths=60):
        print(issues.group_by(["severity", "check"]).agg(pl.len().alias("n")).sort(["severity", "check"]))
    errors = issues.filter(pl.col("severity") == "error").height
    return 1 if (args.strict and errors) else 0


def cmd_report(args) -> int:
    path = write_report(
        load_prices(args.data_dir), load_rejections(args.data_dir), load_issues(args.data_dir), args.reports_dir
    )
    print(f"[report] wrote {path}")
    return 0


def cmd_run(args) -> int:
    status = cmd_collect(args)
    for step in (cmd_build, cmd_validate, cmd_report):
        status = step(args) or status
    return status


def cmd_demo(args) -> int:
    """Run the full pipeline on the bundled synthetic fixtures (no network needed)."""
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(FIXTURES, out / "data" / "raw")
    args.data_dir, args.reports_dir, args.strict = out / "data", out / "reports", False
    for step in (cmd_build, cmd_validate, cmd_report):
        step(args)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gpu-prices", description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("collect", "run"):
        p = sub.add_parser(name)
        p.add_argument("--providers", default="azure,aws,gcp")
        p.add_argument("--date", help="snapshot date, YYYY-MM-DD (default: today, UTC)")
        p.add_argument("--strict", action="store_true", help="exit 1 if any error-level issue is found")
    sub.add_parser("build")
    sub.add_parser("validate").add_argument("--strict", action="store_true")
    sub.add_parser("report")
    sub.add_parser("demo").add_argument("--out", default="demo_output")

    args = parser.parse_args(argv)
    handler = {
        "collect": cmd_collect,
        "build": cmd_build,
        "validate": cmd_validate,
        "report": cmd_report,
        "run": cmd_run,
        "demo": cmd_demo,
    }[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
