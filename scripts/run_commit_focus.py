from __future__ import annotations
import argparse
from collections import defaultdict
from dataclasses import asdict, replace
import json
from pathlib import Path
from statistics import fmean, pstdev
from typing import Any
from rmwbench.baseline import BASELINE_POLICIES
from rmwbench.commit_focus import WORKLOADS, stable_json_hash
from rmwbench.config import Config
from rmwbench.runner import run_benchmark
from rmwbench.workload import generate_workload
RMWBENCH_ROOT = Path(__file__).resolve().parents[1]
POLICIES = {**BASELINE_POLICIES, 'atxserve': {'mode': 'vllm_lock_manager', 'refine_access_sets': True, 'reuse_probe_snapshot': True}}
DEFAULT_POLICIES = ('original', 'atxserve')
METRICS = ('operations.successful', 'operations.aborted', 'conflicts.realized_stale_target_operations', 'efficiency.retry_operations', 'efficiency.elapsed_seconds', 'efficiency.throughput_ops_s', 'efficiency.useful_operations', 'efficiency.useful_throughput_ops_gpu_hour', 'efficiency.model_request_seconds', 'efficiency.wasted_model_seconds', 'efficiency.waste_ratio', 'latency_ms.p50', 'latency_ms.p95', 'tokens.model_input_total', 'tokens.model_output_total')

def _path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else RMWBENCH_ROOT / path

def _value(summary: dict[str, Any], path: str) -> float:
    value: Any = summary
    for component in path.split('.'):
        value = value[component]
    return float(value)

def _complete_summary(run_dir: Path, config: Config) -> dict[str, Any] | None:
    paths = tuple((run_dir / name for name in ('summary.json', 'operations.jsonl', 'events.jsonl')))
    if not all((path.is_file() for path in paths)):
        return None
    with paths[0].open(encoding='utf-8') as handle:
        summary = json.load(handle)
    archived = summary.get('config')
    if isinstance(archived, dict):
        archived.get('txserve', {}).setdefault('predict_access_sets_after_plan', False)
    return summary if archived == asdict(config) else None

def _operation_signature(path: Path) -> dict[str, tuple[Any, ...]]:
    with path.open(encoding='utf-8') as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    return {str(row['op_id']): (int(row['wave']), row['selected_target'], tuple(sorted(row['predicted_read_blocks'])), tuple(sorted(row['predicted_write_blocks'])), tuple(sorted(row['actual_read_blocks'])), tuple(sorted(row['actual_write_blocks']))) for row in rows}

def _validate_pair(original_dir: Path, atxserve_dir: Path) -> None:
    original = _operation_signature(original_dir / 'operations.jsonl')
    atxserve = _operation_signature(atxserve_dir / 'operations.jsonl')
    if original != atxserve:
        raise ValueError('paired runs differ in operations, waves, targets, or access sets')

def _policy_config(base: Config, *, workload: str, policy: str, seed: int) -> Config:
    values = POLICIES[policy]
    config = replace(base, benchmark=replace(base.benchmark, name=f'{base.benchmark.name}-{workload}-{policy}', seed=seed), dataset=replace(base.dataset, schedule=workload), txserve=replace(base.txserve, mode=str(values['mode']), refine_access_sets=bool(values['refine_access_sets']), reuse_probe_snapshot=bool(values['reuse_probe_snapshot'])))
    config.validate()
    return config

def _input_signature(config: Config) -> str:
    workload = generate_workload(config)
    return stable_json_hash({'blocks': [(block.block_id, block.content, block.anchors, block.token_count) for block in workload.blocks], 'tasks': sorted(((task.op_id, task.entity, task.routing_key, task.intended_target, task.candidate_targets, task.catalog_block, task.relevant_blocks, task.expected_status, task.expected_timestamp, task.summary_slot) for task in workload.tasks))})

def _record(workload: str, policy: str, seed: int, run_dir: Path, summary: dict[str, Any], offered: dict[str, Any]) -> dict[str, Any]:
    summary_path = run_dir / 'summary.json'
    try:
        portable_summary_path = summary_path.relative_to(RMWBENCH_ROOT).as_posix()
    except ValueError:
        portable_summary_path = str(summary_path)
    return {'workload': workload, 'policy': policy, 'seed': seed, 'summary_path': portable_summary_path, 'offered_ww_pair_rate': offered['offered_ww_pair_rate'], 'offered_rw_pair_rate': offered['offered_rw_pair_rate'], 'offered_any_pair_rate': offered['offered_any_pair_rate'], 'offered_conflicting_txn_rate': offered['offered_conflicting_txn_rate'], **{metric: _value(summary, metric) for metric in METRICS}}

