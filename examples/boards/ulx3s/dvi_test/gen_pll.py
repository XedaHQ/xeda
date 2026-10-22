#!/usr/bin/env python3
import logging
from pathlib import Path

from xeda.flows.nextpnr import EcpPLL

log = logging.getLogger(__name__)

log.root.setLevel(logging.DEBUG)

f_pixel = 25.0

if __name__ == "__main__":
    # ecppll writes the Verilog module beside this script
    EcpPLL(clkouts=[600]).generate(cwd=Path(__file__).resolve().parent)  # type: ignore
