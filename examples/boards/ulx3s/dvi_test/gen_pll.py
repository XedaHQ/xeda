#!/usr/bin/env python3
import logging
from xeda.flows.nextpnr import EcpPLL

log = logging.getLogger(__name__)

log.root.setLevel(logging.DEBUG)

f_pixel = 25.0

if __name__ == "__main__":
    EcpPLL(clkouts=[600]).generate()  # type: ignore
