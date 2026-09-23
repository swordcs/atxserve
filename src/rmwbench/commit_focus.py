from __future__ import annotations
import ast
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass
import hashlib
import json
from itertools import combinations
from pathlib import Path
import random
import subprocess
from typing import Any, Iterable, Mapping, Sequence
WORKLOADS = ('dispersed', 'mixed', 'focused')

@dataclass(frozen=True, slots=True)
class CommitTransaction:
    task_id: str
    repo: str
    anchor_commit: str
    source_commit: str
    source_parent: str
    commit_message: str
    component_id: str
    read_set: tuple[str, ...]
    write_set: tuple[str, ...]
    prefill_files: tuple[str, ...]
    source_words: int
    changed_lines: int

    def to_mapping(self) -> dict[str, Any]:
        value = asdict(self)
        for name in ('read_set', 'write_set', 'prefill_files'):
            value[name] = list(value[name])
        return value

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> 'CommitTransaction':
        value = dict(raw)
        for name in ('read_set', 'write_set', 'prefill_files'):
            value[name] = tuple((str(item) for item in value[name]))
        return cls(**value)

def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, text=True)
    return completed.stdout

def validate_repository(root: str | Path, anchor: str) -> Path:
    path = Path(root).resolve()
    if not (path / '.git').exists():
        raise ValueError(f'not a Git checkout: {path}')
    head = _git(path, 'rev-parse', 'HEAD').strip()
    if head != anchor:
        raise ValueError(f'{path} is at {head}; expected anchor {anchor}')
    return path

def _module_variants(relative_path: str) -> tuple[str, ...]:
    parts = list(Path(relative_path).with_suffix('').parts)
    if parts and parts[-1] == '__init__':
        parts.pop()
    variants = []
    if parts:
        variants.append('.'.join(parts))
    if parts and parts[0] == 'src' and (len(parts) > 1):
        variants.append('.'.join(parts[1:]))
    return tuple(dict.fromkeys(variants))

def build_module_index(root: str | Path) -> dict[str, str]:
    repository = Path(root).resolve()
    index: dict[str, str] = {}
    paths = sorted((path for path in repository.rglob('*.py') if path.is_file() and '.git' not in path.relative_to(repository).parts))
    for path in paths:
        relative = path.relative_to(repository).as_posix()
        for module in _module_variants(relative):
            index.setdefault(module, relative)
    return index

def _current_package(relative_path: str) -> tuple[str, ...]:
    variants = _module_variants(relative_path)
    module = variants[-1] if variants else ''
    parts = module.split('.') if module else []
    if Path(relative_path).stem == '__init__':
        return tuple(parts)
    return tuple(parts[:-1])

def resolve_internal_imports(relative_path: str, source: str, module_index: Mapping[str, str], limit: int=3) -> tuple[str, ...]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ()
    package = _current_package(relative_path)
    candidates: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names.extend((alias.name for alias in node.names))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                remove = max(0, node.level - 1)
                base = package[:len(package) - remove if remove else len(package)]
                if node.module:
                    base = (*base, *node.module.split('.'))
                names.append('.'.join(base))
                names.extend(('.'.join((*base, alias.name)) for alias in node.names if alias.name != '*'))
            elif node.module:
                names.append(node.module)
                names.extend((f'{node.module}.{alias.name}' for alias in node.names if alias.name != '*'))
        for name in names:
            path = module_index.get(name)
            if path and path != relative_path and (path not in candidates):
                candidates.append(path)
                if len(candidates) == limit:
                    return tuple(candidates)
    return tuple(candidates)

def _commit_rows(root: Path, anchor: str) -> list[tuple[str, str, str]]:
    output = _git(root, 'log', '--no-merges', '--format=%H%x1f%P%x1f%s', anchor)
    rows = []
    for line in output.splitlines():
        commit, parents, subject = line.split('\x1f', 2)
        parent_parts = parents.split()
        if len(parent_parts) == 1:
            rows.append((commit, parent_parts[0], subject))
    return rows

