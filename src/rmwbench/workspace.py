from __future__ import annotations
from contextlib import ExitStack
from dataclasses import dataclass, field
import hashlib
import re
from threading import Lock, RLock
from time import perf_counter_ns
from typing import Iterable
from .config import StorageConfig
from .models import AgentTask, BlockSeed, OperationTrace, ReadResult, Task, WriteOutcome
_ANCHOR_PATTERN = re.compile('\\[ANCHOR:[0-9A-F]{8}\\]')

def content_hash(content: str) -> str:
    canonical = content.replace('\r\n', '\n')
    return 'sha256:' + hashlib.sha256(canonical.encode('utf-8')).hexdigest()

@dataclass(slots=True)
class _Block:
    content: str
    anchors: tuple[str, ...]
    token_count: int
    version: int = 0
    content_hash: str = ''
    active_updates: dict[int, str] = field(default_factory=dict)
    last_writer: str | None = None
    last_commit_id: str | None = None
    lock: RLock = field(default_factory=RLock)

@dataclass(frozen=True, slots=True)
class _ReadRecord:
    handle: str
    agent_id: str
    op_id: str
    block_id: str
    version: int
    content_hash: str
    read_ns: int
    active_updates: tuple[tuple[int, str], ...]

@dataclass(frozen=True, slots=True)
class _Decision:
    agent_id: str
    selected_target: str
    decide_ns: int

