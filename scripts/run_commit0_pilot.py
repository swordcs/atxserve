from __future__ import annotations
import argparse
from collections import defaultdict
from dataclasses import asdict, replace
import json
from pathlib import Path
from statistics import fmean
import subprocess
from typing import Any
from rmwbench.baseline import BASELINE_POLICIES
from rmwbench.config import Config
from rmwbench.runner import run_benchmark
RMWBENCH_ROOT = Path(__file__).resolve().parents[1]
REPOSITORIES = {'babel': {'url': 'https://github.com/python-babel/babel.git', 'commit': 'f91754b01cb9f32b83aeaa80b74ed10b5dfccb6a'}, 'jinja': {'url': 'https://github.com/pallets/jinja.git', 'commit': 'eb0df049deac83610f2ff09026fd39554bd4072b'}, 'marshmallow': {'url': 'https://github.com/marshmallow-code/marshmallow.git', 'commit': '947724c7391814a740b94239807eb964c9bf40d7'}}
CONFLICTS = {'low': (0.0, 0.0), 'medium': (0.5, 0.25), 'high': (1.0, 0.5)}
POLICIES = {**BASELINE_POLICIES, 'optimized': {'mode': 'vllm_lock_manager', 'refine_access_sets': True, 'reuse_probe_snapshot': True}}
DEFAULT_POLICIES = ('original', 'optimized')
METRICS = ('operations.successful', 'operations.aborted', 'correctness.semantic_correctness', 'conflicts.realized_stale_target_operations', 'efficiency.retry_operations', 'efficiency.elapsed_seconds', 'efficiency.useful_operations', 'efficiency.useful_throughput_ops_gpu_hour', 'efficiency.waste_ratio', 'efficiency.model_request_seconds', 'efficiency.wasted_model_seconds', 'efficiency.read_amplification', 'latency_ms.p95', 'admission.wait_ms.mean', 'admission.snapshot_reuse_rate')

def _value(summary: dict[str, Any], path: str) -> float:
    value: Any = summary
    for component in path.split('.'):
        value = value[component]
    return float(value)

def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding='utf-8') as handle:
        return [json.loads(line) for line in handle if line.strip()]

def operation_signature(path: Path) -> dict[str, tuple[int, str | None]]:
    return {str(row['op_id']): (int(row['wave']), row['selected_target']) for row in _load_jsonl(path)}

def validate_pair(original_dir: Path, optimized_dir: Path) -> None:
    original = operation_signature(original_dir / 'operations.jsonl')
    optimized = operation_signature(optimized_dir / 'operations.jsonl')
    if len(original) != len(optimized):
        raise ValueError('paired runs have different operation counts')
    if original != optimized:
        raise ValueError('paired runs differ in operation IDs, waves, or targets')

def _complete_summary(run_dir: Path, config: Config) -> dict[str, Any] | None:
    paths = tuple((run_dir / name for name in ('summary.json', 'operations.jsonl', 'events.jsonl')))
    if not all((path.is_file() for path in paths)):
        return None
    with paths[0].open(encoding='utf-8') as handle:
        summary = json.load(handle)
    return summary if summary.get('config') == asdict(config) else None

def _validate_repo(repo: str, root: Path) -> None:
    if not (root / '.git').exists():
        raise ValueError(f'{repo} is not a Git checkout: {root}')
    completed = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True)
    actual = completed.stdout.strip()
    expected = str(REPOSITORIES[repo]['commit'])
    if actual != expected:
        raise ValueError(f'{repo} is at {actual}; expected Commit0 base commit {expected}')

def _policy_config(base: Config, *, repo: str, repo_root: Path, conflict: str, policy: str, seed: int) -> Config:
    ww_rate, rw_rate = CONFLICTS[conflict] if conflict != 'custom' else (base.conflict.ww_rate, base.conflict.rw_rate)
    values = POLICIES[policy]
    config = replace(base, benchmark=replace(base.benchmark, name=f'{base.benchmark.name}-{repo}-{conflict}-{policy}', seed=seed), dataset=replace(base.dataset, source='commit0', root=str(repo_root.resolve())), conflict=replace(base.conflict, ww_rate=ww_rate, rw_rate=rw_rate), txserve=replace(base.txserve, mode=str(values['mode']), refine_access_sets=bool(values['refine_access_sets']), reuse_probe_snapshot=bool(values['reuse_probe_snapshot'])))
    config.validate()
    return config

