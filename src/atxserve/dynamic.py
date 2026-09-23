from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
from pathlib import Path, PurePosixPath
from random import Random
import subprocess
import tempfile
from time import monotonic, perf_counter, sleep

from rmwbench.config import ModelConfig
from .api import serving
from .benchmark import POLICIES, write_json
from .model import ModelClient, Usage
from .protocol import AdmissionManager, Declaration, ProtocolError, VersionedWorkspace


def repository_files(repository: Path, commit: str) -> dict[str, str]:
    resolved = subprocess.check_output(["git", "-C", str(repository), "rev-parse", commit], text=True).strip()
    if resolved != commit or len(commit) != 40:
        raise ValueError("Dynamic workloads require a full pinned commit")
    listing = subprocess.check_output(["git", "-C", str(repository), "ls-tree", "-rz", commit])
    result = {}
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        path = raw_path.decode()
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts or mode == "120000":
            raise ValueError("Unsafe repository path")
        if kind != "blob":
            continue
        body = subprocess.check_output(["git", "-C", str(repository), "cat-file", "blob", oid])
        try:
            result[path] = body.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return result


def run_checks(snapshot: dict[str, str], commands: list[list[str]], timeout: float, output: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix="atxserve-score-") as directory:
        root = Path(directory)
        for name, content in snapshot.items():
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Unsafe snapshot path")
            destination = root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
        passed = True
        logs = []
        deadline = monotonic() + timeout
        for command in commands:
            remaining = deadline - monotonic()
            if remaining <= 0:
                passed = False
                logs.append({"command": command, "timeout": True})
                break
            try:
                completed = subprocess.run(command, cwd=root, capture_output=True, text=True,
                                           timeout=remaining, check=False)
                passed &= completed.returncode == 0
                logs.append({"command": command, "returncode": completed.returncode,
                             "stdout": completed.stdout, "stderr": completed.stderr})
            except subprocess.TimeoutExpired:
                passed = False
                logs.append({"command": command, "timeout": True})
                break
        write_json(output, logs)
        return passed


