from __future__ import annotations
import json
import re
from time import perf_counter_ns, sleep
from typing import Any, Protocol
import urllib.request
from uuid import uuid4
from .config import ModelConfig, RunConfig
from .models import AdmissionGrant, AgentTask, ControlRequestResult, IntentPrediction, PatchDecision, ReadResult, TargetDecision

class Reasoner(Protocol):

    def plan_target(self, task: AgentTask, routing_index: str) -> TargetDecision:
        ...

    def acquire(self, task: AgentTask, intent: IntentPrediction) -> AdmissionGrant | None:
        ...

    def make_patch(self, task: AgentTask, evidence: list[ReadResult], intent: IntentPrediction, grant: AdmissionGrant | None=None) -> PatchDecision:
        ...

    def release(self, task: AgentTask, intent: IntentPrediction, grant: AdmissionGrant) -> ControlRequestResult:
        ...

    def commit(self, task: AgentTask, intent: IntentPrediction, grant: AdmissionGrant) -> ControlRequestResult:
        ...

def _target_from_index(task: AgentTask, routing_index: str) -> str:
    prefix = f'ROUTE key={task.routing_key} target='
    for line in routing_index.splitlines():
        if line.startswith(prefix):
            target = line[len(prefix):].strip()
            if target not in task.candidate_targets:
                raise ValueError('planned target is outside the candidate set')
            return target
    raise ValueError(f'routing key {task.routing_key} is absent from index')

class MockReasoner:

    def __init__(self, run_config: RunConfig):
        self._delay_seconds = run_config.mock_delay_ms / 1000.0

    def plan_target(self, task: AgentTask, routing_index: str) -> TargetDecision:
        start = perf_counter_ns()
        target = _target_from_index(task, routing_index)
        return TargetDecision(target, len(routing_index.split()), 1, perf_counter_ns() - start)

    def acquire(self, task: AgentTask, intent: IntentPrediction) -> AdmissionGrant | None:
        return AdmissionGrant(f'mock-{task.op_id}', ControlRequestResult(0, 0, 0))

    def make_patch(self, task: AgentTask, evidence: list[ReadResult], intent: IntentPrediction, grant: AdmissionGrant | None=None) -> PatchDecision:
        start = perf_counter_ns()
        pattern = re.compile(f'FACT task={re.escape(task.op_id)} entity={re.escape(task.entity)} ts=(\\d+) status=([a-z]+) valid=([01])')
        candidates: list[tuple[int, str]] = []
        for result in evidence:
            for match in pattern.finditer(result.content):
                if match.group(3) == '1':
                    candidates.append((int(match.group(1)), match.group(2)))
        if not candidates:
            raise ValueError(f'no valid evidence found for {task.op_id}')
        if self._delay_seconds:
            sleep(self._delay_seconds)
        timestamp, status = max(candidates)
        return PatchDecision(status, timestamp, sum((result.token_count for result in evidence)), 2, perf_counter_ns() - start)

    def release(self, task: AgentTask, intent: IntentPrediction, grant: AdmissionGrant) -> ControlRequestResult:
        return ControlRequestResult(0, 0, 0)

    def commit(self, task: AgentTask, intent: IntentPrediction, grant: AdmissionGrant) -> ControlRequestResult:
        return ControlRequestResult(0, 0, 0)

