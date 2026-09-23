from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", choices=["babel", "jinja", "marshmallow"], nargs="+")
    args = parser.parse_args()
    manifest = json.loads((ROOT / "data/commit_focus/dataset_manifest.json").read_text(encoding="utf-8"))
    for name, spec in manifest["repositories"].items():
        if args.repository and name not in args.repository:
            continue
        destination = ROOT / "data/commit0" / name
        if destination.exists():
            head = subprocess.check_output(["git", "-C", str(destination), "rev-parse", "HEAD"], text=True).strip()
            dirty = subprocess.check_output(["git", "-C", str(destination), "status", "--porcelain"], text=True)
            if head != spec["anchor_commit"] or dirty:
                raise RuntimeError(f"Existing repository is not the clean pinned input: {destination}")
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--no-checkout", spec["url"], str(destination)], check=True)
        subprocess.run(["git", "-C", str(destination), "checkout", "--detach", spec["anchor_commit"]], check=True)


if __name__ == "__main__":
    main()
