from __future__ import annotations
import argparse
from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
from typing import Any
from rmwbench.config import Config
from rmwbench.runner import run_benchmark
from rmwbench.workload import generate_workload
CONFLICTS = {'low': (0.0, 0.0), 'medium': (0.5, 0.25), 'high': (1.0, 0.5)}

@dataclass(frozen=True, slots=True)
class MatrixCase:
    suite: str
    operations: int
    agents: int
    block_tokens: int
    min_output_tokens: int
    conflict: str

    @property
    def case_id(self) -> str:
        return f'ops{self.operations}_a{self.agents}_b{self.block_tokens}_g{self.min_output_tokens}_{self.conflict}'

def add_case(cases: dict[str, tuple[MatrixCase, set[int]]], case: MatrixCase, seeds: tuple[int, ...]) -> None:
    existing = cases.get(case.case_id)
    if existing is None:
        cases[case.case_id] = (case, set(seeds))
        return
    existing_case, existing_seeds = existing
    if existing_case != case:
        raise AssertionError(f'case id collision: {case.case_id}')
    existing_seeds.update(seeds)

def build_matrix(main_seeds: tuple[int, ...], factor_seeds: tuple[int, ...]) -> dict[str, tuple[MatrixCase, set[int]]]:
    cases: dict[str, tuple[MatrixCase, set[int]]] = {}
    for agents in (8, 16, 32):
        conflicts = CONFLICTS if agents == 16 else ('medium', 'high')
        for conflict in conflicts:
            add_case(cases, MatrixCase('scale', 1024, agents, 640, 0, conflict), main_seeds)
    for block_tokens in (320, 960):
        add_case(cases, MatrixCase('block', 1024, 16, block_tokens, 0, 'high'), factor_seeds)
    for min_output_tokens in (0, 64, 128):
        add_case(cases, MatrixCase('generation', 512, 16, 640, min_output_tokens, 'high'), factor_seeds)
    add_case(cases, MatrixCase('joint', 512, 32, 960, 128, 'high'), factor_seeds)
    return cases

