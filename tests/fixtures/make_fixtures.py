"""Generate the SYNTHETIC raw snapshots in tests/fixtures/raw/.

These files copy the *structure* of each provider's real API response, field
for field, but every price in them is made up. They exist so the tests and
`gpu-prices demo` run without network access or credentials, and they are
seeded with the specific kinds of mess the real price lists contain, so each
cleaning rule and quality check has something to catch.

Re-generate with:  python tests/fixtures/make_fixtures.py
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

OUT = Path(__file__).parent / "raw"
DATES = ["2026-10-01", "2026-10-02", "2026-10-03"]


# --------------------------------------------------------------------- Azure
def az(sku, meter, region, price, *, product=None, type_="Consumption", currency="USD", unit="1 Hour", **extra):
    short = sku.replace("Standard_", "")
    return {
        "currencyCode": currency,
        "tierMinimumUnits": 0.0,
        "retailPrice": price,
        "unitPrice": price,
        "armRegionName": region,
        "location": region,
        "effectiveStartDate": "2026-09-01T00:00:00Z",
        "meterId": f"meter-{short}-{region}-{meter.replace(' ', '')}",
        "meterName": meter,
        "productId": f"PROD-{short}",
        "skuId": f"PROD-{short}/{region}",
        "productName": product or f"Virtual Machines {short} Series",
        "skuName": meter,
        "serviceName": "Virtual Machines",
        "serviceId": "DZH313Z7MMC8",
        "serviceFamily": "Compute",
        "unitOfMeasure": unit,
        "type": type_,
        "isPrimaryMeterRegion": True,
        "armSkuName": sku,
        **extra,
    }


def azure(day: int) -> list[dict]:
    h100 = "Standard_ND96isr_H100_v5"
    h200 = "Standard_ND96isr_H200_v5"
    a100 = "Standard_NC24ads_A100_v4"
    h100_regions = {"eastus": 98.0, "westus3": 98.0, "southcentralus": 98.0, "swedencentral": 104.0, "westeurope": 110.0, "japaneast": 125.0}
    if day == 2:
        h100_regions["westeurope"] = 154.0  # +40% overnight -> large_price_change
        del h100_regions["japaneast"]  # delisted -> series_disappeared
    items = []
    for region, price in h100_regions.items():
        items.append(az(h100, "ND96isr H100 v5", region, price))
        spot = price * (0.40 if day == 0 else 0.36)  # spot drifts down ~10%
        items.append(az(h100, "ND96isr H100 v5 Spot", region, round(spot, 4)))
    for region, price in {"eastus": 110.0, "swedencentral": 116.0}.items():
        items.append(az(h200, "ND96isr H200 v5", region, price))
    items += [
        az(a100, "NC24ads A100 v4", "eastus", 3.67),
        az(a100, "NC24ads A100 v4", "westeurope", 4.10),
        az(a100, "NC24ads A100 v4 Spot", "eastus", 1.10),
        # Spot priced above on-demand on day 3 -> spot_at_or_above_on_demand
        az(a100, "NC24ads A100 v4 Spot", "westeurope", 4.50 if day == 2 else 1.25),
        # --- rows that cleaning should reject ---
        az(a100, "NC24ads A100 v4", "eastus", 4.40, product="Virtual Machines NCadsA100v4 Series Windows"),
        az(a100, "NC24ads A100 v4", "eastus", 2.20, type_="DevTestConsumption"),
        az(a100, "NC24ads A100 v4", "eastus", 19000.0, type_="Reservation", reservationTerm="1 Year"),
        az(a100, "NC24ads A100 v4 Low Priority", "eastus", 0.73),
        az(a100, "NC24ads A100 v4", "eastus", 3.67, product="NCadsA100v4 Series Cloud Services"),
        az(a100, "NC24ads A100 v4", "westeurope", 3.80, currency="EUR"),
        az("Standard_NC6s_v3", "NC6s v3", "eastus", 3.06),  # not in our catalog
    ]
    if day == 2:
        # Same series twice with different prices -> conflicting_duplicate
        items.append(az(a100, "NC24ads A100 v4", "eastus", 3.90))
    return items


# --------------------------------------------------------------------- AWS
def aws_product(instance, region, price, *, gpu=None, os="Linux", tenancy="Shared", location_type="AWS Region", capacity="Used"):
    sku = "SKU" + hashlib.sha1(f"{instance}|{region}|{os}|{tenancy}|{location_type}|{capacity}".encode()).hexdigest()[:10].upper()
    return {
        "product": {
            "productFamily": "Compute Instance",
            "sku": sku,
            "attributes": {
                "instanceType": instance,
                "regionCode": region,
                "locationType": location_type,
                "operatingSystem": os,
                "tenancy": tenancy,
                "preInstalledSw": "NA",
                "capacitystatus": capacity,
                "licenseModel": "No License required",
                **({"gpu": str(gpu)} if gpu is not None else {}),
            },
        },
        "serviceCode": "AmazonEC2",
        "terms": {
            "OnDemand": {
                f"{sku}.JRTCKXETXF": {
                    "priceDimensions": {
                        f"{sku}.JRTCKXETXF.6YS6EN2CT7": {
                            "unit": "Hrs",
                            "pricePerUnit": {"USD": f"{price:.10f}"},
                            "description": f"${price} per On Demand Linux {instance} Instance Hour",
                        }
                    },
                    "termAttributes": {},
                }
            }
        },
    }


def aws(day: int) -> list[dict]:
    p5 = {"us-east-1": 55.04, "us-east-2": 55.04, "us-west-2": 55.04, "eu-west-2": 63.0, "ap-northeast-1": 70.0}
    if day == 1:
        p5["us-east-1"] = 49.54  # -10%: a normal move, not flagged
    if day == 2:
        p5["us-east-1"] = 27.52  # -44% price cut -> large_price_change
    products = [aws_product("p5.48xlarge", r, p, gpu=8) for r, p in p5.items()]
    # 8x too cheap: looks like a per-GPU price filed as a per-instance price -> price_outlier
    products.append(aws_product("p5.48xlarge", "ap-south-1", 6.88, gpu=8))
    products += [
        aws_product("p5en.48xlarge", "us-east-1", 63.30, gpu=8),
        aws_product("p5en.48xlarge", "us-east-2", 63.30, gpu=8),
        aws_product("p4d.24xlarge", "us-east-1", 21.96, gpu=8),
        aws_product("p4d.24xlarge", "us-west-2", 21.96, gpu=4),  # vendor metadata disagrees -> gpu_count_mismatch
        aws_product("p4d.24xlarge", "sa-east-1", 0.0, gpu=8),  # zero price -> non_positive_price
        aws_product("g6.xlarge", "us-east-1", 0.8048, gpu=1),
        aws_product("g6.xlarge", "us-west-2", 0.8048, gpu=1),
        # --- rows that cleaning should reject ---
        aws_product("p5.48xlarge", "us-east-1", 62.0, gpu=8, os="Windows"),
        aws_product("p5.48xlarge", "us-east-1", 60.5, gpu=8, tenancy="Dedicated"),
        aws_product("g6.xlarge", "us-east-1-lax-1", 0.95, gpu=1, location_type="AWS Local Zone"),
        aws_product("p5.48xlarge", "us-east-1", 55.04, gpu=8, capacity="UnusedCapacityReservation"),
        aws_product("p3.2xlarge", "us-east-1", 3.06, gpu=1),  # not in our catalog
    ]
    return products


# --------------------------------------------------------------------- GCP
def money(price: float) -> dict:
    units = int(price)
    return {"currencyCode": "USD", "units": str(units), "nanos": round((price - units) * 1e9)}


def gcp_sku(sku_id, description, regions, price, usage="OnDemand", group="GPU", unit="h"):
    return {
        "name": f"services/6F81-5844-456A/skus/{sku_id}",
        "skuId": sku_id,
        "description": description,
        "category": {
            "serviceDisplayName": "Compute Engine",
            "resourceFamily": "Compute",
            "resourceGroup": group,
            "usageType": usage,
        },
        "serviceRegions": regions,
        "pricingInfo": [
            {
                "effectiveTime": "2026-09-01T00:00:00Z",
                "pricingExpression": {
                    "usageUnit": unit,
                    "displayQuantity": 1,
                    "tieredRates": [{"startUsageAmount": 0, "unitPrice": money(price)}],
                },
            }
        ],
        "serviceProviderName": "Google",
    }


def gcp(day: int) -> list[dict]:
    us = ["us-central1", "us-east4", "us-east5"]
    eu = ["europe-west4"]
    h100_spot = 2.25 if day == 0 else 2.05
    return [
        gcp_sku("A001-H100-US", "Nvidia H100 80GB GPU running in Americas", us, 9.80),
        gcp_sku("A002-H100-EU", "Nvidia H100 80GB GPU running in Netherlands", eu, 10.78),
        gcp_sku("A003-H100-SPOT", "Spot Preemptible Nvidia H100 80GB GPU running in Americas", us, h100_spot, usage="Preemptible"),
        gcp_sku("A004-H100P", "Nvidia H100 80GB Plus GPU running in Americas", us, 11.10),
        gcp_sku("A005-A100", "Nvidia Tesla A100 GPU running in Americas", us, 2.93),
        gcp_sku("A006-A100-80", "Nvidia A100 80GB GPU running in Americas", us, 3.93),
        gcp_sku("A007-L4", "Nvidia L4 GPU running in Americas", us, 0.56),
        gcp_sku("A008-H200", "Nvidia H200 141GB GPU running in Americas", us, 10.50),
        # --- rows that cleaning should reject ---
        gcp_sku("B001-COMMIT", "Commitment v1: Nvidia H100 80GB GPU in Americas for 1 Year", us, 6.40, usage="Commit1Yr"),
        gcp_sku("B002-DWS", "Nvidia H100 80GB GPU DWS Defined Duration running in Americas", us, 7.10),
        gcp_sku("B003-T4", "Nvidia Tesla T4 GPU running in Americas", us, 0.35),
        gcp_sku("B004-ODD", "GPU accelerator usage, legacy format", us, 1.00),
        gcp_sku("B005-MONTH", "Nvidia L4 GPU attached to reservation, Americas", us, 400.0, unit="mo"),
    ]


def main() -> None:
    for day, date in enumerate(DATES):
        folder = OUT / date
        folder.mkdir(parents=True, exist_ok=True)
        payloads = {
            "azure": {"source": "SYNTHETIC FIXTURE (Azure Retail Prices API format)", "items": azure(day)},
            "aws": {"source": "SYNTHETIC FIXTURE (AWS Price List API format)", "products": aws(day)},
            "gcp": {"source": "SYNTHETIC FIXTURE (GCP Cloud Billing Catalog API format)", "skus": gcp(day)},
        }
        for provider, payload in payloads.items():
            record = {"provider": provider, "snapshot_date": date, "collected_at": f"{date}T12:00:00+00:00", **payload}
            (folder / f"{provider}.json").write_text(json.dumps(record, indent=1, sort_keys=True))
    print(f"wrote fixtures for {len(DATES)} days to {OUT}")


if __name__ == "__main__":
    main()
