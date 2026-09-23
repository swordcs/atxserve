from __future__ import annotations
import argparse
import json
from pathlib import Path
from .config import Config
from .runner import run_benchmark

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='rmwbench', description='Run the dependency-free RMWBench.')
    parser.add_argument('--config', required=True, type=Path, help='TOML config path')
    parser.add_argument('--output', type=Path, default=Path('results/latest'), help='artifact directory')
    parser.add_argument('--quiet', action='store_true', help='do not print the summary')
    parser.add_argument('--dump-workspace', action='store_true', help='write inspectable initial and final workspace files outside timing')
    return parser

def main() -> None:
    args = _parser().parse_args()
    artifacts = run_benchmark(Config.load(args.config), args.output, dump_workspace=args.dump_workspace)
    if not args.quiet:
        print(json.dumps(artifacts.summary, indent=2, sort_keys=True))
if __name__ == '__main__':
    main()
