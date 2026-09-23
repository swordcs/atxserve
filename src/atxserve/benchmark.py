from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from itertools import groupby
import json
from pathlib import Path
from random import Random
from time import perf_counter, perf_counter_ns, sleep

from rmwbench.config import Config, PredictionConfig
from rmwbench.models import Task
from rmwbench.prediction import predict_access_sets
from rmwbench.workload import generate_workload
from .model import ModelClient, Usage
from .protocol import AdmissionManager, Declaration, Observation, ProtocolError, VersionedWorkspace


POLICIES = {
    "OCC": (None, None),
    "OCC Backoff": (None, None),
    "PCC-FIFO": ("fifo_hol", None),
    "FIFO-HOL": ("fifo_hol", None),
    "FIFO-compatible": ("fifo_compatible", 8),
    "Random-compatible": ("random", None),
    "Minimum-degree": ("minimum_degree", None),
    "AtxServe": ("minimum_degree", 8),
    "w/o Reservation": ("minimum_degree", None),
    "w/o Resolution": ("minimum_degree", 8),
    "Serial": ("fifo_hol", None),
}


@dataclass(frozen=True)
class ProtocolConfig:
    policy: str = "AtxServe"
    capacity: int = 8
    reservation: int | None = 8
    collection_ms: float = 1.0
    validate_plan: bool = True
    resolve_hints: bool = True
    declaration_precision: float = 1.0
    expand_inputs: bool = False
    snapshot_reuse: bool = False
    protocol: str = "validated"
    placement: str = "api"
    admission_timeout_seconds: float = 600.0


def percentile(values, fraction):
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def replace_slot(task: Task, content: str, status: str, timestamp: int) -> str:
    prefix = f"[[SUMMARY_SLOT:{task.summary_slot:04d}]] "
    lines = content.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if line.startswith(prefix)]
    if len(matches) != 1:
        raise ProtocolError("INVALID_SUMMARY_SLOT")
    index = matches[0]
    suffix = "\n" if lines[index].endswith("\n") else ""
    lines[index] = (prefix + f"[[RMWBENCH_UPDATE op={task.op_id} entity={task.entity} "
                    f"status={status} ts={timestamp}]]" + suffix)
    return "".join(lines)


