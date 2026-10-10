"""Turn raw vendor price-list records into rows of the common schema.

Each normalizer returns (rows, rejected): the rows that made it through, and a
count of everything filtered out, keyed by reason. Keeping the rejection counts
is deliberate: a sudden jump in "instance_not_in_catalog" means a vendor
launched something new, and a jump in "unparseable_description" means a vendor
changed a format. Silent drops would hide both.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from typing import Any

from .catalog import Catalog

Row = dict[str, Any]
Result = tuple[list[Row], Counter]


def _row(
    snapshot_date: dt.date,
    provider: str,
    region: str,
    instance_type: str,
    gpu_model: str,
    gpu_memory_gb: int,
    gpu_count: int,
    pricing_model: str,
    price_basis: str,
    price: float,
    vendor_gpu_count: int | None,
    source_id: str,
) -> Row:
    return {
        "snapshot_date": snapshot_date,
        "provider": provider,
        "region": region,
        "instance_type": instance_type,
        "gpu_class": f"{gpu_model}-{gpu_memory_gb}GB",
        "gpu_model": gpu_model,
        "gpu_memory_gb": gpu_memory_gb,
        "gpu_count": gpu_count,
        "pricing_model": pricing_model,
        "price_basis": price_basis,
        "price_per_hour_usd": price,
        "price_per_gpu_hour_usd": price / gpu_count,
        "vendor_gpu_count": vendor_gpu_count,
        "source_id": source_id,
    }


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- Azure

_CLOUD_SERVICES = re.compile(r"cloud\s*services", re.IGNORECASE)


def _azure_reject_reason(item: dict) -> str | None:
    meter = (item.get("meterName") or "").lower()
    product = item.get("productName") or ""
    if item.get("currencyCode") != "USD":
        return "non_usd_currency"
    if item.get("serviceName") != "Virtual Machines":
        return "not_a_vm"
    if item.get("type") != "Consumption":
        # DevTestConsumption (discounted dev/test subscriptions) and Reservation
        # (priced per 1- or 3-year term, not per hour) aren't comparable.
        return f"price_type_{(item.get('type') or 'missing').lower()}"
    if item.get("unitOfMeasure") != "1 Hour":
        return "non_hourly_unit"
    if float(item.get("tierMinimumUnits") or 0) > 0:
        return "tiered_rate"
    if "windows" in product.lower():
        # Windows prices include an OS license; we compare Linux prices only.
        return "windows_license_included"
    if _CLOUD_SERVICES.search(product):
        # Same VM sold through the legacy Cloud Services product: a duplicate quote.
        return "cloud_services_product"
    if "low priority" in meter:
        # Legacy Azure Batch tier, superseded by Spot.
        return "low_priority_batch"
    return None


def normalize_azure(raw: dict, snapshot_date: dt.date, catalog: Catalog) -> Result:
    rows: list[Row] = []
    rejected: Counter = Counter()
    for item in raw.get("items", []):
        reason = _azure_reject_reason(item)
        inst = catalog.get(("azure", item.get("armSkuName", "")))
        if reason is None and inst is None:
            reason = "instance_not_in_catalog"
        if reason:
            rejected[reason] += 1
            continue
        meter = (item.get("meterName") or "").lower()
        rows.append(
            _row(
                snapshot_date,
                "azure",
                item["armRegionName"],
                inst.instance_type,
                inst.gpu_model,
                inst.gpu_memory_gb,
                inst.gpu_count,
                "spot" if "spot" in meter else "on_demand",
                "instance",
                float(item["retailPrice"]),
                None,
                f"{item.get('skuId')}|{item.get('meterId')}",
            )
        )
    return rows, rejected


# --------------------------------------------------------------------------- AWS


def _aws_reject_reason(attrs: dict) -> str | None:
    if attrs.get("locationType", "AWS Region") != "AWS Region":
        return "non_region_location"  # Local Zones / Wavelength price differently
    if attrs.get("operatingSystem") != "Linux":
        return "non_linux_os"
    if attrs.get("tenancy") != "Shared":
        return f"tenancy_{(attrs.get('tenancy') or 'missing').lower()}"
    if attrs.get("preInstalledSw", "NA") != "NA":
        return "preinstalled_software"
    if attrs.get("capacitystatus", "Used") != "Used":
        return "capacity_reservation_line"
    return None


def normalize_aws(raw: dict, snapshot_date: dt.date, catalog: Catalog) -> Result:
    rows: list[Row] = []
    rejected: Counter = Counter()
    for product in raw.get("products", []):
        attrs = product.get("product", {}).get("attributes", {})
        reason = _aws_reject_reason(attrs)
        inst = catalog.get(("aws", attrs.get("instanceType", "")))
        if reason is None and inst is None:
            reason = "instance_not_in_catalog"

        hourly = []
        if reason is None:
            dims = [
                dim
                for term in product.get("terms", {}).get("OnDemand", {}).values()
                for dim in term.get("priceDimensions", {}).values()
            ]
            hourly = [d for d in dims if d.get("unit") == "Hrs"]
            if not dims:
                reason = "no_on_demand_terms"
            elif not hourly:
                reason = "non_hourly_unit"
            elif len(hourly) > 1:
                reason = "ambiguous_price_dimensions"

        if reason:
            rejected[reason] += 1
            continue
        rows.append(
            _row(
                snapshot_date,
                "aws",
                attrs["regionCode"],
                inst.instance_type,
                inst.gpu_model,
                inst.gpu_memory_gb,
                inst.gpu_count,
                "on_demand",
                "instance",
                float(hourly[0]["pricePerUnit"]["USD"]),
                _to_int(attrs.get("gpu")),
                product.get("product", {}).get("sku", ""),
            )
        )
    return rows, rejected


# --------------------------------------------------------------------------- GCP

# GCP lists GPUs as their own SKUs, e.g. "Nvidia H100 80GB GPU running in Americas",
# "Nvidia Tesla A100 GPU running in Belgium", "Nvidia H100 80GB Plus GPU running in ...".
_GCP_DESC = re.compile(
    r"Nvidia\s+(?:Tesla\s+)?(?P<model>[A-Z]+\d+[A-Z]*)(?P<nvl>\s+NVL)?"
    r"(?:\s+(?P<mem>\d+)\s*GB)?(?P<plus>\s+Plus)?\s+GPU\b",
    re.IGNORECASE,
)
# Memory when the description leaves it out (older SKUs, e.g. "Tesla A100" = 40GB).
GCP_DEFAULT_MEMORY_GB = {"A100": 40, "H100": 80, "H200": 141, "B200": 180, "L4": 24, "L40S": 48}
_GCP_SPECIAL_PROGRAMS = re.compile(r"commit|reserved|\bdws\b|calendar|sole tenancy|flex", re.IGNORECASE)


def _gcp_base_price(sku: dict) -> tuple[float | None, str | None]:
    expr = ((sku.get("pricingInfo") or [{}])[0]).get("pricingExpression", {})
    if expr.get("usageUnit") != "h":
        return None, "non_hourly_unit"
    base = [r for r in expr.get("tieredRates") or [] if float(r.get("startUsageAmount", 0)) == 0]
    if not base:
        return None, "no_base_rate"
    unit_price = base[0].get("unitPrice", {})
    if unit_price.get("currencyCode", "USD") != "USD":
        return None, "non_usd_currency"
    # Google Money type: whole units as a string, plus nanos (1e-9 units).
    return int(unit_price.get("units") or 0) + (unit_price.get("nanos") or 0) / 1e9, None


def normalize_gcp(raw: dict, snapshot_date: dt.date, catalog: Catalog | None = None) -> Result:
    rows: list[Row] = []
    rejected: Counter = Counter()
    for sku in raw.get("skus", []):
        category = sku.get("category", {})
        desc = sku.get("description", "")
        usage = category.get("usageType")
        match = _GCP_DESC.search(desc)
        model = match["model"].upper() if match else None

        reason = None
        if category.get("resourceGroup") != "GPU":
            reason = "not_a_gpu_sku"
        elif usage not in ("OnDemand", "Preemptible"):
            reason = "commitment_or_other_usage_type"
        elif _GCP_SPECIAL_PROGRAMS.search(desc):
            reason = "special_pricing_program"
        elif not match:
            reason = "unparseable_description"
        elif model not in GCP_DEFAULT_MEMORY_GB:
            reason = "unsupported_gpu_model"
        elif not sku.get("serviceRegions"):
            reason = "no_regions"

        price = None
        if reason is None:
            price, reason = _gcp_base_price(sku)
        if reason:
            rejected[reason] += 1
            continue

        memory = int(match["mem"]) if match["mem"] else GCP_DEFAULT_MEMORY_GB[model]
        gpu_model = model + (" NVL" if match["nvl"] else "")
        variant = " Plus" if match["plus"] else ""
        for region in sku["serviceRegions"]:
            rows.append(
                _row(
                    snapshot_date,
                    "gcp",
                    region,
                    f"gpu:{gpu_model} {memory}GB{variant}",
                    gpu_model,
                    memory,
                    1,
                    "spot" if usage == "Preemptible" else "on_demand",
                    # GPU line item only: the VM's vCPU/RAM is billed separately.
                    "accelerator_only",
                    price,
                    None,
                    sku.get("skuId", ""),
                )
            )
    return rows, rejected


NORMALIZERS = {"azure": normalize_azure, "aws": normalize_aws, "gcp": normalize_gcp}
