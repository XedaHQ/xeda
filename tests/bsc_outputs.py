"""Read the Verilog files that a `bsc` run collected, from its declared `design` output."""

from __future__ import annotations

from typing import Any


def collected_verilog(flow: Any) -> list[str]:
    """Return the Verilog files of the derived design of `flow`, in the order of its sources.

    They are the file that defines the reset macro, the modules of the bsc library that the top
    module needs, the Verilog sources of the design itself and the modules that bsc generated."""
    record = flow.results["outputs"]["design"]
    return [
        str(flow.design.rtl.sources[item["source"]].file) if "source" in item else item["generated"]
        for item in record["sources"]
        if item.get("type") == "Verilog"
    ]
