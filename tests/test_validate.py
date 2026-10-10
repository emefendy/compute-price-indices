import datetime as dt

import polars as pl
import pytest

from gpu_prices.schema import PRICE_SCHEMA
from gpu_prices.validate import (
    check_disappeared_series,
    check_duplicates,
    check_gpu_count_mismatch,
    check_non_positive_price,
    check_price_jumps,
    check_price_outliers,
    check_spot_above_on_demand,
    run_checks,
)

D1, D2 = dt.date(2026, 10, 1), dt.date(2026, 10, 2)


def quote(price, region="r1", date=D1, instance="p5.48xlarge", pricing="on_demand", gpus=8, vendor=None):
    return {
        "snapshot_date": date,
        "provider": "aws",
        "region": region,
        "instance_type": instance,
        "gpu_class": "H100-80GB",
        "gpu_model": "H100",
        "gpu_memory_gb": 80,
        "gpu_count": gpus,
        "pricing_model": pricing,
        "price_basis": "instance",
        "price_per_hour_usd": price,
        "price_per_gpu_hour_usd": price / gpus,
        "vendor_gpu_count": vendor,
        "source_id": "x",
    }


def frame(*rows):
    return pl.DataFrame(list(rows), schema=PRICE_SCHEMA)


def test_non_positive_price():
    issues = check_non_positive_price(frame(quote(0.0), quote(55.0, region="r2")))
    assert issues["region"].to_list() == ["r1"]


def test_duplicates_conflicting_vs_exact():
    issues = check_duplicates(
        frame(quote(55.0), quote(56.0), quote(60.0, region="r2"), quote(60.0, region="r2"))
    )
    by_check = dict(zip(issues["check"], issues["severity"]))
    assert by_check == {"conflicting_duplicate": "error", "exact_duplicate": "warning"}


def test_gpu_count_mismatch_only_when_vendor_reports():
    issues = check_gpu_count_mismatch(frame(quote(55.0, vendor=4), quote(55.0, region="r2", vendor=None)))
    assert issues.height == 1


def test_outlier_flags_a_tenfold_error_but_not_normal_spread():
    normal = [quote(p, region=f"r{i}") for i, p in enumerate([55, 55, 55, 60, 63, 70])]
    typo = quote(5.5, region="bad")
    issues = check_price_outliers(frame(*normal, typo))
    assert issues["region"].to_list() == ["bad"]


def test_outlier_ignores_tiny_groups():
    assert check_price_outliers(frame(quote(55), quote(55, region="r2"), quote(5.5, region="r3"))).is_empty()


def test_spot_above_on_demand():
    issues = check_spot_above_on_demand(frame(quote(10.0), quote(12.0, pricing="spot")))
    assert issues.height == 1 and issues["severity"][0] == "error"


@pytest.mark.parametrize("new_price, flagged", [(55.0 * 1.2, False), (55.0 * 1.5, True), (55.0 * 0.5, True)])
def test_price_jumps(new_price, flagged):
    issues = check_price_jumps(frame(quote(55.0), quote(new_price, date=D2)))
    assert (issues.height == 1) is flagged


def test_disappeared_series():
    issues = check_disappeared_series(frame(quote(55.0), quote(55.0, region="r2"), quote(55.0, date=D2)))
    assert issues["region"].to_list() == ["r2"] and issues["severity"][0] == "info"


def test_run_checks_sorts_errors_first():
    issues = run_checks(frame(quote(0.0), quote(55.0, region="r2", vendor=4)))
    assert issues["severity"].to_list() == ["error", "warning"]


def test_run_checks_on_empty_frame():
    assert run_checks(frame()).is_empty()
