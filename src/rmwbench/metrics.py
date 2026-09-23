from __future__ import annotations
from collections import Counter, defaultdict
from dataclasses import asdict
from itertools import combinations
from typing import Any
from .models import OperationResult, Task, access_sets_conflict

def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0

def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentile)
    return ordered[index]

def _selected_conflict_ops(results: list[OperationResult]) -> set[str]:
    by_wave: dict[int, list[OperationResult]] = defaultdict(list)
    for result in results:
        by_wave[result.wave].append(result)
    conflicted: set[str] = set()
    for wave_results in by_wave.values():
        counts = Counter((result.selected_target for result in wave_results if result.selected_target is not None))
        conflicted.update((result.op_id for result in wave_results if result.selected_target is not None and counts[result.selected_target] > 1))
    return conflicted

def _access_quality(results: list[OperationResult], predicted_field: str, actual_field: str) -> dict[str, float | int]:
    true_positives = 0
    false_positives = 0
    false_negatives = 0
    for result in results:
        predicted = set(getattr(result, predicted_field))
        actual = set(getattr(result, actual_field))
        true_positives += len(predicted & actual)
        false_positives += len(predicted - actual)
        false_negatives += len(actual - predicted)
    return {'true_positives': true_positives, 'false_positives': false_positives, 'false_negatives': false_negatives, 'precision': _rate(true_positives, true_positives + false_positives), 'recall': _rate(true_positives, true_positives + false_negatives)}

def _access_set_quality(results: list[OperationResult], read_field: str, write_field: str) -> dict[str, Any]:
    by_wave: dict[int, list[OperationResult]] = defaultdict(list)
    for result in results:
        by_wave[result.wave].append(result)
    actual_conflicts = 0
    predicted_conflicts = 0
    conflict_true_positives = 0
    for wave_results in by_wave.values():
        for left, right in combinations(wave_results, 2):
            actual = access_sets_conflict(left.actual_read_blocks, left.actual_write_blocks, right.actual_read_blocks, right.actual_write_blocks)
            predicted = access_sets_conflict(getattr(left, read_field), getattr(left, write_field), getattr(right, read_field), getattr(right, write_field))
            actual_conflicts += actual
            predicted_conflicts += predicted
            conflict_true_positives += actual and predicted
    exact_operations = 0
    underpredicted_operations = 0
    overpredicted_operations = 0
    for result in results:
        predicted_reads = set(getattr(result, read_field))
        predicted_writes = set(getattr(result, write_field))
        actual_reads = set(result.actual_read_blocks)
        actual_writes = set(result.actual_write_blocks)
        exact_operations += predicted_reads == actual_reads and predicted_writes == actual_writes
        underpredicted_operations += bool(actual_reads - predicted_reads) or bool(actual_writes - predicted_writes)
        overpredicted_operations += bool(predicted_reads - actual_reads) or bool(predicted_writes - actual_writes)
    return {'read': _access_quality(results, read_field, 'actual_read_blocks'), 'write': _access_quality(results, write_field, 'actual_write_blocks'), 'conflict': {'actual_pairs': actual_conflicts, 'predicted_pairs': predicted_conflicts, 'true_positive_pairs': conflict_true_positives, 'precision': _rate(conflict_true_positives, predicted_conflicts), 'recall': _rate(conflict_true_positives, actual_conflicts)}, 'exact_operations': exact_operations, 'underpredicted_operations': underpredicted_operations, 'overpredicted_operations': overpredicted_operations}

def _prediction_quality(results: list[OperationResult]) -> dict[str, Any]:
    return _access_set_quality(results, 'predicted_read_blocks', 'predicted_write_blocks')

def _admission_quality(results: list[OperationResult]) -> dict[str, Any]:
    quality = _access_set_quality(results, 'admission_read_blocks', 'admission_write_blocks')
    probed = sum((result.probe_reads > 0 for result in results))
    reused = sum((result.probe_snapshot_reused for result in results))
    rebound = sum((result.probe_snapshot_rebound for result in results))
    bypasses = [result.admission_bypasses for result in results]
    admission_wait_ms = [result.admission_wait_ns / 1000000 for result in results]
    forced = sum((result.fairness_forced for result in results))
    quality.update(refined_operations=sum((result.access_refined for result in results)), refinement_rate=_rate(sum((result.access_refined for result in results)), len(results)), probe_reads=sum((result.probe_reads for result in results)), probed_operations=probed, snapshot_reused_operations=reused, snapshot_rebound_operations=rebound, snapshot_reuse_rate=_rate(reused, probed), bypasses_total=sum(bypasses), bypasses_mean=_rate(sum(bypasses), len(bypasses)), bypasses_p95=_percentile(bypasses, 0.95), bypasses_max=max(bypasses, default=0), fairness_forced_operations=forced, fairness_forced_rate=_rate(forced, len(results)), wait_ms={'mean': _rate(sum(admission_wait_ms), len(admission_wait_ms)), 'p95': _percentile(admission_wait_ms, 0.95), 'max': max(admission_wait_ms, default=0.0)})
    return quality

