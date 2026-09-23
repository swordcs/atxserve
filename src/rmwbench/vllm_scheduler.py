from __future__ import annotations
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
import json
from math import isfinite
from time import monotonic
from typing import Any
from vllm.logger import init_logger
from vllm.v1.core.sched.output import SchedulerOutput
from vllm.v1.core.sched.request_queue import RequestQueue, SchedulingPolicy
from vllm.v1.core.sched.scheduler import Scheduler
from vllm.v1.request import Request, RequestStatus
from .models import access_sets_conflict
from .vllm_control import released_grant_id
logger = init_logger('vllm.txserve')

@dataclass(frozen=True, slots=True)
class _AccessIntent:
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    phase: str = 'one_shot'
    grant_id: str | None = None
    lease_seconds: float = 300.0

@dataclass(frozen=True, slots=True)
class _Lease:
    intent: _AccessIntent
    deadline: float
_TX_PHASES = frozenset({'one_shot', 'admit', 'execute'})
_MAX_LEASE_SECONDS = 3600.0

def _string_list(value: Any) -> tuple[str, ...] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, list) or any((not isinstance(item, str) for item in value)):
        return None
    return tuple(value)

def request_access_intent(request: Request) -> _AccessIntent | None:
    sampling_params = getattr(request, 'sampling_params', None)
    extra_args = getattr(sampling_params, 'extra_args', None)
    if not isinstance(extra_args, dict):
        return None
    raw_reads = extra_args.get('tx_read_blocks')
    raw_writes = extra_args.get('tx_write_blocks')
    if raw_reads is None and raw_writes is None:
        return None
    reads = _string_list(raw_reads)
    writes = _string_list(raw_writes)
    if reads is None or writes is None:
        return None
    phase = extra_args.get('tx_phase', 'one_shot')
    if not isinstance(phase, str) or phase not in _TX_PHASES:
        return None
    grant_id = extra_args.get('tx_grant_id')
    if phase != 'one_shot' and (not isinstance(grant_id, str) or not grant_id):
        return None
    raw_lease_seconds = extra_args.get('tx_lease_seconds', 300.0)
    if isinstance(raw_lease_seconds, bool) or not isinstance(raw_lease_seconds, (int, float)) or (not isfinite(raw_lease_seconds)) or (raw_lease_seconds <= 0):
        return None
    lease_seconds = min(float(raw_lease_seconds), _MAX_LEASE_SECONDS)
    return _AccessIntent(reads, writes, str(phase), grant_id, lease_seconds)

class _PolicyRequestQueue(RequestQueue):

    def __init__(self, delegate: RequestQueue, select: Callable[[tuple[Request, ...]], Request | None]) -> None:
        self._delegate = delegate
        self._select = select

    def _selected(self) -> Request | None:
        return self._select(tuple(self._delegate))

    def add_request(self, request: Request) -> None:
        self._delegate.add_request(request)

    def pop_request(self) -> Request:
        request = self._selected()
        if request is None:
            raise IndexError('pop from a queue with no selected request')
        self._delegate.remove_request(request)
        return request

    def peek_request(self) -> Request:
        request = self._selected()
        if request is None:
            raise IndexError('peek from a queue with no selected request')
        return request

    def prepend_request(self, request: Request) -> None:
        self._delegate.prepend_request(request)

    def prepend_requests(self, requests: RequestQueue) -> None:
        self._delegate.prepend_requests(requests)

    def remove_request(self, request: Request) -> None:
        self._delegate.remove_request(request)

    def remove_requests(self, requests: Iterable[Request]) -> None:
        self._delegate.remove_requests(requests)

    def __bool__(self) -> bool:
        return self._selected() is not None

    def __len__(self) -> int:
        return len(self._delegate)

    def __iter__(self) -> Iterator[Request]:
        return iter(self._delegate)

