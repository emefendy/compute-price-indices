"""Storage layout and the raw -> clean build step.

    data/
      raw/<YYYY-MM-DD>/<provider>.json   untouched API responses, one folder per day
      clean/prices.csv                   all normalized quotes, every snapshot
      clean/rejections.csv               what was filtered out, and why
      clean/issues.csv                   output of the data-quality checks

`build` always regenerates clean/ from raw/, so changing a cleaning rule and
rebuilding re-applies it to the full history.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Iterator

import polars as pl

from .catalog import Catalog
from .normalize import NORMALIZERS
from .schema import ISSUE_SCHEMA, PRICE_SCHEMA, REJECTION_SCHEMA


def raw_path(data_dir: Path, date: dt.date, provider: str) -> Path:
    return data_dir / "raw" / date.isoformat() / f"{provider}.json"


def save_raw(data_dir: Path, date: dt.date, provider: str, payload: dict) -> Path:
    path = raw_path(data_dir, date, provider)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "provider": provider,
        "snapshot_date": date.isoformat(),
        "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        **payload,
    }
    path.write_text(json.dumps(record, indent=1, sort_keys=True))
    return path


def iter_raw(data_dir: Path) -> Iterator[tuple[dt.date, str, dict]]:
    for day_dir in sorted((data_dir / "raw").glob("*")):
        try:
            date = dt.date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        for path in sorted(day_dir.glob("*.json")):
            if path.stem in NORMALIZERS:
                yield date, path.stem, json.loads(path.read_text())


def build(data_dir: Path, catalog: Catalog) -> tuple[pl.DataFrame, pl.DataFrame]:
    rows, rejections = [], []
    for date, provider, raw in iter_raw(data_dir):
        provider_rows, rejected = NORMALIZERS[provider](raw, date, catalog)
        rows.extend(provider_rows)
        rejections.extend(
            {"snapshot_date": date, "provider": provider, "reason": reason, "count": n}
            for reason, n in sorted(rejected.items())
        )

    prices = pl.DataFrame(rows, schema=PRICE_SCHEMA).sort(
        ["snapshot_date", "provider", "gpu_class", "instance_type", "region", "pricing_model"]
    )
    rejected_df = pl.DataFrame(rejections, schema=REJECTION_SCHEMA)

    clean = data_dir / "clean"
    clean.mkdir(parents=True, exist_ok=True)
    prices.write_csv(clean / "prices.csv")
    rejected_df.write_csv(clean / "rejections.csv")
    return prices, rejected_df


def _read(path: Path, schema: dict) -> pl.DataFrame:
    if not path.exists():
        return pl.DataFrame(schema=schema)
    return pl.read_csv(path, schema=schema)


def load_prices(data_dir: Path) -> pl.DataFrame:
    return _read(data_dir / "clean" / "prices.csv", PRICE_SCHEMA)


def load_rejections(data_dir: Path) -> pl.DataFrame:
    return _read(data_dir / "clean" / "rejections.csv", REJECTION_SCHEMA)


def load_issues(data_dir: Path) -> pl.DataFrame:
    return _read(data_dir / "clean" / "issues.csv", ISSUE_SCHEMA)
