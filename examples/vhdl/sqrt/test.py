import os
import subprocess
import sys
from pathlib import Path

from xeda import Design, Flow
from xeda.flow_runner import DefaultRunner
from xeda.flows import GhdlSim

SCRIPT_DIR = Path(__file__).parent.resolve()

subprocess.check_call(
    [
        sys.executable,
        "-m",
        "pip",
        "install",
        "-U",
        "-r",
        SCRIPT_DIR / "tb-requirements.txt",
    ]
)

design = Design.from_toml(SCRIPT_DIR / "sqrt.toml")
xeda_runner = DefaultRunner()


def test_sqrt() -> None:
    LONG_TEST = [8, 9, 16, 17, 18, 20, 21, 31, 32, 63, 64, 66]
    QUICK_TEST = [8, 32]
    ws = QUICK_TEST
    if os.environ.get("LONG_TEST"):
        print("runing long test!")
        ws = LONG_TEST
    for w in ws:
        assert design.tb
        design.tb.parameters = {**design.tb.parameters, "G_IN_WIDTH": w}
        f: Flow = xeda_runner.run_flow(GhdlSim, design)
        assert f.succeeded, f"test failed for w={w}"