def extract_transactions(repo: str, root: str | Path, anchor: str, *, limit: int, max_changed_lines: int=300, max_imports: int=3) -> tuple[CommitTransaction, ...]:
    repository = validate_repository(root, anchor)
    module_index = build_module_index(repository)
    transactions: list[CommitTransaction] = []
    for commit, parent, subject in _commit_rows(repository, anchor):
        paths = tuple((line.strip() for line in _git(repository, 'diff-tree', '--no-commit-id', '--name-only', '-r', '--diff-filter=M', commit, '--', '*.py').splitlines() if line.strip().endswith('.py')))
        if len(paths) != 1:
            continue
        relative_path = paths[0]
        source_path = repository / relative_path
        if not source_path.is_file():
            continue
        numstat = _git(repository, 'diff', '--numstat', parent, commit, '--', relative_path).strip()
        if not numstat:
            continue
        added, deleted, *_ = numstat.split('\t')
        if not added.isdigit() or not deleted.isdigit():
            continue
        changed_lines = int(added) + int(deleted)
        if not 0 < changed_lines <= max_changed_lines:
            continue
        source = source_path.read_text(encoding='utf-8', errors='replace')
        dependencies = resolve_internal_imports(relative_path, source, module_index, max_imports)
        read_paths = tuple(dict.fromkeys((relative_path, *dependencies)))
        resource = lambda path: f'{repo}:{path}'
        write_set = (resource(relative_path),)
        read_set = tuple((resource(path) for path in read_paths))
        transactions.append(CommitTransaction(task_id=f'{repo}:{commit[:12]}', repo=repo, anchor_commit=anchor, source_commit=commit, source_parent=parent, commit_message=subject, component_id=resource(relative_path), read_set=read_set, write_set=write_set, prefill_files=read_set, source_words=sum((len((repository / path).read_text(encoding='utf-8', errors='replace').split()) for path in read_paths)), changed_lines=changed_lines))
        if len(transactions) == limit:
            break
    if len(transactions) != limit:
        raise ValueError(f'{repo} yielded {len(transactions)} eligible commits; {limit} required')
    return tuple(transactions)

def _seeded_queue(key: str, transactions: Iterable[CommitTransaction], seed: int) -> deque[CommitTransaction]:
    values = sorted(transactions, key=lambda item: item.task_id)
    digest = hashlib.sha256(f'{seed}:{key}'.encode()).digest()
    random.Random(int.from_bytes(digest, 'big')).shuffle(values)
    return deque(values)

def _queues(transactions: Sequence[CommitTransaction], seed: int) -> dict[str, deque[CommitTransaction]]:
    grouped: dict[str, list[CommitTransaction]] = defaultdict(list)
    for transaction in transactions:
        grouped[transaction.component_id].append(transaction)
    return {key: _seeded_queue(key, values, seed) for key, values in sorted(grouped.items())}

def _pop_dispersed(queues: dict[str, deque[CommitTransaction]], count: int, *, excluded: set[str] | None=None) -> list[CommitTransaction]:
    selected: list[CommitTransaction] = []
    excluded = excluded or set()
    while len(selected) < count and queues:
        keys = [key for key in sorted(queues) if key not in excluded and queues[key]]
        if not keys:
            keys = [key for key in sorted(queues) if queues[key]]
        if not keys:
            break
        used: set[str] = set()
        for key in keys:
            if len(selected) == count:
                break
            if key in used:
                continue
            selected.append(queues[key].popleft())
            used.add(key)
            if not queues[key]:
                del queues[key]
        excluded = set()
    return selected

def _schedule_dispersed(transactions: Sequence[CommitTransaction], concurrency: int, seed: int) -> list[list[CommitTransaction]]:
    queues = _queues(transactions, seed)
    waves = []
    while queues:
        wave = _pop_dispersed(queues, concurrency)
        if not wave:
            raise AssertionError('dispersed scheduler made no progress')
        waves.append(wave)
    return waves

def _schedule_focused(transactions: Sequence[CommitTransaction], concurrency: int, seed: int) -> list[list[CommitTransaction]]:
    queues = _queues(transactions, seed)
    waves = []
    while queues:
        wave: list[CommitTransaction] = []
        while len(wave) < concurrency and queues:
            key = min(queues, key=lambda item: (-len(queues[item]), item))
            take = min(concurrency - len(wave), len(queues[key]))
            wave.extend((queues[key].popleft() for _ in range(take)))
            if not queues[key]:
                del queues[key]
        waves.append(wave)
    return waves

def _schedule_mixed(transactions: Sequence[CommitTransaction], concurrency: int, seed: int) -> list[list[CommitTransaction]]:
    queues = _queues(transactions, seed)
    center_count = (concurrency + 1) // 2
    waves = []
    while queues:
        center = min(queues, key=lambda item: (-len(queues[item]), item))
        take = min(center_count, len(queues[center]))
        wave = [queues[center].popleft() for _ in range(take)]
        if not queues[center]:
            del queues[center]
        wave.extend(_pop_dispersed(queues, concurrency - len(wave), excluded={center}))
        if len(wave) < concurrency:
            wave.extend(_pop_dispersed(queues, concurrency - len(wave)))
        waves.append(wave)
    return waves

