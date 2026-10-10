"""Download raw price lists from each provider's public pricing API.

Collectors only fetch and return raw records. All filtering and cleaning happens
in normalize.py, so the raw snapshots saved to disk can always be re-processed
when the cleaning rules change.

| Provider | Endpoint                         | Auth                                   |
|----------|----------------------------------|----------------------------------------|
| Azure    | Retail Prices API                | none                                   |
| AWS      | Price List Query API (boto3)     | any AWS credentials (free to call)     |
| GCP      | Cloud Billing Catalog API        | a free API key in GCP_API_KEY          |
"""

from __future__ import annotations

import json
import os

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .catalog import Catalog, instance_types

AZURE_URL = "https://prices.azure.com/api/retail/prices"
AZURE_API_VERSION = "2023-01-01-preview"
GCP_COMPUTE_ENGINE_SERVICE_ID = "6F81-5844-456A"
GCP_URL = f"https://cloudbilling.googleapis.com/v1/services/{GCP_COMPUTE_ENGINE_SERVICE_ID}/skus"


def _session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers["User-Agent"] = "gpu-prices/0.1 (public price tracker)"
    return session


def collect_azure(catalog: Catalog, session: requests.Session | None = None) -> dict:
    """One filtered query per catalog SKU, following NextPageLink pagination."""
    session = session or _session()
    items: list[dict] = []
    for sku in instance_types(catalog, "azure"):
        url: str | None = AZURE_URL
        params: dict | None = {
            "api-version": AZURE_API_VERSION,
            "$filter": f"serviceName eq 'Virtual Machines' and armSkuName eq '{sku}'",
        }
        while url:
            resp = session.get(url, params=params, timeout=60)
            resp.raise_for_status()
            body = resp.json()
            items.extend(body.get("Items", []))
            url, params = body.get("NextPageLink"), None  # next link already has the query
    return {"source": AZURE_URL, "items": items}


def collect_aws(catalog: Catalog) -> dict:
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("AWS collection needs boto3: pip install 'compute-price-indices[aws]'") from exc

    # The pricing API is only served from a few regions; us-east-1 covers all of EC2.
    client = boto3.client("pricing", region_name="us-east-1")
    paginator = client.get_paginator("get_products")
    products: list[dict] = []
    for instance_type in instance_types(catalog, "aws"):
        filters = [
            {"Type": "TERM_MATCH", "Field": field, "Value": value}
            for field, value in [
                ("instanceType", instance_type),
                ("operatingSystem", "Linux"),
                ("tenancy", "Shared"),
                ("preInstalledSw", "NA"),
                ("capacitystatus", "Used"),
            ]
        ]
        for page in paginator.paginate(ServiceCode="AmazonEC2", Filters=filters, FormatVersion="aws_v1"):
            products.extend(json.loads(item) for item in page["PriceList"])
    return {"source": "aws-price-list-api:AmazonEC2", "products": products}


def collect_gcp(api_key: str | None = None, session: requests.Session | None = None) -> dict:
    api_key = api_key or os.environ.get("GCP_API_KEY")
    if not api_key:
        raise RuntimeError("GCP collection needs an API key in the GCP_API_KEY environment variable")
    session = session or _session()
    params = {"key": api_key, "currencyCode": "USD", "pageSize": 5000}
    skus: list[dict] = []
    while True:
        resp = session.get(GCP_URL, params=params, timeout=60)
        resp.raise_for_status()
        body = resp.json()
        # Compute Engine has tens of thousands of SKUs; keep only GPU ones.
        skus.extend(s for s in body.get("skus", []) if s.get("category", {}).get("resourceGroup") == "GPU")
        token = body.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token
    return {"source": GCP_URL, "skus": skus}  # note: the API key is never written to disk
