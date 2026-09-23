from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import asdict, replace
import json
from pathlib import Path
from random import Random
from statistics import fmean

from rmwbench.config import Config, PredictionConfig
from .benchmark import Benchmark, POLICIES, ProtocolConfig, write_json
from .model import ModelClient, Usage


ROOT = Path(__file__).resolve().parents[2]
CONFLICTS = {"low": (0.0, 0.0), "medium": (0.5, 0.25), "high": (1.0, 0.5)}
HINTS = {"Exact": (1.0, 1.0), "FP75": (0.75, 1.0), "FP50": (0.5, 1.0),
         "FN75": (1.0, 0.75), "FN50": (1.0, 0.5), "Mixed50": (0.5, 0.5)}
CORE = ("OCC", "OCC Backoff", "PCC-FIFO", "AtxServe")
STUDIES = ("controlled", "focus", "commit0", "selection", "paired", "hints", "sustained",
           "scaling", "declarations", "backoff", "instrumentation")


def base_config(kind, seed, model, base_url, case="high", repository="babel", agents=8):
    names = {"controlled": "vllm_dynamic_write_qwen3_4b.toml",
             "focus": "vllm_dynamic_write_commit_focus_qwen3_4b.toml",
             "commit0": "vllm_dynamic_write_commit0_qwen3_4b.toml"}
    config = Config.load(ROOT / "configs" / names[kind])
    dataset = config.dataset
    if kind == "focus":
        dataset = replace(dataset, root=str(ROOT / "data/commit_focus"), schedule=case)
    elif kind == "commit0":
        dataset = replace(dataset, root=str(ROOT / "data/commit0" / repository))
    ww, rw = CONFLICTS.get(case, (0.0, 0.0))
    return replace(config, benchmark=replace(config.benchmark, seed=seed), dataset=dataset,
                   agents=replace(config.agents, count=agents),
                   conflict=replace(config.conflict, ww_rate=ww, rw_rate=rw),
                   run=replace(config.run, backoff_base_seconds=0.4, backoff_cap_seconds=0.4),
                   txserve=replace(config.txserve, max_batch_size=agents),
                   model=replace(config.model, name=model, base_url=base_url))


def jobs(args):
    study = args.study
    seeds = args.seeds or ([7] if study in {"commit0", "scaling"} else
                          [37, 47, 57] if study == "backoff" else
                          [7, 17, 27, 37, 47] if study == "paired" else [7, 17, 27])
    cases = list(CONFLICTS)
    if study == "focus":
        settings = [("focus", case, "", 8) for case in ("dispersed", "mixed", "focused")]
    elif study == "commit0":
        settings = [("commit0", case, repository, 8) for repository in ("babel", "jinja", "marshmallow")
                    for case in ("medium", "high")]
    elif study in {"selection", "paired"}:
        settings = [("controlled", "high", "", 8), ("focus", "focused", "", 8)]
        if study == "paired":
            settings += [("controlled", "low", "", 8), ("commit0", "high", "babel", 8)]
    elif study == "scaling":
        settings = [("controlled", case, "", agents) for agents in (8, 16, 32) for case in ("medium", "high")]
    elif study == "sustained":
        settings = [("controlled", "high", "", 32)]
    elif study in {"hints", "declarations"}:
        settings = [("controlled", case, "", 8) for case in ("low", "high")]
    elif study == "backoff":
        settings = [("controlled", case, "", 8) for case in ("medium", "high")]
    else:
        settings = [("controlled", case, "", 8) for case in cases]
    for repetition, seed in enumerate(seeds, 1):
        for kind, case, repository, agents in settings:
            if args.agents and agents not in args.agents:
                continue
            config = base_config(kind, seed, args.model, args.base_url, case, repository or "babel", agents)
            policies = CORE
            variants = [("Exact", 1.0, 1.0)]
            if study == "selection":
                policies = ("FIFO-HOL", "FIFO-compatible", "Random-compatible", "Minimum-degree")
            elif study == "paired":
                policies = ("OCC Backoff", "FIFO-compatible", "AtxServe")
                config = replace(config, model=replace(config.model, min_tokens=0, max_tokens=64))
            elif study == "hints":
                policies = ("OCC", "w/o Resolution", "AtxServe")
                names = ("Exact", "FP75", "FP50") if case == "low" else ("Exact", "FP50", "FN75", "FN50", "Mixed50")
                variants = [(name, *HINTS[name]) for name in names]
            elif study == "declarations":
                policies = ("OCC", "PCC-FIFO", "AtxServe")
                variants = [("Exact", 1.0, 1.0), ("FP75", 0.75, 1.0), ("FP50", 0.5, 1.0)]
                config = replace(config, model=replace(config.model, min_tokens=0, max_tokens=64))
            elif study == "sustained":
                policies = ("OCC", "PCC-FIFO", "w/o Reservation", "AtxServe")
                config = replace(config, run=replace(config.run, operations=512),
                                 payload=replace(config.payload, target_tokens=960))
            elif study == "scaling":
                config = replace(config, run=replace(config.run, operations=1024),
                                 payload=replace(config.payload, target_tokens=640),
                                 model=replace(config.model, min_tokens=0, max_tokens=64))
            elif study == "instrumentation":
                policies = ("PCC-FIFO", "w/o Reservation", "AtxServe")
            elif study == "backoff":
                policies = ("OCC Backoff",)
                variants = [(f"base{base}_cap{cap}", base / 1000, cap / 1000)
                            for base in (50, 100, 400) for cap in (400, 1600, 6400)]
            for variant, precision, recall in variants:
                selected_policies = [policy for policy in policies if not args.policies or policy in args.policies]
                Random(f"{seed}:{study}:{kind}:{case}:{variant}").shuffle(selected_policies)
                for policy in selected_policies:
                    current = config
                    if study == "hints":
                        current = replace(current, prediction=PredictionConfig(precision, recall, precision, recall))
                    if study == "backoff":
                        current = replace(current, run=replace(current.run,
                                          backoff_base_seconds=precision, backoff_cap_seconds=recall))
                    validated = study in {"selection", "paired", "declarations"}
                    reservation = None if study == "selection" else POLICIES[policy][1]
                    protocol = ProtocolConfig(
                        policy=policy, capacity=args.capacity or agents, reservation=reservation,
                        validate_plan=validated, protocol="validated" if validated else "fixed",
                        resolve_hints=policy != "w/o Resolution",
                        declaration_precision=precision if study == "declarations" else 1.0,
                        placement=args.placement)
                    metadata = {"study": study, "kind": kind, "case": case, "repository": repository,
                                "agents": agents, "profile": variant, "policy": policy, "seed": seed,
                                "repetition": repetition, "model": args.model}
                    yield current, protocol, metadata


