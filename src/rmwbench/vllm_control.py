from __future__ import annotations
import asyncio
import os
from dataclasses import dataclass
from math import isfinite
from random import Random
from time import monotonic, thread_time_ns
from typing import Any
import re
from starlette.responses import JSONResponse
_RELEASE_PREFIX = '__txserve_release__:'
_GRANT_ID = re.compile('[A-Za-z0-9_.:-]{1,256}')
_MAX_LEASE_SECONDS = 3600.0

@dataclass(frozen=True, slots=True)
class _ControlIntent:
    grant_id: str
    reads: frozenset[str]
    writes: frozenset[str]
    lease_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(self, 'reads', frozenset(self.reads))
        object.__setattr__(self, 'writes', frozenset(self.writes))

@dataclass(frozen=True, slots=True)
class _AdmissionDecision:
    bypasses: int
    wait_ns: int
    fairness_forced: bool

@dataclass(slots=True)
class _PendingAdmission:
    intent: _ControlIntent
    future: asyncio.Future[Any]
    position: int
    enqueued_at: float = 0.0
    bypasses: int = 0

class _APILockManager:

    def __init__(self, max_active: int, max_bypasses: int | None=None, selection: str='minimum_degree') -> None:
        if max_active <= 0:
            raise ValueError('max_active must be positive')
        if max_bypasses is not None and max_bypasses < 0:
            raise ValueError('max_bypasses cannot be negative')
        if selection not in {'minimum_degree', 'first_fit', 'strict_fifo', 'random'}:
            raise ValueError('invalid selection policy')
        self.selection = selection
        self._rng = Random(int(os.environ.get('TXSERVE_SELECTION_SEED', '7')))
        self._max_active = max_active
        self._max_bypasses = max_bypasses
        self._lock = asyncio.Lock()
        self._leases: dict[str, tuple[_ControlIntent, float]] = {}
        self._waiting: list[_PendingAdmission] = []
        self._next_position = 0
        self._measurement_start = monotonic()
        self._occupancy_at = self._measurement_start
        self._slot_seconds = 0.0
        self._decision_cpu_ns: list[int] = []
        self._admission_widths: list[int] = []

    def _integrate_occupancy(self) -> None:
        now = monotonic()
        self._slot_seconds += (now - self._occupancy_at) * len(self._leases)
        self._occupancy_at = now

    def measurements(self) -> dict[str, Any]:
        self._integrate_occupancy()
        elapsed = monotonic() - self._measurement_start
        times = sorted(self._decision_cpu_ns)
        position = (len(times) - 1) * 0.95
        index = int(max(0, position))
        p95 = times[index] + (times[min(index + 1, len(times) - 1)] - times[index]) * (position - index) if times else 0.0
        return {'selection': self.selection, 'max_bypasses': self.max_bypasses, 'capacity': self._max_active, 'collection_window_ms': 1, 'elapsed_seconds': elapsed, 'active': len(self._leases), 'waiting': len(self._waiting), 'slot_seconds': self._slot_seconds, 'slot_occupancy_pct': 100 * self._slot_seconds / (elapsed * self._max_active) if elapsed else 0, 'admission_width_mean': sum(self._admission_widths) / len(self._admission_widths) if self._admission_widths else 0, 'nonempty_decisions': len(self._admission_widths), 'decision_cpu_p95_ms': p95 / 1000000.0, 'decision_cpu_ns': self._decision_cpu_ns, 'admission_widths': self._admission_widths}

    def _choose(self, candidates: list[_PendingAdmission]) -> _PendingAdmission:
        if self.selection == 'random':
            return self._rng.choice(candidates)
        if self.selection in {'first_fit', 'strict_fifo'}:
            return min(candidates, key=lambda pending: pending.position)
        return min(candidates, key=lambda pending: (sum((self._conflicts(pending.intent, other.intent) for other in candidates if other is not pending)), pending.position))

    @property
    def max_bypasses(self) -> int | None:
        return self._max_bypasses

    @staticmethod
    def _conflicts(left: _ControlIntent, right: _ControlIntent) -> bool:
        return not left.writes.isdisjoint(right.reads) or not left.writes.isdisjoint(right.writes) or (not right.writes.isdisjoint(left.reads))

    def _prune(self) -> None:
        now = monotonic()
        for grant_id in [grant_id for grant_id, (_, deadline) in self._leases.items() if deadline <= now]:
            del self._leases[grant_id]

    def _grant_waiters(self) -> None:
        self._integrate_occupancy()
        count_before = self._next_position - len(self._waiting)
        started = thread_time_ns()
        self._grant_waiters_inner()
        self._decision_cpu_ns.append(thread_time_ns() - started)
        width = self._next_position - len(self._waiting) - count_before
        if width > 0:
            self._admission_widths.append(width)

    def _grant_waiters_inner(self) -> None:
        self._prune()
        while self._waiting and len(self._leases) < self._max_active:
            active = [intent for intent, _ in self._leases.values()]
            candidates = [pending for pending in self._waiting if not pending.future.cancelled() and all((not self._conflicts(pending.intent, intent) for intent in active))]
            if self.selection == 'strict_fifo':
                head = min(self._waiting, key=lambda pending: pending.position)
                candidates = [head] if head in candidates else []
            if not candidates:
                break
            protected = [pending for pending in self._waiting if not pending.future.cancelled() if pending.bypasses >= self._max_bypasses] if self._max_bypasses is not None else []
            fairness_forced = False
            if protected:
                reservation = min(protected, key=lambda pending: pending.position)
                if reservation in candidates:
                    selected = reservation
                    fairness_forced = True
                else:
                    candidates = [pending for pending in candidates if not self._conflicts(pending.intent, reservation.intent)]
                    if not candidates:
                        break
                    selected = self._choose(candidates)
            else:
                selected = self._choose(candidates)
            for pending in self._waiting:
                if pending is not selected and pending.position < selected.position and self._conflicts(pending.intent, selected.intent):
                    pending.bypasses += 1
            self._waiting.remove(selected)
            intent = selected.intent
            granted_at = monotonic()
            self._leases[intent.grant_id] = (intent, granted_at + intent.lease_seconds)
            asyncio.get_running_loop().call_later(intent.lease_seconds, self._schedule_expiry, intent.grant_id)
            if not selected.future.done():
                selected.future.set_result(_AdmissionDecision(selected.bypasses, int(max(0.0, granted_at - selected.enqueued_at) * 1000000000.0), fairness_forced))

    def _schedule_expiry(self, grant_id: str) -> None:
        asyncio.create_task(self._expire(grant_id))

    async def _expire(self, grant_id: str) -> None:
        async with self._lock:
            lease = self._leases.get(grant_id)
            if lease is not None and lease[1] <= monotonic():
                del self._leases[grant_id]
                self._grant_waiters()

    async def acquire(self, intent: _ControlIntent) -> _AdmissionDecision:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        async with self._lock:
            if intent.grant_id in self._leases or any((pending.intent.grant_id == intent.grant_id for pending in self._waiting)):
                raise ValueError('duplicate grant_id')
            pending = _PendingAdmission(intent, future, self._next_position, monotonic())
            self._next_position += 1
            self._waiting.append(pending)
        try:
            await asyncio.sleep(0.001)
            async with self._lock:
                self._grant_waiters()
            return await future
        except BaseException:
            async with self._lock:
                if pending in self._waiting:
                    self._waiting.remove(pending)
                elif future.done() and (not future.cancelled()):
                    self._leases.pop(intent.grant_id, None)
                self._grant_waiters()
            raise

    async def release(self, grant_id: str) -> bool:
        async with self._lock:
            self._integrate_occupancy()
            released = self._leases.pop(grant_id, None) is not None
            self._grant_waiters()
            return released
