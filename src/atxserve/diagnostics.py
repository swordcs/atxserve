from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
from pathlib import Path
from random import Random
from threading import Event
from time import perf_counter, perf_counter_ns

from rmwbench.config import ModelConfig
from .benchmark import write_json
from .model import ModelClient, Usage
from .protocol import AdmissionManager, Declaration, ProtocolError, VersionedWorkspace


SCENARIOS = ("stable", "route_before_bind", "unrelated_before_bind", "managed_after_bind",
             "external_after_bind", "route_bind_check_off")


def trial(model, scenario, repetition, validate_plan=None):
    manager = AdmissionManager(capacity=8)
    store = VersionedWorkspace({"catalog": "A", "A": "value=17", "B": "value=29",
                                "output_A": "", "output_B": "", "unrelated": "0"}, manager)
    usage = Usage()
    counts = {"attempts": 0, "stale_at_bind": 0, "stale_plans_in_inference": 0,
              "commit_rejects": 0, "wrong_target_commits": 0, "completed": 0,
              "capture_ns": 0, "binding_ns": 0}
    owner = f"diagnostic:{repetition}"
    changed = False
    writer = None
    writer_started = Event()

    def managed_writer():
        writer_started.set()
        declaration = Declaration(writes=frozenset({"A"}))
        grant = manager.acquire("writer", declaration)
        attempt = store.begin("writer")
        try:
            declaration = store.finalize(attempt, "writer", declaration)
            store.bind(attempt, "writer", [], grant.grant_id)
            store.publish(attempt, "writer", {"A": "value=31"})
        finally:
            manager.release(grant.grant_id, "writer")
            store.finish(attempt, "writer")

    with ThreadPoolExecutor(max_workers=1) as pool:
        for index in range(3):
            counts["attempts"] += 1
            attempt = store.begin(owner)
            grant = None
            try:
                captured = perf_counter_ns()
                observation = store.plan_read(attempt, owner, "catalog")
                counts["capture_ns"] += perf_counter_ns() - captured
                plan = model.complete("Copy this routing target into JSON as target: " + observation.content,
                                      "plan", f"{owner}:{index}:plan", usage, max_tokens=64)
                target = plan["target"]
                if target not in {"A", "B"}:
                    raise ValueError("Planner returned an invalid target")
                output = "output_" + target
                declaration = store.finalize(attempt, owner,
                                             Declaration(frozenset({target}), frozenset({output})))
                if not changed and scenario in {"route_before_bind", "route_bind_check_off"}:
                    store.external_write("catalog", "B")
                    changed = True
                    counts["stale_at_bind"] += 1
                if not changed and scenario == "unrelated_before_bind":
                    store.external_write("unrelated", "1")
                    changed = True
                grant = manager.acquire(owner, declaration)
                binding = perf_counter_ns()
                try:
                    snapshot = store.bind(attempt, owner, [target], grant.grant_id,
                                          validate_plan=(scenario != "route_bind_check_off"
                                                         if validate_plan is None else validate_plan))
                finally:
                    counts["binding_ns"] += perf_counter_ns() - binding
                stale_plan = store.snapshot()["catalog"].version != observation.version
                counts["stale_plans_in_inference"] += stale_plan
                if not changed and scenario == "managed_after_bind":
                    writer = pool.submit(managed_writer)
                    writer_started.wait()
                    changed = True
                if not changed and scenario == "external_after_bind":
                    store.external_write(target, "value=31")
                    changed = True
                result = model.complete("Return JSON with the integer value from: " + snapshot[target].content,
                                        "execute", f"{owner}:{index}:execute", usage)
                correct = result.get("value") == int(snapshot[target].content.split("=")[1])
                store.publish(attempt, owner, {output: json.dumps(result)})
                counts["wrong_target_commits"] += target != store.snapshot()["catalog"].content
                counts["completed"] = int(correct)
                break
            except ProtocolError as error:
                if error.reason == "STALE_READ":
                    counts["commit_rejects"] += 1
                elif error.reason != "STALE_PLAN":
                    raise
            finally:
                if grant is not None:
                    manager.release(grant.grant_id, owner)
                store.finish(attempt, owner)
        if writer is not None:
            writer.result()
    return {**counts, **usage.totals(), "scenario": scenario, "repetition": repetition,
            "calls": [asdict(call) for call in usage.calls]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18080/v1")
    parser.add_argument("--model", default="qwen3-4b")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument("--scenario", choices=SCENARIOS, nargs="+", default=list(SCENARIOS))
    parser.add_argument("--stable-overhead", action="store_true")
    args = parser.parse_args()
    client = ModelClient(ModelConfig(backend="vllm", base_url=args.base_url, name=args.model), 7)
    if args.output.exists():
        raise FileExistsError(args.output)
    results = []
    started = perf_counter()
    if args.stable_overhead:
        for pair in range(5):
            paths = [False, True]
            Random(7 + pair).shuffle(paths)
            for validated in paths:
                beginning = perf_counter()
                tasks = [trial(client, "stable", f"{pair}:{task}", validated) for task in range(32)]
                results.append({"pair": pair + 1, "validate_plan": validated,
                                "elapsed_seconds": perf_counter() - beginning,
                                "model_calls": sum(task["model_calls"] for task in tasks),
                                "capture_ms_per_attempt": sum(task["capture_ns"] for task in tasks) / 32e6,
                                "binding_ms_per_attempt": sum(task["binding_ns"] for task in tasks) / 32e6,
                                "tasks": tasks})
        write_json(args.output, {"elapsed_seconds": perf_counter() - started, "pairs": results})
        return
    for scenario in args.scenario:
        for repetition in range(args.repetitions):
            results.append(trial(client, scenario, repetition))
    write_json(args.output, {"elapsed_seconds": perf_counter() - started, "trials": results})


if __name__ == "__main__":
    main()
