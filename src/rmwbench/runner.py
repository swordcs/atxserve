from __future__ import annotations
from dataclasses import asdict, dataclass
import json
from functools import partial
from pathlib import Path
from time import perf_counter
from typing import Any
from .agent import MockAgent, TransactionAgent
from .baseline import occ_backoff_delay
from .config import Config
from .metrics import evaluate
from .reasoner import make_reasoner
from .scheduler import run_schedule, run_transaction_schedule
from .workspace import Workspace
from .workload import generate_workload

@dataclass(frozen=True, slots=True)
class RunArtifacts:
    summary: dict[str, Any]
    summary_path: Path
    events_path: Path

def _write_workspace_snapshot(output_dir: Path, snapshots: tuple[dict[str, object], ...]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    by_file: dict[str, list[dict[str, object]]] = {}
    for snapshot in snapshots:
        file_id = str(snapshot['block_id']).split(':', 1)[0]
        by_file.setdefault(file_id, []).append(snapshot)
    for file_id, blocks in by_file.items():
        with (output_dir / f'{file_id}.txt').open('w', encoding='utf-8') as handle:
            for block in blocks:
                handle.write(f"===== {block['block_id']} version={block['version']} hash={block['content_hash']} last_writer={block['last_writer']} last_commit_id={block['last_commit_id']} =====\n")
                handle.write(str(block['content']))
                if not str(block['content']).endswith('\n'):
                    handle.write('\n')

def run_benchmark(config: Config, output_dir: str | Path, *, dump_workspace: bool=False) -> RunArtifacts:
    config.validate()
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    generation_start = perf_counter()
    workload = generate_workload(config)
    generation_seconds = perf_counter() - generation_start
    initialization_start = perf_counter()
    workspace = Workspace(workload.blocks, config.storage, workload.tasks)
    initialization_seconds = perf_counter() - initialization_start
    initial_snapshot = workspace.snapshot() if dump_workspace else ()
    run_start = perf_counter()
    if config.txserve.mode == 'legacy':
        executions = run_schedule(workload.tasks, MockAgent(workspace, config.run), config.agents.count, config.run.scheduler)
    else:
        executions = run_transaction_schedule(workload.tasks, TransactionAgent(workspace, config.run, make_reasoner(config.model, config.run, config.txserve.mode), config.prediction, config.benchmark.seed, config.txserve.refine_access_sets, config.txserve.reuse_probe_snapshot, partial(occ_backoff_delay, base_seconds=config.run.backoff_base_seconds, cap_seconds=config.run.backoff_cap_seconds) if config.txserve.mode == 'occ_backoff' else None, config.txserve.late_bind_write_target, config.txserve.predict_access_sets_after_plan), config.agents.count, config.txserve.max_batch_size, config.txserve.mode, config.txserve.aging_factor, config.run.scheduler)
    elapsed_seconds = perf_counter() - run_start
    results = [execution.result for execution in executions]
    metrics = evaluate(workload.tasks, results, workspace.lost_op_ids, elapsed_seconds)
    operation_results = metrics.pop('operation_results')
    summary: dict[str, Any] = {'schema_version': 2 if config.txserve.mode != 'legacy' else 1, 'benchmark': config.benchmark.name, 'seed': config.benchmark.seed, 'config': asdict(config), 'workspace': {'blocks': len(workload.blocks), 'generation_seconds': generation_seconds, 'initialization_seconds': initialization_seconds}, **metrics}
    events = [event for execution in executions for event in execution.events]
    events.sort(key=lambda event: event['ts_ns'])
    events_path = output / 'events.jsonl'
    with events_path.open('w', encoding='utf-8', buffering=1024 * 1024) as handle:
        for event in events:
            handle.write(json.dumps(event, separators=(',', ':'), sort_keys=True))
            handle.write('\n')
    operations_path = output / 'operations.jsonl'
    with operations_path.open('w', encoding='utf-8', buffering=1024 * 1024) as handle:
        for result in operation_results:
            handle.write(json.dumps(result, separators=(',', ':'), sort_keys=True))
            handle.write('\n')
    summary_path = output / 'summary.json'
    with summary_path.open('w', encoding='utf-8') as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
        handle.write('\n')
    if dump_workspace:
        _write_workspace_snapshot(output / 'workspace' / 'initial', initial_snapshot)
        _write_workspace_snapshot(output / 'workspace' / 'final', workspace.snapshot())
    return RunArtifacts(summary, summary_path, events_path)
