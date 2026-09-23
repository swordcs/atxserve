from __future__ import annotations
import argparse
from collections import defaultdict
from dataclasses import replace
import json
from pathlib import Path
from statistics import mean, pstdev
from typing import Any
from rmwbench.baseline import BASELINE_SETTINGS_BY_MODE
from rmwbench.config import Config
from rmwbench.runner import run_benchmark
CONFLICTS = {'low': (0.0, 0.0), 'medium': (0.5, 0.25), 'high': (1.0, 0.5), 'rw_only': (0.0, 1.0)}
METRICS = ('operations.successful', 'operations.aborted', 'correctness.semantic_correctness', 'conflicts.realized_stale_target_operations', 'efficiency.useful_operations', 'efficiency.useful_throughput_ops_gpu_hour', 'efficiency.waste_ratio', 'efficiency.elapsed_seconds', 'efficiency.model_request_seconds', 'efficiency.control_request_seconds', 'efficiency.read_amplification', 'snapshot_exposure_ms.mean', 'latency_ms.p95', 'tokens.model_output_per_second', 'tokens.control_output_total')

def _value(summary: dict[str, Any], path: str) -> float:
    value: Any = summary
    for key in path.split('.'):
        value = value[key]
    return float(value)

def _complete_summary(run_dir: Path, config: Config) -> dict[str, Any] | None:
    paths = tuple((run_dir / name for name in ('summary.json', 'operations.jsonl', 'events.jsonl')))
    if not all((path.is_file() for path in paths)):
        return None
    summary = json.loads(paths[0].read_text())
    archived = summary['config']
    if summary['seed'] != config.benchmark.seed or archived['conflict']['ww_rate'] != config.conflict.ww_rate or archived['conflict']['rw_rate'] != config.conflict.rw_rate or (archived['txserve']['mode'] != config.txserve.mode) or (archived['txserve'].get('late_bind_write_target', False) != config.txserve.late_bind_write_target) or (archived['txserve'].get('predict_access_sets_after_plan', False) != config.txserve.predict_access_sets_after_plan) or (archived['run']['operations'] != config.run.operations) or (archived['model']['name'] != config.model.name):
        return None
    return summary

def _aggregate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[str(record['mode']), str(record['conflict'])].append(record)
    aggregates = []
    for (mode, conflict), rows in sorted(groups.items()):
        aggregate: dict[str, Any] = {'mode': mode, 'conflict': conflict, 'runs': len(rows)}
        for metric in METRICS:
            values = [float(row[metric]) for row in rows]
            aggregate[metric + '.mean'] = mean(values)
            aggregate[metric + '.std'] = pstdev(values)
        aggregates.append(aggregate)
    return aggregates

def _txserve_mode(config: Any, mode: str) -> Any:
    settings = BASELINE_SETTINGS_BY_MODE.get(mode)
    if settings is None:
        return replace(config, mode=mode)
    return replace(config, mode=mode, refine_access_sets=bool(settings['refine_access_sets']), reuse_probe_snapshot=bool(settings['reuse_probe_snapshot']))

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--variant', required=True)
    parser.add_argument('--seeds', type=int, nargs='+', default=[7, 17, 27])
    parser.add_argument('--conflicts', nargs='+', choices=list(CONFLICTS), default=['low', 'high'])
    parser.add_argument('--modes', nargs='+', choices=['eager_fifo', 'occ_backoff', 'pessimistic_fifo', 'jit_fifo', 'txserve', 'vllm_lease', 'vllm_lock_manager', 'vllm_serial_graph'], help='optional transaction modes to compare on the same server')
    args = parser.parse_args()
    base = Config.load(args.config)
    modes = tuple(args.modes or (base.txserve.mode,))
    root = args.output / args.variant
    root.mkdir(parents=True, exist_ok=True)
    warmup = replace(base, benchmark=replace(base.benchmark, name=f'{base.benchmark.name}-warmup'), run=replace(base.run, operations=base.agents.count), conflict=replace(base.conflict, ww_rate=0.0, rw_rate=0.0), txserve=_txserve_mode(base.txserve, modes[0]))
    run_benchmark(warmup, root / '_warmup')
    records: list[dict[str, Any]] = []
    conflict_names = tuple(args.conflicts)
    for seed_index, seed in enumerate(args.seeds):
        ordered_conflicts = conflict_names if seed_index % 2 == 0 else tuple(reversed(conflict_names))
        for conflict_name in ordered_conflicts:
            ww_rate, rw_rate = CONFLICTS[conflict_name]
            ordered_modes = modes[seed_index % len(modes):] + modes[:seed_index % len(modes)]
            for mode in ordered_modes:
                config = replace(base, benchmark=replace(base.benchmark, name=f'{base.benchmark.name}-{args.variant}-{conflict_name}-{mode}', seed=seed), conflict=replace(base.conflict, ww_rate=ww_rate, rw_rate=rw_rate), txserve=_txserve_mode(base.txserve, mode))
                run_dir = root / mode / conflict_name / f'seed_{seed}'
                summary = _complete_summary(run_dir, config)
                if summary is None:
                    summary = run_benchmark(config, run_dir).summary
                record = {'variant': args.variant, 'mode': mode, 'conflict': conflict_name, 'seed': seed, 'summary_path': str(run_dir / 'summary.json'), **{metric: _value(summary, metric) for metric in METRICS}}
                records.append(record)
                print(json.dumps(record, sort_keys=True), flush=True)
    payload = {'variant': args.variant, 'records': records, 'aggregates': _aggregate(records)}
    with (root / 'aggregate.json').open('w', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write('\n')
if __name__ == '__main__':
    main()