def build_schedules(transactions: Sequence[CommitTransaction], *, concurrency: int, seed: int) -> dict[str, list[dict[str, Any]]]:
    if not transactions:
        raise ValueError('transaction pool is empty')
    if len(transactions) % concurrency:
        raise ValueError('transaction count must be divisible by concurrency')
    builders = {'dispersed': _schedule_dispersed, 'mixed': _schedule_mixed, 'focused': _schedule_focused}
    schedules: dict[str, list[dict[str, Any]]] = {}
    expected = sorted((transaction.task_id for transaction in transactions))
    for workload, builder in builders.items():
        waves = builder(transactions, concurrency, seed)
        records = [{'workload': workload, 'seed': seed, 'wave_id': wave_id, 'arrival_slot': wave_id, 'task_ids': [transaction.task_id for transaction in wave]} for wave_id, wave in enumerate(waves)]
        actual = sorted((task_id for record in records for task_id in record['task_ids']))
        if actual != expected:
            raise AssertionError(f'{workload} schedule changed the task multiset')
        if any((len(record['task_ids']) != concurrency for record in records)):
            raise AssertionError(f'{workload} schedule has an incomplete wave')
        schedules[workload] = records
    return schedules

def _pair_type(left: CommitTransaction, right: CommitTransaction) -> str | None:
    left_reads = set(left.read_set)
    left_writes = set(left.write_set)
    right_reads = set(right.read_set)
    right_writes = set(right.write_set)
    if left_writes & right_writes:
        return 'ww'
    if left_writes & right_reads - right_writes or right_writes & left_reads - left_writes:
        return 'rw'
    return None

def offered_conflicts(transactions: Sequence[CommitTransaction], schedule: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_id = {transaction.task_id: transaction for transaction in transactions}
    total_pairs = 0
    ww_pairs = 0
    rw_pairs = 0
    conflicting_tasks: set[str] = set()
    ww_tasks: set[str] = set()
    rw_tasks: set[str] = set()
    per_wave = []
    for record in schedule:
        task_ids = [str(task_id) for task_id in record['task_ids']]
        wave_pairs = 0
        wave_ww = 0
        wave_rw = 0
        wave_conflicting: set[str] = set()
        for left_id, right_id in combinations(task_ids, 2):
            pair_type = _pair_type(by_id[left_id], by_id[right_id])
            wave_pairs += 1
            if pair_type == 'ww':
                wave_ww += 1
                ww_tasks.update((left_id, right_id))
            elif pair_type == 'rw':
                wave_rw += 1
                rw_tasks.update((left_id, right_id))
            if pair_type:
                wave_conflicting.update((left_id, right_id))
        total_pairs += wave_pairs
        ww_pairs += wave_ww
        rw_pairs += wave_rw
        conflicting_tasks.update(wave_conflicting)
        per_wave.append({'wave_id': int(record['wave_id']), 'pairs': wave_pairs, 'ww_pairs': wave_ww, 'rw_pairs': wave_rw, 'offered_ww_pair_rate': wave_ww / wave_pairs, 'offered_rw_pair_rate': wave_rw / wave_pairs, 'offered_any_pair_rate': (wave_ww + wave_rw) / wave_pairs, 'offered_conflicting_txn_rate': len(wave_conflicting) / len(task_ids)})
    rw_only_tasks = rw_tasks - ww_tasks
    return {'transactions': len(transactions), 'waves': len(schedule), 'pairs': total_pairs, 'ww_pairs': ww_pairs, 'rw_pairs': rw_pairs, 'offered_ww_pair_rate': ww_pairs / total_pairs, 'offered_rw_pair_rate': rw_pairs / total_pairs, 'offered_any_pair_rate': (ww_pairs + rw_pairs) / total_pairs, 'offered_conflicting_txn_rate': len(conflicting_tasks) / len(transactions), 'ww_task_ids': sorted(ww_tasks), 'rw_task_ids': sorted(rw_only_tasks), 'per_wave': per_wave}

def schedule_path(root: str | Path, workload: str, seed: int) -> Path:
    if workload not in WORKLOADS:
        raise ValueError(f'unknown commit focus workload: {workload}')
    return Path(root) / 'schedules' / f'{workload}_seed_{seed}.jsonl'

def load_transactions(path: str | Path) -> tuple[CommitTransaction, ...]:
    with Path(path).open(encoding='utf-8') as handle:
        values = tuple((CommitTransaction.from_mapping(json.loads(line)) for line in handle if line.strip()))
    task_ids = [value.task_id for value in values]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError('commit focus transaction IDs are not unique')
    return values

def load_schedule(path: str | Path) -> tuple[dict[str, Any], ...]:
    with Path(path).open(encoding='utf-8') as handle:
        return tuple((json.loads(line) for line in handle if line.strip()))

def stable_json_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    return 'sha256:' + hashlib.sha256(payload).hexdigest()

def classify_task_conflicts(summary: Mapping[str, Any]) -> dict[str, tuple[bool, bool]]:
    ww = set((str(value) for value in summary['ww_task_ids']))
    rw = set((str(value) for value in summary['rw_task_ids']))
    return {task_id: (task_id in ww, task_id in rw) for task_id in ww | rw}

def focus_gradient(summaries: Mapping[str, Mapping[str, Any]]) -> bool:
    values = [float(summaries[name]['offered_any_pair_rate']) for name in WORKLOADS]
    return values[0] < values[1] < values[2]