_api_lock_manager: _APILockManager | None = None

@dataclass(slots=True)
class _SerialLease:
    intent: _ControlIntent
    deadline: float
    predecessors: set[str]
    frozen: bool = False
    commit_waiter: asyncio.Future[None] | None = None

class _SerializationGraphManager:

    def __init__(self, max_active: int) -> None:
        if max_active <= 0:
            raise ValueError('max_active must be positive')
        self._max_active = max_active
        self._lock = asyncio.Lock()
        self._active: dict[str, _SerialLease] = {}
        self._waiting: list[_PendingAdmission] = []
        self._next_position = 0

    @staticmethod
    def _relations(candidate: _ControlIntent, active: _ControlIntent) -> tuple[bool, bool, bool]:
        ww = not candidate.writes.isdisjoint(active.writes)
        active_before_candidate = not active.reads.isdisjoint(candidate.writes)
        candidate_before_active = not candidate.reads.isdisjoint(active.writes)
        hard_conflict = ww or (active_before_candidate and candidate_before_active)
        return (hard_conflict, active_before_candidate, candidate_before_active)

    def _path_exists(self, start: str, target: str) -> bool:
        successors: dict[str, set[str]] = {grant_id: set() for grant_id in self._active}
        for grant_id, lease in self._active.items():
            for predecessor in lease.predecessors:
                successors.setdefault(predecessor, set()).add(grant_id)
        stack = [start]
        seen: set[str] = set()
        while stack:
            node = stack.pop()
            if node == target:
                return True
            if node not in seen:
                seen.add(node)
                stack.extend(successors.get(node, ()))
        return False

    def _admission_edges(self, intent: _ControlIntent) -> tuple[set[str], set[str]] | None:
        predecessors: set[str] = set()
        successors: set[str] = set()
        for grant_id, lease in self._active.items():
            hard, active_before, candidate_before = self._relations(intent, lease.intent)
            if hard:
                return None
            if active_before:
                predecessors.add(grant_id)
            if candidate_before:
                if lease.frozen:
                    return None
                successors.add(grant_id)
        if any((self._path_exists(successor, predecessor) for successor in successors for predecessor in predecessors)):
            return None
        return (predecessors, successors)

    def _drop(self, grant_id: str) -> bool:
        lease = self._active.pop(grant_id, None)
        if lease is None:
            return False
        if lease.commit_waiter is not None and (not lease.commit_waiter.done()):
            lease.commit_waiter.set_exception(RuntimeError('serialization grant released before commit'))
        for other in self._active.values():
            other.predecessors.discard(grant_id)
        return True

    def _wake_commits(self) -> None:
        for lease in self._active.values():
            waiter = lease.commit_waiter
            if waiter is not None and (not waiter.done()) and (not lease.predecessors):
                lease.frozen = True
                waiter.set_result(None)

    def _prune(self) -> None:
        now = monotonic()
        expired = [grant_id for grant_id, lease in self._active.items() if lease.deadline <= now]
        for grant_id in expired:
            self._drop(grant_id)
        if expired:
            self._wake_commits()

    def _grant_waiters(self) -> None:
        self._prune()
        inference_active = sum((lease.commit_waiter is None for lease in self._active.values()))
        while self._waiting and inference_active < self._max_active:
            admissible = [(pending, self._admission_edges(pending.intent)) for pending in self._waiting if not pending.future.cancelled()]
            candidates = [(pending, edges) for pending, edges in admissible if edges is not None]
            if not candidates:
                break
            selected, edges = min(candidates, key=lambda item: (sum((self._relations(item[0].intent, other[0].intent)[0] for other in candidates if other[0] is not item[0])), item[0].position))
            if edges is None:
                raise AssertionError('selected inadmissible transaction')
            self._waiting.remove(selected)
            predecessors, successors = edges
            intent = selected.intent
            self._active[intent.grant_id] = _SerialLease(intent, monotonic() + intent.lease_seconds, predecessors)
            for successor in successors:
                self._active[successor].predecessors.add(intent.grant_id)
            asyncio.get_running_loop().call_later(intent.lease_seconds, self._schedule_expiry, intent.grant_id)
            if not selected.future.done():
                selected.future.set_result(None)
            inference_active += 1

    def _schedule_expiry(self, grant_id: str) -> None:
        asyncio.create_task(self._expire(grant_id))

    async def _expire(self, grant_id: str) -> None:
        async with self._lock:
            lease = self._active.get(grant_id)
            if lease is not None and lease.deadline <= monotonic():
                self._drop(grant_id)
                self._wake_commits()
                self._grant_waiters()

    async def acquire(self, intent: _ControlIntent) -> None:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        async with self._lock:
            if intent.grant_id in self._active or any((pending.intent.grant_id == intent.grant_id for pending in self._waiting)):
                raise ValueError('duplicate grant_id')
            pending = _PendingAdmission(intent, future, self._next_position)
            self._next_position += 1
            self._waiting.append(pending)
        try:
            await asyncio.sleep(0.001)
            async with self._lock:
                self._grant_waiters()
            await future
        except BaseException:
            async with self._lock:
                if pending in self._waiting:
                    self._waiting.remove(pending)
                elif future.done() and (not future.cancelled()):
                    self._drop(intent.grant_id)
                self._wake_commits()
                self._grant_waiters()
            raise

    async def commit(self, grant_id: str) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            lease = self._active.get(grant_id)
            if lease is None:
                raise ValueError('unknown grant_id')
            if lease.frozen:
                return
            if lease.commit_waiter is not None:
                raise ValueError('duplicate commit request')
            waiter = loop.create_future()
            lease.commit_waiter = waiter
            self._wake_commits()
            self._grant_waiters()
        try:
            await waiter
        except BaseException:
            async with self._lock:
                lease = self._active.get(grant_id)
                if lease is not None and lease.commit_waiter is waiter and (not lease.frozen):
                    lease.commit_waiter = None
            raise

    async def release(self, grant_id: str) -> bool:
        async with self._lock:
            released = self._drop(grant_id)
            self._wake_commits()
            self._grant_waiters()
            return released
