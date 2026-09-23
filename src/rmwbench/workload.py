from __future__ import annotations
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import random
from typing import Iterable
from .commit_focus import build_schedules, classify_task_conflicts, load_schedule, load_transactions, offered_conflicts, schedule_path, stable_json_hash, validate_repository
from .config import Config
from .models import BlockSeed, Task

@dataclass(frozen=True, slots=True)
class Workload:
    blocks: tuple[BlockSeed, ...]
    tasks: tuple[Task, ...]
_RMWBENCH_ROOT = Path(__file__).resolve().parents[2]

def load_commit0_python_files(root: str | Path, count: int) -> tuple[tuple[str, str], ...]:
    source_root = Path(root)
    if not source_root.is_absolute():
        source_root = _RMWBENCH_ROOT / source_root
    source_root = source_root.resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(f'Commit0 repository is absent: {source_root}')
    paths = sorted((path for path in source_root.rglob('*.py') if path.is_file() and '.git' not in path.relative_to(source_root).parts), key=lambda path: path.relative_to(source_root).as_posix())
    if len(paths) < count:
        raise ValueError(f'Commit0 repository {source_root} has {len(paths)} Python files; {count} are required')
    return tuple(((path.relative_to(source_root).as_posix(), path.read_text(encoding='utf-8', errors='replace')) for path in paths[:count]))

def _wave_sizes(operations: int, agents: int) -> list[int]:
    return [min(agents, operations - start) for start in range(0, operations, agents)]

def _requested_count(operations: int, rate: float, name: str) -> int:
    requested = operations * rate
    rounded = round(requested)
    if not math.isclose(requested, rounded, abs_tol=1e-09):
        raise ValueError(f'exact {name} rate {rate} is not representable by {operations} operations')
    return rounded

def _allocate_counts(wave_sizes: list[int], total: int, *, minimum: int, name: str, requires_peer: bool=False, balanced: bool=False) -> list[int]:
    reachable = 1
    history = [reachable]
    options_by_wave: list[tuple[int, ...]] = []
    for size in wave_sizes:
        eligible = size >= minimum and (not requires_peer or size >= 2)
        options = (0, *range(minimum, size + 1)) if eligible else (0,)
        options_by_wave.append(options)
        next_reachable = 0
        for count in options:
            next_reachable |= reachable << count
        reachable = next_reachable
        history.append(reachable)
    if not reachable >> total & 1:
        raise ValueError(f'exact {name} conflict count {total} cannot be formed by waves {wave_sizes}; adjust operations, agents, or the rate')
    allocation = [0] * len(wave_sizes)
    remaining = total
    total_size = sum(wave_sizes)
    for index in range(len(wave_sizes) - 1, -1, -1):
        previous = history[index]
        feasible = [count for count in options_by_wave[index] if count <= remaining and previous >> remaining - count & 1]
        if balanced:
            target = total * wave_sizes[index] / total_size
            count = min(feasible, key=lambda value: (abs(value - target), value))
        else:
            count = feasible[0]
        allocation[index] = count
        remaining -= count
    if remaining:
        raise AssertionError('conflict allocation reconstruction failed')
    return allocation

def _zipf_group_sizes(participants: int, wave_size: int, alpha: float) -> list[int]:
    if participants < 2:
        return []
    maximum_group_size = max(2, math.ceil(wave_size / 2))
    group_count = math.ceil(participants / maximum_group_size)
    sizes = [2] * group_count
    remaining = participants - 2 * group_count
    if not remaining:
        return sizes
    weights = [1.0 / (rank + 1) ** alpha for rank in range(group_count)]
    weight_sum = sum(weights)
    quotas = [remaining * weight / weight_sum for weight in weights]
    extras = [math.floor(quota) for quota in quotas]
    for index in sorted(range(group_count), key=lambda item: (quotas[item] - extras[item], -item), reverse=True)[:remaining - sum(extras)]:
        extras[index] += 1
    return [size + extra for size, extra in zip(sizes, extras, strict=True)]