def _percent(value: float, baseline: float) -> float | None:
    return None if baseline == 0.0 else (value / baseline - 1.0) * 100.0

def _mean_records(records: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, float]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record['workload'], record['policy']].append(record)
    aggregates: dict[str, dict[str, dict[str, float]]] = defaultdict(dict)
    fields = (*METRICS, 'offered_ww_pair_rate', 'offered_rw_pair_rate', 'offered_any_pair_rate', 'offered_conflicting_txn_rate')
    for (workload, policy), values in grouped.items():
        aggregates[workload][policy] = {field: fmean((float(value[field]) for value in values)) for field in fields}
        aggregates[workload][policy]['seeds'] = float(len(values))
        aggregates[workload][policy]['useful_tp_std'] = pstdev((float(value['efficiency.useful_throughput_ops_gpu_hour']) for value in values))
    return dict(aggregates)

def _comparisons(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped = {(record['workload'], record['policy'], record['seed']): record for record in records}
    comparisons = []
    keys = sorted({(record['workload'], record['seed']) for record in records})
    for workload, seed in keys:
        original = grouped.get((workload, 'original', seed))
        atxserve = grouped.get((workload, 'atxserve', seed))
        if original is None or atxserve is None:
            continue
        comparisons.append({'workload': workload, 'seed': seed, 'offered_any_pair_rate': original['offered_any_pair_rate'], 'original': {metric: original[metric] for metric in METRICS}, 'atxserve': {metric: atxserve[metric] for metric in METRICS}, 'change_pct': {metric: _percent(atxserve[metric], original[metric]) for metric in METRICS}})
    return comparisons

def _acceptance(records: list[dict[str, Any]], comparisons: list[dict[str, Any]], expected_runs: int, gradient: bool) -> dict[str, Any]:
    mixed = [item for item in comparisons if item['workload'] == 'mixed']
    focused = [item for item in comparisons if item['workload'] == 'focused']

    def useful_tp_gain(item: dict[str, Any]) -> float:
        return float(item['change_pct']['efficiency.useful_throughput_ops_gpu_hour'] or 0.0)
    checks = {'complete_matrix': len(records) == expected_runs, 'offered_conflict_strictly_increases': gradient, 'all_pairs_validated': len(comparisons) * 2 == sum((record['policy'] in DEFAULT_POLICIES for record in records)), 'atxserve_success_not_lower': all((item['atxserve']['operations.successful'] >= item['original']['operations.successful'] for item in comparisons)), 'mixed_mean_useful_tp_positive': bool(mixed) and fmean((useful_tp_gain(item) for item in mixed)) > 0.0, 'focused_mean_useful_tp_at_least_5pct': bool(focused) and fmean((useful_tp_gain(item) for item in focused)) >= 5.0, 'focused_stale_retry_waste_reduced_every_seed': bool(focused) and all((item['atxserve']['conflicts.realized_stale_target_operations'] < item['original']['conflicts.realized_stale_target_operations'] and item['atxserve']['efficiency.retry_operations'] < item['original']['efficiency.retry_operations'] and (item['atxserve']['efficiency.waste_ratio'] < item['original']['efficiency.waste_ratio']) for item in focused))}
    return {'checks': checks, 'validated': all(checks.values())}

def _markdown(model: str, comparisons: list[dict[str, Any]], acceptance: dict[str, Any]) -> str:
    lines = [f'# Commit focus paired experiment on {model}', '', 'All workloads replay the same commit-derived transaction multiset.', '', '| Workload | Seed | Offered any pair | Success O/A | Stale O/A | Retry O/A | Elapsed change | UsefulTP change | Waste O/A | P95 change |', '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    order = {name: index for index, name in enumerate(WORKLOADS)}
    for item in sorted(comparisons, key=lambda value: (order[value['workload']], value['seed'])):
        original = item['original']
        atxserve = item['atxserve']
        change = item['change_pct']
        lines.append(f"| {item['workload']} | {item['seed']} | {item['offered_any_pair_rate']:.3f} | {original['operations.successful']:.0f}/{atxserve['operations.successful']:.0f} | {original['conflicts.realized_stale_target_operations']:.0f}/{atxserve['conflicts.realized_stale_target_operations']:.0f} | {original['efficiency.retry_operations']:.0f}/{atxserve['efficiency.retry_operations']:.0f} | {change['efficiency.elapsed_seconds']:+.1f}% | {change['efficiency.useful_throughput_ops_gpu_hour']:+.1f}% | {original['efficiency.waste_ratio']:.3f}/{atxserve['efficiency.waste_ratio']:.3f} | {change['latency_ms.p95']:+.1f}% |")
    lines.extend(['', '## Acceptance', ''])
    for name, passed in acceptance['checks'].items():
        lines.append(f"- {('PASS' if passed else 'FAIL')}: {name}")
    return '\n'.join(lines) + '\n'

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='configs/vllm_commit_focus_qwen3_4b.toml')
    parser.add_argument('--output', default='results/qwen3_4b/commit_focus_3seed')
    parser.add_argument('--seeds', type=int, nargs='+', default=[7, 17, 27])
    parser.add_argument('--workloads', nargs='+', choices=WORKLOADS, default=list(WORKLOADS))
    parser.add_argument('--policies', nargs='+', choices=tuple(POLICIES), default=list(DEFAULT_POLICIES))
    parser.add_argument('--max-runs', type=int)
    parser.add_argument('--dry-run', action='store_true')
    return parser.parse_args()

def main() -> None:
    args = parse_args()
    base = Config.load(_path(args.config))
    output = _path(args.output)
    dataset_root = _path(base.dataset.root)
    with (dataset_root / 'dataset_manifest.json').open(encoding='utf-8') as handle:
        dataset_manifest = json.load(handle)
    with (dataset_root / 'offered_conflicts.json').open(encoding='utf-8') as handle:
        offered = json.load(handle)
    input_hashes: dict[str, str] = {}
    for seed in args.seeds:
        signatures = {workload: _input_signature(_policy_config(base, workload=workload, policy='original', seed=seed)) for workload in WORKLOADS}
        if len(set(signatures.values())) != 1:
            raise ValueError(f'seed {seed} changes payloads across focus levels')
        input_hashes[str(seed)] = next(iter(signatures.values()))
        values = [offered[f'{workload}:seed_{seed}']['offered_any_pair_rate'] for workload in WORKLOADS]
        if not values[0] < values[1] < values[2]:
            raise ValueError(f'seed {seed} lacks the required offered-conflict gradient')
    matrix = []
    for seed_index, seed in enumerate(args.seeds):
        for workload_index, workload in enumerate(args.workloads):
            policies = list(args.policies)
            if (seed_index + workload_index) % 2:
                policies.reverse()
            for policy in policies:
                config = _policy_config(base, workload=workload, policy=policy, seed=seed)
                run_dir = output / workload / policy / f'seed_{seed}'
                matrix.append((workload, policy, seed, config, run_dir))
    if args.dry_run:
        for workload, policy, seed, _, run_dir in matrix:
            print(workload, policy, seed, run_dir)
        return
    records = []
    completed_runs = 0
    for workload, policy, seed, config, run_dir in matrix:
        summary = _complete_summary(run_dir, config)
        if summary is None:
            if args.max_runs is not None and completed_runs >= args.max_runs:
                continue
            print(f'running {workload} {policy} seed={seed}')
            summary = run_benchmark(config, run_dir).summary
            completed_runs += 1
        else:
            print(f'reusing {workload} {policy} seed={seed}')
        records.append(_record(workload, policy, seed, run_dir, summary, offered[f'{workload}:seed_{seed}']))
        pair_root = output / workload
        original_dir = pair_root / 'original' / f'seed_{seed}'
        atxserve_dir = pair_root / 'atxserve' / f'seed_{seed}'
        if all(((path / 'operations.jsonl').is_file() for path in (original_dir, atxserve_dir))):
            _validate_pair(original_dir, atxserve_dir)
    comparisons = _comparisons(records)
    gradient = all((offered[f'dispersed:seed_{seed}']['offered_any_pair_rate'] < offered[f'mixed:seed_{seed}']['offered_any_pair_rate'] < offered[f'focused:seed_{seed}']['offered_any_pair_rate'] for seed in args.seeds))
    acceptance = _acceptance(records, comparisons, len(args.seeds) * len(args.workloads) * len(args.policies), gradient)
    payload = {'schema_version': 1, 'model': base.model.name, 'dataset_manifest': dataset_manifest, 'cross_workload_input_hashes': input_hashes, 'records': records, 'aggregates': _mean_records(records), 'comparisons': comparisons, 'acceptance': acceptance}
    output.mkdir(parents=True, exist_ok=True)
    with (output / 'aggregate.json').open('w', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write('\n')
    (output / 'comparison.md').write_text(_markdown(base.model.name, comparisons, acceptance), encoding='utf-8')
    print(output / 'comparison.md')
    full_matrix = set(args.workloads) == set(WORKLOADS) and set(args.policies) == set(DEFAULT_POLICIES)
    if full_matrix and len(records) == len(matrix) and (not acceptance['validated']):
        raise SystemExit('commit focus pilot completed but failed acceptance')
if __name__ == '__main__':
    main()
