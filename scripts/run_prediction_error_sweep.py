from __future__ import annotations
import argparse
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
from statistics import mean, pstdev
from typing import Any
from rmwbench.baseline import BASELINE_SETTINGS_BY_MODE
from rmwbench.config import Config, PredictionConfig
from rmwbench.runner import run_benchmark
CONFLICTS = {'low': (0.0, 0.0), 'high': (1.0, 0.5)}
PROFILES = {'oracle': PredictionConfig(), 'fp75': PredictionConfig(read_precision=0.75, write_precision=0.75), 'fp50': PredictionConfig(read_precision=0.5, write_precision=0.5), 'fn75': PredictionConfig(read_recall=0.75, write_recall=0.75), 'fn50': PredictionConfig(read_recall=0.5, write_recall=0.5), 'mixed50': PredictionConfig(read_precision=0.5, read_recall=0.5, write_precision=0.5, write_recall=0.5)}

@dataclass(frozen=True, slots=True)
class Case:
    case_id: str
    conflict: str
    profile: str
CASES = (Case('low_oracle', 'low', 'oracle'), Case('low_fp75', 'low', 'fp75'), Case('low_fp50', 'low', 'fp50'), Case('high_oracle', 'high', 'oracle'), Case('high_fp50', 'high', 'fp50'), Case('high_fn75', 'high', 'fn75'), Case('high_fn50', 'high', 'fn50'), Case('high_mixed50', 'high', 'mixed50'))
METRICS = ('operations.successful', 'operations.aborted', 'correctness.semantic_correctness', 'correctness.basis_correctness', 'correctness.freshness_rate', 'prediction.read.precision', 'prediction.read.recall', 'prediction.write.precision', 'prediction.write.recall', 'prediction.conflict.precision', 'prediction.conflict.recall', 'admission.read.precision', 'admission.read.recall', 'admission.write.precision', 'admission.write.recall', 'admission.conflict.precision', 'admission.conflict.recall', 'admission.exact_operations', 'admission.refined_operations', 'admission.refinement_rate', 'admission.probe_reads', 'admission.snapshot_reused_operations', 'admission.snapshot_rebound_operations', 'admission.snapshot_reuse_rate', 'admission.bypasses_mean', 'admission.bypasses_p95', 'admission.bypasses_max', 'admission.fairness_forced_operations', 'admission.wait_ms.mean', 'admission.wait_ms.p95', 'admission.wait_ms.max', 'prediction.conflict.actual_pairs', 'prediction.conflict.predicted_pairs', 'prediction.exact_operations', 'prediction.underpredicted_operations', 'prediction.overpredicted_operations', 'conflicts.realized_stale_target_operations', 'efficiency.retry_operations', 'efficiency.retry_operation_rate', 'efficiency.extra_attempts', 'efficiency.read_amplification', 'efficiency.waste_ratio', 'efficiency.elapsed_seconds', 'efficiency.model_request_seconds', 'efficiency.control_request_seconds', 'efficiency.useful_operations', 'efficiency.useful_throughput_ops_gpu_hour', 'latency_ms.p95', 'queue_ms.mean', 'queue_ms.p95', 'snapshot_exposure_ms.mean', 'snapshot_exposure_ms.p95')

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
    expected_prediction = asdict(config.prediction)
    if summary['seed'] != config.benchmark.seed or archived['prediction'] != expected_prediction or archived['conflict']['ww_rate'] != config.conflict.ww_rate or (archived['conflict']['rw_rate'] != config.conflict.rw_rate) or (archived['txserve']['mode'] != config.txserve.mode) or (archived['txserve']['refine_access_sets'] != config.txserve.refine_access_sets) or (archived['txserve'].get('reuse_probe_snapshot', True) != config.txserve.reuse_probe_snapshot) or (archived['txserve'].get('predict_access_sets_after_plan', False) != config.txserve.predict_access_sets_after_plan) or (archived['run']['operations'] != config.run.operations):
        return None
    return summary

