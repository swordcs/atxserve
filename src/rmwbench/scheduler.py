from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from itertools import groupby
from threading import Barrier
from time import perf_counter_ns
from .agent import MockAgent, PreparedTransaction, TransactionAgent
from .models import AgentExecution, IntentPrediction, Task, access_sets_conflict
_MODEL_ADMISSION_MODES = frozenset({'vllm_lease', 'vllm_lock_manager', 'vllm_serial_graph', 'pessimistic_fifo'})
_EAGER_BIND_MODES = frozenset({'eager_fifo', 'occ_backoff'})

def run_schedule(tasks: tuple[Task, ...], agent: MockAgent, agent_count: int, scheduler: str) -> list[AgentExecution]:
    executions: list[AgentExecution] = []
    forced_stale = scheduler == 'forced_stale'
    with ThreadPoolExecutor(max_workers=agent_count) as pool:
        for _, grouped in groupby(tasks, key=lambda task: task.wave):
            wave_tasks = list(grouped)
            barrier = Barrier(len(wave_tasks)) if forced_stale and len(wave_tasks) > 1 else None
            futures = [pool.submit(agent.execute, f'agent_{position:03d}', task.agent_view(), barrier, forced_stale) for position, task in enumerate(wave_tasks)]
            executions.extend((future.result() for future in futures))
    return executions

def intents_conflict(left: IntentPrediction, right: IntentPrediction) -> bool:
    return access_sets_conflict(left.read_blocks, left.write_blocks, right.read_blocks, right.write_blocks)

def _select_batch(pending: list[PreparedTransaction], capacity: int, mode: str, aging_factor: float) -> list[PreparedTransaction]:
    if mode != 'txserve':
        return pending[:capacity]
    selected: list[PreparedTransaction] = []
    candidates = list(pending)
    while candidates and len(selected) < capacity:
        transaction = min(candidates, key=lambda candidate: (
            sum(intents_conflict(candidate.intent, other.intent)
                for other in candidates if other is not candidate),
            candidate.ready_ns, candidate.task.op_id))
        selected.append(transaction)
        candidates = [candidate for candidate in candidates
                      if candidate is not transaction
                      and not intents_conflict(candidate.intent, transaction.intent)]
    if not selected:
        raise AssertionError('conflict-aware admission selected no transaction')
    return selected

def run_transaction_schedule(tasks: tuple[Task, ...], agent: TransactionAgent, agent_count: int, max_batch_size: int, mode: str, aging_factor: float, scheduler: str='concurrent') -> list[AgentExecution]:
    if scheduler == 'rolling':
        if mode not in {'eager_fifo', 'occ_backoff', 'jit_fifo', 'vllm_lease', 'vllm_lock_manager', 'vllm_serial_graph', 'pessimistic_fifo'}:
            raise ValueError('rolling scheduler requires eager_fifo, occ_backoff, jit_fifo, vllm_lease, vllm_lock_manager, vllm_serial_graph, or pessimistic_fifo')
        if max_batch_size not in {0, agent_count}:
            raise ValueError('rolling scheduler requires max_batch_size=0 or agents.count')
        completed: dict[str, AgentExecution] = {}
        by_agent: list[list[Task]] = [[] for _ in range(agent_count)]
        for position, task in enumerate(tasks):
            by_agent[position % agent_count].append(task)

        def run_agent(agent_id: str, assigned: list[Task]) -> list[AgentExecution]:
            executions = []
            for task in assigned:
                transaction = agent.prepare(agent_id, task.agent_view())
                if mode not in _MODEL_ADMISSION_MODES:
                    agent.bind(transaction)
                executions.append(agent.execute(transaction, mode in _MODEL_ADMISSION_MODES, mode == 'vllm_serial_graph'))
            return executions
        with ThreadPoolExecutor(max_workers=agent_count) as pool:
            futures = [pool.submit(run_agent, f'agent_{index:03d}', assigned) for index, assigned in enumerate(by_agent) if assigned]
            for future in futures:
                for execution in future.result():
                    completed[execution.result.op_id] = execution
        return [completed[task.op_id] for task in tasks]
    completed: dict[str, AgentExecution] = {}
    capacity = max_batch_size or agent_count
    with ThreadPoolExecutor(max_workers=capacity) as pool:
        position = 0
        for _, grouped in groupby(tasks, key=lambda task: task.wave):
            transactions = []
            for task in grouped:
                transactions.append(agent.prepare(f'agent_{position % agent_count:03d}', task.agent_view()))
                position += 1
            if mode in _EAGER_BIND_MODES:
                for transaction in transactions:
                    agent.bind(transaction)
            pending = list(transactions)
            while pending:
                selected = _select_batch(pending, capacity, mode, aging_factor)
                if mode not in {*_EAGER_BIND_MODES, 'vllm_lease', 'vllm_lock_manager', 'vllm_serial_graph', 'pessimistic_fifo'}:
                    for transaction in selected:
                        agent.bind(transaction)
                futures = [pool.submit(agent.execute, transaction, mode in _MODEL_ADMISSION_MODES, mode == 'vllm_serial_graph') for transaction in selected]
                for transaction, future in zip(selected, futures, strict=True):
                    completed[transaction.task.op_id] = future.result()
                selected_ids = {transaction.task.op_id for transaction in selected}
                pending = [transaction for transaction in pending if transaction.task.op_id not in selected_ids]
    return [completed[task.op_id] for task in tasks]
