from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import tomllib
from typing import Any, Mapping

@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    name: str = 'rmwbench'
    seed: int = 20260826

@dataclass(frozen=True, slots=True)
class DatasetConfig:
    files: int = 4
    blocks_per_file: int = 16
    source: str = 'synthetic'
    root: str = ''
    schedule: str = ''

@dataclass(frozen=True, slots=True)
class PayloadConfig:
    target_tokens: int = 256
    tolerance: float = 0.1
    anchors_per_block: int = 4
    source_token_variation: float = 0.0

@dataclass(frozen=True, slots=True)
class AgentsConfig:
    count: int = 4

@dataclass(frozen=True, slots=True)
class RunConfig:
    operations: int = 16
    scheduler: str = 'forced_stale'
    invalid_write_policy: str = 'reject'
    max_retries: int = 1
    mock_delay_ms: float = 1.0
    backoff_base_seconds: float = 0.1
    backoff_cap_seconds: float = 1.6

@dataclass(frozen=True, slots=True)
class ReasoningConfig:
    candidate_facts: int = 8
    relevant_facts: int = 3
    candidate_targets: int = 4

@dataclass(frozen=True, slots=True)
class ConflictConfig:
    mode: str = 'exact'
    ww_rate: float = 0.5
    rw_rate: float = 0.25
    zipf_alpha: float = 0.0

@dataclass(frozen=True, slots=True)
class PredictionConfig:
    read_precision: float = 1.0
    read_recall: float = 1.0
    write_precision: float = 1.0
    write_recall: float = 1.0

@dataclass(frozen=True, slots=True)
class StorageConfig:
    write_validation: str = 'observe'
    hash: str = 'sha256'

@dataclass(frozen=True, slots=True)
class TxServeConfig:
    mode: str = 'legacy'
    aging_factor: float = 0.01
    max_batch_size: int = 0
    refine_access_sets: bool = False
    reuse_probe_snapshot: bool = True
    late_bind_write_target: bool = False
    predict_access_sets_after_plan: bool = False

@dataclass(frozen=True, slots=True)
class ModelConfig:
    backend: str = 'mock'
    base_url: str = 'http://127.0.0.1:18080/v1'
    name: str = 'qwen3-4b'
    min_tokens: int = 0
    max_tokens: int = 64
    timeout_seconds: float = 120.0
    admission_lease_seconds: float = 300.0

