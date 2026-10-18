"""What each netlist, checkpoint and database `SourceType` is.

A member is one row: the container (the file format that a reader parses), the abstraction (what
the cells are, or the stage of a physical result) and the target kind (which cell library the file
instantiates). The concrete target stays in the shared settings: `platform`, `corner` and `liberty`
for standard cells, `fpga` for FPGA primitives. A member is split only where a pair is invalid by
design (a netlist of yosys cells cannot go to a tool that reads a Liberty library).

The triples are unique, so a description of an artifact by these three values names exactly one
member. The table only describes. Flows still declare `SourceType` members, and the one edge
predicate (`flow_runner.chains.fitting_outputs`) still compares them. `tests/test_artifact_kinds.py`
requires a row for each member and checks the table against the declarations of the flows.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .design import SourceType

__all__ = [
    "Abstraction",
    "ArtifactKind",
    "Container",
    "KINDS",
    "TargetKind",
    "member_for",
]


class Container(str, Enum):
    """The file format that a reader parses."""

    VERILOG = "verilog"
    VHDL = "vhdl"
    JSON = "json"
    EDIF = "edif"
    BLIF = "blif"
    VIVADO_CHECKPOINT = "vivado_checkpoint"
    OPENROAD_DATABASE = "openroad_database"


class Abstraction(str, Enum):
    """What the cells are, or the stage of a physical result.

    `generic`: cells of the tool's own library, so there is no target. `mapped`: cells of a target
    library. The stages are those of a checkpoint or a database. `unspecified` is an explicit
    unknown of a member that no flow declares, and never a wildcard."""

    GENERIC = "generic"
    MAPPED = "mapped"
    PLACED = "placed"
    CLOCK_TREE = "clock_tree"
    GLOBAL_ROUTED = "global_routed"
    ROUTED = "routed"
    UNSPECIFIED = "unspecified"


class TargetKind(str, Enum):
    """Which cell library the file instantiates.

    `standard_cells`: a Liberty library. `fpga_primitives`: the primitives of an FPGA family.
    `fpga_timing_primitives`: the timing-simulation primitives of an FPGA family, which carry
    delays and differ from the functional library. `none`: the cells of the tool itself."""

    NONE = "none"
    STANDARD_CELLS = "standard_cells"
    FPGA_PRIMITIVES = "fpga_primitives"
    FPGA_TIMING_PRIMITIVES = "fpga_timing_primitives"
    UNSPECIFIED = "unspecified"


@dataclass(frozen=True)
class ArtifactKind:
    """The row of one member."""

    container: Container
    abstraction: Abstraction
    target: TargetKind
    #: One sentence, shown in the documentation.
    meaning: str

    @property
    def triple(self) -> tuple[Container, Abstraction, TargetKind]:
        """The key that names the member: it is unique in `KINDS`."""
        return (self.container, self.abstraction, self.target)


def _row(
    container: Container, abstraction: Abstraction, target: TargetKind, meaning: str
) -> ArtifactKind:
    return ArtifactKind(container, abstraction, target, meaning)


_V, _A, _T = Container, Abstraction, TargetKind

KINDS: dict[SourceType, ArtifactKind] = {
    SourceType.VerilogNetlist: _row(
        _V.VERILOG,
        _A.MAPPED,
        _T.STANDARD_CELLS,
        "A Verilog netlist mapped to the cells of a Liberty library.",
    ),
    SourceType.GenericVerilogNetlist: _row(
        _V.VERILOG,
        _A.GENERIC,
        _T.NONE,
        "A Verilog netlist of the cells of yosys (gates, LUTs or coarse cells). No Liberty "
        "library holds them.",
    ),
    SourceType.JsonNetlist: _row(
        _V.JSON,
        _A.MAPPED,
        _T.FPGA_PRIMITIVES,
        "The JSON netlist that the family pass of yosys writes, with the primitives of an FPGA "
        "family.",
    ),
    SourceType.Edif: _row(
        _V.EDIF,
        _A.MAPPED,
        _T.FPGA_PRIMITIVES,
        "An EDIF netlist of the primitives of an FPGA family.",
    ),
    SourceType.FpgaNetlist: _row(
        _V.VERILOG,
        _A.MAPPED,
        _T.FPGA_PRIMITIVES,
        "The functional Verilog netlist of FPGA primitives that Vivado writes.",
    ),
    SourceType.FpgaTimingNetlist: _row(
        _V.VERILOG,
        _A.MAPPED,
        _T.FPGA_TIMING_PRIMITIVES,
        "The timing Verilog netlist of FPGA primitives that Vivado writes. A simulation reads it "
        "with an SDF file.",
    ),
    SourceType.SynthCheckpoint: _row(
        _V.VIVADO_CHECKPOINT,
        _A.MAPPED,
        _T.FPGA_PRIMITIVES,
        "A Vivado checkpoint after synthesis.",
    ),
    SourceType.RoutedCheckpoint: _row(
        _V.VIVADO_CHECKPOINT,
        _A.ROUTED,
        _T.FPGA_PRIMITIVES,
        "A Vivado checkpoint after routing.",
    ),
    SourceType.OpenroadDbPlaced: _row(
        _V.OPENROAD_DATABASE,
        _A.PLACED,
        _T.STANDARD_CELLS,
        "The OpenROAD database after placement.",
    ),
    SourceType.OpenroadDbCts: _row(
        _V.OPENROAD_DATABASE,
        _A.CLOCK_TREE,
        _T.STANDARD_CELLS,
        "The OpenROAD database after clock tree synthesis.",
    ),
    SourceType.OpenroadDbGlobalRouted: _row(
        _V.OPENROAD_DATABASE,
        _A.GLOBAL_ROUTED,
        _T.STANDARD_CELLS,
        "The OpenROAD database after global routing.",
    ),
    SourceType.OpenroadDbRouted: _row(
        _V.OPENROAD_DATABASE,
        _A.ROUTED,
        _T.STANDARD_CELLS,
        "The OpenROAD database of the routed design.",
    ),
    SourceType.VhdlNetlist: _row(
        _V.VHDL,
        _A.UNSPECIFIED,
        _T.UNSPECIFIED,
        "A VHDL netlist. No flow declares it.",
    ),
    SourceType.Blif: _row(
        _V.BLIF,
        _A.UNSPECIFIED,
        _T.UNSPECIFIED,
        "A BLIF netlist. No flow declares it.",
    ),
    SourceType.Checkpoint: _row(
        _V.VIVADO_CHECKPOINT,
        _A.UNSPECIFIED,
        _T.UNSPECIFIED,
        "A Vivado checkpoint of no stated stage. No flow takes it.",
    ),
    SourceType.Odb: _row(
        _V.OPENROAD_DATABASE,
        _A.UNSPECIFIED,
        _T.UNSPECIFIED,
        "An OpenROAD database of no stated stage. No flow takes it.",
    ),
}


def member_for(container: Container, abstraction: Abstraction, target: TargetKind) -> SourceType:
    """Return the member that the triple names. Raise `KeyError` when there is none."""
    for member, kind in KINDS.items():
        if kind.triple == (container, abstraction, target):
            return member
    raise KeyError((container, abstraction, target))
