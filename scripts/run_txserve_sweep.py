from __future__ import annotations
import argparse
from collections import defaultdict
from dataclasses import replace
import json
from pathlib import Path
from statistics import mean, pstdev
from typing import Any
from rmwbench.config import Config
from rmwbench.runner import run_benchmark
CONFLICTS = {'low': (0.0, 0.0), 'medium': (0.5, 0.25), 'high': (1.0, 0.5)}
MODES = ('eager_fifo', 'jit_fifo', 'txserve')

def _mode_order(index: int) -> tuple[str, ...]:
    offset = index % len(MODES)
    return MODES[offset:] + MODES[:offset]

def _value(summary: dict[str, Any], path: str) -> float:
    value: Any = summary
    for key in path.split('.'):
        value = value[key]
    return float(value)
METRICS = ('operations.successful', 'correctness.semantic_correctness', 'conflicts.realized_stale_target_operations', 'efficiency.useful_operations', 'efficiency.useful_throughput_ops_gpu_hour', 'efficiency.waste_ratio', 'efficiency.write_amplification', 'efficiency.elapsed_seconds', 'snapshot_exposure_ms.mean', 'latency_ms.p95')

def _record(config: Config, output: Path, experiment: str, conflict: str, mode: str, seed: int, batch_size: int) -> dict[str, Any]:
    run_dir = output / experiment / conflict / f'batch_{batch_size}' / mode / f'seed_{seed}'
    summary = run_benchmark(config, run_dir).summary
    record = {'experiment': experiment, 'conflict': conflict, 'mode': mode, 'seed': seed, 'batch_size': batch_size, 'summary_path': str(run_dir / 'summary.json'), **{metric: _value(summary, metric) for metric in METRICS}}
    print(json.dumps(record, sort_keys=True), flush=True)
    return record

def _aggregate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[object, ...], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        key = (record['experiment'], record['conflict'], record['mode'], record['batch_size'])
        groups[key].append(record)
    aggregates = []
    for key, rows in sorted(groups.items()):
        aggregate = {'experiment': key[0], 'conflict': key[1], 'mode': key[2], 'batch_size': key[3], 'runs': len(rows)}
        for metric in METRICS:
            values = [float(row[metric]) for row in rows]
            aggregate[metric + '.mean'] = mean(values)
            aggregate[metric + '.std'] = pstdev(values)
        aggregates.append(aggregate)
    return aggregates

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seeds', type=int, nargs='+', default=[7, 17, 27])
    parser.add_argument('--capacity-seeds', type=int, nargs='+', default=[37, 47, 57], help='independent seeds for the batch-capacity sensitivity')
    args = parser.parse_args()
    base = Config.load(args.config)
    args.output.mkdir(parents=True, exist_ok=True)
    warmup = replace(base, benchmark=replace(base.benchmark, name=f'{base.benchmark.name}-warmup'), run=replace(base.run, operations=base.agents.count), conflict=replace(base.conflict, ww_rate=0.0, rw_rate=0.0), txserve=replace(base.txserve, mode='jit_fifo'))
    run_benchmark(warmup, args.output / '_warmup')
    records: list[dict[str, Any]] = []
    for conflict_name, (ww_rate, rw_rate) in CONFLICTS.items():
        for seed_index, seed in enumerate(args.seeds):
            for mode in _mode_order(seed_index):
                config = replace(base, benchmark=replace(base.benchmark, name=f'{base.benchmark.name}-{conflict_name}-{mode}', seed=seed), conflict=replace(base.conflict, ww_rate=ww_rate, rw_rate=rw_rate), txserve=replace(base.txserve, mode=mode))
                records.append(_record(config, args.output, 'conflict_sweep', conflict_name, mode, seed, config.txserve.max_batch_size))
    ww_rate, rw_rate = CONFLICTS['high']
    for seed_index, seed in enumerate(args.capacity_seeds):
        for batch_size in (2, 4, 8):
            for mode in _mode_order(seed_index):
                config = replace(base, benchmark=replace(base.benchmark, name=f'{base.benchmark.name}-capacity-{batch_size}-{mode}', seed=seed), conflict=replace(base.conflict, ww_rate=ww_rate, rw_rate=rw_rate), txserve=replace(base.txserve, mode=mode, max_batch_size=batch_size))
                records.append(_record(config, args.output, 'capacity_sweep', 'high', mode, seed, batch_size))
    payload = {'records': records, 'aggregates': _aggregate(records)}
    with (args.output / 'aggregate.json').open('w', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write('\n')
if __name__ == '__main__':
    main()
