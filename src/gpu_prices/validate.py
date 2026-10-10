"""Data-quality checks on the normalized price table.

Checks never modify the data; they report. Each issue has a severity:

  error    the data is wrong or self-contradictory (zero price, two different
           prices for the same quote, spot priced above on-demand)
  warning  the data is suspicious and worth a human look (an outlier, a big jump,
           vendor metadata disagreeing with our catalog)
  info     something changed that isn't necessarily wrong (a series disappeared)
"""

from __future__ import annotations

import polars as pl

from .schema import ISSUE_SCHEMA, SERIES_KEY

OUTLIER_Z = 3.5  # Iglewicz & Hoaglin's recommended cutoff for the modified z-score
OUTLIER_MIN_GROUP = 5  # don't call anything an outlier in a group of 3
# Floor on the MAD of log prices (~10%). Price lists have many identical quotes
# (e.g. three US regions at exactly the same price), which can shrink the MAD to
# almost nothing and turn ordinary regional differences into "outliers".
OUTLIER_MAD_FLOOR = 0.10
JUMP_THRESHOLD = 0.30  # flag day-over-day moves bigger than +/-30%
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def _issues(df: pl.DataFrame, check: str, severity: str, detail: pl.Expr) -> pl.DataFrame:
    return df.select(
        pl.lit(check).alias("check"),
        pl.lit(severity).alias("severity"),
        pl.col("snapshot_date"),
        *[pl.col(c) for c in SERIES_KEY],
        detail.cast(pl.Utf8).alias("detail"),
    ).cast(ISSUE_SCHEMA)


def check_non_positive_price(df: pl.DataFrame) -> pl.DataFrame:
    bad = df.filter(pl.col("price_per_hour_usd").is_null() | (pl.col("price_per_hour_usd") <= 0))
    return _issues(bad, "non_positive_price", "error", pl.format("price_per_hour_usd={}", pl.col("price_per_hour_usd")))


def check_duplicates(df: pl.DataFrame) -> pl.DataFrame:
    """Same series, same day, more than one row. Conflicting prices are an error."""
    groups = (
        df.group_by(["snapshot_date", *SERIES_KEY])
        .agg(
            pl.len().alias("n"),
            pl.col("price_per_hour_usd").n_unique().alias("n_prices"),
            pl.col("price_per_hour_usd").min().alias("lo"),
            pl.col("price_per_hour_usd").max().alias("hi"),
        )
        .filter(pl.col("n") > 1)
    )
    conflicting = _issues(
        groups.filter(pl.col("n_prices") > 1),
        "conflicting_duplicate",
        "error",
        pl.format("{} rows with prices from ${} to ${}", pl.col("n"), pl.col("lo"), pl.col("hi")),
    )
    exact = _issues(
        groups.filter(pl.col("n_prices") == 1),
        "exact_duplicate",
        "warning",
        pl.format("{} identical rows", pl.col("n")),
    )
    return pl.concat([conflicting, exact])


def check_gpu_count_mismatch(df: pl.DataFrame) -> pl.DataFrame:
    """Vendor metadata disagrees with our catalog: one of them is wrong."""
    bad = df.filter(
        pl.col("vendor_gpu_count").is_not_null() & (pl.col("vendor_gpu_count") != pl.col("gpu_count"))
    )
    return _issues(
        bad,
        "gpu_count_mismatch",
        "warning",
        pl.format("catalog says {} GPUs, vendor says {}", pl.col("gpu_count"), pl.col("vendor_gpu_count")),
    )