def _json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for position, character in enumerate(text):
        if character != '{':
            continue
        try:
            value, _ = decoder.raw_decode(text[position:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError(f'model did not return a JSON object: {text[-300:]!r}')

class VLLMReasoner:

    def __init__(self, config: ModelConfig, control_plane_admission: bool=False, serialization_graph: bool=False):
        self._config = config
        self._control_plane_admission = control_plane_admission
        self._serialization_graph = serialization_graph
        self._endpoint = config.base_url.rstrip('/') + '/chat/completions'
        self._control_endpoint = config.base_url.rstrip('/') + '/completions'
        api_root = config.base_url.rstrip('/')
        if api_root.endswith('/v1'):
            api_root = api_root[:-3]
        self._admit_endpoint = api_root + '/txserve/admit'
        self._commit_endpoint = api_root + '/txserve/commit'
        self._release_endpoint = api_root + '/txserve/release'
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _post(self, endpoint: str, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        request = urllib.request.Request(endpoint, data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'}, method='POST')
        start = perf_counter_ns()
        with self._opener.open(request, timeout=self._config.timeout_seconds) as response:
            body = json.loads(response.read())
        return (body, perf_counter_ns() - start)

    @staticmethod
    def _control_result(body: dict[str, Any], model_ns: int) -> ControlRequestResult:
        usage = body.get('usage', {})
        return ControlRequestResult(model_ns, int(usage.get('prompt_tokens', 0)), int(usage.get('completion_tokens', 0)))

    def _admission_payload(self, request_id: str, grant_id: str, intent: IntentPrediction) -> dict[str, Any]:
        return {'model': self._config.name, 'request_id': request_id, 'prompt': 'x', 'temperature': 0, 'max_tokens': 1, 'vllm_xargs': {'tx_phase': 'admit', 'tx_grant_id': grant_id, 'tx_lease_seconds': self._config.admission_lease_seconds, 'tx_read_blocks': json.dumps(intent.read_blocks, separators=(',', ':')), 'tx_write_blocks': json.dumps(intent.write_blocks, separators=(',', ':'))}}

    def plan_target(self, task: AgentTask, routing_index: str) -> TargetDecision:
        prompt = f'Find the ROUTE line whose key exactly matches the routing key. Routing key: {task.routing_key}. Return its target exactly as JSON with one target field.\n\n{routing_index}'
        payload = {'model': self._config.name, 'request_id': f'{task.op_id}:target-plan:{uuid4().hex}', 'messages': [{'role': 'user', 'content': prompt}], 'temperature': 0, 'max_tokens': 64, 'chat_template_kwargs': {'enable_thinking': False}, 'response_format': {'type': 'json_schema', 'json_schema': {'name': 'target_plan', 'strict': True, 'schema': {'type': 'object', 'properties': {'target': {'type': 'string', 'enum': sorted(task.candidate_targets)}}, 'required': ['target'], 'additionalProperties': False}}}}
        body, model_ns = self._post(self._endpoint, payload)
        content = body['choices'][0]['message']['content']
        value = _json_object(content)
        usage = body.get('usage', {})
        target = str(value.get('target', ''))
        if target not in task.candidate_targets:
            raise ValueError(f'model returned target outside candidate set: {target!r}')
        return TargetDecision(target, int(usage.get('prompt_tokens', 0)), int(usage.get('completion_tokens', 0)), model_ns)

    def acquire(self, task: AgentTask, intent: IntentPrediction) -> AdmissionGrant:
        grant_id = f'{task.op_id}-{uuid4().hex}'
        if self._control_plane_admission:
            body, model_ns = self._post(self._admit_endpoint, {'grant_id': grant_id, 'lease_seconds': self._config.admission_lease_seconds, 'read_blocks': list(intent.read_blocks), 'write_blocks': list(intent.write_blocks)})
            return AdmissionGrant(grant_id, self._control_result(body, model_ns), int(body.get('bypasses', 0)), int(body.get('wait_ns', 0)), bool(body.get('fairness_forced', False)))
        payload = self._admission_payload(f'{grant_id}:admit', grant_id, intent)
        body, model_ns = self._post(self._control_endpoint, payload)
        return AdmissionGrant(grant_id, self._control_result(body, model_ns))

    def make_patch(self, task: AgentTask, evidence: list[ReadResult], intent: IntentPrediction, grant: AdmissionGrant | None=None) -> PatchDecision:
        context = '\n\n'.join((f'--- {result.block_id} ---\n{result.content}' for result in evidence))
        prompt = f'Find FACT lines whose task is exactly {task.op_id} and whose\nentity is exactly {task.entity}. Ignore every other task and entity. Among only\nthose matching lines with valid=1, choose the one with the greatest integer\nafter ts=. Copy the word after status= into the JSON status field, and copy the\ninteger after ts= into timestamp. The valid field is only a filter and is never\nthe answer.\n\n{context}\n\nFinal check for task {task.op_id}, entity {task.entity}: reject every line with\nvalid=0. Use the status= word and ts= integer from a valid=1 line. Return exactly\none JSON object and nothing else:\n{{"status":"copied status value","timestamp":123}}\n'
        extra_args: dict[str, Any] = {'tx_read_blocks': list(intent.read_blocks), 'tx_write_blocks': list(intent.write_blocks)}
        request_id = task.op_id
        if grant is not None:
            extra_args.update(tx_phase='execute', tx_grant_id=grant.grant_id, tx_lease_seconds=self._config.admission_lease_seconds)
            request_id = f'{grant.grant_id}:execute:{uuid4().hex}'
        payload = {'model': self._config.name, 'request_id': request_id, 'messages': [{'role': 'user', 'content': prompt}], 'temperature': 0, 'min_tokens': self._config.min_tokens, 'max_tokens': self._config.max_tokens, 'chat_template_kwargs': {'enable_thinking': False}, 'vllm_xargs': extra_args}
        if self._config.min_tokens == 0:
            payload['response_format'] = {'type': 'json_object'}
        body, model_ns = self._post(self._endpoint, payload)
        content = body['choices'][0]['message']['content']
        value = _json_object(content)
        usage = body.get('usage', {})
        status = str(value.get('status', ''))
        timestamp = int(value['timestamp'])
        if not re.fullmatch('[a-z]+', status):
            raise ValueError(f'model returned invalid status {status!r}')
        return PatchDecision(status, timestamp, int(usage.get('prompt_tokens', 0)), int(usage.get('completion_tokens', 0)), model_ns)

    def release(self, task: AgentTask, intent: IntentPrediction, grant: AdmissionGrant) -> ControlRequestResult:
        body, model_ns = self._post(self._release_endpoint, {'grant_id': grant.grant_id})
        return self._control_result(body, model_ns)

    def commit(self, task: AgentTask, intent: IntentPrediction, grant: AdmissionGrant) -> ControlRequestResult:
        if not self._serialization_graph:
            return ControlRequestResult(0, 0, 0)
        body, model_ns = self._post(self._commit_endpoint, {'grant_id': grant.grant_id})
        return self._control_result(body, model_ns)

def make_reasoner(model: ModelConfig, run: RunConfig, txserve_mode: str='legacy') -> Reasoner:
    reasoner: Reasoner = MockReasoner(run) if model.backend == 'mock' else VLLMReasoner(model, txserve_mode in {'vllm_lock_manager', 'vllm_serial_graph'}, txserve_mode == 'vllm_serial_graph')
    if txserve_mode == 'pessimistic_fifo':
        from .baseline import PessimisticFIFOReasoner
        return PessimisticFIFOReasoner(reasoner)
    return reasoner
