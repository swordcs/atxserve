from __future__ import annotations

import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--model-name", default="qwen3-4b")
    parser.add_argument("--gpu", default="0")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--capacity", type=int, default=8)
    parser.add_argument("--max-model-len", type=int, default=8192)
    parser.add_argument("--vllm", default="vllm")
    parser.add_argument("--legacy-api", action="store_true")
    parser.add_argument("--legacy-engine", action="store_true")
    args = parser.parse_args()
    if args.capacity < 1 or not args.model_path.is_dir():
        raise ValueError("A positive capacity and existing model directory are required")
    if args.legacy_api and args.legacy_engine:
        raise ValueError("Choose only one legacy placement")
    environment = dict(os.environ)
    environment["CUDA_VISIBLE_DEVICES"] = args.gpu
    command = [args.vllm, "serve", str(args.model_path), "--served-model-name", args.model_name,
               "--host", "127.0.0.1", "--port", str(args.port), "--dtype", "bfloat16",
               "--tensor-parallel-size", "1", "--gpu-memory-utilization", "0.70",
               "--max-model-len", str(args.max_model_len), "--max-num-seqs", str(args.capacity),
               "--scheduling-policy", "fcfs", "--enforce-eager", "--no-enable-prefix-caching"]
    if args.legacy_api:
        command.extend(["--middleware", "rmwbench.vllm_control.txserve_lock_manager_middleware"])
        environment["TXSERVE_MAX_BYPASSES"] = "8"
        environment["TXSERVE_EXPERIMENT_CONTROL"] = "1"
    if args.legacy_engine:
        command.extend(["--middleware", "rmwbench.vllm_control.txserve_control_middleware",
                        "--scheduler-cls", "rmwbench.vllm_scheduler.ConflictGraphTxServeScheduler"])
    os.execvpe(command[0], command, environment)


if __name__ == "__main__":
    main()
