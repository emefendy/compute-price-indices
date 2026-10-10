"""Summary tables and a chained price index."""

from __future__ import annotations

import math

import polars as pl

from .schema import SERIES_KEY


def collapse(df: pl.DataFrame) -> pl.DataFrame:
    """One price per series per day (median, in case duplicates slipped through)."""
    return df.group_by(["snapshot_date", *SERIES_KEY, "gpu_class", "price_basis", "gpu_count"]).agg(
        pl.col("price_per_hour_usd").median(),
        pl.col("price_per_gpu_hour_usd").median(),
    )


def latest(df: pl.DataFrame) -> pl.DataFrame:
    return df.filter(pl.col("snapshot_date") == df["snapshot_date"].max())


def usable(df: pl.DataFrame) -> pl.DataFrame:
    """Drop quotes that can't be priced (zero/null) before summarizing."""
    return df.filter(pl.col("price_per_hour_usd") > 0)


def price_summary(df: pl.DataFrame) -> pl.DataFrame:
    """Latest on-demand $/GPU-hour by GPU class and provider, across regions."""
    d = latest(collapse(usable(df))).filter(pl.col("pricing_model") == "on_demand")
    gpu_hr = pl.col("price_per_gpu_hour_usd")
    return (
        d.group_by(["gpu_class", "provider", "price_basis"])
        .agg(
            pl.col("region").n_unique().alias("regions"),
            gpu_hr.min().round(2).alias("min"),
            gpu_hr.median().round(2).alias("median"),
            gpu_hr.max().round(2).alias("max"),
        )
        .sort(["gpu_class", "median"])
    )


def spot_discount(df: pl.DataFrame) -> pl.DataFrame:
    """How much cheaper spot is than on-demand for the same instance in the same region."""
    d = latest(collapse(usable(df)))
    key = ["provider", "region", "instance_type", "gpu_class"]
    od = d.filter(pl.col("pricing_model") == "on_demand").select(*key, pl.col("price_per_hour_usd").alias("od"))
    spot = d.filter(pl.col("pricing_model") == "spot").select(*key, pl.col("price_per_hour_usd").alias("spot"))
    return (
        spot.join(od, on=key)
        .with_columns((1 - pl.col("spot") / pl.col("od")).alias("discount"))
        .group_by(["gpu_class", "provider"])
        .agg(pl.len().alias("pairs"), (pl.col("discount").median() * 100).round(1).alias("median_discount_pct"))
        .sort(["gpu_class", "provider"])
    )


def price_index(
    df: pl.DataFrame,
    gpu_class: str,
    pricing_model: str = "on_demand",
    price_basis: str = "instance",
) -> pl.DataFrame:
    """Daily index of $/GPU-hour for one GPU class, base = 100 on the first day.

    Two versions:

    median_price   median of whatever quotes exist that day. Easy, but biased:
                   if an expensive region stops being listed, the median falls
                   even though no price changed.

    chained_index  a chained Jevons index (geometric mean of price relatives),
                   the method statistical agencies use for elementary price
                   indices. Each day is compared only with series present on
                   both days, so composition changes don't show up as price moves.
    """
    d = collapse(usable(df)).filter(
        (pl.col("gpu_class") == gpu_class)
        & (pl.col("pricing_model") == pricing_model)
        & (pl.col("price_basis") == price_basis)
    )
    dates = d["snapshot_date"].unique().sort().to_list()
    out = []
    level = 100.0
    prev = None
    for date in dates:
        today = d.filter(pl.col("snapshot_date") == date)
        matched = 0
        if prev is not None:
            pairs = today.join(prev, on=SERIES_KEY, suffix="_prev")
            matched = pairs.height
            if matched:
                rel = (pairs["price_per_gpu_hour_usd"] / pairs["price_per_gpu_hour_usd_prev"]).log().mean()
                level *= math.exp(rel)
        out.append(
            {
                "snapshot_date": date,
                "quotes": today.height,
                "matched_quotes": matched,
                "median_price": today["price_per_gpu_hour_usd"].median(),
                "chained_index": level,
            }
        )
        prev = today
    return pl.DataFrame(
        out,
        schema={
            "snapshot_date": pl.Date,
            "quotes": pl.Int64,
            "matched_quotes": pl.Int64,
            "median_price": pl.Float64,
            "chained_index": pl.Float64,
        },
    ).with_columns(
        (pl.col("median_price") / pl.col("median_price").first() * 100).alias("median_index"),
    )
