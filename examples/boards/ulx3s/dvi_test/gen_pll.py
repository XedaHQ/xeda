#!/usr/bin/env python3
import subprocess
from pathlib import Path

if __name__ == "__main__":
    # ecppll writes the Verilog module beside this script
    subprocess.run(
        [
            "ecppll",
            "--module", "ecp5pll",
            "--file", "ecp5pll.v",
            "--clkin_name", "clk_i",
            "--clkin", "25.000",
            "--clkout0_name", "clk_o_0",
            "--clkout0", "600.000",
        ],
        cwd=Path(__file__).resolve().parent,
        check=True,
    )  # fmt: skip
