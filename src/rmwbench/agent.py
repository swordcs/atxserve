from __future__ import annotations
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Barrier
from time import perf_counter_ns, sleep
from .config import PredictionConfig, RunConfig
from .models import AdmissionGrant, AgentExecution, AgentTask, IntentPrediction, OperationResult, OperationTrace, ReadResult
from .prediction import predict_access_sets
from .reasoner import Reasoner
from .workspace import Workspace

class MockAgent:

    def __init__(self, workspace: Workspace, run_config: RunConfig):
        self._workspace = workspace
        self._run_config = run_config

    def execute(self, agent_id: str, task: AgentTask, barrier: Barrier | None=None, forced_stale: bool=False) -> AgentExecution:
        trace = OperationTrace(task.op_id, agent_id, task.wave)
        trace.record('TASK_START', routing_key=task.routing_key)
        reads = 0
        input_tokens = 0
        output_tokens = 0
        attempts = 0
        selected_target: str | None = None
        final_outcome = None
        saw_target_conflict = False
        first_read_ns: int | None = None

        def read(block_id: str) -> ReadResult:
            nonlocal reads, input_tokens, first_read_ns
            if first_read_ns is None:
                first_read_ns = perf_counter_ns()
            result = self._workspace.read(agent_id, task, block_id, trace)
            reads += 1
            input_tokens += result.token_count
            return result
        try:
            evidence_reads = [read(block_id) for block_id in task.source_blocks]
            status, timestamp = self._select_evidence(task, evidence_reads)
            catalog_read = read(task.catalog_block)
            selected_target = self._route(task, catalog_read.content)
            self._workspace.decide(agent_id, task, selected_target, [*(result.read_handle for result in evidence_reads), catalog_read.read_handle], trace)
            while attempts <= self._run_config.max_retries:
                if attempts:
                    evidence_reads = [read(block_id) for block_id in task.source_blocks]
                    status, timestamp = self._select_evidence(task, evidence_reads)
                target_read = read(selected_target)
                replacement, replacement_tokens = self._replace_summary(task, target_read, status, timestamp)
                output_tokens += replacement_tokens
                attempts += 1
                if forced_stale and attempts == 1 and (barrier is not None):
                    barrier.wait(timeout=30)
                elif not forced_stale and self._run_config.mock_delay_ms:
                    sleep(self._run_config.mock_delay_ms / 1000.0)
                final_outcome = self._workspace.write(agent_id, task, selected_target, replacement, replacement_tokens, target_read.content_hash, [*(result.read_handle for result in evidence_reads), target_read.read_handle], trace)
                saw_target_conflict |= final_outcome.target_stale
                if final_outcome.success:
                    break
                if final_outcome.reason != 'target_cas_conflict':
                    break
            if final_outcome is None:
                raise AssertionError('mock agent made no write attempt')
            end_ns = perf_counter_ns()
            if first_read_ns is None:
                raise AssertionError('mock agent completed without reading')
            result = OperationResult(op_id=task.op_id, wave=task.wave, selected_target=selected_target, success=final_outcome.success, abort_reason=final_outcome.reason, attempts=attempts, reads=reads, input_tokens=input_tokens, output_tokens=output_tokens, semantic_correct=final_outcome.semantic_correct, basis_correct=final_outcome.basis_correct, final_fresh=final_outcome.success and (not final_outcome.stale_basis), saw_target_conflict=saw_target_conflict, latency_ns=end_ns - first_read_ns)
            trace.record('TASK_END', success=result.success, attempts=attempts, reads=reads, latency_ns=result.latency_ns, abort_reason=result.abort_reason)
            return AgentExecution(result, tuple(trace.events))
        finally:
            self._workspace.release_task(task.op_id)

    @staticmethod
    def _select_evidence(task: AgentTask, reads: list[ReadResult]) -> tuple[str, int]:
        pattern = re.compile(f'FACT task={re.escape(task.op_id)} entity={re.escape(task.entity)} ts=(\\d+) status=([a-z]+) valid=([01])')
        candidates: list[tuple[int, str]] = []
        for result in reads:
            for match in pattern.finditer(result.content):
                if match.group(3) == '1':
                    candidates.append((int(match.group(1)), match.group(2)))
        if not candidates:
            raise ValueError(f'no valid evidence found for {task.op_id}')
        timestamp, status = max(candidates)
        return (status, timestamp)

    @staticmethod
    def _route(task: AgentTask, catalog: str) -> str:
        prefix = f'ROUTE key={task.routing_key} target='
        start = catalog.find(prefix)
        if start < 0:
            raise ValueError(f'routing key {task.routing_key} is absent from catalog')
        start += len(prefix)
        end = catalog.find('\n', start)
        target = catalog[start:] if end < 0 else catalog[start:end]
        if target not in task.candidate_targets:
            raise ValueError('catalog target is outside the task candidate set')
        return target

    @staticmethod
    def _replace_summary(task: AgentTask, target: ReadResult, status: str, timestamp: int) -> tuple[str, int]:
        prefix = f'[[SUMMARY_SLOT:{task.summary_slot:04d}]] '
        start = target.content.find(prefix)
        if start < 0:
            raise ValueError(f'summary slot {task.summary_slot} is missing')
        end = target.content.find('\n', start)
        if end < 0:
            end = len(target.content)
        old_line = target.content[start:end]
        marker = f'[[RMWBENCH_UPDATE op={task.op_id} entity={task.entity} status={status} ts={timestamp}]]'
        new_line = prefix + marker
        replacement = target.content[:start] + new_line + target.content[end:]
        token_count = target.token_count - len(old_line.split()) + len(new_line.split())
        return (replacement, token_count)