def check_price_outliers(df: pl.DataFrame) -> pl.DataFrame:
    """Modified z-score of log $/GPU-hour within comparable quotes.

    Log prices because prices are multiplicative (a 10x typo is as far from the
    median as a 1/10x one). Median/MAD instead of mean/std because the outliers
    we're hunting would otherwise drag the mean and inflate the std toward them.
    """
    group = ["snapshot_date", "gpu_class", "pricing_model", "price_basis"]
    scored = (
        df.filter(pl.col("price_per_gpu_hour_usd") > 0)
        .with_columns(pl.col("price_per_gpu_hour_usd").log().alias("lp"))
        .with_columns(pl.col("lp").median().over(group).alias("med"), pl.len().over(group).alias("n"))
        .with_columns(
            (pl.col("lp") - pl.col("med")).abs().median().over(group).clip(lower_bound=OUTLIER_MAD_FLOOR).alias("mad")
        )
        .with_columns(
            (0.6745 * (pl.col("lp") - pl.col("med")) / pl.col("mad")).alias("z")
        )
    )
    bad = scored.filter((pl.col("n") >= OUTLIER_MIN_GROUP) & (pl.col("z").abs() > OUTLIER_Z))
    return _issues(
        bad,
        "price_outlier",
        "warning",
        pl.format(
            "${}/GPU-hr vs {} median ${} (modified z={})",
            pl.col("price_per_gpu_hour_usd").round(2),
            pl.col("gpu_class"),
            pl.col("med").exp().round(2),
            pl.col("z").round(1),
        ),
    )


def check_spot_above_on_demand(df: pl.DataFrame) -> pl.DataFrame:
    key = ["snapshot_date", "provider", "region", "instance_type"]
    price = pl.col("price_per_hour_usd").median()
    on_demand = df.filter(pl.col("pricing_model") == "on_demand").group_by(key).agg(price.alias("od"))
    spot = df.filter(pl.col("pricing_model") == "spot").group_by(key).agg(price.alias("spot"))
    bad = (
        spot.join(on_demand, on=key)
        .filter(pl.col("spot") >= pl.col("od"))
        .with_columns(pl.lit("spot").alias("pricing_model"))
    )
    return _issues(
        bad,
        "spot_at_or_above_on_demand",
        "error",
        pl.format("spot ${} vs on-demand ${}", pl.col("spot"), pl.col("od")),
    )


def check_price_jumps(df: pl.DataFrame, threshold: float = JUMP_THRESHOLD) -> pl.DataFrame:
    """Compare each series to its own previous snapshot."""
    series = (
        df.group_by(["snapshot_date", *SERIES_KEY])
        .agg(pl.col("price_per_hour_usd").median().alias("p"))
        .sort("snapshot_date")
        .with_columns(
            pl.col("p").shift(1).over(SERIES_KEY).alias("prev"),
            pl.col("snapshot_date").shift(1).over(SERIES_KEY).alias("prev_date"),
        )
        .filter(pl.col("prev").is_not_null() & (pl.col("prev") > 0))
        .with_columns((pl.col("p") / pl.col("prev") - 1).alias("chg"))
        .filter(pl.col("chg").abs() > threshold)
    )
    return _issues(
        series,
        "large_price_change",
        "warning",
        pl.format(
            "{}% since {} (${} -> ${})",
            (pl.col("chg") * 100).round(1),
            pl.col("prev_date"),
            pl.col("prev"),
            pl.col("p"),
        ),
    )


def check_disappeared_series(df: pl.DataFrame) -> pl.DataFrame:
    dates = df["snapshot_date"].unique().sort()
    if len(dates) < 2:
        return pl.DataFrame(schema=ISSUE_SCHEMA)
    prev, last = dates[-2], dates[-1]
    before = df.filter(pl.col("snapshot_date") == prev).select(SERIES_KEY).unique()
    now = df.filter(pl.col("snapshot_date") == last).select(SERIES_KEY).unique()
    gone = before.join(now, on=SERIES_KEY, how="anti").with_columns(pl.lit(last).alias("snapshot_date"))
    return _issues(gone, "series_disappeared", "info", pl.lit(f"listed on {prev}, missing on {last}"))


CHECKS = [
    check_non_positive_price,
    check_duplicates,
    check_gpu_count_mismatch,
    check_price_outliers,
    check_spot_above_on_demand,
    check_price_jumps,
    check_disappeared_series,
]


def run_checks(df: pl.DataFrame) -> pl.DataFrame:
    if df.is_empty():
        return pl.DataFrame(schema=ISSUE_SCHEMA)
    issues = pl.concat([check(df) for check in CHECKS])
    return (
        issues.with_columns(pl.col("severity").replace_strict(SEVERITY_ORDER).alias("_rank"))
        .sort(["_rank", "check", "snapshot_date", "provider", "region", "instance_type"])
        .drop("_rank")
    )
