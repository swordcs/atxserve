from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
from random import Random
from time import perf_counter

from rmwbench.config import PredictionConfig
from rmwbench.prediction import predict_access_sets
from rmwbench.workload import generate_workload
from .benchmark import write_json
from .experiments import base_config
from .model import ModelClient, Usage
from .protocol import AdmissionManager, Declaration, VersionedWorkspace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18080/v1")
    parser.add_argument("--model", default="qwen3-4b")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seeds", type=int, nargs="+", default=[7, 17, 27])
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    trials = []
    for seed in args.seeds:
        config = base_config("controlled", seed, args.model, args.base_url, "low")
        workload = generate_workload(config)
        model = ModelClient(config.model, seed)
        manager = AdmissionManager()
        store = VersionedWorkspace({item.block_id: item.content for item in workload.blocks}, manager)
        snapshots = store.snapshot()
        conditions = [("Reference", 1.0), ("Scope-only", 0.75), ("Scope-only", 0.5),
                      ("Context-added", 0.75), ("Context-added", 0.5)]
        Random(seed).shuffle(conditions)
        rows = []
        for path, precision in conditions:
            started = perf_counter()
            for task in workload.tasks:
                usage = Usage()
                target = model.plan(task.agent_view(), snapshots[task.catalog_block].content,
                                    f"{task.op_id}:fixed:plan", usage)
                base = tuple(dict.fromkeys((*task.relevant_blocks, task.catalog_block, target)))
                reads, writes = predict_access_sets(
                    task.agent_view(), base, (target,), PredictionConfig(precision, 1.0, precision, 1.0), seed)
                extras = tuple(sorted(set(reads) - set(base))) if path == "Context-added" else ()
                evidence_keys = tuple(dict.fromkeys((*task.relevant_blocks, *extras)))
                evidence = [snapshots[key] for key in evidence_keys]
                status, timestamp = model.patch(task.agent_view(), evidence, f"{task.op_id}:fixed:execute", usage)
                rows.append({"task": task.op_id, "path": path, "precision": precision,
                             "reads": list(reads), "writes": list(writes),
                             "prompt_matches_reference": not extras,
                             "correct": status == task.expected_status and timestamp == task.expected_timestamp,
                             **usage.totals(), "calls": [asdict(call) for call in usage.calls]})
            trials.append({"seed": seed, "path": path, "precision": precision,
                           "elapsed_seconds": perf_counter() - started})
        reference = {row["task"]: row for row in rows if row["path"] == "Reference"}
        summaries = []
        for path, precision in conditions:
            selected = [row for row in rows if row["path"] == path and row["precision"] == precision]
            summaries.append({"path": path, "precision": precision, "tasks": len(selected),
                              "correct": sum(row["correct"] for row in selected),
                              "prompt_matches": sum(row["prompt_matches_reference"] for row in selected),
                              "input_tokens": sum(row["input_tokens"] for row in selected),
                              "correct_to_wrong": sum(reference[row["task"]]["correct"] and not row["correct"] for row in selected),
                              "wrong_to_correct": sum(not reference[row["task"]]["correct"] and row["correct"] for row in selected)})
        write_json(args.output / str(seed) / "operations.json", rows)
        write_json(args.output / str(seed) / "summary.json", summaries)
    write_json(args.output / "trials.json", trials)


if __name__ == "__main__":
    main()

