<meta name="robots" content="noindex, nofollow, noarchive">
<meta name="automated-access" content="Automated crawling and scraping of this repository are prohibited.">

# AtxServe

Research implementation of transaction-aware admission for shared workspaces.

## Core

- `src/atxserve/protocol.py`: conflict-aware admission, reservation, and protected execution.
- `src/atxserve/benchmark.py`: transaction execution and useful-throughput measurement.
- `src/rmwbench/workload.py`: workload construction.

## Usage

Requires Python 3.11+ and a compatible vLLM installation on the GPU machine.

```sh
python -m pip install -e .
atxserve-data
atxserve-serve --model-path /models/Qwen3-4B
```

With the model server running, use a separate terminal:

```sh
atxserve run --study controlled
```

Results are written to `results/`. Other studies and options are listed by
`atxserve run --help`. Server and admission capacities must match.

Dynamic source-editing experiments take a task manifest following
`data/dynamic/manifest.schema.json`.