def _block_ids(files: int, blocks_per_file: int) -> list[str]:
    return [f'file_{file_index:03d}:block_{block_index:04d}' for file_index in range(files) for block_index in range(blocks_per_file)]
_FILLER_WORDS = ('context', 'evidence', 'timeline', 'detail', 'neutral', 'record', 'history', 'observation', 'account', 'activity', 'annotation', 'archive', 'assessment', 'attribute', 'category', 'checkpoint', 'chronology', 'classification', 'condition', 'confirmation', 'correlation', 'description', 'event', 'exception', 'finding', 'indicator', 'item', 'log', 'measurement', 'memo', 'narrative', 'notice', 'outcome', 'reference', 'report', 'revision', 'sequence', 'signal', 'snapshot', 'source', 'state', 'summary', 'trace', 'transition', 'validation', 'value', 'version', 'window')

def _filler_words(block_id: str, seed: int) -> tuple[str, ...]:
    digest = hashlib.sha256(f'filler:{seed}:{block_id}'.encode()).hexdigest()
    words = list(_FILLER_WORDS)
    words.extend((f'ref_{digest[index:index + 6]}' for index in range(0, 48, 6)))
    random.Random(int(digest, 16)).shuffle(words)
    return tuple(words)

def _filler(count: int, words: tuple[str, ...], offset: int) -> str:
    if count <= 0:
        return ''
    rotated = words[offset % len(words):] + words[:offset % len(words)]
    whole, remainder = divmod(count, len(rotated))
    return (' '.join(rotated) + ' ') * whole + ' '.join(rotated[:remainder])

def _make_content(block_id: str, lines: Iterable[str], target_tokens: int, tolerance: float, anchor_count: int, seed: int, source_payload: str | None=None) -> tuple[str, tuple[str, ...], int]:
    header = '\n'.join(lines).rstrip() + '\n'
    header_tokens = len(header.split())
    anchors = tuple((f"[ANCHOR:{hashlib.sha256(f'{seed}:{block_id}:{index}'.encode()).hexdigest()[:8].upper()}]" for index in range(anchor_count)))
    source_marker_tokens = 2 if source_payload is not None else 0
    minimum_tokens = header_tokens + len(anchors) + source_marker_tokens
    maximum_tokens = int(target_tokens * (1.0 + tolerance))
    if minimum_tokens > maximum_tokens:
        raise ValueError(f'metadata in {block_id} needs {minimum_tokens} tokens, exceeding the configured payload limit {maximum_tokens}')
    payload_words: tuple[str, ...] = ()
    if source_payload is not None:
        available = max(0, target_tokens - minimum_tokens)
        payload_words = tuple(source_payload.split()[:available])
    filler_tokens = max(0, target_tokens - minimum_tokens - len(payload_words))
    chunk_count = len(anchors) + 1
    base, extra = divmod(filler_tokens, chunk_count)
    filler_words = _filler_words(block_id, seed)
    parts = [header]
    if source_payload is not None:
        parts.append('SOURCE_CODE_BEGIN\n')
        if payload_words:
            parts.append(' '.join(payload_words) + '\n')
        parts.append('SOURCE_CODE_END\n')
    for index in range(chunk_count):
        count = base + (1 if index < extra else 0)
        text = _filler(count, filler_words, index * 11)
        if text:
            parts.append(text + '\n')
        if index < len(anchors):
            parts.append(anchors[index] + '\n')
    content = ''.join(parts)
    token_count = len(content.split())
    return (content, anchors, token_count)

def _commit_focus_root(value: str | Path) -> Path:
    root = Path(value)
    if not root.is_absolute():
        root = _RMWBENCH_ROOT / root
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f'commit focus dataset is absent: {root}')
    return root