@dataclass(frozen=True, slots=True)
class Config:
    benchmark: BenchmarkConfig = field(default_factory=BenchmarkConfig)
    dataset: DatasetConfig = field(default_factory=DatasetConfig)
    payload: PayloadConfig = field(default_factory=PayloadConfig)
    agents: AgentsConfig = field(default_factory=AgentsConfig)
    run: RunConfig = field(default_factory=RunConfig)
    reasoning: ReasoningConfig = field(default_factory=ReasoningConfig)
    conflict: ConflictConfig = field(default_factory=ConflictConfig)
    prediction: PredictionConfig = field(default_factory=PredictionConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    txserve: TxServeConfig = field(default_factory=TxServeConfig)
    model: ModelConfig = field(default_factory=ModelConfig)

    @classmethod
    def load(cls, path: str | Path) -> 'Config':
        with Path(path).open('rb') as handle:
            raw = tomllib.load(handle)
        config = cls.from_mapping(raw)
        config.validate()
        return config

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> 'Config':
        known = {'benchmark', 'dataset', 'payload', 'agents', 'run', 'reasoning', 'conflict', 'prediction', 'storage', 'txserve', 'model'}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f'unknown configuration sections: {sorted(unknown)}')

        def section(name: str) -> dict[str, Any]:
            value = raw.get(name, {})
            if not isinstance(value, Mapping):
                raise ValueError(f'configuration section {name!r} must be a table')
            return dict(value)
        try:
            return cls(benchmark=BenchmarkConfig(**section('benchmark')), dataset=DatasetConfig(**section('dataset')), payload=PayloadConfig(**section('payload')), agents=AgentsConfig(**section('agents')), run=RunConfig(**section('run')), reasoning=ReasoningConfig(**section('reasoning')), conflict=ConflictConfig(**section('conflict')), prediction=PredictionConfig(**section('prediction')), storage=StorageConfig(**section('storage')), txserve=TxServeConfig(**section('txserve')), model=ModelConfig(**section('model')))
        except TypeError as error:
            raise ValueError(f'invalid configuration key: {error}') from error

    def validate(self) -> None:
        if not 0 < self.run.backoff_base_seconds <= self.run.backoff_cap_seconds < float('inf'):
            raise ValueError('backoff requires finite 0 < base <= cap')
        positive = {'dataset.files': self.dataset.files, 'dataset.blocks_per_file': self.dataset.blocks_per_file, 'payload.target_tokens': self.payload.target_tokens, 'agents.count': self.agents.count, 'run.operations': self.run.operations, 'reasoning.candidate_facts': self.reasoning.candidate_facts, 'reasoning.relevant_facts': self.reasoning.relevant_facts, 'reasoning.candidate_targets': self.reasoning.candidate_targets}
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f'{name} must be positive')
        if self.dataset.source not in {'synthetic', 'commit0', 'commit_focus'}:
            raise ValueError('dataset.source must be synthetic, commit0, or commit_focus')
        if self.dataset.source in {'commit0', 'commit_focus'}:
            if not self.dataset.root:
                raise ValueError(f'dataset.root is required for source={self.dataset.source}')
            if self.dataset.blocks_per_file != 1:
                raise ValueError(f'source={self.dataset.source} requires blocks_per_file=1')
        if self.dataset.source == 'commit_focus' and self.dataset.schedule not in {'dispersed', 'mixed', 'focused'}:
            raise ValueError('source=commit_focus requires dispersed, mixed, or focused schedule')
        if self.reasoning.relevant_facts > self.reasoning.candidate_facts:
            raise ValueError('relevant_facts cannot exceed candidate_facts')
        if not 0.0 <= self.payload.tolerance <= 1.0:
            raise ValueError('payload.tolerance must be between 0 and 1')
        if not 0.0 <= self.payload.source_token_variation < 1.0:
            raise ValueError('payload.source_token_variation must be in [0, 1)')
        if self.payload.anchors_per_block < 0:
            raise ValueError('payload.anchors_per_block cannot be negative')
        if self.agents.count > self.run.operations:
            raise ValueError('agents.count cannot exceed run.operations')
        if self.run.scheduler not in {'concurrent', 'forced_stale', 'rolling'}:
            raise ValueError('run.scheduler must be concurrent, forced_stale, or rolling')
        if self.run.invalid_write_policy != 'reject':
            raise ValueError('only invalid_write_policy=reject is supported')
        if self.run.max_retries < 0 or self.run.mock_delay_ms < 0:
            raise ValueError('retry count and mock delay cannot be negative')
        if self.conflict.mode != 'exact':
            raise ValueError('only conflict.mode=exact is supported')
        for name, rate in {'ww_rate': self.conflict.ww_rate, 'rw_rate': self.conflict.rw_rate}.items():
            if not 0.0 <= rate <= 1.0:
                raise ValueError(f'conflict.{name} must be between 0 and 1')
        if self.conflict.zipf_alpha < 0:
            raise ValueError('conflict.zipf_alpha cannot be negative')
        for name, value in {'read_precision': self.prediction.read_precision, 'write_precision': self.prediction.write_precision}.items():
            if not 0.0 < value <= 1.0:
                raise ValueError(f'prediction.{name} must be in (0, 1]')
        for name, value in {'read_recall': self.prediction.read_recall, 'write_recall': self.prediction.write_recall}.items():
            if not 0.0 <= value <= 1.0:
                raise ValueError(f'prediction.{name} must be in [0, 1]')
        if self.storage.write_validation not in {'observe', 'target_cas', 'basis_occ'}:
            raise ValueError('write_validation must be observe, target_cas, or basis_occ')
        if self.storage.hash != 'sha256':
            raise ValueError('only sha256 hashing is supported')
        if self.txserve.mode not in {'legacy', 'eager_fifo', 'occ_backoff', 'pessimistic_fifo', 'jit_fifo', 'txserve', 'vllm_lease', 'vllm_lock_manager', 'vllm_serial_graph'}:
            raise ValueError('txserve.mode must be legacy, eager_fifo, occ_backoff, pessimistic_fifo, jit_fifo, txserve, vllm_lease, vllm_lock_manager, or vllm_serial_graph')
        if self.txserve.aging_factor < 0:
            raise ValueError('txserve.aging_factor cannot be negative')
        if not isinstance(self.txserve.refine_access_sets, bool):
            raise ValueError('txserve.refine_access_sets must be a boolean')
        if not isinstance(self.txserve.reuse_probe_snapshot, bool):
            raise ValueError('txserve.reuse_probe_snapshot must be a boolean')
        if not isinstance(self.txserve.late_bind_write_target, bool):
            raise ValueError('txserve.late_bind_write_target must be a boolean')
        if not isinstance(self.txserve.predict_access_sets_after_plan, bool):
            raise ValueError('txserve.predict_access_sets_after_plan must be a boolean')
        if self.txserve.predict_access_sets_after_plan and (not self.txserve.late_bind_write_target):
            raise ValueError('post-plan access prediction requires a late-bound target')
        if self.txserve.late_bind_write_target:
            if self.txserve.mode not in {'eager_fifo', 'occ_backoff', 'pessimistic_fifo', 'vllm_lease', 'vllm_lock_manager'}:
                raise ValueError('late-bound writes currently support eager_fifo, occ_backoff, pessimistic_fifo, vllm_lease, and vllm_lock_manager only')
            if self.txserve.mode in {'pessimistic_fifo', 'vllm_lease', 'vllm_lock_manager'} and (not self.txserve.refine_access_sets) and (not self.txserve.predict_access_sets_after_plan):
                raise ValueError('late-bound admission requires access refinement or post-plan access prediction')
        if self.txserve.refine_access_sets and self.txserve.mode not in {'vllm_lease', 'vllm_lock_manager', 'vllm_serial_graph', 'pessimistic_fifo'}:
            raise ValueError('access refinement requires a control-plane admission mode')
        if not 0 <= self.txserve.max_batch_size <= self.agents.count:
            raise ValueError('txserve.max_batch_size must be zero or at most agents.count')
        if self.model.backend not in {'mock', 'vllm'}:
            raise ValueError('model.backend must be mock or vllm')
        if not self.model.base_url or not self.model.name:
            raise ValueError('model.base_url and model.name cannot be empty')
        if self.model.min_tokens < 0 or self.model.max_tokens <= 0 or self.model.min_tokens > self.model.max_tokens or (self.model.timeout_seconds <= 0) or (self.model.admission_lease_seconds <= 0):
            raise ValueError('model token bounds, timeout, and admission lease are invalid')
