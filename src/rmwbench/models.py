from __future__ import annotations
from collections.abc import Iterable
from dataclasses import dataclass, field
from time import perf_counter_ns
from typing import Any

@dataclass(frozen=True, slots=True)
class BlockSeed:
    block_id: str
    content: str
    anchors: tuple[str, ...]
    token_count: int

@dataclass(frozen=True, slots=True)
class AgentTask:
    op_id: str
    wave: int
    entity: str
    routing_key: str
    candidate_targets: tuple[str, ...]
    catalog_block: str
    source_blocks: tuple[str, ...]
    summary_slot: int

    @property
    def allowed_blocks(self) -> frozenset[str]:
        return frozenset((*self.candidate_targets, self.catalog_block, *self.source_blocks))

@dataclass(frozen=True, slots=True)
class Task:
    op_id: str
    wave: int
    entity: str
    routing_key: str
    intended_target: str
    candidate_targets: tuple[str, ...]
    catalog_block: str
    relevant_blocks: tuple[str, ...]
    expected_status: str
    expected_timestamp: int
    summary_slot: int
    offered_ww: bool
    offered_rw: bool

    def agent_view(self) -> AgentTask:
        return AgentTask(self.op_id, self.wave, self.entity, self.routing_key, self.candidate_targets, self.catalog_block, self.relevant_blocks, self.summary_slot)

@dataclass(frozen=True, slots=True)
class ReadResult:
    content: str
    block_id: str
    version: int
    content_hash: str
    read_handle: str
    token_count: int

@dataclass(frozen=True, slots=True)
class IntentPrediction:
    selected_target: str
    read_blocks: tuple[str, ...]
    write_blocks: tuple[str, ...]
    estimated_cost: float

def access_sets_conflict(left_reads: Iterable[str], left_writes: Iterable[str], right_reads: Iterable[str], right_writes: Iterable[str]) -> bool:
    left_read_set = set(left_reads)
    left_write_set = set(left_writes)
    right_read_set = set(right_reads)
    right_write_set = set(right_writes)
    return bool(left_write_set & (right_read_set | right_write_set) or right_write_set & left_read_set)

@dataclass(frozen=True, slots=True)
class PatchDecision:
    status: str
    timestamp: int
    input_tokens: int
    output_tokens: int
    model_ns: int

@dataclass(frozen=True, slots=True)
class TargetDecision:
    selected_target: str
    input_tokens: int
    output_tokens: int
    model_ns: int

@dataclass(frozen=True, slots=True)
class ControlRequestResult:
    model_ns: int
    input_tokens: int
    output_tokens: int

@dataclass(frozen=True, slots=True)
class AdmissionGrant:
    grant_id: str
    control: ControlRequestResult
    bypasses: int = 0
    wait_ns: int = 0
    fairness_forced: bool = False

@dataclass(frozen=True, slots=True)
class WriteOutcome:
    success: bool
    reason: str | None
    semantic_correct: bool
    basis_correct: bool
    target_stale: bool
    stale_basis: bool
    lost_op_ids: tuple[str, ...] = ()
    committed_version: int | None = None
    committed_hash: str | None = None

@dataclass(frozen=True, slots=True)
class OperationResult:
    op_id: str
    wave: int
    selected_target: str | None
    success: bool
    abort_reason: str | None
    attempts: int
    reads: int
    input_tokens: int
    output_tokens: int
    semantic_correct: bool
    basis_correct: bool
    final_fresh: bool
    saw_target_conflict: bool
    latency_ns: int
    queue_ns: int = 0
    snapshot_exposure_ns: int = 0
    model_ns: int = 0
    wasted_model_ns: int = 0
    model_input_tokens: int = 0
    model_output_tokens: int = 0
    control_ns: int = 0
    control_input_tokens: int = 0
    control_output_tokens: int = 0
    predicted_read_blocks: tuple[str, ...] = ()
    predicted_write_blocks: tuple[str, ...] = ()
    actual_read_blocks: tuple[str, ...] = ()
    actual_write_blocks: tuple[str, ...] = ()
    admission_read_blocks: tuple[str, ...] = ()
    admission_write_blocks: tuple[str, ...] = ()
    access_refined: bool = False
    probe_reads: int = 0
    probe_snapshot_reused: bool = False
    probe_snapshot_rebound: bool = False
    admission_bypasses: int = 0
    admission_wait_ns: int = 0
    fairness_forced: bool = False
    target_late_bound: bool = False
    target_plan_ns: int = 0
    target_plan_input_tokens: int = 0
    target_plan_output_tokens: int = 0

@dataclass(slots=True)
class OperationTrace:
    op_id: str
    agent_id: str
    wave: int
    events: list[dict[str, Any]] = field(default_factory=list)

    def record(self, event: str, **fields: Any) -> None:
        self.events.append({'ts_ns': perf_counter_ns(), 'event': event, 'op_id': self.op_id, 'agent_id': self.agent_id, 'wave': self.wave, **fields})

@dataclass(frozen=True, slots=True)
class AgentExecution:
    result: OperationResult
    events: tuple[dict[str, Any], ...]
