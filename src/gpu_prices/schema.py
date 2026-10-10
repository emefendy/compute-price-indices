"""Column definitions shared by every stage of the pipeline."""

import polars as pl

# One row = one price quote for one instance type, in one region,
# under one pricing model, on one snapshot date.
PRICE_SCHEMA: dict[str, pl.DataType] = {
    "snapshot_date": pl.Date,
    "provider": pl.Utf8,
    "region": pl.Utf8,
    "instance_type": pl.Utf8,
    "gpu_class": pl.Utf8,  # e.g. "H100-80GB": model + memory, the unit we compare on
    "gpu_model": pl.Utf8,
    "gpu_memory_gb": pl.Int32,
    "gpu_count": pl.Int32,
    "pricing_model": pl.Utf8,  # "on_demand" | "spot"
    "price_basis": pl.Utf8,  # "instance" (whole VM) | "accelerator_only" (GPU line item only)
    "price_per_hour_usd": pl.Float64,
    "price_per_gpu_hour_usd": pl.Float64,
    "vendor_gpu_count": pl.Int32,  # what the vendor says, when it says anything
    "source_id": pl.Utf8,
}

# Identifies one price series through time.
SERIES_KEY = ["provider", "region", "instance_type", "pricing_model"]

REJECTION_SCHEMA: dict[str, pl.DataType] = {
    "snapshot_date": pl.Date,
    "provider": pl.Utf8,
    "reason": pl.Utf8,
    "count": pl.Int64,
}

ISSUE_SCHEMA: dict[str, pl.DataType] = {
    "check": pl.Utf8,
    "severity": pl.Utf8,  # "error" | "warning" | "info"
    "snapshot_date": pl.Date,
    "provider": pl.Utf8,
    "region": pl.Utf8,
    "instance_type": pl.Utf8,
    "pricing_model": pl.Utf8,
    "detail": pl.Utf8,
}