class Workspace:

    def __init__(self, blocks: Iterable[BlockSeed], storage: StorageConfig, tasks: Iterable[Task]):
        self._blocks: dict[str, _Block] = {}
        for seed in blocks:
            self._blocks[seed.block_id] = _Block(content=seed.content, anchors=seed.anchors, token_count=seed.token_count, content_hash=content_hash(seed.content))
        self._storage = storage
        self._tasks = {task.op_id: task for task in tasks}
        self._agent_tasks = {task.op_id: task.agent_view() for task in self._tasks.values()}
        self._handles: dict[str, _ReadRecord] = {}
        self._handles_by_op: dict[str, set[str]] = {}
        self._handle_counter = 0
        self._handle_lock = Lock()
        self._decisions: dict[str, _Decision] = {}
        self._decision_lock = Lock()
        self._lost_op_ids: set[str] = set()
        self._lost_lock = Lock()
        self._head_version = 0
        self._head_lock = Lock()

    @property
    def lost_op_ids(self) -> frozenset[str]:
        with self._lost_lock:
            return frozenset(self._lost_op_ids)

    @property
    def head_version(self) -> int:
        with self._head_lock:
            return self._head_version

    def intent_index(self, task: AgentTask) -> str:
        self._validate_agent_task(task)
        block = self._blocks[task.catalog_block]
        with block.lock:
            return '\n'.join((line for line in block.content.splitlines() if line.startswith('ROLE ') or line.startswith('ROUTE ')))

    def snapshot(self) -> tuple[dict[str, object], ...]:
        snapshots: list[dict[str, object]] = []
        for block_id, block in self._blocks.items():
            with block.lock:
                snapshots.append({'block_id': block_id, 'content': block.content, 'version': block.version, 'content_hash': block.content_hash, 'last_writer': block.last_writer, 'last_commit_id': block.last_commit_id})
        return tuple(snapshots)

    def read(self, agent_id: str, task: AgentTask, block_id: str, trace: OperationTrace) -> ReadResult:
        self._validate_agent_task(task)
        if block_id not in task.allowed_blocks:
            raise ValueError(f'{task.op_id} cannot access block {block_id}')
        try:
            block = self._blocks[block_id]
        except KeyError as error:
            raise ValueError(f'unknown block {block_id}') from error
        with block.lock:
            content = block.content
            version = block.version
            digest = block.content_hash
            token_count = block.token_count
            active_updates = tuple(block.active_updates.items())
            read_ns = perf_counter_ns()
        with self._handle_lock:
            self._handle_counter += 1
            handle = f'R_{self._handle_counter:012d}'
            record = _ReadRecord(handle, agent_id, task.op_id, block_id, version, digest, read_ns, active_updates)
            self._handles[handle] = record
            self._handles_by_op.setdefault(task.op_id, set()).add(handle)
        trace.record('READ', block_id=block_id, version=version, content_hash=digest, read_handle=handle, token_count=token_count)
        return ReadResult(content, block_id, version, digest, handle, token_count)

    def snapshot_is_current(self, reads: Iterable[ReadResult]) -> bool:
        by_block: dict[str, ReadResult] = {}
        for result in reads:
            previous = by_block.setdefault(result.block_id, result)
            if previous.version != result.version or previous.content_hash != result.content_hash:
                return False
        try:
            blocks = {block_id: self._blocks[block_id] for block_id in sorted(by_block)}
        except KeyError:
            return False
        with ExitStack() as stack:
            for block in blocks.values():
                stack.enter_context(block.lock)
            return all((blocks[block_id].version == result.version and blocks[block_id].content_hash == result.content_hash for block_id, result in by_block.items()))

    def decide(self, agent_id: str, task: AgentTask, selected_target: str, basis: Iterable[str], trace: OperationTrace) -> None:
        self._validate_agent_task(task)
        latent_task = self._tasks[task.op_id]
        if selected_target not in task.candidate_targets:
            raise ValueError('selected target is outside the task candidate set')
        basis_tuple = tuple(basis)
        with self._handle_lock:
            records = [self._handles.get(handle) for handle in basis_tuple]
        if any((record is None for record in records)):
            raise ValueError('decision contains an unknown read handle')
        if any((record.agent_id != agent_id or record.op_id != task.op_id for record in records if record is not None)):
            raise ValueError('decision contains a foreign read handle')
        decision = _Decision(agent_id, selected_target, perf_counter_ns())
        with self._decision_lock:
            if task.op_id in self._decisions:
                raise ValueError(f'task {task.op_id} already made a routing decision')
            self._decisions[task.op_id] = decision
        trace.record('DECIDE', target_block=selected_target, basis=list(basis_tuple), routing_correct=selected_target == latent_task.intended_target)

    def _take_basis(self, agent_id: str, task: AgentTask, basis: tuple[str, ...]) -> tuple[_ReadRecord, ...] | None:
        if not basis or len(set(basis)) != len(basis):
            return None
        with self._handle_lock:
            records = tuple((self._handles.get(handle) for handle in basis))
            if any((record is None for record in records)):
                return None
            typed_records = tuple((record for record in records if record is not None))
            if any((record.agent_id != agent_id or record.op_id != task.op_id for record in typed_records)):
                return None
            for record in typed_records:
                del self._handles[record.handle]
                self._handles_by_op[task.op_id].discard(record.handle)
            return typed_records

    def write(self, agent_id: str, task: AgentTask, target_block: str, replacement: str, replacement_token_count: int, expected_target_hash: str, basis: Iterable[str], trace: OperationTrace) -> WriteOutcome:
        self._validate_agent_task(task)
        latent_task = self._tasks[task.op_id]
        basis_tuple = tuple(basis)
        trace.record('WRITE_ATTEMPT', target_block=target_block, expected_target_hash=expected_target_hash, basis=list(basis_tuple), replacement_token_count=replacement_token_count)
        with self._decision_lock:
            decision = self._decisions.get(task.op_id)
        if decision is None or decision.agent_id != agent_id:
            return self._reject(trace, 'missing_decision')
        if decision.selected_target != target_block:
            return self._reject(trace, 'target_differs_from_decision')
        records = self._take_basis(agent_id, task, basis_tuple)
        if records is None:
            return self._reject(trace, 'invalid_or_reused_basis')
        target_reads = [record for record in records if record.block_id == target_block and record.read_ns > decision.decide_ns]
        if not target_reads:
            return self._reject(trace, 'target_not_reread_after_decision')
        target_read = max(target_reads, key=lambda record: record.read_ns)
        if target_read.content_hash != expected_target_hash:
            return self._reject(trace, 'expected_hash_does_not_match_target_read')
        basis_blocks = {record.block_id for record in records}
        basis_correct = set(latent_task.relevant_blocks).issubset(basis_blocks)
        lock_ids = sorted(basis_blocks | {target_block})
        with ExitStack() as stack:
            for block_id in lock_ids:
                stack.enter_context(self._blocks[block_id].lock)
            stale_records = [record for record in records if self._blocks[record.block_id].version != record.version or self._blocks[record.block_id].content_hash != record.content_hash]
            target = self._blocks[target_block]
            target_stale = target.version != target_read.version or target.content_hash != target_read.content_hash
            semantic_correct = self._semantic_correct(latent_task, replacement)
            anchors_correct = tuple(_ANCHOR_PATTERN.findall(replacement)) == target.anchors
            if not anchors_correct:
                return self._reject(trace, 'immutable_anchor_changed', semantic_correct=semantic_correct, basis_correct=basis_correct, target_stale=target_stale, stale_basis=bool(stale_records))
            if self._storage.write_validation == 'target_cas' and target_stale:
                return self._reject(trace, 'target_cas_conflict', semantic_correct=semantic_correct, basis_correct=basis_correct, target_stale=True, stale_basis=bool(stale_records))
            if self._storage.write_validation == 'basis_occ' and stale_records:
                return self._reject(trace, 'basis_occ_conflict', semantic_correct=semantic_correct, basis_correct=basis_correct, target_stale=target_stale, stale_basis=True)
            base_active = dict(target_read.active_updates)
            new_active = dict(base_active)
            new_active[latent_task.summary_slot] = task.op_id
            concurrent_ids = set(target.active_updates.values()) - set(base_active.values())
            lost_ids = tuple(sorted(concurrent_ids - set(new_active.values())))
            target.content = replacement
            target.token_count = replacement_token_count
            target.version += 1
            target.content_hash = content_hash(replacement)
            target.active_updates = new_active
            target.last_writer = agent_id
            target.last_commit_id = task.op_id
            committed_version = target.version
            committed_hash = target.content_hash
            with self._head_lock:
                self._head_version += 1
                head_version = self._head_version
        if lost_ids:
            with self._lost_lock:
                self._lost_op_ids.update(lost_ids)
        trace.record('WRITE', target_block=target_block, status='committed', version=committed_version, content_hash=committed_hash, target_stale=target_stale, stale_basis=bool(stale_records), semantic_correct=semantic_correct, basis_correct=basis_correct, lost_op_ids=list(lost_ids), head_version=head_version)
        return WriteOutcome(True, None, semantic_correct, basis_correct, target_stale, bool(stale_records), lost_ids, committed_version, committed_hash)

    @staticmethod
    def _semantic_correct(task: Task, replacement: str) -> bool:
        prefix = f'[[SUMMARY_SLOT:{task.summary_slot:04d}]] '
        start = replacement.find(prefix)
        if start < 0:
            return False
        end = replacement.find('\n', start)
        if end < 0:
            end = len(replacement)
        marker = f'[[RMWBENCH_UPDATE op={task.op_id} entity={task.entity} status={task.expected_status} ts={task.expected_timestamp}]]'
        return replacement[start:end] == prefix + marker

    @staticmethod
    def _reject(trace: OperationTrace, reason: str, *, semantic_correct: bool=False, basis_correct: bool=False, target_stale: bool=False, stale_basis: bool=False) -> WriteOutcome:
        trace.record('WRITE', status='rejected', reason=reason, semantic_correct=semantic_correct, basis_correct=basis_correct, target_stale=target_stale, stale_basis=stale_basis)
        return WriteOutcome(False, reason, semantic_correct, basis_correct, target_stale, stale_basis)

    def release_task(self, op_id: str) -> None:
        with self._handle_lock:
            handles = self._handles_by_op.pop(op_id, set())
            for handle in handles:
                self._handles.pop(handle, None)
        with self._decision_lock:
            self._decisions.pop(op_id, None)

    def _validate_agent_task(self, task: AgentTask) -> None:
        if self._agent_tasks.get(task.op_id) != task:
            raise ValueError(f'unregistered or modified agent task {task.op_id}')
