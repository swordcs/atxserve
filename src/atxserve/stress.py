from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
from random import Random
from time import monotonic, sleep
from urllib.request import ProxyHandler, Request, build_opener

from .benchmark import percentile, write_json
from .protocol import AdmissionManager, Declaration, ProtocolError


def arrival_trace(seed):
    rng = Random(seed)
    rows = []
    for index in range(180):
        if index % 12 == 1:
            writes = ["hot0", "hot1", "hot2", "hot3"]
            kind = "center"
        elif index % 3 == 0:
            writes = [f"independent{index % 7}"]
            kind = "independent"
        else:
            writes = [f"hot{rng.randrange(4)}"]
            kind = "leaf"
        rows.append({"id": f"s{seed}-{index:04d}", "arrival_s": index * 0.25,
                     "reads": [], "writes": writes, "kind": kind,
                     "tokens": rng.choice([64, 96, 128])})
    return rows


def run_trial(seed, policy, bound, base_url, model, output):
    if output.exists():
        raise FileExistsError(output)
    manager = AdmissionManager(8, "fifo_hol" if policy == "PCC-FIFO" else "minimum_degree", bound)
    offered = arrival_trace(seed)
    started = monotonic() + 1
    deadline = started + 60

    def execute(item):
        sleep(max(0, started + item["arrival_s"] - monotonic()))
        row = {**item, "arrived_s": monotonic() - started, "admitted_s": None, "finished_s": None}
        grant = None
        try:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return row
            grant = manager.acquire(item["id"], Declaration(frozenset(item["reads"]), frozenset(item["writes"])),
                                    lease_seconds=300, timeout=remaining)
            row["admitted_s"] = monotonic() - started
            row["scheduler_overtakes"] = grant.overtakes
            if monotonic() >= deadline:
                return row
            payload = {"model": model,
                       "prompt": "Continue this ordered sequence of integers separated by spaces: 1 2 3 4 5",
                       "temperature": 0, "min_tokens": item["tokens"], "max_tokens": item["tokens"],
                       "ignore_eos": True}
            request = Request(base_url.rstrip("/") + "/completions", json.dumps(payload).encode(),
                              {"Content-Type": "application/json"})
            with build_opener(ProxyHandler({})).open(request, timeout=180) as response:
                value = json.load(response)
            row["finished_s"] = monotonic() - started
            row["output_tokens"] = value["usage"]["completion_tokens"]
            if row["output_tokens"] != item["tokens"]:
                raise ValueError("The model server did not honor the fixed token budget")
        except ProtocolError as error:
            if error.reason != "ADMISSION_TIMEOUT":
                raise
        finally:
            if grant is not None:
                manager.release(grant.grant_id, item["id"])
        return row

    with ThreadPoolExecutor(max_workers=len(offered)) as pool:
        futures = [pool.submit(execute, item) for item in offered]
        rows = [future.result() for future in futures]
    sleep(max(0, deadline - monotonic()))
    admitted = [row for row in rows if row["admitted_s"] is not None and row["admitted_s"] <= 60]
    finished = [row for row in rows if row["finished_s"] is not None and row["finished_s"] <= 60]
    for row in rows:
        scope = Declaration(frozenset(row["reads"]), frozenset(row["writes"]))
        until = min(60, row["admitted_s"] if row["admitted_s"] is not None else 60)
        row["overtakes_at_deadline"] = sum(
            other["arrived_s"] > row["arrived_s"] and other["admitted_s"] < until
            and scope.conflicts(Declaration(frozenset(other["reads"]), frozenset(other["writes"])))
            for other in admitted)
    waits = [row["admitted_s"] - row["arrived_s"] for row in admitted]
    summary = {"policy": policy, "reservation": bound, "seed": seed, "observation_seconds": 60,
               "completed_stages_per_second": len(finished) / 60, "completed": len(finished),
               "unfinished": len(rows) - len(finished), "wait_p95_seconds": percentile(waits, 0.95),
               "wait_max_seconds": max(waits, default=0),
               "overtakes_max": max(row["overtakes_at_deadline"] for row in rows),
               "metric": "GPU stage completions per second"}
    write_json(output / "requests.json", rows)
    write_json(output / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18080/v1")
    parser.add_argument("--model", default="qwen3-4b")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seeds", type=int, nargs="+", default=[7, 17, 27])
    args = parser.parse_args()
    rows = []
    for seed in args.seeds:
        policies = [("PCC-FIFO", None), ("AtxServe", 4), ("AtxServe", 8), ("AtxServe", 16),
                    ("w/o Reservation", None)]
        Random(seed).shuffle(policies)
        for policy, bound in policies:
            name = policy.replace("/", "").replace(" ", "_") + f"_B{bound}"
            rows.append(run_trial(seed, policy, bound, args.base_url, args.model, args.output / name / str(seed)))
    write_json(args.output / "aggregate.json", rows)


if __name__ == "__main__":
    main()