def _record(repo: str, conflict: str, policy: str, seed: int, run_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
    return {'repo': repo, 'commit': REPOSITORIES[repo]['commit'], 'conflict': conflict, 'policy': policy, 'seed': seed, 'summary_path': str(run_dir / 'summary.json'), **{metric: _value(summary, metric) for metric in METRICS}}

def _percent(value: float, baseline: float) -> float | None:
    return None if baseline == 0.0 else (value / baseline - 1.0) * 100.0

def _comparison(scope: str, conflict: str, seed: int | None, original: dict[str, float], optimized: dict[str, float]) -> dict[str, Any]:
    return {'scope': scope, 'conflict': conflict, 'seed': seed, 'original': original, 'optimized': optimized, 'change_pct': {metric: _percent(optimized[metric], original[metric]) for metric in METRICS}, 'absolute_delta': {metric: optimized[metric] - original[metric] for metric in METRICS}}

def _comparisons(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, dict[str, float]]]]:
    grouped: dict[tuple[str, str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in records:
        key = (str(row['repo']), str(row['conflict']), int(row['seed']))
        grouped[key][str(row['policy'])] = row
    comparisons = []
    for (repo, conflict, seed), policies in sorted(grouped.items()):
        if not set(DEFAULT_POLICIES).issubset(policies):
            continue
        per_policy = {policy: {metric: float(policies[policy][metric]) for metric in METRICS} for policy in DEFAULT_POLICIES}
        comparisons.append(_comparison(repo, conflict, seed, per_policy['original'], per_policy['optimized']))
    aggregates: dict[str, dict[str, dict[str, float]]] = {}
    for conflict in sorted({str(row['conflict']) for row in records}):
        aggregates[conflict] = {}
        for policy in POLICIES:
            rows = [row for row in records if row['conflict'] == conflict and row['policy'] == policy]
            if rows:
                aggregates[conflict][policy] = {metric: fmean((float(row[metric]) for row in rows)) for metric in METRICS}
        if set(DEFAULT_POLICIES).issubset(aggregates[conflict]):
            comparisons.append(_comparison('aggregate', conflict, None, aggregates[conflict]['original'], aggregates[conflict]['optimized']))
    return (comparisons, aggregates)

def _acceptance(comparisons: list[dict[str, Any]]) -> dict[str, Any]:
    per_repo = [row for row in comparisons if row['scope'] != 'aggregate']
    aggregate = {str(row['conflict']): row for row in comparisons if row['scope'] == 'aggregate'}
    complete = {'low', 'medium', 'high'} <= set(aggregate)
    success_ok = bool(per_repo) and all((row['optimized']['operations.successful'] >= row['original']['operations.successful'] for row in per_repo))
    positive_high_repos = {str(row['scope']) for row in per_repo if row['conflict'] == 'high' and (row['change_pct']['efficiency.useful_throughput_ops_gpu_hour'] or 0.0) > 0.0}
    high_useful = complete and (aggregate['high']['change_pct']['efficiency.useful_throughput_ops_gpu_hour'] or 0.0) >= 10.0
    high_reductions = complete and all((aggregate['high']['optimized'][metric] < aggregate['high']['original'][metric] for metric in ('efficiency.waste_ratio', 'conflicts.realized_stale_target_operations', 'efficiency.retry_operations')))
    medium_positive = complete and (aggregate['medium']['change_pct']['efficiency.useful_throughput_ops_gpu_hour'] or 0.0) > 0.0
    low_change = aggregate.get('low', {}).get('change_pct', {}).get('efficiency.useful_throughput_ops_gpu_hour')
    low_neutral = complete and low_change is not None and (abs(low_change) <= 5.0)
    checks = {'complete_matrix': complete, 'optimized_success_not_lower': success_ok, 'high_positive_repositories_at_least_2': len(positive_high_repos) >= 2, 'high_aggregate_useful_tp_at_least_10pct': high_useful, 'high_waste_stale_retry_reduced': high_reductions, 'medium_aggregate_useful_tp_positive': medium_positive, 'low_aggregate_useful_tp_within_5pct': low_neutral}
    return {'validated': all(checks.values()), 'checks': checks, 'high_positive_repositories': sorted(positive_high_repos)}

def _format_change(value: float | None) -> str:
    return 'n/a' if value is None else f'{value:+.1f}%'

def _markdown(model: str, comparisons: list[dict[str, Any]], acceptance: dict[str, Any], seeds: list[int]) -> str:
    seed_text = ', '.join((str(seed) for seed in seeds))
    lines = [f'# Commit0 {model} paired pilot', '', f'Seeds {seed_text}, 64 operations per run, 8 agents, and identical targets per pair.', '', '| repo | conflict | seed | success original to optimized | abort original to optimized | stale original to optimized | retry original to optimized | elapsed change | UsefulTP change | p95 change | waste change |', '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for row in comparisons:
        if row['scope'] == 'aggregate':
            continue
        original = row['original']
        optimized = row['optimized']
        change = row['change_pct']
        lines.append(f"| {row['scope']} | {row['conflict']} | {row['seed']} | {original['operations.successful']:.0f} to {optimized['operations.successful']:.0f} | {original['operations.aborted']:.0f} to {optimized['operations.aborted']:.0f} | {original['conflicts.realized_stale_target_operations']:.0f} to {optimized['conflicts.realized_stale_target_operations']:.0f} | {original['efficiency.retry_operations']:.0f} to {optimized['efficiency.retry_operations']:.0f} | {_format_change(change['efficiency.elapsed_seconds'])} | {_format_change(change['efficiency.useful_throughput_ops_gpu_hour'])} | {_format_change(change['latency_ms.p95'])} | {_format_change(change['efficiency.waste_ratio'])} |")
    lines.extend(['', '## Aggregate across repositories', '', '| conflict | elapsed change | UsefulTP change | p95 change |', '|---|---:|---:|---:|'])
    for row in comparisons:
        if row['scope'] != 'aggregate':
            continue
        change = row['change_pct']
        lines.append(f"| {row['conflict']} | {_format_change(change['efficiency.elapsed_seconds'])} | {_format_change(change['efficiency.useful_throughput_ops_gpu_hour'])} | {_format_change(change['latency_ms.p95'])} |")
    lines.extend(['', '## Acceptance', '', f"Validated: {acceptance['validated']}", ''])
    for name, passed in acceptance['checks'].items():
        lines.append(f"- {('PASS' if passed else 'FAIL')}: {name}")
    lines.append('')
    return '\n'.join(lines)

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=RMWBENCH_ROOT / 'configs/vllm_commit0_qwen3_8b.toml')
    parser.add_argument('--data-root', type=Path, default=RMWBENCH_ROOT / 'data/commit0')
    parser.add_argument('--output', type=Path, default=RMWBENCH_ROOT / 'results/qwen3_8b/commit0_pilot')
    parser.add_argument('--repos', nargs='+', choices=tuple(REPOSITORIES), default=tuple(REPOSITORIES))
    parser.add_argument('--conflicts', nargs='+', choices=(*CONFLICTS, 'custom'), default=tuple(CONFLICTS))
    parser.add_argument('--policies', nargs='+', choices=tuple(POLICIES), default=DEFAULT_POLICIES)
    parser.add_argument('--seeds', type=int, nargs='+', default=[7])
    parser.add_argument('--operations', type=int)
    parser.add_argument('--skip-warmup', action='store_true')
    args = parser.parse_args()
    base = Config.load(args.config)
    if args.operations is not None:
        base = replace(base, run=replace(base.run, operations=args.operations))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    repo_roots = {repo: (args.data_root / repo).resolve() for repo in args.repos}
    for repo, repo_root in repo_roots.items():
        _validate_repo(repo, repo_root)
    if not args.skip_warmup:
        repo = args.repos[0]
        warmup = _policy_config(base, repo=repo, repo_root=repo_roots[repo], conflict='low', policy=args.policies[-1], seed=args.seeds[0])
        warmup = replace(warmup, benchmark=replace(warmup.benchmark, name=base.benchmark.name + '-warmup'), run=replace(warmup.run, operations=warmup.agents.count))
        warmup_dir = output / '_warmup'
        if _complete_summary(warmup_dir, warmup) is None:
            run_benchmark(warmup, warmup_dir)
    records: list[dict[str, Any]] = []
    for repo in args.repos:
        for conflict in args.conflicts:
            for seed in args.seeds:
                pair_dirs: dict[str, Path] = {}
                for policy in args.policies:
                    config = _policy_config(base, repo=repo, repo_root=repo_roots[repo], conflict=conflict, policy=policy, seed=seed)
                    run_dir = output / repo / policy / conflict / f'seed_{seed}'
                    summary = _complete_summary(run_dir, config)
                    if summary is None:
                        summary = run_benchmark(config, run_dir).summary
                    pair_dirs[policy] = run_dir
                    row = _record(repo, conflict, policy, seed, run_dir, summary)
                    records.append(row)
                    print(f"{repo}/{conflict}/{policy}/seed_{seed} success={row['operations.successful']:.0f} UsefulTP={row['efficiency.useful_throughput_ops_gpu_hour']:.1f}", flush=True)
                if set(DEFAULT_POLICIES).issubset(pair_dirs):
                    validate_pair(pair_dirs['original'], pair_dirs['optimized'])
    comparisons, aggregates = _comparisons(records)
    acceptance = _acceptance(comparisons)
    payload = {'schema_version': 1, 'model': base.model.name, 'seeds': args.seeds, 'repositories': {repo: {**REPOSITORIES[repo], 'root': str(repo_roots[repo])} for repo in args.repos}, 'conflicts': {name: {'ww_rate': CONFLICTS[name][0] if name != 'custom' else base.conflict.ww_rate, 'rw_rate': CONFLICTS[name][1] if name != 'custom' else base.conflict.rw_rate, 'zipf_alpha': base.conflict.zipf_alpha} for name in args.conflicts}, 'policies': POLICIES, 'records': records, 'aggregates': aggregates, 'comparisons': comparisons, 'acceptance': acceptance}
    with (output / 'aggregate.json').open('w', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write('\n')
    (output / 'comparison.md').write_text(_markdown(base.model.name, comparisons, acceptance, args.seeds), encoding='utf-8')
if __name__ == '__main__':
    main()