@dataclass(slots=True)
class PreparedTransaction:
    agent_id: str
    task: AgentTask
    intent: IntentPrediction
    trace: OperationTrace
    arrived_ns: int
    ready_ns: int
    routing_index: str
    bound_ns: int | None = None
    planned_target: str | None = None
    target_plan_ns: int = 0
    target_plan_input_tokens: int = 0
    target_plan_output_tokens: int = 0
    evidence_reads: list[ReadResult] = field(default_factory=list)
    catalog_read: ReadResult | None = None
    target_read: ReadResult | None = None
    reads: int = 0
    input_tokens: int = 0
    actual_read_blocks: set[str] = field(default_factory=set)

class TransactionAgent:

    def __init__(self, workspace: Workspace, run_config: RunConfig, reasoner: Reasoner, prediction_config: PredictionConfig | None=None, prediction_seed: int=0, refine_access_sets: bool=False, reuse_probe_snapshot: bool=True, retry_backoff: Callable[[str, int], float] | None=None, late_bind_write_target: bool=False, predict_access_sets_after_plan: bool=False):
        self._workspace = workspace
        self._run_config = run_config
        self._reasoner = reasoner
        self._prediction_config = prediction_config or PredictionConfig()
        self._prediction_seed = prediction_seed
        self._refine_access_sets = refine_access_sets
        self._reuse_probe_snapshot = reuse_probe_snapshot
        self._retry_backoff = retry_backoff
        self._late_bind_write_target = late_bind_write_target
        self._predict_access_sets_after_plan = predict_access_sets_after_plan

    def prepare(self, agent_id: str, task: AgentTask) -> PreparedTransaction:
        arrived_ns = perf_counter_ns()
        trace = OperationTrace(task.op_id, agent_id, task.wave)
        trace.record('TASK_START', routing_key=task.routing_key)
        repository_index = self._workspace.intent_index(task)
        selected_target = MockAgent._route(task, repository_index)
        oracle_read_blocks = tuple(dict.fromkeys((*task.source_blocks, task.catalog_block, selected_target)))
        oracle_write_blocks = (selected_target,)
        prediction_read_basis = oracle_read_blocks
        prediction_write_basis = oracle_write_blocks
        if self._late_bind_write_target:
            prediction_read_basis = tuple(dict.fromkeys((*task.source_blocks, task.catalog_block)))
            prediction_write_basis = ()
        read_blocks, write_blocks = predict_access_sets(task, prediction_read_basis, prediction_write_basis, self._prediction_config, self._prediction_seed)
        intent = IntentPrediction(selected_target=selected_target, read_blocks=read_blocks, write_blocks=write_blocks, estimated_cost=max(1.0, float(len(task.source_blocks))))
        ready_ns = perf_counter_ns()
        trace.record('INTENT_READY', predicted_reads=list(intent.read_blocks), predicted_writes=list(intent.write_blocks), oracle_reads=list(oracle_read_blocks), oracle_writes=list(oracle_write_blocks), prediction_target={'read_precision': self._prediction_config.read_precision, 'read_recall': self._prediction_config.read_recall, 'write_precision': self._prediction_config.write_precision, 'write_recall': self._prediction_config.write_recall}, false_negative_reads=sorted(set(oracle_read_blocks) - set(read_blocks)), false_negative_writes=sorted(set(oracle_write_blocks) - set(write_blocks)), estimated_cost=intent.estimated_cost, intent_source='pre_plan_hint' if self._late_bind_write_target else 'visible_index', late_bind_write_target=self._late_bind_write_target)
        return PreparedTransaction(agent_id, task, intent, trace, arrived_ns, ready_ns, repository_index)

    def resolve_target(self, transaction: PreparedTransaction) -> str:
        if not self._late_bind_write_target:
            return transaction.intent.selected_target
        if transaction.planned_target is not None:
            return transaction.planned_target
        transaction.trace.record('TARGET_PLAN_START', routing_key=transaction.task.routing_key)
        decision = self._reasoner.plan_target(transaction.task, transaction.routing_index)
        transaction.planned_target = decision.selected_target
        transaction.target_plan_ns = decision.model_ns
        transaction.target_plan_input_tokens = decision.input_tokens
        transaction.target_plan_output_tokens = decision.output_tokens
        transaction.trace.record('TARGET_PLAN_READY', selected_target=decision.selected_target, model_ns=decision.model_ns, model_input_tokens=decision.input_tokens, model_output_tokens=decision.output_tokens)
        if self._predict_access_sets_after_plan:
            oracle_reads = tuple(dict.fromkeys((*transaction.task.source_blocks, transaction.task.catalog_block, decision.selected_target)))
            oracle_writes = (decision.selected_target,)
            predicted_reads, predicted_writes = predict_access_sets(transaction.task, oracle_reads, oracle_writes, self._prediction_config, self._prediction_seed)
            transaction.intent = IntentPrediction(selected_target=decision.selected_target, read_blocks=predicted_reads, write_blocks=predicted_writes, estimated_cost=transaction.intent.estimated_cost)
            transaction.ready_ns = perf_counter_ns()
            transaction.trace.record('PLANNED_INTENT_READY', predicted_reads=list(predicted_reads), predicted_writes=list(predicted_writes), oracle_reads=list(oracle_reads), oracle_writes=list(oracle_writes), false_negative_reads=sorted(set(oracle_reads) - set(predicted_reads)), false_negative_writes=sorted(set(oracle_writes) - set(predicted_writes)))
        return transaction.planned_target

    def _selected_target(self, transaction: PreparedTransaction) -> str:
        if self._late_bind_write_target:
            if transaction.planned_target is None:
                raise ValueError(f'{transaction.task.op_id} has no target plan')
            return transaction.planned_target
        return transaction.intent.selected_target

    def _read(self, transaction: PreparedTransaction, block_id: str) -> ReadResult:
        result = self._workspace.read(transaction.agent_id, transaction.task, block_id, transaction.trace)
        transaction.reads += 1
        transaction.input_tokens += result.token_count
        transaction.actual_read_blocks.add(block_id)
        return result

    def bind(self, transaction: PreparedTransaction) -> None:
        if transaction.bound_ns is not None:
            raise ValueError(f'{transaction.task.op_id} is already bound')
        if self._late_bind_write_target:
            self.resolve_target(transaction)
        transaction.trace.record('GPU_RESERVED', head_version=self._workspace.head_version)
        transaction.bound_ns = perf_counter_ns()
        transaction.evidence_reads = [self._read(transaction, block_id) for block_id in transaction.task.source_blocks]
        transaction.catalog_read = self._read(transaction, transaction.task.catalog_block)
        self._workspace.decide(transaction.agent_id, transaction.task, self._selected_target(transaction), [*(result.read_handle for result in transaction.evidence_reads), transaction.catalog_read.read_handle], transaction.trace)
        transaction.target_read = self._read(transaction, self._selected_target(transaction))
        transaction.trace.record('SNAPSHOT_BOUND', base_version=self._workspace.head_version, read_blocks=sorted(transaction.actual_read_blocks))

    def _refresh_bind(self, transaction: PreparedTransaction) -> None:
        if transaction.bound_ns is None:
            raise ValueError(f'{transaction.task.op_id} is not bound')
        transaction.trace.record('GPU_RERESERVED', head_version=self._workspace.head_version)
        transaction.bound_ns = perf_counter_ns()
        transaction.actual_read_blocks.clear()
        transaction.evidence_reads = [self._read(transaction, block_id) for block_id in transaction.task.source_blocks]
        transaction.catalog_read = self._read(transaction, transaction.task.catalog_block)
        transaction.target_read = self._read(transaction, self._selected_target(transaction))
        transaction.trace.record('SNAPSHOT_REBOUND', base_version=self._workspace.head_version, read_blocks=sorted(transaction.actual_read_blocks))

    def _reuse_probe_if_current(self, transaction: PreparedTransaction) -> bool:
        if transaction.catalog_read is None or transaction.target_read is None:
            raise ValueError(f'{transaction.task.op_id} has no probe snapshot')
        reads = [*transaction.evidence_reads, transaction.catalog_read, transaction.target_read]
        if not self._workspace.snapshot_is_current(reads):
            return False
        transaction.bound_ns = perf_counter_ns()
        transaction.trace.record('SNAPSHOT_REUSED', base_version=self._workspace.head_version, read_blocks=sorted(transaction.actual_read_blocks))
        return True

    def execute(self, transaction: PreparedTransaction, model_admission: bool=False, commit_fence: bool=False) -> AgentExecution:
        task = transaction.task
        if self._late_bind_write_target:
            self.resolve_target(transaction)
        selected_target = self._selected_target(transaction)
        grant: AdmissionGrant | None = None
        active_intent = transaction.intent
        released_grants: list[AdmissionGrant] = []
        access_refined = False
        declared_access_fast_path = False
        probe_reads = 0
        probe_snapshot_reused = False
        probe_snapshot_rebound = False
        commit_granted = False
        attempts = 0
        output_tokens = 0
        model_input_tokens = transaction.target_plan_input_tokens
        model_output_tokens = transaction.target_plan_output_tokens
        control_ns = 0
        control_input_tokens = 0
        control_output_tokens = 0
        model_ns = transaction.target_plan_ns
        wasted_model_ns = 0
        saw_target_conflict = False
        final_outcome = None

        def release_grant(grant_to_release: AdmissionGrant | None, intent: IntentPrediction) -> bool:
            nonlocal control_ns, control_input_tokens, control_output_tokens
            if grant_to_release is None or any((released is grant_to_release for released in released_grants)):
                return True
            try:
                control = self._reasoner.release(task, intent, grant_to_release)
            except Exception as error:
                transaction.trace.record('GRANT_RELEASE_FAILED', grant_id=grant_to_release.grant_id, error_type=type(error).__name__)
                return False
            released_grants.append(grant_to_release)
            control_ns += control.model_ns
            control_input_tokens += control.input_tokens
            control_output_tokens += control.output_tokens
            transaction.trace.record('GRANT_RELEASED', grant_id=grant_to_release.grant_id, control_ns=control.model_ns, control_input_tokens=control.input_tokens, control_output_tokens=control.output_tokens)
            return True
        try:
            if model_admission:
                if self._refine_access_sets:
                    if self._late_bind_write_target:
                        declared_access_fast_path = True
                        actual_reads = tuple(sorted(dict.fromkeys((*task.source_blocks, task.catalog_block, selected_target))))
                        transaction.trace.record('DECLARED_ACCESS_SET_READY', declared_reads=list(actual_reads), declared_writes=[selected_target])
                    else:
                        transaction.trace.record('ACCESS_PROBE_START')
                        self.bind(transaction)
                        actual_reads = tuple(sorted(transaction.actual_read_blocks))
                        probe_reads = transaction.reads
                    actual_writes = (selected_target,)
                    access_refined = set(transaction.intent.read_blocks) != set(actual_reads) or set(transaction.intent.write_blocks) != set(actual_writes)
                    active_intent = IntentPrediction(selected_target=selected_target, read_blocks=actual_reads, write_blocks=actual_writes, estimated_cost=transaction.intent.estimated_cost)
                    transaction.trace.record('ACCESS_REFINEMENT_START', mismatch=access_refined, predicted_reads=list(transaction.intent.read_blocks), predicted_writes=list(transaction.intent.write_blocks), observed_reads=list(actual_reads), observed_writes=list(actual_writes), access_source='declared_plan' if declared_access_fast_path else 'runtime_probe')
                else:
                    transaction.trace.record('ADMISSION_START')
                grant = self._reasoner.acquire(task, active_intent)
                if grant is None:
                    raise RuntimeError('model-side admission did not return a grant')
                control_ns += grant.control.model_ns
                control_input_tokens += grant.control.input_tokens
                control_output_tokens += grant.control.output_tokens
                if self._refine_access_sets:
                    transaction.trace.record('ACCESS_REFINEMENT_GRANTED', grant_id=grant.grant_id, refined_reads=list(active_intent.read_blocks), refined_writes=list(active_intent.write_blocks), control_ns=grant.control.model_ns, control_input_tokens=grant.control.input_tokens, control_output_tokens=grant.control.output_tokens, admission_bypasses=grant.bypasses, admission_wait_ns=grant.wait_ns, fairness_forced=grant.fairness_forced)
                    if declared_access_fast_path:
                        self.bind(transaction)
                    elif self._reuse_probe_snapshot:
                        probe_snapshot_reused = self._reuse_probe_if_current(transaction)
                        if not probe_snapshot_reused:
                            transaction.trace.record('PROBE_SNAPSHOT_STALE')
                            probe_snapshot_rebound = True
                            self._refresh_bind(transaction)
                    else:
                        transaction.trace.record('PROBE_SNAPSHOT_REUSE_DISABLED')
                        probe_snapshot_rebound = True
                        self._refresh_bind(transaction)
                    if set(transaction.actual_read_blocks) != set(active_intent.read_blocks):
                        raise RuntimeError('access set changed while binding refined transaction')
                else:
                    transaction.trace.record('ADMISSION_GRANTED', grant_id=grant.grant_id, control_ns=grant.control.model_ns, control_input_tokens=grant.control.input_tokens, control_output_tokens=grant.control.output_tokens, admission_bypasses=grant.bypasses, admission_wait_ns=grant.wait_ns, fairness_forced=grant.fairness_forced)
                    self.bind(transaction)
            if transaction.bound_ns is None or transaction.target_read is None:
                raise ValueError(f'{task.op_id} has no bound snapshot')
            while attempts <= self._run_config.max_retries:
                if attempts:
                    transaction.evidence_reads = [self._read(transaction, block_id) for block_id in task.source_blocks]
                    transaction.catalog_read = self._read(transaction, task.catalog_block)
                    transaction.target_read = self._read(transaction, selected_target)
                transaction.trace.record('MODEL_START', attempt=attempts + 1)
                patch = self._reasoner.make_patch(task, transaction.evidence_reads, active_intent, grant)
                model_ns += patch.model_ns
                model_input_tokens += patch.input_tokens
                model_output_tokens += patch.output_tokens
                transaction.trace.record('PATCH_READY', attempt=attempts + 1, status=patch.status, timestamp=patch.timestamp, model_ns=patch.model_ns, model_input_tokens=patch.input_tokens, model_output_tokens=patch.output_tokens)
                replacement, replacement_tokens = MockAgent._replace_summary(task, transaction.target_read, patch.status, patch.timestamp)
                output_tokens += replacement_tokens
                attempts += 1
                if commit_fence and (not commit_granted):
                    if grant is None:
                        raise RuntimeError('commit fence requires a grant')
                    control = self._reasoner.commit(task, active_intent, grant)
                    control_ns += control.model_ns
                    control_input_tokens += control.input_tokens
                    control_output_tokens += control.output_tokens
                    commit_granted = True
                    transaction.trace.record('COMMIT_GRANTED', grant_id=grant.grant_id, control_ns=control.model_ns, control_input_tokens=control.input_tokens, control_output_tokens=control.output_tokens)
                final_outcome = self._workspace.write(transaction.agent_id, task, selected_target, replacement, replacement_tokens, transaction.target_read.content_hash, [*(result.read_handle for result in transaction.evidence_reads), transaction.catalog_read.read_handle, transaction.target_read.read_handle], transaction.trace)
                saw_target_conflict |= final_outcome.target_stale
                if final_outcome.success:
                    if final_outcome.stale_basis or not final_outcome.semantic_correct or (not final_outcome.basis_correct):
                        wasted_model_ns += patch.model_ns
                    transaction.trace.record('COMMIT_PUBLISHED', version=final_outcome.committed_version)
                    break
                wasted_model_ns += patch.model_ns
                if final_outcome.reason not in {'target_cas_conflict', 'basis_occ_conflict'}:
                    break
                if self._retry_backoff is not None and attempts <= self._run_config.max_retries:
                    delay_seconds = self._retry_backoff(task.op_id, attempts)
                    transaction.trace.record('OCC_BACKOFF', retry_index=attempts, delay_ms=delay_seconds * 1000.0)
                    sleep(delay_seconds)
            if final_outcome is None:
                raise AssertionError('transaction made no write attempt')
            transaction_end_ns = perf_counter_ns()
            release_grant(grant, active_intent)
            end_ns = perf_counter_ns()
            result = OperationResult(op_id=task.op_id, wave=task.wave, selected_target=selected_target, success=final_outcome.success, abort_reason=final_outcome.reason, attempts=attempts, reads=transaction.reads, input_tokens=transaction.input_tokens, output_tokens=output_tokens, semantic_correct=final_outcome.semantic_correct, basis_correct=final_outcome.basis_correct, final_fresh=final_outcome.success and (not final_outcome.stale_basis), saw_target_conflict=saw_target_conflict, latency_ns=end_ns - transaction.arrived_ns, queue_ns=transaction.bound_ns - transaction.ready_ns, snapshot_exposure_ns=transaction_end_ns - transaction.bound_ns, model_ns=model_ns, wasted_model_ns=wasted_model_ns, model_input_tokens=model_input_tokens, model_output_tokens=model_output_tokens, control_ns=control_ns, control_input_tokens=control_input_tokens, control_output_tokens=control_output_tokens, predicted_read_blocks=transaction.intent.read_blocks, predicted_write_blocks=transaction.intent.write_blocks, actual_read_blocks=tuple(sorted(transaction.actual_read_blocks)), actual_write_blocks=(selected_target,), admission_read_blocks=active_intent.read_blocks, admission_write_blocks=active_intent.write_blocks, access_refined=access_refined, probe_reads=probe_reads, probe_snapshot_reused=probe_snapshot_reused, probe_snapshot_rebound=probe_snapshot_rebound, admission_bypasses=grant.bypasses if grant is not None else 0, admission_wait_ns=grant.wait_ns if grant is not None else 0, fairness_forced=grant.fairness_forced if grant is not None else False, target_late_bound=self._late_bind_write_target, target_plan_ns=transaction.target_plan_ns, target_plan_input_tokens=transaction.target_plan_input_tokens, target_plan_output_tokens=transaction.target_plan_output_tokens)
            transaction.trace.record('TASK_END', success=result.success, attempts=attempts, reads=transaction.reads, latency_ns=result.latency_ns, queue_ns=result.queue_ns, snapshot_exposure_ns=result.snapshot_exposure_ns, model_ns=result.model_ns, wasted_model_ns=result.wasted_model_ns, abort_reason=result.abort_reason)
            return AgentExecution(result, tuple(transaction.trace.events))
        finally:
            release_grant(grant, active_intent)
            self._workspace.release_task(task.op_id)