def aggregate(root: Path, output: Path):
    groups = defaultdict(list)
    for path in sorted(root.rglob("summary.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(row, dict) or row.get("schema_version") != 3 or "metadata" not in row:
            continue
        meta = row["metadata"]
        key = tuple(meta.get(field, "") for field in
                    ("study", "model", "kind", "repository", "case", "agents", "profile", "policy"))
        groups[key].append(row)
    rows = []
    for key, values in groups.items():
        names = ("study", "model", "kind", "repository", "case", "agents", "profile", "policy")
        row = dict(zip(names, key))
        row["runs"] = len(values)
        row["seeds"] = [value["metadata"]["seed"] for value in values]
        for field in ("useful_throughput_ops_gpu_hour", "elapsed_seconds", "useful_operations",
                      "commits", "aborts", "input_tokens", "output_tokens", "waste_ratio",
                      "stale_operations", "retry_operations", "replanned_operations"):
            row[field] = fmean(value[field] for value in values)
        row["ratio_of_means_ops_gpu_hour"] = 3600 * row["useful_operations"] / row["elapsed_seconds"]
        rows.append(row)
    for row in rows:
        own_exact = row["study"] == "declarations"
        baseline_policy = (row["policy"] if own_exact else
                           "OCC Backoff" if row["study"] == "paired" else "OCC")
        baseline_profile = "Exact" if own_exact else row["profile"]
        baseline = next((other for other in rows
                         if other["policy"] == baseline_policy and other["profile"] == baseline_profile
                         and all(other[key] == row[key] for key in
                                 ("study", "model", "kind", "repository", "case", "agents"))), None)
        field = "ratio_of_means_ops_gpu_hour" if own_exact else "useful_throughput_ops_gpu_hour"
        ratio = row[field] / baseline[field] if baseline and baseline[field] else None
        row["relative_throughput_pct"] = 100 * (ratio - 1) if ratio is not None else None
        row["normalized_throughput_pct"] = 100 * ratio if ratio is not None else None
    write_json(output, rows)
    return rows


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--study", choices=STUDIES, required=True)
    run.add_argument("--model", default="qwen3-4b")
    run.add_argument("--base-url", default="http://127.0.0.1:18080/v1")
    run.add_argument("--output", type=Path, default=ROOT / "results")
    run.add_argument("--seeds", type=int, nargs="+")
    run.add_argument("--policies", choices=list(POLICIES), nargs="+")
    run.add_argument("--capacity", type=int, default=8)
    run.add_argument("--agents", type=int, nargs="+")
    run.add_argument("--placement", choices=["api", "local"], default="api")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--warmup-requests", type=int, default=8)
    listing = subparsers.add_parser("aggregate")
    listing.add_argument("--input", type=Path, required=True)
    listing.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "aggregate":
        aggregate(args.input, args.output)
        return
    warmed = False
    for config, protocol, meta in jobs(args):
        name = "_".join(str(meta[key]).replace("/", "").replace(" ", "_") for key in
                        ("kind", "repository", "case", "agents", "profile", "policy", "seed"))
        output = args.output / args.study / args.model / name
        if args.resume and (output / "summary.json").exists():
            saved = json.loads((output / "configuration.json").read_text(encoding="utf-8"))
            expected = {"benchmark": asdict(config), "protocol": asdict(protocol), "metadata": meta}
            if saved != expected:
                raise ValueError(f"Resume configuration differs: {output}")
            continue
        if not warmed:
            usage = Usage()
            model = ModelClient(config.model, config.benchmark.seed)
            for index in range(args.warmup_requests):
                model.complete('Return exactly this JSON object: {"status":"ready","timestamp":1}',
                               "execute", f"warmup:{index}", usage)
            write_json(args.output / f"warmup_{args.study}_{args.model}.json",
                       {"excluded_from_trial_timing": True, **usage.totals()})
            warmed = True
        Benchmark(config, protocol).run(output, meta)
    aggregate(args.output, args.output / "aggregate.json")


if __name__ == "__main__":
    main()
