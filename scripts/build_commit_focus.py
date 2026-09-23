from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
from typing import Any, Iterable
from rmwbench.commit_focus import WORKLOADS, CommitTransaction, build_schedules, extract_transactions, focus_gradient, offered_conflicts, stable_json_hash
RMWBENCH_ROOT = Path(__file__).resolve().parents[1]
REPOSITORIES = {'babel': {'root': RMWBENCH_ROOT / 'data' / 'commit0' / 'babel', 'anchor': 'f91754b01cb9f32b83aeaa80b74ed10b5dfccb6a', 'url': 'https://github.com/python-babel/babel.git'}, 'jinja': {'root': RMWBENCH_ROOT / 'data' / 'commit0' / 'jinja', 'anchor': 'eb0df049deac83610f2ff09026fd39554bd4072b', 'url': 'https://github.com/pallets/jinja.git'}, 'marshmallow': {'root': RMWBENCH_ROOT / 'data' / 'commit0' / 'marshmallow', 'anchor': '947724c7391814a740b94239807eb964c9bf40d7', 'url': 'https://github.com/marshmallow-code/marshmallow.git'}}

def _path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else RMWBENCH_ROOT / path

def _write_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as handle:
        for value in values:
            handle.write(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False))
            handle.write('\n')

def _print_conflicts(summaries: dict[str, dict[str, Any]]) -> None:
    print('workload    WW-pair   RW-pair  any-pair  conflicting-txn')
    for workload in WORKLOADS:
        value = summaries[workload]
        print(f"{workload:<10} {value['offered_ww_pair_rate']:>8.3f} {value['offered_rw_pair_rate']:>9.3f} {value['offered_any_pair_rate']:>9.3f} {value['offered_conflicting_txn_rate']:>16.3f}")

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='data/commit_focus', help='Output directory relative to the RMWBench root.')
    parser.add_argument('--per-repo', type=int, default=32)
    parser.add_argument('--concurrency', type=int, default=8)
    parser.add_argument('--seeds', type=int, nargs='+', default=[7, 17, 27])
    parser.add_argument('--max-changed-lines', type=int, default=300)
    parser.add_argument('--max-imports', type=int, default=3)
    return parser.parse_args()

def main() -> None:
    args = parse_args()
    output = _path(args.output).resolve()
    if args.per_repo <= 0 or args.concurrency <= 0:
        raise ValueError('per-repo and concurrency must be positive')
    transactions: list[CommitTransaction] = []
    repository_manifest: dict[str, dict[str, Any]] = {}
    for repo, spec in REPOSITORIES.items():
        root = Path(spec['root']).resolve()
        extracted = extract_transactions(repo, root, str(spec['anchor']), limit=args.per_repo, max_changed_lines=args.max_changed_lines, max_imports=args.max_imports)
        transactions.extend(extracted)
        repository_manifest[repo] = {'url': spec['url'], 'root': os.path.relpath(root, output), 'anchor_commit': spec['anchor'], 'transactions': len(extracted)}
    transactions.sort(key=lambda value: (value.repo, value.task_id))
    if len(transactions) % args.concurrency:
        raise ValueError(f'{len(transactions)} transactions are not divisible by concurrency {args.concurrency}')
    transaction_rows = [transaction.to_mapping() for transaction in transactions]
    _write_jsonl(output / 'transactions.jsonl', transaction_rows)
    all_conflicts: dict[str, dict[str, Any]] = {}
    schedule_manifest: dict[str, dict[str, Any]] = {}
    for seed in args.seeds:
        schedules = build_schedules(transactions, concurrency=args.concurrency, seed=seed)
        summaries = {workload: offered_conflicts(transactions, schedule) for workload, schedule in schedules.items()}
        _print_conflicts(summaries)
        if not focus_gradient(summaries):
            raise ValueError(f'seed {seed}')
        for workload, records in schedules.items():
            _write_jsonl(output / 'schedules' / f'{workload}_seed_{seed}.jsonl', records)
            all_conflicts[f'{workload}:seed_{seed}'] = summaries[workload]
            schedule_manifest[f'{workload}:seed_{seed}'] = {'waves': len(records), 'schedule_hash': stable_json_hash(records), 'offered_any_pair_rate': summaries[workload]['offered_any_pair_rate'], 'offered_conflicting_txn_rate': summaries[workload]['offered_conflicting_txn_rate']}
    with (output / 'offered_conflicts.json').open('w', encoding='utf-8') as handle:
        json.dump(all_conflicts, handle, indent=2, sort_keys=True)
        handle.write('\n')
    manifest = {'schema_version': 1, 'name': 'commit-derived transaction replay under controlled development focus', 'repositories': repository_manifest, 'selection': {'non_merge': True, 'modified_python_files_exactly': 1, 'file_must_exist_at_anchor': True, 'max_changed_lines': args.max_changed_lines, 'max_direct_internal_imports': args.max_imports, 'selection_order': 'reverse chronological from pinned anchor', 'conflict_aware_selection': False}, 'transactions': len(transactions), 'unique_commits': len({transaction.source_commit for transaction in transactions}), 'unique_write_files': len({block for transaction in transactions for block in transaction.write_set}), 'unique_read_files': len({block for transaction in transactions for block in transaction.read_set}), 'components': len({transaction.component_id for transaction in transactions}), 'concurrency': args.concurrency, 'seeds': args.seeds, 'transaction_hash': stable_json_hash(transaction_rows), 'schedules': schedule_manifest}
    output.mkdir(parents=True, exist_ok=True)
    with (output / 'dataset_manifest.json').open('w', encoding='utf-8') as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write('\n')
    print(f"wrote {len(transactions)} unique transactions, {manifest['unique_write_files']} write files, and {manifest['unique_read_files']} materialized read files to {output}")
if __name__ == '__main__':
    main()