class Benchmark:
    def __init__(self, config: Config, protocol: ProtocolConfig, workspace=None):
        config.validate()
        if protocol.policy not in POLICIES:
            raise ValueError("Unknown transaction policy")
        if not 0 < protocol.declaration_precision <= 1:
            raise ValueError("Declaration precision must be in (0, 1]")
        self.config = config
        self.protocol = protocol
        self.workload = generate_workload(config)
        selection, _ = POLICIES[protocol.policy]
        capacity = 1 if protocol.policy == "Serial" else protocol.capacity
        self.manager = AdmissionManager(capacity, selection or "minimum_degree",
                                        protocol.reservation if selection else None,
                                        protocol.collection_ms, config.benchmark.seed,
                                        resolve_hints=protocol.resolve_hints)
        self.workspace = workspace or VersionedWorkspace(
            {block.block_id: block.content for block in self.workload.blocks}, self.manager)
        self.model = ModelClient(config.model, config.benchmark.seed)
        self.protected = selection is not None

    def execute(self, task: Task) -> dict:
        owner = task.op_id
        usage = Usage()
        events = []
        started = perf_counter_ns()
        rng = Random(f"{self.config.benchmark.seed}:{task.op_id}:backoff")
        success = useful = False
        stale_plans = stale_reads = main_attempts = reads = 0
        wasted_ns = wait_ns = bypasses = capture_ns = binding_ns = exposure_ns = 0
        forced = False
        target = None
        reason = None
        declaration = Declaration()
        graph = Declaration()
        actual = set()
        cached_target = None
        cached_catalog = None

        def event(name, attempt, **values):
            events.append({"event": name, "op_id": task.op_id, "attempt": attempt,
                           "ts_ns": perf_counter_ns(), **values})

        for index in range(self.config.run.max_retries + 1):
            attempt = self.workspace.begin(owner)
            grant = None
            bound_at = None
            call_start = len(usage.calls)
            event("ATTEMPT_START", index)
            try:
                captured = perf_counter_ns()
                catalog = self.workspace.plan_read(attempt, owner, task.catalog_block)
                capture_ns += perf_counter_ns() - captured
                reads += 1
                event("PLAN_READ", index, resource=catalog.resource, version=catalog.version)
                if catalog.content is None:
                    raise ProtocolError("MISSING_CATALOG")
                if cached_target is None or cached_catalog != catalog.version or self.protocol.protocol != "fixed":
                    target = self.model.plan(task.agent_view(), catalog.content,
                                             f"{task.op_id}:plan:{index}", usage)
                else:
                    target = cached_target
                cached_target = target
                cached_catalog = catalog.version
                inputs = tuple(dict.fromkeys((*task.relevant_blocks, task.catalog_block, target)))
                declaration = Declaration(frozenset(inputs), frozenset({target}))
                if self.protocol.declaration_precision < 1:
                    expanded_reads, expanded_writes = predict_access_sets(
                        task.agent_view(), tuple(sorted(declaration.reads)), tuple(sorted(declaration.writes)),
                        PredictionConfig(self.protocol.declaration_precision, 1.0,
                                         self.protocol.declaration_precision, 1.0),
                        self.config.benchmark.seed)
                    declaration = Declaration(frozenset(expanded_reads), frozenset(expanded_writes))
                declaration = self.workspace.finalize(attempt, owner, declaration)
                hinted_reads, hinted_writes = predict_access_sets(
                    task.agent_view(), tuple(sorted(declaration.reads)), tuple(sorted(declaration.writes)),
                    self.config.prediction, self.config.benchmark.seed)
                graph = Declaration(frozenset(hinted_reads), frozenset(hinted_writes))
                if self.protocol.resolve_hints:
                    graph = declaration
                event("PLAN_READY", index, target=target, reads=sorted(declaration.reads),
                      writes=sorted(declaration.writes), graph_reads=sorted(graph.reads),
                      graph_writes=sorted(graph.writes))
                if self.protected:
                    grant = self.manager.acquire(owner, declaration, self.config.model.admission_lease_seconds,
                                                 graph_scope=graph, timeout=self.protocol.admission_timeout_seconds)
                    wait_ns += grant.wait_ns
                    bypasses += grant.overtakes
                    forced |= grant.reserved
                    event("GRANT", index, grant_id=grant.grant_id, wait_ns=grant.wait_ns,
                          overtakes=grant.overtakes, reserved=grant.reserved)
                bind_started = perf_counter_ns()
                selected_inputs = tuple(sorted(declaration.reads)) if self.protocol.expand_inputs else inputs
                snapshot = self.workspace.bind(attempt, owner, selected_inputs,
                                               grant.grant_id if grant else None,
                                               self.protocol.validate_plan, self.protected)
                binding_ns += perf_counter_ns() - bind_started
                bound_at = perf_counter_ns()
                reads += len(snapshot)
                actual.update(snapshot)
                event("BOUND", index, versions={key: item.version for key, item in snapshot.items()})
                extras = tuple(sorted(declaration.reads - set(inputs))) if self.protocol.expand_inputs else ()
                evidence_keys = (*task.relevant_blocks, *extras)
                evidence = [snapshot[key] for key in dict.fromkeys(evidence_keys)]
                if any(item.content is None for item in evidence) or snapshot[target].content is None:
                    raise ProtocolError("MISSING_INPUT")
                main_attempts += 1
                status, timestamp = self.model.patch(task.agent_view(), evidence,
                                                    f"{task.op_id}:execute:{index}", usage)
                replacement = replace_slot(task, snapshot[target].content, status, timestamp)

                def check(bound, patches):
                    expected = replace_slot(task, bound[target].content, status, timestamp)
                    if patches != {target: expected}:
                        raise ProtocolError("UNRELATED_CONTENT_CHANGED")

                versions = self.workspace.publish(attempt, owner, {target: replacement}, check)
                success = True
                routing = f"ROUTE key={task.routing_key} target={target}" in snapshot[task.catalog_block].content.splitlines()
                useful = (routing and target == task.intended_target and status == task.expected_status
                          and timestamp == task.expected_timestamp and set(task.relevant_blocks) <= set(snapshot))
                event("COMMITTED", index, versions=versions, useful=useful, routing_correct=routing)
            except ProtocolError as error:
                reason = error.reason
                event("REJECTED", index, reason=reason, resource=error.resource, permission=error.permission)
                stale_plans += reason == "STALE_PLAN"
                stale_reads += reason == "STALE_READ"
                if reason == "STALE_PLAN":
                    cached_target = None
                if reason not in {"STALE_PLAN", "STALE_READ", "UNDECLARED_ACCESS", "INVALID_GRANT"}:
                    break
            except ValueError as error:
                reason = "MODEL_OUTPUT_ERROR"
                event("REJECTED", index, reason=reason, detail=str(error))
            finally:
                if bound_at is not None:
                    exposure_ns += perf_counter_ns() - bound_at
                if not useful:
                    wasted_ns += sum(call.elapsed_ns for call in usage.calls[call_start:])
                try:
                    if grant is not None:
                        self.manager.release(grant.grant_id, owner)
                        event("RELEASED", index, grant_id=grant.grant_id)
                finally:
                    self.workspace.finish(attempt, owner)
            if success:
                break
            if index < self.config.run.max_retries and self.protocol.policy == "OCC Backoff":
                cap = min(self.config.run.backoff_cap_seconds,
                          self.config.run.backoff_base_seconds * 2 ** index)
                delay = rng.uniform(0, cap)
                event("BACKOFF", index, seconds=delay)
                sleep(delay)
        ended = perf_counter_ns()
        return {"op_id": task.op_id, "wave": task.wave, "success": success, "useful": useful,
                "abort_reason": None if success else reason, "attempts": index + 1,
                "main_attempts": main_attempts, "stale_plans": stale_plans, "stale_reads": stale_reads,
                "selected_target": target, "reads": reads, "latency_ns": ended - started,
                "wait_ns": wait_ns, "overtakes": bypasses, "reserved": forced,
                "capture_ns": capture_ns, "binding_ns": binding_ns, "exposure_ns": exposure_ns,
                "wasted_model_ns": wasted_ns, "declaration_reads": sorted(declaration.reads),
                "declaration_writes": sorted(declaration.writes), "actual_reads": sorted(actual),
                **usage.totals(), "calls": [asdict(call) for call in usage.calls], "events": events}

    def run(self, output: Path, metadata: dict | None = None) -> dict:
        from .api import serving

        local_workspace, local_manager = self.workspace, self.manager
        context = (serving(local_workspace, self.protected, self.protocol.validate_plan)
                   if self.protocol.placement == "api" else nullcontext((local_workspace, local_manager)))
        with context as (workspace, manager):
            self.workspace, self.manager = workspace, manager
            try:
                return self._run(output, metadata)
            finally:
                self.workspace, self.manager = local_workspace, local_manager

    def _run(self, output: Path, metadata: dict | None = None) -> dict:
        if (output / "summary.json").exists():
            raise FileExistsError(f"Refusing to overwrite a completed trial: {output}")
        output.mkdir(parents=True, exist_ok=True)
        write_json(output / "configuration.json",
                   {"benchmark": asdict(self.config), "protocol": asdict(self.protocol),
                    "metadata": metadata or {}})
        started = perf_counter()
        tasks = self.workload.tasks
        workers = 1 if self.protocol.policy == "Serial" else self.config.agents.count
        results = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            if self.config.run.scheduler == "rolling":
                assignments = [tasks[index::workers] for index in range(workers)]

                def run_agent(assigned):
                    return [self.execute(task) for task in assigned]

                futures = [pool.submit(run_agent, assigned) for assigned in assignments]
                for future in futures:
                    results.extend(future.result())
            else:
                for _, grouped in groupby(tasks, key=lambda task: task.wave):
                    futures = [pool.submit(self.execute, task) for task in grouped]
                    results.extend(future.result() for future in futures)
        elapsed = perf_counter() - started
        results.sort(key=lambda item: item["op_id"])
        useful = sum(item["useful"] for item in results)
        model_ns = sum(item["model_ns"] for item in results)
        summary = {
            "schema_version": 3, "config": asdict(self.config), "protocol": asdict(self.protocol),
            "metadata": metadata or {}, "operations": len(results),
            "commits": sum(item["success"] for item in results),
            "aborts": sum(not item["success"] for item in results), "useful_operations": useful,
            "useful_throughput_ops_gpu_hour": useful * 3600 / elapsed,
            "elapsed_seconds": elapsed, "allocated_gpus": 1,
            "stale_operations": sum(item["stale_reads"] > 0 for item in results),
            "retry_operations": sum(item["main_attempts"] > 1 for item in results),
            "replanned_operations": sum(item["planning_calls"] > 1 for item in results),
            "extra_plans": sum(max(0, item["planning_calls"] - 1) for item in results),
            "stale_plan_rejections": sum(item["stale_plans"] for item in results),
            "commit_rejections": sum(item["stale_reads"] for item in results),
            "input_tokens": sum(item["input_tokens"] for item in results),
            "output_tokens": sum(item["output_tokens"] for item in results),
            "model_calls": sum(item["model_calls"] for item in results),
            "model_request_seconds": model_ns / 1e9,
            "waste_ratio": sum(item["wasted_model_ns"] for item in results) / model_ns if model_ns else 0,
            "read_amplification": sum(item["reads"] for item in results) /
                                  max(1, sum(item["success"] for item in results)),
            "wait_p95_seconds": percentile([item["wait_ns"] / 1e9 for item in results], 0.95)
                                if self.protected else None,
            "overtakes_max": max((item["overtakes"] for item in results), default=0)
                             if self.protected else None,
            "admission": self.manager.measurements(),
        }
        with (output / "operations.jsonl").open("w", encoding="utf-8") as handle:
            for item in results:
                handle.write(json.dumps({key: value for key, value in item.items() if key != "events"}) + "\n")
        with (output / "events.jsonl").open("w", encoding="utf-8") as handle:
            for event in sorted((event for item in results for event in item["events"]), key=lambda item: item["ts_ns"]):
                handle.write(json.dumps(event) + "\n")
        write_json(output / "summary.json", summary)
        return summary