class SourceAgent:
    def __init__(self, store, manager, model, policy, budgets, seed):
        self.store = store
        self.manager = manager
        self.model = model
        self.policy = policy
        self.budgets = budgets
        self.seed = seed

    def execute(self, task: dict) -> dict:
        owner = task["id"]
        usage = Usage()
        required_reads = set()
        required_writes = set()
        started = perf_counter()
        plans = 0
        committed = False
        failure = "BUDGET_EXHAUSTED"
        actual_writes = []
        publications = []
        events = []
        rng = Random(f"{self.seed}:{owner}:backoff")
        deadline = monotonic() + self.budgets["task_seconds"]

        def call(prompt, phase, attempt_index, step):
            if monotonic() >= deadline or usage.totals()["model_calls"] >= self.budgets["model_calls"]:
                raise ProtocolError("BUDGET_EXHAUSTED")
            result = self.model.complete(prompt, phase, f"{owner}:{attempt_index}:{phase}:{step}", usage)
            if usage.totals()["input_tokens"] + usage.totals()["output_tokens"] > self.budgets["tokens"]:
                raise ProtocolError("BUDGET_EXHAUSTED")
            return result

        for index in range(self.budgets["attempts"]):
            attempt = self.store.begin(owner)
            grant = None
            plans += 1
            patches = {}
            try:
                history = [{"task": task["instruction"],
                            "required_reads": sorted(required_reads), "required_writes": sorted(required_writes)}]
                plan = None
                for step in range(self.budgets["planning_steps"]):
                    prompt = ("Plan a bounded source-editing transaction. Return a JSON action. "
                              "Actions: list {directory}, read {path}, search {directory,text}, "
                              "declare {reads:[paths],writes:[paths],inputs:[paths]}. "
                              "Use directory: for the repository root. Record every dependency in reads. "
                              "Declare writes before editing. Do not use a shell.\n" + json.dumps(history))
                    action = call(prompt, "plan", index, step)
                    operation = action.get("action")
                    if operation == "declare":
                        plan = action
                        break
                    if operation == "read":
                        item = self.store.plan_read(attempt, owner, action["path"])
                        result = asdict(item)
                    elif operation == "list":
                        item = self.store.plan_read(attempt, owner, action.get("directory", "directory:"))
                        result = asdict(item)
                    elif operation == "search":
                        result = [asdict(item) for item in self.store.search(
                            attempt, owner, action.get("directory", "directory:"), action["text"])]
                    else:
                        raise ProtocolError("INVALID_TOOL_ACTION")
                    history.append({"action": action, "result": result})
                if plan is None:
                    raise ProtocolError("BUDGET_EXHAUSTED")
                reads = set(plan["reads"]) | required_reads
                writes = set(plan["writes"]) | required_writes
                declaration = self.store.finalize(attempt, owner, Declaration(frozenset(reads), frozenset(writes)))
                protected = self.policy not in {"OCC", "OCC Backoff"}
                if protected:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise ProtocolError("BUDGET_EXHAUSTED")
                    grant = self.manager.acquire(owner, declaration, lease_seconds=remaining,
                                                 timeout=remaining)
                selected = tuple(plan.get("inputs", []))
                bound = self.store.bind(attempt, owner, selected, grant.grant_id if grant else None,
                                       require_grant=protected)
                execution = [{"task": task["instruction"], "reads": sorted(declaration.reads),
                              "writes": sorted(declaration.writes),
                              "inputs": {key: item.content for key, item in bound.items()}}]
                finished = False
                for step in range(self.budgets["execution_steps"]):
                    prompt = ("Execute one source-editing stage using JSON actions. "
                              "Actions: read {path}, write {path,content}, finish {}. "
                              "Writes are staged until finish. Request missing resources with read or write; "
                              "scope violations restart planning. Use no shell or external inputs.\n"
                              + json.dumps(execution))
                    action = call(prompt, "execute", index, step)
                    operation = action.get("action")
                    if operation == "finish":
                        finished = True
                        break
                    if operation == "read":
                        item = self.store.read(attempt, owner, action["path"])
                        result = {"content": patches.get(action["path"], item.content)}
                    elif operation == "write":
                        self.store.check_write(attempt, owner, action["path"])
                        if not isinstance(action.get("content"), str):
                            raise ProtocolError("INVALID_PATCH")
                        patches[action["path"]] = action["content"]
                        result = {"staged": action["path"]}
                    else:
                        raise ProtocolError("INVALID_TOOL_ACTION")
                    execution.append({"action": action, "result": result})
                if not finished or not patches or monotonic() >= deadline:
                    raise ProtocolError("BUDGET_EXHAUSTED")
                versions = self.store.publish(attempt, owner, patches)
                committed = True
                failure = None
                actual_writes = sorted(patches)
                publications.append({"attempt": index, "versions": versions})
            except ProtocolError as error:
                failure = error.reason
                events.append({"attempt": index, "reason": error.reason,
                               "resource": error.resource, "permission": error.permission})
                if error.reason == "UNDECLARED_ACCESS":
                    if error.permission == "read":
                        required_reads.add(error.resource)
                    else:
                        required_writes.add(error.resource)
                if error.reason not in {"UNDECLARED_ACCESS", "STALE_PLAN", "STALE_READ", "INVALID_GRANT"}:
                    break
            except (ValueError, KeyError, TypeError) as error:
                failure = "MODEL_OUTPUT_ERROR"
                events.append({"attempt": index, "reason": failure, "detail": str(error)})
            finally:
                try:
                    if grant is not None:
                        self.manager.release(grant.grant_id, owner)
                finally:
                    self.store.finish(attempt, owner)
            if committed:
                break
            if self.policy == "OCC Backoff":
                sleep(rng.uniform(0, 0.4))
        return {"id": owner, "committed": committed, "failure": failure, "plans": plans,
                "extra_plans": max(0, plans - 1), "replanned": plans > 1, "writes": actual_writes,
                "publications": publications, "elapsed_seconds": perf_counter() - started,
                **usage.totals(), "calls": [asdict(item) for item in usage.calls], "events": events}


