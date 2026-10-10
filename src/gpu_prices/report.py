"""Write reports/README.md plus two charts from the clean data."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import polars as pl  # noqa: E402

from .analyze import price_index, price_summary, spot_discount  # noqa: E402

# Fixed color per provider (color follows the entity, never its rank).
PROVIDER_COLORS = {"aws": "#2a78d6", "azure": "#eb6834", "gcp": "#1baf7a"}
SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
INK, MUTED, GRID = "#222222", "#6b6b6b", "#e6e6e6"


def _style(ax) -> None:
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.grid(color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def to_markdown(df: pl.DataFrame) -> str:
    if df.is_empty():
        return "_No data._\n"
    cols = df.columns
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for row in df.iter_rows():
        lines.append("| " + " | ".join("" if v is None else str(v) for v in row) + " |")
    return "\n".join(lines) + "\n"


def chart_prices(summary: pl.DataFrame, path: Path) -> bool:
    d = summary.filter(pl.col("price_basis") == "instance").sort(["gpu_class", "provider"])
    if d.is_empty():
        return False
    # One bar per (class, provider) that exists, grouped by class with a gap between groups.
    ys, labels_y, label_names, y = [], [], [], 0.0
    for gpu_class in sorted(d["gpu_class"].unique().to_list()):
        group = d.filter(pl.col("gpu_class") == gpu_class)
        start = y
        for row in group.iter_rows(named=True):
            ys.append((y, row))
            y += 1
        labels_y.append((start + y - 1) / 2)
        label_names.append(gpu_class)
        y += 0.6
    fig, ax = plt.subplots(figsize=(8, 0.9 + 0.38 * y))
    seen = set()
    for pos, row in ys:
        provider = row["provider"]
        bar = ax.barh(
            pos,
            row["median"],
            height=0.8,
            color=PROVIDER_COLORS.get(provider, MUTED),
            label=None if provider in seen else provider.upper(),
        )
        seen.add(provider)
        ax.bar_label(bar, labels=[f"${row['median']:.2f}"], padding=3, fontsize=8, color=INK)
    ax.set_yticks(labels_y, label_names)
    ax.invert_yaxis()
    ax.set_xlabel("Median on-demand price, $ per GPU-hour (whole-VM price ÷ GPUs)", color=MUTED, fontsize=9)
    ax.set_title(
        "On-demand GPU prices, latest snapshot (AWS & Azure; GCP prices GPUs separately)",
        loc="left",
        color=INK,
        fontsize=10,
    )
    ax.legend(frameon=False, fontsize=9, loc="lower right")
    _style(ax)
    ax.grid(axis="y", visible=False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return True


def chart_index(indices: dict[str, pl.DataFrame], path: Path) -> bool:
    indices = {k: v for k, v in indices.items() if v.height >= 2}
    if not indices:
        return False
    fig, ax = plt.subplots(figsize=(8, 4))
    for color, (gpu_class, idx) in zip(SERIES_COLORS, sorted(indices.items())):
        ax.plot(idx["snapshot_date"], idx["chained_index"], color=color, linewidth=2, marker="o", markersize=4, label=gpu_class)
    ax.axhline(100, color=MUTED, linewidth=0.8, linestyle="--")
    ax.set_ylabel("Index (first day = 100)", color=MUTED, fontsize=9)
    ax.set_title("Chained Jevons index of on-demand $/GPU-hour", loc="left", color=INK, fontsize=11)
    ax.legend(frameon=False, fontsize=9, loc="center left", bbox_to_anchor=(1.0, 0.5))
    _style(ax)
    span_days = max((v["snapshot_date"][-1] - v["snapshot_date"][0]).days for v in indices.values())
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, span_days // 8)))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return True


def write_report(prices: pl.DataFrame, rejections: pl.DataFrame, issues: pl.DataFrame, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    md = ["# GPU price report\n"]
    if prices.is_empty():
        md.append("No data yet. Run `gpu-prices run` to collect a snapshot.\n")
        (out_dir / "README.md").write_text("\n".join(md))
        return out_dir / "README.md"

    dates = prices["snapshot_date"].unique().sort()
    last = dates[-1]
    md.append(
        f"Latest snapshot: **{last}** · {len(dates)} snapshot(s) since {dates[0]} · "
        f"{prices.filter(pl.col('snapshot_date') == last).height} quotes today.\n"
    )

    summary = price_summary(prices)
    md.append("## On-demand price per GPU-hour\n")
    if chart_prices(summary, out_dir / "prices.png"):
        md.append("![On-demand prices](prices.png)\n")
    md.append(
        "`instance` = whole-VM price divided by GPU count (includes CPU, RAM, networking). "
        "`accelerator_only` = GCP's GPU line item, which excludes the VM it's attached to, "
        "so it reads lower and **is not directly comparable**.\n"
    )
    md.append(to_markdown(summary))

    md.append("## Spot discount vs on-demand\n")
    md.append(to_markdown(spot_discount(prices)))

    md.append("## Price index\n")
    classes = prices.filter(pl.col("price_basis") == "instance")["gpu_class"].unique().sort().to_list()
    indices = {c: price_index(prices, c) for c in classes}
    if chart_index(indices, out_dir / "index.png"):
        md.append("![Price index](index.png)\n")
    else:
        md.append("_The index needs at least two snapshots._\n")
    for gpu_class, idx in indices.items():
        if idx.height >= 2:
            md.append(f"**{gpu_class}**\n")
            md.append(to_markdown(idx.with_columns(pl.col(pl.Float64).round(2))))

    md.append("## Data quality\n")
    if issues.is_empty():
        md.append("No issues found.\n")
    else:
        counts = issues.group_by(["severity", "check"]).agg(pl.len().alias("count")).sort(["severity", "check"])
        md.append(to_markdown(counts))
        md.append("<details><summary>All issues</summary>\n\n" + to_markdown(issues) + "\n</details>\n")

    md.append("## Rows filtered during cleaning\n")
    md.append(
        to_markdown(
            rejections.filter(pl.col("snapshot_date") == last)
            .group_by(["provider", "reason"])
            .agg(pl.col("count").sum())
            .sort(["provider", "count"], descending=[False, True])
        )
    )
    path = out_dir / "README.md"
    path.write_text("\n".join(md))
    return path
