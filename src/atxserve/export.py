from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from .benchmark import write_json


def export(aggregate: Path, output: Path, figures=False):
    rows = json.loads(aggregate.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("Expected the experiment aggregate list")
    output.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row})
    with (output / "results.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fields)
        writer.writeheader()
        writer.writerows(rows)
    grouped = {}
    for row in rows:
        key = (row["study"], row["model"])
        grouped.setdefault(key, []).append(row)
    for (study, model), values in grouped.items():
        name = f"{study}_{model}"
        labels = ["Setting", "Policy", "Runs", "UsefulTP", "Useful", "Elapsed (s)", "Input tokens", "Output tokens"]
        lines = [" | ".join(labels), " | ".join(["---"] * len(labels))]
        for row in values:
            setting = "/".join(str(row.get(key, "")) for key in ("kind", "repository", "case", "agents", "profile"))
            rate = row["ratio_of_means_ops_gpu_hour"] if study == "declarations" else row["useful_throughput_ops_gpu_hour"]
            lines.append(" | ".join([setting, row["policy"], str(row["runs"]), f"{rate:.1f}",
                                     f'{row["useful_operations"]:.1f}', f'{row["elapsed_seconds"]:.1f}',
                                     f'{row["input_tokens"]:.0f}', f'{row["output_tokens"]:.0f}']))
        (output / f"{name}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        if figures:
            plot(values, output / f"{name}.pdf")


def plot(rows, destination):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    policies = list(dict.fromkeys(row["policy"] for row in rows))
    settings = list(dict.fromkeys((row["kind"], row["repository"], row["case"],
                                   row["agents"], row["profile"]) for row in rows))
    labels = ["/".join(str(part) for part in setting if part != "") for setting in settings]
    colors = {"OCC": "#50545A", "OCC Backoff": "#BC7041", "PCC-FIFO": "#637AB0",
              "AtxServe": "#267C65", "FIFO-compatible": "#637AB0",
              "w/o Resolution": "#B23A48", "w/o Reservation": "#8B63A6"}
    figure, axis = plt.subplots(figsize=(max(6, len(settings) * 0.8), 3.6))
    normalized = rows[0]["study"] == "declarations"
    field = "normalized_throughput_pct" if normalized else "useful_throughput_ops_gpu_hour"
    for policy in policies:
        by_setting = {(row["kind"], row["repository"], row["case"], row["agents"], row["profile"]): row
                      for row in rows if row["policy"] == policy}
        values = [by_setting[setting][field]
                  if setting in by_setting and by_setting[setting][field] is not None else float("nan")
                  for setting in settings]
        axis.plot(range(len(settings)), values, marker="o", markersize=4, linewidth=1.5,
                  label=policy, color=colors.get(policy))
    axis.set_xticks(range(len(settings)), labels, rotation=35, ha="right", fontsize=8)
    axis.set_ylabel("Useful throughput (% of Exact)" if normalized else "Useful operations / GPU hour")
    axis.spines[["top", "right"]].set_visible(False)
    axis.legend(frameon=False, fontsize=8)
    axis.grid(axis="y", alpha=0.2)
    figure.tight_layout()
    figure.savefig(destination)
    plt.close(figure)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--aggregate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--figures", action="store_true")
    args = parser.parse_args()
    export(args.aggregate, args.output, args.figures)


if __name__ == "__main__":
    main()