def case_config(base: Config, case: MatrixCase, seed: int, mode: str, variant: str, refine_access_sets: bool, reuse_probe_snapshot: bool) -> Config:
    ww_rate, rw_rate = CONFLICTS[case.conflict]
    candidate_targets = max(64, case.agents * 8, case.operations // 4)
    max_tokens = max(64, case.min_output_tokens + 32)
    config = replace(base, benchmark=replace(base.benchmark, name=f'{base.benchmark.name}-{variant}-{case.case_id}', seed=seed), dataset=replace(base.dataset, files=8, blocks_per_file=64), payload=replace(base.payload, target_tokens=case.block_tokens, source_token_variation=0.6), agents=replace(base.agents, count=case.agents), run=replace(base.run, operations=case.operations, scheduler='rolling', max_retries=3), reasoning=replace(base.reasoning, candidate_targets=candidate_targets), conflict=replace(base.conflict, ww_rate=ww_rate, rw_rate=rw_rate, zipf_alpha=1.2), txserve=replace(base.txserve, mode=mode, max_batch_size=case.agents, refine_access_sets=refine_access_sets, reuse_probe_snapshot=reuse_probe_snapshot), model=replace(base.model, min_tokens=case.min_output_tokens, max_tokens=max_tokens, timeout_seconds=600.0, admission_lease_seconds=600.0))
    config.validate()
    generate_workload(config)
    return config

def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write('\n')
    temporary.replace(path)

def completed_summary(path: Path, config: Config) -> dict[str, Any] | None:
    summary_path = path / 'summary.json'
    operations_path = path / 'operations.jsonl'
    events_path = path / 'events.jsonl'
    if not summary_path.is_file():
        return None
    if not operations_path.is_file() or not events_path.is_file():
        raise RuntimeError(f'incomplete archived run: {path}')
    with summary_path.open(encoding='utf-8') as handle:
        summary = json.load(handle)
    archived = summary['config']
    expected = {'seed': config.benchmark.seed, 'operations': config.run.operations, 'agents': config.agents.count, 'block_tokens': config.payload.target_tokens, 'min_output_tokens': config.model.min_tokens, 'max_output_tokens': config.model.max_tokens, 'mode': config.txserve.mode, 'ww_rate': config.conflict.ww_rate, 'rw_rate': config.conflict.rw_rate, 'refine_access_sets': config.txserve.refine_access_sets, 'reuse_probe_snapshot': config.txserve.reuse_probe_snapshot, 'late_bind_write_target': config.txserve.late_bind_write_target}
    actual = {'seed': archived['benchmark']['seed'], 'operations': archived['run']['operations'], 'agents': archived['agents']['count'], 'block_tokens': archived['payload']['target_tokens'], 'min_output_tokens': archived['model'].get('min_tokens', 0), 'max_output_tokens': archived['model']['max_tokens'], 'mode': archived['txserve']['mode'], 'ww_rate': archived['conflict']['ww_rate'], 'rw_rate': archived['conflict']['rw_rate'], 'refine_access_sets': archived['txserve'].get('refine_access_sets', False), 'reuse_probe_snapshot': archived['txserve'].get('reuse_probe_snapshot', True), 'late_bind_write_target': archived['txserve'].get('late_bind_write_target', False)}
    if actual != expected:
        raise RuntimeError(f'archived run config mismatch at {path}: expected={expected}, actual={actual}')
    return summary

def summary_record(case: MatrixCase, seed: int, variant: str, mode: str, run_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
    return {'case': asdict(case), 'case_id': case.case_id, 'seed': seed, 'variant': variant, 'mode': mode, 'summary_path': str(run_dir / 'summary.json'), 'successful': summary['operations']['successful'], 'aborted': summary['operations']['aborted'], 'stale': summary['conflicts']['realized_stale_target_operations'], 'lost_updates': summary['conflicts']['lost_update_operations'], 'elapsed_s': summary['efficiency']['elapsed_seconds'], 'throughput_ops_s': summary['efficiency']['throughput_ops_s'], 'useful_tp': summary['efficiency']['useful_throughput_ops_gpu_hour'], 'waste_ratio': summary['efficiency']['waste_ratio'], 'model_s': summary['efficiency']['model_request_seconds'], 'wasted_model_s': summary['efficiency']['wasted_model_seconds'], 'read_amplification': summary['efficiency']['read_amplification'], 'latency_p95_ms': summary['latency_ms']['p95'], 'queue_p95_ms': summary['queue_ms']['p95'], 'exposure_p95_ms': summary['snapshot_exposure_ms']['p95'], 'admission_bypasses_mean': summary['admission'].get('bypasses_mean', 0.0), 'admission_bypasses_p95': summary['admission'].get('bypasses_p95', 0.0), 'admission_bypasses_max': summary['admission'].get('bypasses_max', 0.0), 'admission_wait_p95_ms': summary['admission'].get('wait_ms', {}).get('p95', 0.0), 'fairness_forced_operations': summary['admission'].get('fairness_forced_operations', 0), 'model_input_tokens': summary['tokens']['model_input_total'], 'model_output_tokens': summary['tokens']['model_output_total']}

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--variant', required=True)
    parser.add_argument('--mode', choices=('eager_fifo', 'occ_backoff', 'pessimistic_fifo', 'vllm_lock_manager'), required=True)
    parser.add_argument('--main-seeds', type=int, nargs='+', default=(7,))
    parser.add_argument('--factor-seeds', type=int, nargs='+', default=(7,))
    parser.add_argument('--case', action='append', dest='case_ids')
    parser.add_argument('--max-runs', type=int)
    parser.add_argument('--reverse-order', action='store_true')
    parser.add_argument('--refine-access-sets', action='store_true')
    parser.add_argument('--full-reread', action='store_true')
    parser.add_argument('--max-bypasses', type=int, help='document the server-side TXSERVE_MAX_BYPASSES setting')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if args.full_reread and (not args.refine_access_sets):
        parser.error('--full-reread requires --refine-access-sets')
    if args.mode == 'pessimistic_fifo':
        args.refine_access_sets = True
    if args.refine_access_sets and args.mode not in {'pessimistic_fifo', 'vllm_lock_manager'}:
        parser.error('access-set refinement requires pessimistic_fifo or vllm_lock_manager')
    if args.max_bypasses is not None and args.max_bypasses < 0:
        parser.error('--max-bypasses cannot be negative')
    return args

def main() -> None:
    args = parse_args()
    base = Config.load(args.config)
    matrix = build_matrix(tuple(args.main_seeds), tuple(args.factor_seeds))
    if args.case_ids:
        unknown = set(args.case_ids) - set(matrix)
        if unknown:
            raise ValueError(f'unknown cases: {sorted(unknown)}')
        matrix = {case_id: matrix[case_id] for case_id in args.case_ids}
    root = args.output / args.variant
    manifest = {'schema_version': 1, 'variant': args.variant, 'mode': args.mode, 'base_config': str(args.config.resolve()), 'main_seeds': list(args.main_seeds), 'factor_seeds': list(args.factor_seeds), 'refine_access_sets': args.refine_access_sets, 'reuse_probe_snapshot': not args.full_reread, 'late_bind_write_target': base.txserve.late_bind_write_target, 'server_max_bypasses': args.max_bypasses, 'cases': [{**asdict(case), 'case_id': case.case_id, 'seeds': sorted(seeds)} for case, seeds in matrix.values()]}
    print(json.dumps(manifest, sort_keys=True), flush=True)
    if args.dry_run:
        for case, seeds in matrix.values():
            for seed in sorted(seeds):
                case_config(base, case, seed, args.mode, args.variant, args.refine_access_sets, not args.full_reread)
        return
    write_json(root / 'matrix.json', manifest)
    warmup_case = MatrixCase('warmup', 8, 8, 320, 0, 'low')
    warmup_config = case_config(base, warmup_case, args.main_seeds[0], args.mode, args.variant, args.refine_access_sets, not args.full_reread)
    run_benchmark(warmup_config, root / '_warmup')
    all_seeds = sorted({seed for _, seeds in matrix.values() for seed in seeds})
    records: list[dict[str, Any]] = []
    executed = 0
    ordered_cases = sorted(matrix.values(), key=lambda item: item[0].case_id)
    for seed_index, seed in enumerate(all_seeds):
        seed_cases = [item for item in ordered_cases if seed in item[1]]
        if (seed_index + int(args.reverse_order)) % 2:
            seed_cases.reverse()
        for case, _ in seed_cases:
            config = case_config(base, case, seed, args.mode, args.variant, args.refine_access_sets, not args.full_reread)
            run_dir = root / case.case_id / f'seed_{seed}'
            summary = completed_summary(run_dir, config)
            resumed = summary is not None
            if summary is None:
                if args.max_runs is not None and executed >= args.max_runs:
                    write_json(root / 'aggregate.json', {'manifest': manifest, 'records': records})
                    return
                summary = run_benchmark(config, run_dir).summary
                executed += 1
            record = summary_record(case, seed, args.variant, args.mode, run_dir, summary)
            records.append(record)
            print(json.dumps({**record, 'resumed': resumed}, sort_keys=True), flush=True)
            write_json(root / 'aggregate.json', {'manifest': manifest, 'records': records})
if __name__ == '__main__':
    main()
