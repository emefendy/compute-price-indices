import datetime as dt
import json
from pathlib import Path

import pytest

from gpu_prices.catalog import load_catalog
from gpu_prices.normalize import normalize_aws, normalize_azure, normalize_gcp

RAW = Path(__file__).parent / "fixtures" / "raw"
DAY1 = dt.date(2026, 10, 1)


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


def load(provider, date="2026-10-01"):
    return json.loads((RAW / date / f"{provider}.json").read_text())


def test_catalog_loads_and_has_gpu_classes(catalog):
    inst = catalog[("aws", "p5.48xlarge")]
    assert inst.gpu_count == 8
    assert inst.gpu_class == "H100-80GB"
    assert catalog[("azure", "Standard_NC40ads_H100_v5")].gpu_class == "H100 NVL-94GB"


def test_catalog_rejects_duplicates(tmp_path):
    bad = tmp_path / "c.csv"
    bad.write_text(
        "provider,instance_type,gpu_model,gpu_count,gpu_memory_gb\n"
        "aws,p5.48xlarge,H100,8,80\n"
        "aws,p5.48xlarge,H100,8,80\n"
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_catalog(bad)


def test_azure_filters_every_kind_of_noise(catalog):
    rows, rejected = normalize_azure(load("azure"), DAY1, catalog)
    assert rejected == {
        "windows_license_included": 1,
        "price_type_devtestconsumption": 1,
        "price_type_reservation": 1,
        "low_priority_batch": 1,
        "cloud_services_product": 1,
        "non_usd_currency": 1,
        "instance_not_in_catalog": 1,
    }
    # Every surviving row is a Linux, hourly, USD, catalogued VM.
    assert {r["provider"] for r in rows} == {"azure"}
    assert {r["price_basis"] for r in rows} == {"instance"}


def test_azure_spot_detection_and_per_gpu_math(catalog):
    rows, _ = normalize_azure(load("azure"), DAY1, catalog)
    h100_east = {r["pricing_model"]: r for r in rows if r["instance_type"] == "Standard_ND96isr_H100_v5" and r["region"] == "eastus"}
    assert set(h100_east) == {"on_demand", "spot"}
    assert h100_east["on_demand"]["price_per_hour_usd"] == 98.0
    assert h100_east["on_demand"]["price_per_gpu_hour_usd"] == pytest.approx(98.0 / 8)
    assert h100_east["spot"]["price_per_hour_usd"] < h100_east["on_demand"]["price_per_hour_usd"]


def test_aws_filters_and_keeps_vendor_gpu_count(catalog):
    rows, rejected = normalize_aws(load("aws"), DAY1, catalog)
    assert rejected == {
        "non_linux_os": 1,
        "tenancy_dedicated": 1,
        "non_region_location": 1,
        "capacity_reservation_line": 1,
        "instance_not_in_catalog": 1,
    }
    p4d_west = next(r for r in rows if r["instance_type"] == "p4d.24xlarge" and r["region"] == "us-west-2")
    assert p4d_west["gpu_count"] == 8  # from our catalog
    assert p4d_west["vendor_gpu_count"] == 4  # what AWS says; validation flags the disagreement


def test_aws_rejects_ambiguous_price_dimensions(catalog):
    raw = load("aws")
    product = next(p for p in raw["products"] if p["product"]["attributes"]["instanceType"] == "g6.xlarge")
    term = next(iter(product["terms"]["OnDemand"].values()))
    dim = next(iter(term["priceDimensions"].values()))
    term["priceDimensions"]["second"] = dict(dim)
    rows, rejected = normalize_aws({"products": [product]}, DAY1, catalog)
    assert rows == [] and rejected == {"ambiguous_price_dimensions": 1}


def test_gcp_parses_descriptions_and_explodes_regions():
    rows, rejected = normalize_gcp(load("gcp"), DAY1)
    assert rejected == {
        "commitment_or_other_usage_type": 1,
        "special_pricing_program": 1,
        "unsupported_gpu_model": 1,
        "unparseable_description": 1,
        "non_hourly_unit": 1,
    }
    classes = {r["instance_type"]: r["gpu_class"] for r in rows}
    assert classes["gpu:A100 40GB"] == "A100-40GB"  # "Tesla A100" has no memory in its name
    assert classes["gpu:H100 80GB Plus"] == "H100-80GB"
    us_h100 = [r for r in rows if r["instance_type"] == "gpu:H100 80GB" and r["pricing_model"] == "on_demand"]
    assert {r["region"] for r in us_h100} == {"us-central1", "us-east4", "us-east5", "europe-west4"}
    assert all(r["price_basis"] == "accelerator_only" and r["gpu_count"] == 1 for r in rows)


def test_gcp_money_type_is_parsed_exactly():
    sku = load("gcp")["skus"][0]
    sku["pricingInfo"][0]["pricingExpression"]["tieredRates"][0]["unitPrice"] = {
        "currencyCode": "USD",
        "units": "3",
        "nanos": 930000000,
    }
    rows, _ = normalize_gcp({"skus": [sku]}, DAY1)
    assert rows[0]["price_per_hour_usd"] == pytest.approx(3.93)
