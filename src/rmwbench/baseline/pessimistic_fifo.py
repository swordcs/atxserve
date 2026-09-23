from __future__ import annotations
from collections import deque
from dataclasses import dataclass
from threading import Condition
from time import perf_counter_ns
from typing import Any
from ..models import AdmissionGrant, AgentTask, ControlRequestResult, IntentPrediction, PatchDecision, ReadResult, TargetDecision, access_sets_conflict

@dataclass(frozen=True, slots=True)
class _Waiter:
    grant_id: str
    intent: IntentPrediction

class FIFOLockManager:

    def __init__(self) -> None:
        self._condition = Condition()
        self._waiting: deque[_Waiter] = deque()
        self._active: dict[str, IntentPrediction] = {}
        self._next_grant = 0

    @staticmethod
    def _conflicts(left: IntentPrediction, right: IntentPrediction) -> bool:
        return access_sets_conflict(left.read_blocks, left.write_blocks, right.read_blocks, right.write_blocks)

    def acquire(self, task: AgentTask, intent: IntentPrediction) -> AdmissionGrant:
        arrived_ns = perf_counter_ns()
        with self._condition:
            self._next_grant += 1
            waiter = _Waiter(f'pessimistic-fifo-{self._next_grant}-{task.op_id}', intent)
            self._waiting.append(waiter)
            while self._waiting[0] is not waiter or any((self._conflicts(intent, active) for active in self._active.values())):
                self._condition.wait()
            self._waiting.popleft()
            self._active[waiter.grant_id] = intent
            self._condition.notify_all()
        return AdmissionGrant(waiter.grant_id, ControlRequestResult(0, 0, 0), wait_ns=perf_counter_ns() - arrived_ns)

    def release(self, grant_id: str) -> None:
        with self._condition:
            if grant_id not in self._active:
                raise ValueError(f'unknown pessimistic FIFO grant {grant_id}')
            del self._active[grant_id]
            self._condition.notify_all()

class PessimisticFIFOReasoner:

    def __init__(self, reasoner: Any) -> None:
        self._reasoner = reasoner
        self._locks = FIFOLockManager()

    def plan_target(self, task: AgentTask, routing_index: str) -> TargetDecision:
        return self._reasoner.plan_target(task, routing_index)

    def acquire(self, task: AgentTask, intent: IntentPrediction) -> AdmissionGrant:
        return self._locks.acquire(task, intent)

    def make_patch(self, task: AgentTask, evidence: list[ReadResult], intent: IntentPrediction, grant: AdmissionGrant | None=None) -> PatchDecision:
        return self._reasoner.make_patch(task, evidence, intent, None)

    def release(self, task: AgentTask, intent: IntentPrediction, grant: AdmissionGrant) -> ControlRequestResult:
        self._locks.release(grant.grant_id)
        return ControlRequestResult(0, 0, 0)

    def commit(self, task: AgentTask, intent: IntentPrediction, grant: AdmissionGrant) -> ControlRequestResult:
        return ControlRequestResult(0, 0, 0)
