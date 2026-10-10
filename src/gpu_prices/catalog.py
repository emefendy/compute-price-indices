"""Hand-maintained mapping from cloud instance types to the GPUs inside them.

Cloud price lists are priced per *instance*, and instances bundle different
numbers of GPUs (an AWS p5.48xlarge has 8 H100s, an Azure NC40ads_H100_v5 has 1).
To compare prices we need a per-GPU-hour figure, which means we need to know
the GPU count for every instance. Vendors don't expose this consistently
(AWS has a `gpu` attribute, Azure's retail API has nothing), so it lives here,
checked into git where every change is reviewable.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

CATALOG_PATH = Path(__file__).with_name("gpu_catalog.csv")
PROVIDERS = {"aws", "azure"}  # GCP prices GPUs directly, so it needs no catalog


@dataclass(frozen=True)
class GpuInstance:
    provider: str
    instance_type: str
    gpu_model: str
    gpu_count: int
    gpu_memory_gb: int

    @property
    def gpu_class(self) -> str:
        return f"{self.gpu_model}-{self.gpu_memory_gb}GB"


Catalog = dict[tuple[str, str], GpuInstance]


def load_catalog(path: Path = CATALOG_PATH) -> Catalog:
    """Load and sanity-check the catalog. Fails loudly on bad rows."""
    catalog: Catalog = {}
    with open(path, newline="") as f:
        for line_no, rec in enumerate(csv.DictReader(f), start=2):
            inst = GpuInstance(
                provider=rec["provider"].strip(),
                instance_type=rec["instance_type"].strip(),
                gpu_model=rec["gpu_model"].strip(),
                gpu_count=int(rec["gpu_count"]),
                gpu_memory_gb=int(rec["gpu_memory_gb"]),
            )
            if inst.provider not in PROVIDERS:
                raise ValueError(f"{path}:{line_no}: unknown provider {inst.provider!r}")
            if inst.gpu_count <= 0 or inst.gpu_memory_gb <= 0:
                raise ValueError(f"{path}:{line_no}: gpu_count and gpu_memory_gb must be positive")
            key = (inst.provider, inst.instance_type)
            if key in catalog:
                raise ValueError(f"{path}:{line_no}: duplicate entry for {key}")
            catalog[key] = inst
    return catalog


def instance_types(catalog: Catalog, provider: str) -> list[str]:
    return sorted(t for (p, t) in catalog if p == provider)