_serialization_graph_manager: _SerializationGraphManager | None = None

def _configured_max_bypasses() -> int | None:
    raw_value = os.environ.get('TXSERVE_MAX_BYPASSES')
    if raw_value is None or not raw_value.strip():
        return None
    max_bypasses = int(raw_value)
    if max_bypasses < 0:
        raise ValueError('TXSERVE_MAX_BYPASSES cannot be negative')
    return max_bypasses

def _control_intent(payload: object) -> _ControlIntent:
    if not isinstance(payload, dict):
        raise ValueError('request body must be an object')
    grant_id = payload.get('grant_id')
    if not isinstance(grant_id, str) or _GRANT_ID.fullmatch(grant_id) is None:
        raise ValueError('invalid grant_id')

    def resources(name: str) -> tuple[str, ...]:
        value = payload.get(name)
        if not isinstance(value, list) or any((not isinstance(item, str) or not item for item in value)):
            raise ValueError(f'invalid {name}')
        return tuple(dict.fromkeys(value))
    raw_lease = payload.get('lease_seconds')
    if isinstance(raw_lease, bool) or not isinstance(raw_lease, (int, float)) or (not isfinite(raw_lease)) or (raw_lease <= 0):
        raise ValueError('invalid lease_seconds')
    return _ControlIntent(grant_id, resources('read_blocks'), resources('write_blocks'), min(float(raw_lease), _MAX_LEASE_SECONDS))