def _record(case: Case, seed: int, config: Config, run_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
    return {'case': case.case_id, 'profile': case.profile, 'conflict': case.conflict, 'seed': seed, 'summary_path': str(run_dir / 'summary.json'), 'target': asdict(config.prediction), **{metric: _value(summary, metric) for metric in METRICS}}

def _aggregate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record['profile'], record['conflict']].append(record)
    output = []
    for (profile, conflict), rows in sorted(grouped.items()):
        item: dict[str, Any] = {'profile': profile, 'conflict': conflict, 'runs': len(rows), 'target': rows[0]['target']}
        for metric in METRICS:
            values = [float(row[metric]) for row in rows]
            item[metric + '.mean'] = mean(values)
            item[metric + '.std'] = pstdev(values)
        output.append(item)
    return output

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seeds', type=int, nargs='+', default=[7, 17, 27])
    parser.add_argument('--cases', nargs='+', choices=[case.case_id for case in CASES])
    parser.add_argument('--mode', choices=['eager_fifo', 'occ_backoff', 'pessimistic_fifo', 'vllm_lock_manager', 'vllm_lease', 'txserve'], default='vllm_lock_manager')
    parser.add_argument('--refine-access-sets', action='store_true')
    parser.add_argument('--full-reread', action='store_true', help='rebind the protected snapshot instead of validating probe reuse')
    args = parser.parse_args()
    if args.full_reread and (not args.refine_access_sets):
        parser.error('--full-reread requires --refine-access-sets')
    settings = BASELINE_SETTINGS_BY_MODE.get(args.mode, {})
    refine_access_sets = bool(settings.get('refine_access_sets', args.refine_access_sets))
    reuse_probe_snapshot = bool(settings.get('reuse_probe_snapshot', not args.full_reread))
    base = Config.load(args.config)
    selected_ids = set(args.cases or (case.case_id for case in CASES))
    cases = tuple((case for case in CASES if case.case_id in selected_ids))
    root = args.output
    root.mkdir(parents=True, exist_ok=True)
    warmup = replace(base, benchmark=replace(base.benchmark, name=base.benchmark.name + '-warmup'), run=replace(base.run, operations=base.agents.count), conflict=replace(base.conflict, ww_rate=0.0, rw_rate=0.0), prediction=PROFILES['oracle'], txserve=replace(base.txserve, mode=args.mode, refine_access_sets=refine_access_sets, reuse_probe_snapshot=reuse_probe_snapshot))
    run_benchmark(warmup, root / '_warmup')
    records: list[dict[str, Any]] = []
    for seed_index, seed in enumerate(args.seeds):
        offset = seed_index % len(cases)
        ordered_cases = cases[offset:] + cases[:offset]
        for case in ordered_cases:
            ww_rate, rw_rate = CONFLICTS[case.conflict]
            config = replace(base, benchmark=replace(base.benchmark, name=f'{base.benchmark.name}-{case.case_id}', seed=seed), conflict=replace(base.conflict, ww_rate=ww_rate, rw_rate=rw_rate), prediction=PROFILES[case.profile], txserve=replace(base.txserve, mode=args.mode, refine_access_sets=refine_access_sets, reuse_probe_snapshot=reuse_probe_snapshot))
            run_dir = root / case.profile / case.conflict / f'seed_{seed}'
            summary = _complete_summary(run_dir, config)
            if summary is None:
                summary = run_benchmark(config, run_dir).summary
            record = _record(case, seed, config, run_dir, summary)
            records.append(record)
            print(json.dumps(record, sort_keys=True), flush=True)
    payload = {'schema_version': 1, 'mode': args.mode, 'refine_access_sets': refine_access_sets, 'reuse_probe_snapshot': reuse_probe_snapshot, 'late_bind_write_target': base.txserve.late_bind_write_target, 'predict_access_sets_after_plan': base.txserve.predict_access_sets_after_plan, 'seeds': args.seeds, 'cases': [asdict(case) for case in cases], 'profiles': {name: asdict(profile) for name, profile in PROFILES.items()}, 'records': records, 'aggregates': _aggregate(records)}
    with (root / 'aggregate.json').open('w', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write('\n')
if __name__ == '__main__':
    main()
