from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from math import isfinite
from random import Random
from threading import Condition, Event, RLock
from time import monotonic, perf_counter_ns, thread_time_ns
from typing import Callable, Iterable, Mapping
from uuid import uuid4


class ProtocolError(RuntimeError):
    def __init__(self, reason: str, resource: str = "", permission: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.resource = resource
        self.permission = permission


@dataclass(frozen=True)
class Declaration:
    reads: frozenset[str] = frozenset()
    writes: frozenset[str] = frozenset()

    def __post_init__(self):
        object.__setattr__(self, "reads", frozenset(self.reads))
        object.__setattr__(self, "writes", frozenset(self.writes))
        if any(not isinstance(key, str) or not key for key in self.reads | self.writes):
            raise ValueError("Resource identifiers must be nonempty strings")

    def conflicts(self, other: Declaration) -> bool:
        return bool(self.writes & (other.reads | other.writes) or other.writes & self.reads)

    def covers(self, other: Declaration) -> bool:
        return other.reads <= self.reads and other.writes <= self.writes

    def observe(self, observations: Iterable[Observation]) -> Declaration:
        return Declaration(self.reads | {item.resource for item in observations}, self.writes)


@dataclass(frozen=True)
class Observation:
    resource: str
    version: int
    content: str | None


@dataclass(frozen=True)
class Grant:
    grant_id: str
    owner: str
    declaration: Declaration
    graph_scope: Declaration
    deadline: float
    wait_ns: int
    overtakes: int
    reserved: bool


@dataclass
class _Waiter:
    grant_id: str
    owner: str
    declaration: Declaration
    graph_scope: Declaration
    arrival: int
    enqueued: float
    lease_seconds: float
    overtakes: int = 0
    grant: Grant | None = None


class AdmissionManager:
    def __init__(self, capacity: int = 8, selection: str = "minimum_degree",
                 reservation: int | None = 8, collection_ms: float = 1.0, seed: int = 7,
                 *, resolve_hints: bool = True):
        if capacity < 1 or selection not in {"minimum_degree", "fifo_compatible", "fifo_hol", "random"}:
            raise ValueError("Invalid admission configuration")
        if reservation is not None and reservation < 0:
            raise ValueError("Reservation must be nonnegative or None")
        if not isfinite(collection_ms) or collection_ms < 0:
            raise ValueError("Collection window must be finite and nonnegative")
        self.capacity = capacity
        self.selection = selection
        self.reservation = reservation
        self.collection_seconds = collection_ms / 1000
        self.resolve_hints = resolve_hints
        self.lock = RLock()
        self.changed = Condition(self.lock)
        self.active: dict[str, Grant] = {}
        self.waiting: list[_Waiter] = []
        self._arrival = 0
        self._rng = Random(seed)
        self._started = monotonic()
        self._integrated_at = self._started
        self._slot_seconds = 0.0
        self._decision_cpu_ns: list[int] = []
        self._widths: list[int] = []

    def _integrate(self, now: float):
        self._slot_seconds += sum(max(0.0, min(now, grant.deadline) - self._integrated_at)
                                  for grant in self.active.values())
        self._integrated_at = now

    def _prune(self):
        now = monotonic()
        self._integrate(now)
        for key in [key for key, grant in self.active.items() if grant.deadline <= now]:
            del self.active[key]

    def _schedule(self):
        started = thread_time_ns()
        active_before = set(self.active)
        self._prune()
        width = 0
        while self.waiting and len(self.active) < self.capacity:
            feasible = [item for item in self.waiting if all(
                not item.graph_scope.conflicts(grant.graph_scope) for grant in self.active.values())]
            protected = [item for item in self.waiting
                         if self.reservation is not None and item.overtakes >= self.reservation]
            reserved = min(protected, key=lambda item: item.arrival, default=None)
            forced = reserved is not None and reserved in feasible
            if forced:
                selected = reserved
            else:
                if reserved is not None:
                    feasible = [item for item in feasible if not item.graph_scope.conflicts(reserved.graph_scope)]
                if self.selection == "fifo_hol":
                    head = self.waiting[0]
                    feasible = [head] if head in feasible else []
                if not feasible:
                    break
                if self.selection == "minimum_degree":
                    selected = min(feasible, key=lambda item: (
                        sum(item.graph_scope.conflicts(other.graph_scope)
                            for other in feasible if item is not other), item.arrival))
                elif self.selection == "random":
                    selected = self._rng.choice(feasible)
                else:
                    selected = min(feasible, key=lambda item: item.arrival)
            for item in self.waiting:
                if item.arrival < selected.arrival and item.graph_scope.conflicts(selected.graph_scope):
                    item.overtakes += 1
            self.waiting.remove(selected)
            now = monotonic()
            self._integrate(now)
            selected.grant = Grant(selected.grant_id, selected.owner, selected.declaration,
                                   selected.graph_scope, now + selected.lease_seconds,
                                   int((now - selected.enqueued) * 1e9), selected.overtakes, forced)
            self.active[selected.grant_id] = selected.grant
            width += 1
        self._decision_cpu_ns.append(thread_time_ns() - started)
        if width:
            self._widths.append(width)
        if width or set(self.active) != active_before:
            self.changed.notify_all()

    def acquire(self, owner: str, declaration: Declaration, lease_seconds: float = 300,
                graph_scope: Declaration | None = None, timeout: float | None = None,
                cancel: Event | None = None, grant_id: str | None = None) -> Grant:
        if not isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("Lease duration must be finite and positive")
        if timeout is not None and (not isfinite(timeout) or timeout <= 0):
            raise ValueError("Admission timeout must be finite and positive")
        with self.changed:
            key = grant_id or uuid4().hex
            if key in self.active or any(item.grant_id == key for item in self.waiting):
                raise ValueError("Duplicate grant identifier")
            now = monotonic()
            graph = declaration if self.resolve_hints or graph_scope is None else graph_scope
            item = _Waiter(key, owner, declaration, graph,
                           self._arrival, now, lease_seconds)
            self._arrival += 1
            self.waiting.append(item)
            try:
                while item.grant is None:
                    now = monotonic()
                    if cancel is not None and cancel.is_set():
                        raise ProtocolError("CANCELLED")
                    if timeout is not None and now >= item.enqueued + timeout:
                        raise ProtocolError("ADMISSION_TIMEOUT")
                    ready_at = min(waiter.enqueued for waiter in self.waiting) + self.collection_seconds
                    if now >= ready_at:
                        self._schedule()
                        if item.grant is not None:
                            break
                    wake_at = min([now + 0.1, *[g.deadline for g in self.active.values()],
                                   *([ready_at] if ready_at > now else []),
                                   *([item.enqueued + timeout] if timeout is not None else [])])
                    self.changed.wait(max(0.0001, wake_at - now))
                if cancel is not None and cancel.is_set():
                    raise ProtocolError("CANCELLED")
                return item.grant
            except BaseException:
                if item in self.waiting:
                    self.waiting.remove(item)
                self._prune()
                self.active.pop(key, None)
                self._schedule()
                raise

    def require(self, grant_id: str, owner: str, declaration: Declaration) -> Grant:
        grant = self.active.get(grant_id)
        if grant is None or grant.deadline <= monotonic() or grant.owner != owner:
            raise ProtocolError("INVALID_GRANT")
        if not grant.declaration.covers(declaration):
            raise ProtocolError("GRANT_SCOPE_MISMATCH")
        return grant

    @contextmanager
    def guard(self, grant_id: str | None, owner: str, declaration: Declaration):
        with self.changed:
            if grant_id is not None:
                self.require(grant_id, owner, declaration)
            yield

    def release(self, grant_id: str, owner: str) -> bool:
        with self.changed:
            self._prune()
            grant = self.active.get(grant_id)
            if grant is not None and grant.owner != owner:
                raise ProtocolError("FOREIGN_GRANT")
            released = self.active.pop(grant_id, None) is not None
            self._schedule()
            return released

    def measurements(self) -> dict:
        with self.changed:
            self._prune()
            elapsed = monotonic() - self._started
            return {"capacity": self.capacity, "selection": self.selection,
                    "reservation": self.reservation, "collection_ms": self.collection_seconds * 1000,
                    "resolve_hints": self.resolve_hints,
                    "active": len(self.active), "waiting": len(self.waiting),
                    "slot_occupancy": self._slot_seconds / (elapsed * self.capacity) if elapsed else 0,
                    "decision_cpu_ns": list(self._decision_cpu_ns), "admission_widths": list(self._widths),
                    "decision_cpu_mean_ns": sum(self._decision_cpu_ns) / len(self._decision_cpu_ns)
                    if self._decision_cpu_ns else 0,
                    "admission_width_mean": sum(self._widths) / len(self._widths) if self._widths else 0}


@dataclass
class _Resource:
    content: str | None
    version: int = 0
    lock: RLock = field(default_factory=RLock)


@dataclass
class _Attempt:
    owner: str
    observations: list[Observation] = field(default_factory=list)
    declaration: Declaration | None = None
    grant_id: str | None = None
    snapshot: dict[str, Observation] = field(default_factory=dict)
    reads: list[Observation] = field(default_factory=list)
    bound: bool = False
    published: bool = False
    poisoned: bool = False


class VersionedWorkspace:
    def __init__(self, blocks: Mapping[str, str], admission: AdmissionManager,
                 *, write_permissions: Mapping[str, Iterable[str]] | None = None):
        self.admission = admission
        self._resources = {key: _Resource(value) for key, value in blocks.items()}
        self._attempts: dict[str, _Attempt] = {}
        self._directories: dict[str, set[str]] = {}
        self._write_permissions = (None if write_permissions is None else
                                   {owner: frozenset(resources) for owner, resources in write_permissions.items()})

    @contextmanager
    def _locked(self, resources: Iterable[str]):
        with ExitStack() as stack:
            for resource in sorted(set(resources)):
                block = self._resources.setdefault(resource, _Resource(None))
                stack.enter_context(block.lock)
            yield

    def begin(self, owner: str) -> str:
        with self.admission.lock:
            attempt_id = uuid4().hex
            self._attempts[attempt_id] = _Attempt(owner)
            return attempt_id

    def _attempt(self, attempt_id: str, owner: str) -> _Attempt:
        attempt = self._attempts.get(attempt_id)
        if attempt is None or attempt.owner != owner:
            raise ProtocolError("INVALID_ATTEMPT")
        if attempt.poisoned:
            raise ProtocolError("ATTEMPT_TERMINATED")
        return attempt

    def _observe(self, resource: str) -> Observation:
        block = self._resources.setdefault(resource, _Resource(None))
        return Observation(resource, block.version, block.content)

    def plan_read(self, attempt_id: str, owner: str, resource: str) -> Observation:
        with self.admission.lock:
            attempt = self._attempt(attempt_id, owner)
            if attempt.bound or attempt.declaration is not None:
                raise ProtocolError("PLANNING_CLOSED")
            with self._locked([resource]):
                observation = self._observe(resource)
                attempt.observations.append(observation)
                return observation

    def register_directory(self, directory: str, resources: Iterable[str]):
        with self.admission.lock:
            if directory in self._resources:
                raise ValueError("Directory identifier already exists")
            entries = set(resources)
            self._directories[directory] = entries
            self._resources[directory] = _Resource("\n".join(sorted(entries)))

    def search(self, attempt_id: str, owner: str, directory: str, text: str) -> list[Observation]:
        listing = self.plan_read(attempt_id, owner, directory)
        matches = []
        for resource in (listing.content or "").splitlines():
            observation = self.plan_read(attempt_id, owner, resource)
            if observation.content is not None and text in observation.content:
                matches.append(observation)
        return matches

    def finalize(self, attempt_id: str, owner: str, declaration: Declaration) -> Declaration:
        with self.admission.lock:
            attempt = self._attempt(attempt_id, owner)
            if attempt.declaration is not None:
                raise ProtocolError("PLANNING_CLOSED")
            attempt.declaration = declaration.observe(attempt.observations)
            return attempt.declaration

    def observations(self, attempt_id: str, owner: str) -> tuple[Observation, ...]:
        with self.admission.lock:
            return tuple(self._attempt(attempt_id, owner).observations)

    def _current(self, observations: Iterable[Observation]) -> bool:
        return all(self._observe(item.resource).version == item.version for item in observations)

    def _deny(self, attempt: _Attempt, resource: str, permission: str):
        attempt.poisoned = True
        raise ProtocolError("UNDECLARED_ACCESS", resource, permission)

    def bind(self, attempt_id: str, owner: str, inputs: Iterable[str], grant_id: str | None,
             validate_plan: bool = True, require_grant: bool = True) -> dict[str, Observation]:
        with self.admission.lock:
            attempt = self._attempt(attempt_id, owner)
            declaration = attempt.declaration
            if declaration is None or attempt.bound:
                raise ProtocolError("INVALID_BIND_STATE")
            if require_grant and grant_id is None:
                raise ProtocolError("INVALID_GRANT")
            with self.admission.guard(grant_id, owner, declaration), self._locked(declaration.reads | declaration.writes):
                if validate_plan and not self._current(attempt.observations):
                    attempt.poisoned = True
                    raise ProtocolError("STALE_PLAN")
                selected = tuple(dict.fromkeys(inputs))
                for resource in selected:
                    if resource not in declaration.reads:
                        self._deny(attempt, resource, "read")
                keys = sorted(declaration.reads | declaration.writes)
                snapshot = {key: self._observe(key) for key in keys}
                attempt.snapshot = snapshot
                attempt.grant_id = grant_id
                attempt.bound = True
                attempt.reads.extend(snapshot[key] for key in selected)
                return {key: snapshot[key] for key in selected}

    def read(self, attempt_id: str, owner: str, resource: str) -> Observation:
        with self.admission.lock:
            attempt = self._attempt(attempt_id, owner)
            if not attempt.bound or attempt.published:
                raise ProtocolError("INVALID_EXECUTION_STATE")
            if resource not in attempt.declaration.reads:
                self._deny(attempt, resource, "read")
            with self.admission.guard(attempt.grant_id, owner, attempt.declaration), self._locked([resource]):
                observation = attempt.snapshot[resource]
                attempt.reads.append(observation)
                return observation

    def check_write(self, attempt_id: str, owner: str, resource: str):
        with self.admission.lock:
            attempt = self._attempt(attempt_id, owner)
            if not attempt.bound or attempt.published:
                raise ProtocolError("INVALID_EXECUTION_STATE")
            if self._write_permissions is not None and resource not in self._write_permissions.get(owner, ()):
                attempt.poisoned = True
                raise ProtocolError("WRITE_FORBIDDEN", resource, "write")
            if resource not in attempt.declaration.writes:
                self._deny(attempt, resource, "write")

    def publish(self, attempt_id: str, owner: str, patches: Mapping[str, str | None],
                check: Callable[[Mapping[str, Observation], Mapping[str, str | None]], None] | None = None):
        with self.admission.lock:
            attempt = self._attempt(attempt_id, owner)
            if not attempt.bound or attempt.published or not patches:
                raise ProtocolError("INVALID_PUBLICATION_STATE")
            with self.admission.guard(attempt.grant_id, owner, attempt.declaration), self._locked(attempt.declaration.reads | attempt.declaration.writes):
                for resource, content in patches.items():
                    self.check_write(attempt_id, owner, resource)
                    if resource in self._directories:
                        raise ProtocolError("DIRECTORY_PATCH_FORBIDDEN")
                    if content is not None and not isinstance(content, str):
                        raise ProtocolError("INVALID_PATCH")
                    if (self._observe(resource).content is not None) != (content is not None):
                        for directory, entries in self._directories.items():
                            prefix = directory.removeprefix("directory:").rstrip("/")
                            if resource in entries or not prefix or resource.startswith(prefix + "/"):
                                if directory not in attempt.declaration.writes:
                                    self._deny(attempt, directory, "write")
                if not self._current([*attempt.observations, *attempt.reads]):
                    attempt.poisoned = True
                    raise ProtocolError("STALE_READ")
                if check is not None:
                    check(attempt.snapshot, patches)
                if attempt.grant_id is not None:
                    self.admission.require(attempt.grant_id, owner, attempt.declaration)
                for resource, content in patches.items():
                    self._mutate(resource, content)
                attempt.published = True
                return {resource: self._observe(resource).version for resource in patches}

    def _mutate(self, resource: str, content: str | None):
        block = self._resources.setdefault(resource, _Resource(None))
        existed = block.content is not None
        block.content = content
        block.version += 1
        if existed != (content is not None):
            for directory, entries in self._directories.items():
                prefix = directory.removeprefix("directory:").rstrip("/")
                if resource in entries or not prefix or resource.startswith(prefix + "/"):
                    if content is None:
                        entries.discard(resource)
                    else:
                        entries.add(resource)
                    listing = self._resources[directory]
                    listing.content = "\n".join(sorted(entries))
                    listing.version += 1

    def external_write(self, resource: str, content: str | None):
        with self.admission.lock:
            if resource in self._directories:
                raise ValueError("Directory metadata is managed by the workspace")
            self._mutate(resource, content)

    def finish(self, attempt_id: str, owner: str):
        with self.admission.lock:
            attempt = self._attempts.get(attempt_id)
            if attempt is None:
                return
            if attempt.owner != owner:
                raise ProtocolError("FOREIGN_ATTEMPT")
            if attempt.grant_id is not None:
                self.admission.release(attempt.grant_id, owner)
            del self._attempts[attempt_id]

    def snapshot(self) -> dict[str, Observation]:
        with self.admission.lock:
            return {key: self._observe(key) for key in sorted(self._resources)}