def release_request_id(grant_id: str) -> str:
    if _GRANT_ID.fullmatch(grant_id) is None:
        raise ValueError('invalid TxServe grant_id')
    return _RELEASE_PREFIX + grant_id

def released_grant_id(request_id: str) -> str | None:
    if not request_id.startswith(_RELEASE_PREFIX):
        return None
    grant_id = request_id.removeprefix(_RELEASE_PREFIX)
    return grant_id if _GRANT_ID.fullmatch(grant_id) is not None else None

async def txserve_control_middleware(request: Any, call_next: Any) -> Any:
    if request.method != 'POST' or request.url.path != '/txserve/release':
        return await call_next(request)
    try:
        payload = await request.json()
        grant_id = payload.get('grant_id') if isinstance(payload, dict) else None
        control_id = release_request_id(grant_id)
    except (TypeError, ValueError):
        return JSONResponse({'error': 'invalid grant_id'}, status_code=400)
    engine_core = request.app.state.engine_client.engine_core
    await engine_core.abort_requests_async([control_id])
    return JSONResponse({'released': grant_id})

async def txserve_lock_manager_middleware(request: Any, call_next: Any) -> Any:
    global _api_lock_manager
    if request.method == 'POST' and request.url.path == '/txserve/experiment' and (os.environ.get('TXSERVE_EXPERIMENT_CONTROL') == '1'):
        payload = await request.json()
        if payload.get('action') == 'measure':
            return JSONResponse(_api_lock_manager.measurements() if _api_lock_manager else {})
        if payload.get('action') != 'configure':
            return JSONResponse({'error': 'unknown experiment action'}, status_code=400)
        if _api_lock_manager and (_api_lock_manager._leases or _api_lock_manager._waiting):
            return JSONResponse({'error': 'cannot reconfigure an active manager'}, status_code=409)
        capacity = int(request.app.state.vllm_config.scheduler_config.max_num_seqs)
        try:
            _api_lock_manager = _APILockManager(capacity, payload.get('max_bypasses'), payload.get('selection', 'minimum_degree'))
        except (ValueError, TypeError) as error:
            return JSONResponse({'error': str(error)}, status_code=400)
        return JSONResponse(_api_lock_manager.measurements())
    if request.method != 'POST' or request.url.path not in {'/txserve/admit', '/txserve/release'}:
        return await call_next(request)
    try:
        payload = await request.json()
        if request.url.path == '/txserve/admit':
            intent = _control_intent(payload)
            if _api_lock_manager is None:
                max_active = int(request.app.state.vllm_config.scheduler_config.max_num_seqs)
                _api_lock_manager = _APILockManager(max_active, _configured_max_bypasses(), os.environ.get('TXSERVE_SELECTION', 'minimum_degree'))
            decision = await _api_lock_manager.acquire(intent)
            return JSONResponse({'granted': intent.grant_id, 'bypasses': decision.bypasses, 'wait_ns': decision.wait_ns, 'fairness_forced': decision.fairness_forced, 'max_bypasses': _api_lock_manager.max_bypasses})
        grant_id = payload.get('grant_id') if isinstance(payload, dict) else None
        if not isinstance(grant_id, str) or _GRANT_ID.fullmatch(grant_id) is None:
            raise ValueError('invalid grant_id')
        released = _api_lock_manager is not None and await _api_lock_manager.release(grant_id)
        return JSONResponse({'released': grant_id, 'known': released})
    except (AttributeError, TypeError, ValueError):
        return JSONResponse({'error': 'invalid TxServe control request'}, status_code=400)

