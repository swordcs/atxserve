from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
import re
from threading import Lock
from time import perf_counter_ns
from urllib.request import ProxyHandler, Request, build_opener

from rmwbench.config import ModelConfig
from rmwbench.models import AgentTask
from .protocol import Observation


class UsageUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelCall:
    phase: str
    input_tokens: int
    output_tokens: int
    elapsed_ns: int
    content: str
    request_id: str


@dataclass
class Usage:
    calls: list[ModelCall] = field(default_factory=list)
    lock: Lock = field(default_factory=Lock)

    def add(self, call: ModelCall):
        with self.lock:
            self.calls.append(call)

    def totals(self) -> dict:
        with self.lock:
            return {"input_tokens": sum(call.input_tokens for call in self.calls),
                    "output_tokens": sum(call.output_tokens for call in self.calls),
                    "model_ns": sum(call.elapsed_ns for call in self.calls),
                    "model_calls": len(self.calls),
                    "planning_calls": sum(call.phase == "plan" for call in self.calls)}


def json_object(content: str) -> dict:
    decoder = json.JSONDecoder()
    for position, character in enumerate(content):
        if character == "{":
            try:
                value, _ = decoder.raw_decode(content[position:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    raise ValueError("Model response does not contain a JSON object")


class ModelClient:
    def __init__(self, config: ModelConfig, seed: int):
        if config.backend != "vllm":
            raise ValueError("Paper experiments require a real OpenAI-compatible model server")
        self.config = config
        self.seed = seed

    def complete(self, prompt: str, phase: str, request_id: str, usage: Usage,
                 schema: dict | None = None, max_tokens: int | None = None) -> dict:
        seed = int.from_bytes(hashlib.sha256(f"{self.seed}:{request_id}".encode()).digest()[:4], "big")
        payload = {"model": self.config.name, "messages": [{"role": "user", "content": prompt}],
                   "temperature": 0, "seed": seed, "max_tokens": max_tokens or self.config.max_tokens,
                   "chat_template_kwargs": {"enable_thinking": False}}
        if phase == "execute" and self.config.min_tokens:
            payload["min_tokens"] = self.config.min_tokens
        elif schema is not None:
            payload["response_format"] = {"type": "json_schema", "json_schema": {
                "name": phase, "strict": True, "schema": schema}}
        else:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json", "X-Request-Id": request_id}
        if os.environ.get("OPENAI_API_KEY"):
            headers["Authorization"] = "Bearer " + os.environ["OPENAI_API_KEY"]
        request = Request(self.config.base_url.rstrip("/") + "/chat/completions",
                          json.dumps(payload).encode(), headers)
        started = perf_counter_ns()
        content = ""
        inputs = outputs = 0
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=self.config.timeout_seconds) as response:
                body = json.load(response)
            tokens = body.get("usage")
            if tokens is None or "prompt_tokens" not in tokens or "completion_tokens" not in tokens:
                raise UsageUnavailable("Model server did not return token usage")
            inputs = int(tokens["prompt_tokens"])
            outputs = int(tokens["completion_tokens"])
            content = body["choices"][0]["message"]["content"] or ""
            return json_object(content)
        finally:
            usage.add(ModelCall(phase, inputs, outputs, perf_counter_ns() - started, content, request_id))

    def plan(self, task: AgentTask, catalog: str, request_id: str, usage: Usage) -> str:
        routing = "\n".join(line for line in catalog.splitlines()
                            if line.startswith("ROLE ") or line.startswith("ROUTE "))
        prompt = ("Find the ROUTE line whose key exactly matches the routing key. "
                  f"Routing key: {task.routing_key}. Return its target exactly as JSON "
                  f"with one target field.\n\n{routing}")
        schema = {"type": "object", "properties": {"target": {"type": "string",
                  "enum": sorted(task.candidate_targets)}}, "required": ["target"],
                  "additionalProperties": False}
        target = self.complete(prompt, "plan", request_id, usage, schema, 64).get("target")
        if target not in task.candidate_targets:
            raise ValueError("Model selected a target outside the candidate set")
        return target

    def patch(self, task: AgentTask, evidence: list[Observation], request_id: str, usage: Usage) -> tuple[str, int]:
        context = "\n\n".join(f"--- {item.resource} ---\n{item.content}" for item in evidence)
        prompt = f"""Find FACT lines whose task is exactly {task.op_id} and whose
entity is exactly {task.entity}. Ignore every other task and entity. Among only
those matching lines with valid=1, choose the one with the greatest integer
after ts=. Copy the word after status= into the JSON status field, and copy the
integer after ts= into timestamp. The valid field is only a filter and is never
the answer.

{context}

Final check for task {task.op_id}, entity {task.entity}: reject every line with
valid=0. Use the status= word and ts= integer from a valid=1 line. Return exactly
one JSON object and nothing else:
{{"status":"copied status value","timestamp":123}}
"""
        result = self.complete(prompt, "execute", request_id, usage)
        status = result.get("status")
        timestamp = result.get("timestamp")
        if not isinstance(status, str) or re.fullmatch("[a-z]+", status) is None:
            raise ValueError("Invalid status in model patch")
        if type(timestamp) is not int:
            raise ValueError("Invalid timestamp in model patch")
        return status, timestamp
