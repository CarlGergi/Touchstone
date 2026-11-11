"""Runs the experiments and writes results/results.json: python -m touchstone.experiments."""

from __future__ import annotations

import argparse
from pathlib import Path

from touchstone.experiments import engine_bench, results

EXPERIMENTS = {
    "engine": engine_bench.run,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m touchstone.experiments")
    parser.add_argument("--data-dir", type=Path, default=Path("data-server"))
    parser.add_argument("--only", choices=sorted(EXPERIMENTS), help="run one experiment")
    args = parser.parse_args(argv)
    for name, experiment in EXPERIMENTS.items():
        if args.only in (None, name):
            results.update(name, experiment(args.data_dir))
    results.update("headline", results.headline())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