async def txserve_serial_graph_middleware(request: Any, call_next: Any) -> Any:
    global _serialization_graph_manager
    if request.method != 'POST' or request.url.path not in {'/txserve/admit', '/txserve/commit', '/txserve/release'}:
        return await call_next(request)
    try:
        payload = await request.json()
        if request.url.path == '/txserve/admit':
            intent = _control_intent(payload)
            if _serialization_graph_manager is None:
                max_active = int(request.app.state.vllm_config.scheduler_config.max_num_seqs)
                _serialization_graph_manager = _SerializationGraphManager(max_active)
            await _serialization_graph_manager.acquire(intent)
            return JSONResponse({'granted': intent.grant_id})
        grant_id = payload.get('grant_id') if isinstance(payload, dict) else None
        if not isinstance(grant_id, str) or _GRANT_ID.fullmatch(grant_id) is None:
            raise ValueError('invalid grant_id')
        if _serialization_graph_manager is None:
            raise ValueError('lock manager is not initialized')
        if request.url.path == '/txserve/commit':
            await _serialization_graph_manager.commit(grant_id)
            return JSONResponse({'commit_granted': grant_id})
        released = await _serialization_graph_manager.release(grant_id)
        return JSONResponse({'released': grant_id, 'known': released})
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return JSONResponse({'error': 'invalid TxServe serialization request'}, status_code=400)