def evaluate(tasks: tuple[Task, ...], results: list[OperationResult], lost_op_ids: frozenset[str], elapsed_seconds: float) -> dict[str, Any]:
    total = len(tasks)
    successful = [result for result in results if result.success]
    tasks_by_id = {task.op_id: task for task in tasks}
    success_ids = {result.op_id for result in successful}
    selected_conflicts = _selected_conflict_ops(results)
    offered_ww = sum((task.offered_ww for task in tasks))
    offered_rw = sum((task.offered_rw for task in tasks))
    realized = sum((result.saw_target_conflict for result in results))
    lost = len(success_ids & lost_op_ids)
    latencies_ms = [result.latency_ns / 1000000 for result in successful]
    reads = sum((result.reads for result in results))
    attempts = sum((result.attempts for result in results))
    input_tokens = sum((result.input_tokens for result in results))
    output_tokens = sum((result.output_tokens for result in results))
    success_count = len(successful)
    useful = [result for result in successful if result.semantic_correct and result.basis_correct and result.final_fresh and (result.op_id not in lost_op_ids)]
    model_ns = sum((result.model_ns for result in results))
    wasted_model_ns = sum((result.wasted_model_ns for result in results))
    queue_ms = [result.queue_ns / 1000000 for result in results]
    exposure_ms = [result.snapshot_exposure_ns / 1000000 for result in results if result.snapshot_exposure_ns]
    model_input_tokens = sum((result.model_input_tokens for result in results))
    model_output_tokens = sum((result.model_output_tokens for result in results))
    target_plan_ns = sum((result.target_plan_ns for result in results))
    target_plan_input_tokens = sum((result.target_plan_input_tokens for result in results))
    target_plan_output_tokens = sum((result.target_plan_output_tokens for result in results))
    control_ns = sum((result.control_ns for result in results))
    control_input_tokens = sum((result.control_input_tokens for result in results))
    control_output_tokens = sum((result.control_output_tokens for result in results))
    resolution_efficiency = 1.0 - realized / offered_ww if offered_ww else None
    return {'operations': {'requested': total, 'successful': success_count, 'aborted': total - success_count, 'success_rate': _rate(success_count, total)}, 'conflicts': {'offered_ww_operations': offered_ww, 'offered_ww_rate': _rate(offered_ww, total), 'offered_rw_operations': offered_rw, 'offered_rw_rate': _rate(offered_rw, total), 'selected_target_operations': len(selected_conflicts), 'selected_target_rate': _rate(len(selected_conflicts), total), 'realized_stale_target_operations': realized, 'realized_stale_target_rate': _rate(realized, total), 'lost_update_operations': lost, 'lost_update_rate': _rate(lost, success_count), 'resolution_efficiency': resolution_efficiency}, 'correctness': {'routing_accuracy': _rate(sum((result.selected_target == tasks_by_id[result.op_id].intended_target for result in results)), total), 'semantic_correctness': _rate(sum((result.semantic_correct for result in successful)), success_count), 'basis_correctness': _rate(sum((result.basis_correct for result in successful)), success_count), 'freshness_rate': _rate(sum((result.final_fresh for result in successful)), success_count)}, 'prediction': _prediction_quality(results), 'admission': _admission_quality(results), 'efficiency': {'read_amplification': _rate(reads, success_count), 'retry_operations': sum((result.attempts > 1 for result in results)), 'retry_operation_rate': _rate(sum((result.attempts > 1 for result in results)), total), 'write_amplification': _rate(attempts, success_count), 'extra_attempts': attempts - total, 'throughput_ops_s': _rate(success_count, elapsed_seconds), 'useful_operations': len(useful), 'useful_throughput_ops_gpu_hour': _rate(len(useful) * 3600.0, elapsed_seconds), 'model_request_seconds': model_ns / 1000000000, 'target_planning_seconds': target_plan_ns / 1000000000, 'control_request_seconds': control_ns / 1000000000, 'wasted_model_seconds': wasted_model_ns / 1000000000, 'waste_ratio': _rate(wasted_model_ns, model_ns), 'elapsed_seconds': elapsed_seconds}, 'latency_ms': {'mean': _rate(sum(latencies_ms), len(latencies_ms)), 'p50': _percentile(latencies_ms, 0.5), 'p95': _percentile(latencies_ms, 0.95)}, 'queue_ms': {'mean': _rate(sum(queue_ms), len(queue_ms)), 'p50': _percentile(queue_ms, 0.5), 'p95': _percentile(queue_ms, 0.95)}, 'snapshot_exposure_ms': {'mean': _rate(sum(exposure_ms), len(exposure_ms)), 'p50': _percentile(exposure_ms, 0.5), 'p95': _percentile(exposure_ms, 0.95)}, 'tokens': {'input_total': input_tokens, 'output_total': output_tokens, 'input_per_success': _rate(input_tokens, success_count), 'output_per_success': _rate(output_tokens, success_count), 'model_input_total': model_input_tokens, 'model_output_total': model_output_tokens, 'target_plan_input_total': target_plan_input_tokens, 'target_plan_output_total': target_plan_output_tokens, 'control_input_total': control_input_tokens, 'control_output_total': control_output_tokens, 'model_output_per_second': _rate(model_output_tokens, elapsed_seconds)}, 'abort_reasons': dict(Counter((result.abort_reason for result in results if not result.success and result.abort_reason is not None))), 'operation_results': [asdict(result) for result in results]}