class _TxServeScheduler(Scheduler):
    graph_aware = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.policy != SchedulingPolicy.FCFS:
            raise ValueError('TxServe schedulers support only FCFS scheduling')
        self._tx_gate_enabled = False
        self._tx_leases: dict[str, _Lease] = {}
        self.waiting = _PolicyRequestQueue(self.waiting, self._select_request)
        self.skipped_waiting = _PolicyRequestQueue(self.skipped_waiting, self._select_request)
        logger.info('TXSERVE_SCHEDULER_ENABLED policy=%s', 'conflict_graph_min_degree' if self.graph_aware else 'strict_fifo')

    @staticmethod
    def _conflicts(left: _AccessIntent, right: _AccessIntent) -> bool:
        return access_sets_conflict(left.reads, left.writes, right.reads, right.writes)

    def _prune_expired_leases(self) -> None:
        now = monotonic()
        expired = [grant_id for grant_id, lease in self._tx_leases.items() if lease.deadline <= now]
        for grant_id in expired:
            del self._tx_leases[grant_id]
            logger.warning('TXSERVE_GRANT_EXPIRED grant_id=%s', grant_id)

    def _is_admissible(self, request: Request) -> bool:
        if not self._tx_gate_enabled:
            return True
        candidate = request_access_intent(request)
        if candidate is None:
            logger.info_once('TXSERVE_UNTAGGED request metadata is absent or malformed')
            return True
        if candidate.phase == 'admit' and candidate.grant_id in self._tx_leases:
            return False
        if candidate.phase == 'execute' and candidate.grant_id not in self._tx_leases:
            logger.warning_once('TXSERVE_EXECUTE_WITHOUT_GRANT')
            return False
        for grant_id, lease in self._tx_leases.items():
            if candidate.grant_id == grant_id:
                continue
            if self._conflicts(candidate, lease.intent):
                logger.info_once('TXSERVE_CONFLICT_EXCLUSION_ACTIVE')
                return False
        for active in self.running:
            active_intent = request_access_intent(active)
            if active_intent is None:
                continue
            if candidate.grant_id is not None and candidate.grant_id == active_intent.grant_id:
                continue
            if self._conflicts(candidate, active_intent):
                logger.info_once('TXSERVE_CONFLICT_EXCLUSION_ACTIVE')
                return False
        return True

    def _select_request(self, waiting: tuple[Request, ...]) -> Request | None:
        if not waiting:
            return None
        for request in waiting:
            intent = request_access_intent(request)
            if intent is not None and intent.phase == 'execute' and self._is_admissible(request):
                return request
        head = waiting[0]
        head_intent = request_access_intent(head)
        if head_intent is None or head_intent.phase not in {'admit', 'one_shot'}:
            return head if self._is_admissible(head) else None
        if not self.graph_aware:
            return head if self._is_admissible(head) else None
        candidates: list[tuple[int, Request, _AccessIntent]] = []
        for position, request in enumerate(waiting):
            intent = request_access_intent(request)
            if intent is None or intent.phase != head_intent.phase:
                break
            if self._is_admissible(request):
                candidates.append((position, request, intent))
        if not candidates:
            return None
        scored: list[tuple[int, int, Request]] = []
        for position, request, intent in candidates:
            degree = sum((self._conflicts(intent, other_intent) for _, other, other_intent in candidates if other is not request))
            scored.append((degree, position, request))
        _, _, selected = min(scored, key=lambda item: (item[0], item[1]))
        if selected is not head:
            logger.info_once('TXSERVE_CONFLICT_GRAPH_ACTIVE')
        return selected

    def _record_control_transitions(self, scheduler_output: SchedulerOutput) -> None:
        now = monotonic()
        for data in scheduler_output.scheduled_new_reqs:
            request = self.requests.get(data.req_id)
            if request is None:
                continue
            intent = request_access_intent(request)
            if intent is None or intent.grant_id is None:
                continue
            if intent.phase == 'admit':
                self._tx_leases[intent.grant_id] = _Lease(intent, now + intent.lease_seconds)
                logger.debug('TXSERVE_GRANT_ACQUIRED grant_id=%s lease_seconds=%.1f', intent.grant_id, intent.lease_seconds)
            elif intent.phase == 'execute':
                lease = self._tx_leases.get(intent.grant_id)
                if lease is not None:
                    self._tx_leases[intent.grant_id] = _Lease(lease.intent, now + intent.lease_seconds)

    def schedule(self) -> SchedulerOutput:
        self._prune_expired_leases()
        self._tx_gate_enabled = True
        try:
            scheduler_output = super().schedule()
            self._record_control_transitions(scheduler_output)
            return scheduler_output
        finally:
            self._tx_gate_enabled = False

    def finish_requests(self, request_ids: str | Iterable[str] | None, finished_status: RequestStatus) -> list[tuple[str, int]]:
        if request_ids is None or finished_status != RequestStatus.FINISHED_ABORTED:
            return super().finish_requests(request_ids, finished_status)
        ids = (request_ids,) if isinstance(request_ids, str) else tuple(request_ids)
        ordinary_ids: list[str] = []
        for request_id in ids:
            grant_id = released_grant_id(request_id)
            if grant_id is None:
                ordinary_ids.append(request_id)
                continue
            if self._tx_leases.pop(grant_id, None) is not None:
                logger.debug('TXSERVE_GRANT_RELEASED grant_id=%s', grant_id)
        if not ordinary_ids:
            return []
        return super().finish_requests(ordinary_ids, finished_status)

    def _free_request(self, request: Request, delay_free_blocks: bool=False) -> dict[str, Any] | None:
        intent = request_access_intent(request)
        if intent is not None and intent.phase == 'admit' and (intent.grant_id is not None) and (request.status == RequestStatus.FINISHED_ABORTED):
            self._tx_leases.pop(intent.grant_id, None)
        return super()._free_request(request, delay_free_blocks)

class ConflictGraphTxServeScheduler(_TxServeScheduler):
    graph_aware = True

class StrictFIFOTxServeScheduler(_TxServeScheduler):
    pass
