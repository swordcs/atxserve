from __future__ import annotations
from hashlib import sha256
from math import floor
from typing import Iterable
from .config import PredictionConfig
from .models import AgentTask

def _unit_interval(seed: int, op_id: str, namespace: str, block_id: str) -> float:
    payload = f'{seed}|{op_id}|{namespace}|{block_id}'.encode()
    digest = sha256(payload).digest()
    return int.from_bytes(digest[:8], 'big') / 2 ** 64

def _perturb_access_set(*, seed: int, op_id: str, namespace: str, actual: Iterable[str], candidates: Iterable[str], precision: float, recall: float) -> tuple[str, ...]:
    actual_tuple = tuple(dict.fromkeys(actual))
    if recall == 1.0:
        retained = actual_tuple
    elif recall == 0.0:
        retained = ()
    else:
        retained = tuple((block_id for block_id in actual_tuple if _unit_interval(seed, op_id, namespace + ':retain', block_id) < recall))
    false_candidates = sorted(set(candidates) - set(actual_tuple))
    desired_false_positives = len(retained) * (1.0 / precision - 1.0)
    false_positive_count = floor(desired_false_positives)
    fractional = desired_false_positives - false_positive_count
    if fractional and _unit_interval(seed, op_id, namespace + ':round', '__count__') < fractional:
        false_positive_count += 1
    false_positive_count = min(false_positive_count, len(false_candidates))
    extras = sorted(false_candidates, key=lambda block_id: (_unit_interval(seed, op_id, namespace + ':rank', block_id), block_id))[:false_positive_count]
    return (*retained, *extras)

def predict_access_sets(task: AgentTask, actual_reads: Iterable[str], actual_writes: Iterable[str], config: PredictionConfig, seed: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
    predicted_reads = _perturb_access_set(seed=seed, op_id=task.op_id, namespace='read', actual=actual_reads, candidates=task.allowed_blocks, precision=config.read_precision, recall=config.read_recall)
    predicted_writes = _perturb_access_set(seed=seed, op_id=task.op_id, namespace='write', actual=actual_writes, candidates=task.candidate_targets, precision=config.write_precision, recall=config.write_recall)
    return (predicted_reads, predicted_writes)