def _generate_commit_focus_workload(config: Config) -> Workload:
    root = _commit_focus_root(config.dataset.root)
    manifest_path = root / 'dataset_manifest.json'
    with manifest_path.open(encoding='utf-8') as handle:
        manifest = json.load(handle)
    transactions = load_transactions(root / 'transactions.jsonl')
    transaction_rows = [transaction.to_mapping() for transaction in transactions]
    if stable_json_hash(transaction_rows) != manifest['transaction_hash']:
        raise ValueError('commit focus transaction hash differs from the manifest')
    try:
        schedule = load_schedule(schedule_path(root, config.dataset.schedule, config.benchmark.seed))
    except FileNotFoundError:
        schedule = tuple(build_schedules(transactions, concurrency=config.agents.count,
                                         seed=config.benchmark.seed)[config.dataset.schedule])
    if any((record['workload'] != config.dataset.schedule or int(record['seed']) != config.benchmark.seed for record in schedule)):
        raise ValueError('commit focus schedule metadata differs from the config')
    scheduled_ids = [str(task_id) for record in schedule for task_id in record['task_ids']]
    transaction_ids = sorted((transaction.task_id for transaction in transactions))
    if sorted(scheduled_ids) != transaction_ids:
        raise ValueError('commit focus schedule changed the transaction multiset')
    if len(scheduled_ids) != config.run.operations:
        raise ValueError(f'config requests {config.run.operations} operations but the schedule contains {len(scheduled_ids)}')
    if any((len(record['task_ids']) != config.agents.count for record in schedule)):
        raise ValueError('commit focus wave size differs from agents.count')
    repository_roots: dict[str, Path] = {}
    for repo, raw in manifest['repositories'].items():
        repository_roots[repo] = validate_repository(root / str(raw['root']), str(raw['anchor_commit']))
    by_id = {transaction.task_id: transaction for transaction in transactions}
    ordered = [(int(record['wave_id']), by_id[str(task_id)]) for record in schedule for task_id in record['task_ids']]
    summary = offered_conflicts(transactions, schedule)
    conflict_flags = classify_task_conflicts(summary)
    resources = sorted({block for transaction in transactions for block in (*transaction.read_set, *transaction.write_set)})
    target_blocks = sorted({block for transaction in transactions for block in transaction.write_set})
    if len(target_blocks) < 2:
        raise ValueError('commit focus needs at least two write targets')
    catalog_block = 'commit_focus:catalog'
    source_payloads: dict[str, str] = {}
    source_paths: dict[str, str] = {}
    for resource in resources:
        repo, separator, relative = resource.partition(':')
        if not separator or repo not in repository_roots:
            raise ValueError(f'invalid commit focus resource: {resource}')
        repository = repository_roots[repo]
        path = (repository / relative).resolve()
        if not path.is_relative_to(repository) or not path.is_file():
            raise ValueError(f'commit focus resource is absent: {resource}')
        source_payloads[resource] = path.read_text(encoding='utf-8', errors='replace')
        source_paths[resource] = relative
    route_keys = {transaction.task_id: 'route_' + hashlib.sha256(transaction.task_id.encode()).hexdigest()[:12] for transaction in transactions}
    statuses = ('red', 'amber', 'green', 'blue')
    block_lines: dict[str, list[str]] = defaultdict(list)
    drafts: list[dict[str, object]] = []
    candidate_count = min(config.reasoning.candidate_targets, len(target_blocks))
    sorted_transactions = sorted(transactions, key=lambda transaction: transaction.task_id)
    semantic_ordinals = {transaction.task_id: ordinal for ordinal, transaction in enumerate(sorted_transactions)}
    summary_slots = 32
    slot_counts: dict[str, int] = defaultdict(int)
    slots_by_task: dict[str, int] = {}
    for transaction in sorted_transactions:
        target = transaction.write_set[0]
        slot = slot_counts[target]
        if slot >= summary_slots:
            raise ValueError(f'too many transactions for target {target}')
        slots_by_task[transaction.task_id] = slot
        slot_counts[target] += 1
    for wave, transaction in ordered:
        relevant_blocks = tuple(transaction.read_set)
        if not relevant_blocks:
            raise ValueError(f'{transaction.task_id} has an empty read set')
        intended_target = transaction.write_set[0]
        semantic_ordinal = semantic_ordinals[transaction.task_id]
        base_timestamp = 1000000 + semantic_ordinal * 100
        status = statuses[semantic_ordinal % len(statuses)]
        invalid_status = statuses[(semantic_ordinal + 1) % len(statuses)]
        entity = 'entity_' + hashlib.sha256(transaction.task_id.encode()).hexdigest()[:12]
        block_lines[relevant_blocks[0]].append(f'FACT task={transaction.task_id} entity={entity} ts={base_timestamp} status={status} valid=1')
        block_lines[relevant_blocks[-1]].append(f'FACT task={transaction.task_id} entity={entity} ts={base_timestamp + 1} status={invalid_status} valid=0')
        alternatives = [block for block in target_blocks if block != intended_target]
        task_rng = random.Random(int(hashlib.sha256(f'{config.benchmark.seed}:{transaction.task_id}:targets'.encode()).hexdigest(), 16))
        candidates = [intended_target, *task_rng.sample(alternatives, candidate_count - 1)]
        task_rng.shuffle(candidates)
        offered_ww, offered_rw = conflict_flags.get(transaction.task_id, (False, False))
        drafts.append({'op_id': transaction.task_id, 'wave': wave, 'entity': entity, 'routing_key': route_keys[transaction.task_id], 'intended_target': intended_target, 'candidate_targets': tuple(candidates), 'catalog_block': catalog_block, 'relevant_blocks': relevant_blocks, 'expected_status': status, 'expected_timestamp': base_timestamp, 'summary_slot': slots_by_task[transaction.task_id], 'offered_ww': offered_ww, 'offered_rw': offered_rw})
    for lines in block_lines.values():
        lines.sort()
    catalog_lines = ['ROLE routing_catalog']
    catalog_lines.extend((f'ROUTE key={route_keys[transaction.task_id]} target={transaction.write_set[0]}' for transaction in transactions))
    target_set = set(target_blocks)
    seeds: list[BlockSeed] = []
    for block_id in resources:
        lines_for_block = [f'SOURCE_PATH path={source_paths[block_id]}', f'REPOSITORY_RESOURCE id={block_id}']
        if block_id in target_set:
            lines_for_block.extend(('ROLE target', f'TARGET id={block_id}'))
        else:
            lines_for_block.append('ROLE source')
        lines_for_block.extend(block_lines[block_id])
        if block_id in target_set:
            lines_for_block.extend((f'[[SUMMARY_SLOT:{slot:04d}]] [[RMWBENCH_UPDATE op=none entity=none status=none ts=0]]' for slot in range(summary_slots)))
        metadata_words = len('\n'.join([f'BLOCK {block_id}', *lines_for_block]).split())
        target_tokens = max(config.payload.target_tokens, metadata_words + config.payload.anchors_per_block + 258)
        content, anchors, token_count = _make_content(block_id, [f'BLOCK {block_id}', *lines_for_block], target_tokens, config.payload.tolerance, config.payload.anchors_per_block, config.benchmark.seed, source_payloads[block_id])
        seeds.append(BlockSeed(block_id, content, anchors, token_count))
    catalog_tokens = max(config.payload.target_tokens, len('\n'.join([f'BLOCK {catalog_block}', *catalog_lines]).split()) + config.payload.anchors_per_block)
    catalog_content, catalog_anchors, catalog_count = _make_content(catalog_block, [f'BLOCK {catalog_block}', *catalog_lines], catalog_tokens, config.payload.tolerance, config.payload.anchors_per_block, config.benchmark.seed)
    seeds.append(BlockSeed(catalog_block, catalog_content, catalog_anchors, catalog_count))
    return Workload(tuple(seeds), tuple((Task(**draft) for draft in drafts)))

