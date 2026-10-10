"""Lightweight names for flow choices used before flow models load."""

from __future__ import annotations

import sys

# Keep this list independent of flow imports. The runtime parity test ensures that model
# discovery and this completion catalog stay in sync.
_BUILTIN_FLOWS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("bsc", ()),
    ("bsc_sim", ()),
    ("dc", ()),
    ("diamond_synth", ()),
    ("fpga_pack", ()),
    ("ghdl_sim", ("ghdl",)),
    ("ghdl_synth", ()),
    ("ise_synth", ()),
    ("modelsim", ()),
    ("nextpnr", ()),
    ("nvc", ()),
    ("openfpgaloader", ()),
    ("openroad", ()),
    ("quartus", ()),
    ("vcs", ()),
    ("verilator", ()),
    ("vivado_alt_synth", ()),
    ("vivado_impl", ()),
    ("vivado_postsynth_sim", ()),
    ("vivado_power", ()),
    ("vivado_project", ()),
    ("vivado_sim", ()),
    ("vivado_synth", ()),
    ("yosys", ()),
    ("yosys_fpga", ()),
    ("yosys_sim", ()),
)


def flow_names() -> list[str]:
    """Return canonical flow names and aliases without importing flow models."""
    names = {name for canonical, aliases in _BUILTIN_FLOWS for name in (canonical, *aliases)}

    flow_module = sys.modules.get("xeda.flow.flow")
    registered_flows = getattr(flow_module, "registered_flows", {})
    for _, flow_class in registered_flows.values():
        names.add(flow_class.name)
        names.update(flow_class.aliases)

    return sorted(names)


__all__ = ["flow_names"]
