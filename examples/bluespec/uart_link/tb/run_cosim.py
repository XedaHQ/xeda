#!/usr/bin/env python3
"""Run the full cocotb suite of the LeanLink core through Xeda (it takes several minutes).

The suite is every test of the `lean_*.py` modules, on the link with its test endpoint at W = 8
and W = 1 (the design `cosim.yaml` and its target `w1`), and on the bare cores (the targets
`core_w8` and `core_w1`). The random stress test (`lean_stress.py`) runs `--seeds` seeds. The
tests that the continuous integration runs are a subset of these.

    python3 examples/bluespec/uart_link/tb/run_cosim.py [--seeds 100] [--jobs 4] [--run-root DIR]

Each target is one Xeda run with its own run root under DIR, and the runs go in parallel. The
script exits with 0 only if every run passes. It needs `xeda` on the PATH, and bsc, Verilator and
cocotb as `tb/cosim.yaml` says.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
DESIGN = HERE / "cosim.yaml"
TARGETS = (None, "w1", "core_w8", "core_w1")


def run(job: tuple[str | None, int, Path]) -> tuple[str, int, str]:
    target, seeds, run_root = job
    name = target or "w8"
    cmd = [
        "xeda",
        "run",
        "verilator",
        str(DESIGN),
        "--run-root",
        str(run_root / name),
        "--rebuild-all",
    ]
    if target:
        cmd += ["--target", target]
    env = dict(os.environ, LEAN_SEEDS=str(seeds))
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    match = re.search(r"TESTS=(\d+) PASS=(\d+) FAIL=(\d+) SKIP=(\d+)", out)
    summary = (
        "no result line (build error?)"
        if not match
        else ("tests {}, pass {}, fail {}, skip {}".format(*match.groups()))
    )
    (run_root / f"{name}.log").write_text(out)
    return name, proc.returncode, summary


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--seeds", type=int, default=100, help="seeds of the stress test")
    ap.add_argument("--jobs", type=int, default=4, help="runs in parallel")
    ap.add_argument("--run-root", type=Path, help="directory of the runs and logs")
    args = ap.parse_args()
    run_root = args.run_root or Path(tempfile.mkdtemp(prefix="uart_link_cosim_"))
    run_root.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(args.jobs) as pool:
        results = list(pool.map(run, [(t, args.seeds, run_root) for t in TARGETS]))
    for name, code, summary in results:
        print(f"{name:8s} {'ok  ' if code == 0 else 'FAIL'} {summary}")
    print(f"logs: {run_root}")
    return 1 if any(code for _, code, _ in results) else 0


if __name__ == "__main__":
    sys.exit(main())