def generate_workload(config: Config) -> Workload:
    config.validate()
    if config.dataset.source == 'commit_focus':
        return _generate_commit_focus_workload(config)
    rng = random.Random(config.benchmark.seed)
    all_blocks = _block_ids(config.dataset.files, config.dataset.blocks_per_file)
    file_payloads: dict[str, tuple[str, str]] = {}
    if config.dataset.source == 'commit0':
        loaded = load_commit0_python_files(config.dataset.root, config.dataset.files)
        file_payloads = {f'file_{index:03d}:block_0000': payload for index, payload in enumerate(loaded)}
    wave_sizes = _wave_sizes(config.run.operations, config.agents.count)
    target_count = max(config.agents.count, config.reasoning.candidate_targets)
    if target_count + 2 > len(all_blocks):
        raise ValueError('dataset needs at least candidate_targets/agents target blocks, one catalog block, and one source block')
    target_blocks = all_blocks[:target_count]
    catalog_block = all_blocks[target_count]
    source_blocks = all_blocks[target_count + 1:]
    ww_total = _requested_count(config.run.operations, config.conflict.ww_rate, 'WW')
    rw_total = _requested_count(config.run.operations, config.conflict.rw_rate, 'RW')
    zipfian = config.conflict.zipf_alpha > 0
    ww_counts = _allocate_counts(wave_sizes, ww_total, minimum=2, name='WW', balanced=zipfian)
    rw_counts = _allocate_counts(wave_sizes, rw_total, minimum=1, name='RW', requires_peer=True, balanced=zipfian)
    wave_targets: list[list[str]] = []
    wave_rw_positions: list[set[int]] = []
    for wave, size in enumerate(wave_sizes):
        ww_count = ww_counts[wave]
        ww_positions = set(rng.sample(range(size), ww_count)) if ww_count else set()
        target_offset = (wave * size if zipfian else wave) % target_count
        rotated = target_blocks[target_offset:] + target_blocks[:target_offset]
        if ww_positions:
            if zipfian:
                grouped_positions = list(ww_positions)
                rng.shuffle(grouped_positions)
                group_sizes = _zipf_group_sizes(ww_count, size, config.conflict.zipf_alpha)
                assigned = [''] * size
                targets = iter(rotated)
                offset = 0
                for group_size in group_sizes:
                    common_target = next(targets)
                    for position in grouped_positions[offset:offset + group_size]:
                        assigned[position] = common_target
                    offset += group_size
                for position in range(size):
                    if not assigned[position]:
                        assigned[position] = next(targets)
            else:
                common_target = rotated[0]
                unique_targets = iter(rotated[1:])
                assigned = [common_target if position in ww_positions else next(unique_targets) for position in range(size)]
        else:
            assigned = rotated[:size]
        wave_targets.append(assigned)
        rw_count = rw_counts[wave]
        wave_rw_positions.append(set(rng.sample(range(size), rw_count)) if rw_count else set())
    route_keys = {target: f'route_{index:04d}' for index, target in enumerate(target_blocks)}
    block_lines: dict[str, list[str]] = defaultdict(list)
    statuses = ('red', 'amber', 'green', 'blue')
    task_drafts: list[dict[str, object]] = []
    global_index = 0
    for wave, size in enumerate(wave_sizes):
        targets = wave_targets[wave]
        target_counts = Counter(targets)
        for position in range(size):
            op_id = f'op_{global_index:08d}'
            entity = f'entity_{global_index:08d}'
            intended_target = targets[position]
            is_rw = position in wave_rw_positions[wave]
            readonly_fact_blocks = [source_blocks[(global_index * config.reasoning.relevant_facts + fact_index) % len(source_blocks)] for fact_index in range(config.reasoning.relevant_facts)]
            fact_blocks = list(readonly_fact_blocks)
            if is_rw:
                other_position = (position + 1) % size
                fact_blocks[0] = targets[other_position]
            base_timestamp = 1000000 + global_index * 100
            expected_index = config.reasoning.relevant_facts - 2 if config.reasoning.relevant_facts > 1 else 0
            for fact_index, fact_block in enumerate(fact_blocks):
                valid = int(config.reasoning.relevant_facts == 1 or fact_index < config.reasoning.relevant_facts - 1)
                status = statuses[(global_index + fact_index) % len(statuses)]
                block_lines[fact_block].append(f'FACT task={op_id} entity={entity} ts={base_timestamp + fact_index} status={status} valid={valid}')
            distractors = config.reasoning.candidate_facts - config.reasoning.relevant_facts
            for distractor in range(distractors):
                fact_block = readonly_fact_blocks[distractor % len(readonly_fact_blocks)]
                block_lines[fact_block].append(f'FACT task={op_id} entity=noise_{global_index:08d}_{distractor:03d} ts={base_timestamp + 20 + distractor} status={statuses[(global_index + distractor + 1) % len(statuses)]} valid=1')
            alternatives = [target for target in target_blocks if target != intended_target]
            candidates = [intended_target, *rng.sample(alternatives, config.reasoning.candidate_targets - 1)]
            rng.shuffle(candidates)
            relevant_blocks = tuple(dict.fromkeys(fact_blocks))
            task_drafts.append({'op_id': op_id, 'wave': wave, 'entity': entity, 'routing_key': route_keys[intended_target], 'intended_target': intended_target, 'candidate_targets': tuple(candidates), 'catalog_block': catalog_block, 'relevant_blocks': relevant_blocks, 'expected_status': statuses[(global_index + expected_index) % len(statuses)], 'expected_timestamp': base_timestamp + expected_index, 'summary_slot': global_index % max(32, config.agents.count * 2), 'offered_ww': target_counts[intended_target] > 1, 'offered_rw': is_rw})
            global_index += 1
    tasks = tuple((Task(**draft) for draft in task_drafts))
    if sum((task.offered_ww for task in tasks)) != ww_total:
        raise AssertionError('generated WW rate differs from the exact allocation')
    if sum((task.offered_rw for task in tasks)) != rw_total:
        raise AssertionError('generated RW rate differs from the exact allocation')
    summary_slots = max(32, config.agents.count * 2)
    catalog_lines = ['ROLE routing_catalog']
    catalog_lines.extend((f'ROUTE key={route_keys[target]} target={target}' for target in target_blocks))
    seeds: list[BlockSeed] = []
    target_set = set(target_blocks)
    for block_id in all_blocks:
        if block_id in target_set:
            lines = ['ROLE target', f'TARGET id={block_id}', *block_lines[block_id]]
            lines.extend((f'[[SUMMARY_SLOT:{slot:04d}]] [[RMWBENCH_UPDATE op=none entity=none status=none ts=0]]' for slot in range(summary_slots)))
        elif block_id == catalog_block:
            lines = catalog_lines
        else:
            lines = ['ROLE source', *block_lines[block_id]]
        source_payload = None
        if block_id in file_payloads:
            source_path, source_payload = file_payloads[block_id]
            lines = [f'SOURCE_PATH path={source_path}', *lines]
        target_tokens = config.payload.target_tokens
        if block_id == catalog_block:
            catalog_metadata_tokens = len('\n'.join([f'BLOCK {block_id}', *lines]).split()) + config.payload.anchors_per_block
            target_tokens = max(target_tokens, catalog_metadata_tokens)
        if block_id not in target_set and block_id != catalog_block and config.payload.source_token_variation:
            long_source = int(hashlib.sha256(f'cost:{config.benchmark.seed}:{block_id}'.encode()).hexdigest(), 16) & 1
            direction = 1.0 if long_source else -1.0
            target_tokens = round(target_tokens * (1.0 + direction * config.payload.source_token_variation))
        content, anchors, token_count = _make_content(block_id, [f'BLOCK {block_id}', *lines], target_tokens, config.payload.tolerance, config.payload.anchors_per_block, config.benchmark.seed, source_payload)
        seeds.append(BlockSeed(block_id, content, anchors, token_count))
    return Workload(tuple(seeds), tasks)
