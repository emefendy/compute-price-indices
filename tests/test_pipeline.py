import datetime as dt
import shutil
from pathlib import Path

import polars as pl
import pytest

from gpu_prices.analyze import price_index, spot_discount
from gpu_prices.catalog import load_catalog
from gpu_prices.cli import main
from gpu_prices.pipeline import build, load_prices
from gpu_prices.validate import run_checks

RAW = Path(__file__).parent / "fixtures" / "raw"


@pytest.fixture()
def data_dir(tmp_path):
    shutil.copytree(RAW, tmp_path / "raw")
    return tmp_path


def test_build_round_trips_through_csv(data_dir):
    prices, rejections = build(data_dir, load_catalog())
    reloaded = load_prices(data_dir)
    assert reloaded.equals(prices)
    assert prices["snapshot_date"].n_unique() == 3
    assert set(prices["provider"]) == {"aws", "azure", "gcp"}
    assert rejections["count"].sum() > 0


def test_every_seeded_problem_is_caught(data_dir):
    prices, _ = build(data_dir, load_catalog())
    found = set(run_checks(prices)["check"])
    assert found == {
        "non_positive_price",
        "conflicting_duplicate",
        "gpu_count_mismatch",
        "price_outlier",
        "spot_at_or_above_on_demand",
        "large_price_change",
        "series_disappeared",
    }


def test_chained_index_ignores_composition_change(data_dir):
    """Japan East (the most expensive H100 region) is delisted on day 3.

    The naive median drops because the expensive quote left the sample; the
    chained index only reflects prices that actually changed.
    """
    prices, _ = build(data_dir, load_catalog())
    idx = price_index(prices, "H100-80GB")
    assert idx["chained_index"][0] == 100.0
    assert idx["matched_quotes"][2] == idx["quotes"][2]  # all of today's quotes existed yesterday
    assert idx["median_index"][2] < idx["chained_index"][2]


def test_chained_index_is_exact_for_a_uniform_move():
    rows = []
    for date, factor in [(dt.date(2026, 10, 1), 1.0), (dt.date(2026, 10, 2), 0.9)]:
        for region, base in [("a", 10.0), ("b", 20.0)]:
            rows.append(
                {
                    "snapshot_date": date,
                    "provider": "aws",
                    "region": region,
                    "instance_type": "x",
                    "gpu_class": "H100-80GB",
                    "gpu_count": 1,
                    "pricing_model": "on_demand",
                    "price_basis": "instance",
                    "price_per_hour_usd": base * factor,
                    "price_per_gpu_hour_usd": base * factor,
                }
            )
    idx = price_index(pl.DataFrame(rows), "H100-80GB")
    assert idx["chained_index"][1] == pytest.approx(90.0)


def test_spot_discount_is_positive_for_normal_data(data_dir):
    prices, _ = build(data_dir, load_catalog())
    h100 = spot_discount(prices).filter((pl.col("gpu_class") == "H100-80GB") & (pl.col("provider") == "azure"))
    assert 0 < h100["median_discount_pct"][0] < 100


def test_demo_command_end_to_end(tmp_path):
    assert main(["demo", "--out", str(tmp_path / "demo")]) == 0
    report = (tmp_path / "demo" / "reports" / "README.md").read_text()
    assert "On-demand price per GPU-hour" in report
    assert (tmp_path / "demo" / "reports" / "prices.png").exists()
    assert (tmp_path / "demo" / "reports" / "index.png").exists()