def run_manifest(manifest_path: Path, output: Path, base_url: str, model_name: str, capacity: int):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    repository = Path(manifest["repository"])
    if not repository.is_absolute():
        repository = manifest_path.parent / repository
    initial = repository_files(repository, manifest["commit"])
    groups = manifest["groups"]
    if len(groups) != 4 or any(len(group["tasks"]) != 4 for group in groups):
        raise ValueError("Appendix O requires four groups of four tasks")
    identifiers = [task["id"] for group in groups for task in group["tasks"]]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Task identifiers must be unique")
    model = ModelConfig(backend="vllm", base_url=base_url, name=model_name,
                        max_tokens=manifest["budgets"]["max_output_tokens"],
                        timeout_seconds=manifest["budgets"]["request_seconds"])
    for seed in manifest.get("seeds", [7, 17, 27]):
        for group in groups:
            permissions = {task["id"]: task["allowed_writes"] for task in group["tasks"]}
            policies = ["Serial", "OCC Backoff", "FIFO-compatible", "AtxServe"]
            Random(f"{seed}:{group['id']}").shuffle(policies)
            for policy in policies:
                trial = output / group["id"] / str(seed) / policy.replace(" ", "_")
                if trial.exists():
                    raise FileExistsError(trial)
                selection, reservation = POLICIES[policy]
                manager = AdmissionManager(1 if policy == "Serial" else capacity,
                                           selection or "minimum_degree", reservation)
                store = VersionedWorkspace(initial, manager, write_permissions=permissions)
                store.register_directory("directory:", initial)
                client = ModelClient(model, seed)
                started = perf_counter()
                with serving(store, policy != "OCC Backoff") as (remote, admission):
                    agent = SourceAgent(remote, admission, client, policy, manifest["budgets"], seed)
                    workers = 1 if policy == "Serial" else 4
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        futures = [pool.submit(agent.execute, task) for task in group["tasks"]]
                        results = [future.result() for future in futures]
                final = {key: item.content for key, item in store.snapshot().items()
                         if not key.startswith("directory:") and item.content is not None}
                score_started = perf_counter()
                for task, result in zip(group["tasks"], results, strict=True):
                    tests = run_checks(final, task["checks"], manifest["budgets"]["score_seconds"],
                                       trial / f"{task['id']}_checks.json")
                    publication = (result["committed"] and len(result["publications"]) == 1
                                   and set(result["writes"]) <= set(task["allowed_writes"]))
                    result["task_tests_passed"] = tests
                    result["publication_checks_passed"] = publication
                    result["passed"] = tests and publication
                regressions = run_checks(final, group["regression_checks"], manifest["budgets"]["score_seconds"],
                                         trial / "regressions.json")
                reserved = manifest["budgets"].get("reserved_scoring_seconds", 0)
                sleep(max(0, reserved - (perf_counter() - score_started)))
                elapsed = perf_counter() - started
                passed = sum(result["passed"] for result in results)
                write_json(trial / "snapshot.json", final)
                write_json(trial / "tasks.json", results)
                write_json(trial / "summary.json",
                           {"group": group["id"], "seed": seed, "policy": policy,
                            "commit": manifest["commit"], "tasks": 4, "passed": passed,
                            "elapsed_seconds": elapsed, "tasks_per_gpu_hour": passed * 3600 / elapsed,
                            "replanned_tasks": sum(result["replanned"] for result in results),
                            "extra_plans": sum(result["extra_plans"] for result in results),
                            "input_tokens": sum(result["input_tokens"] for result in results),
                            "output_tokens": sum(result["output_tokens"] for result in results),
                            "regressions_passed": regressions})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:18080/v1")
    parser.add_argument("--model", default="qwen3-4b")
    parser.add_argument("--capacity", type=int, default=8)
    args = parser.parse_args()
    run_manifest(args.manifest, args.output, args.base_url, args.model, args.capacity)


if __name__ == "__main__":
    main()
