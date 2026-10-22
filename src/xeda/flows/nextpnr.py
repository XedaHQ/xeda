import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Any, Dict, List, Literal, Optional, Tuple, Union
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from importlib_resources import files

from ..board import FPGA_OR_BOARD_REQUIRED, WithFpgaBoardSettings
from ..dataclass import WORKING, Field, XedaBaseModel, deliverable, field_validator
from ..design import SourceType
from ..flow import (
    Flow,
    FlowFatalError,
    FlowSettingsException,
    FpgaSynthFlow,
    In,
    Out,
    MetricSpec,
    describe_results,
)
from ..tool import NonZeroExitCode, Tool
from ..run_dir import RunDirectory
from ..units import count, ratio
from ..utils import replacing_file, setting_flag
from .nextpnr_constraints import ClockUse, Constraints, merge_constraints, reconcile_clocks
from .xilinx import (
    XilinxLayout,
    XilinxSelection,
    chipdb_identity,
    find_xilinx_layout,
    locate_chipdb,
    prepare_chipdb,
    select_xilinx,
)

__all__ = ["Nextpnr"]

log = logging.getLogger(__name__)


#: The number of error lines from the nextpnr log that an unclassified failure quotes.
ERRORS_SHOWN = 5


@dataclass(frozen=True)
class _BoardFallback:
    """The location of a board's pin file, as `Nextpnr._locate_board_input` finds it."""

    path: Optional[Path] = None
    url: Optional[str] = None
    #: The content of a packaged board file that is not on the file system. It is copied into the
    #: cache.
    content: Optional[bytes] = None


class NextpnrTool(Tool):
    """The nextpnr executable, which prints its version banner to stderr.

    The banner looks like::

        "nextpnr-ecp5" -- Next Generation Place and Route (Version nextpnr-0.11.1-3-g930fef44)
        "nextpnr-himbaechel" -- Next Generation Place and Route (Version 1.0.0-7-g334d1b18)

    The generic patterns in `Tool` do not match this banner, so the reported version would be
    empty without `version_regexps`. `Tool` already tries again with stderr included.
    """

    version_regexps: List[Union[re.Pattern, str]] = [
        re.compile(r"Version\s+(?:nextpnr-)?(?P<version>\d+(?:\.\d+)*)")
    ]


#: The number of decimal places that slack is rounded to for reporting. The unrounded value
#: decides pass or fail.
_SLACK_DECIMALS = 6

#: Canonical resource name -> the nextpnr bel types that implement it, for ECP5.
#:
#: Checked against nextpnr-ecp5 0.11.1 from the OSS CAD Suite. `TRELLIS_COMB` is one LUT4 and
#: `TRELLIS_FF` is one flip-flop, so each maps to a count. Older nextpnr versions reported
#: `TRELLIS_SLICE` (two LUT4s and two flip-flops for each slice). That count cannot be split, so
#: it is reported as `slice`, and `lut` and `ff` stay unset.
ECP5_RESOURCES: Dict[str, Tuple[str, ...]] = {
    "lut": ("TRELLIS_COMB",),
    "ff": ("TRELLIS_FF",),
    "slice": ("TRELLIS_SLICE",),
    "bram": ("DP16KD",),
    "dsp": ("MULT18X18D", "ALU54B"),
    "io": ("TRELLIS_IO",),
}

#: Canonical resource name -> the bel types that implement it, for Xilinx 7-series
#: (nextpnr-himbaechel 1.0.0, uarch `xilinx`). `lut` is not in the table. `SLICE_LUTX` counts 5LUT
#: and 6LUT positions, two for each physical LUT, so `lut` comes from the placement dump
#: (`xilinx_lut_locations`). Block RAM counts primitives. `CARRY4` stays a raw count.
XILINX_RESOURCES: dict[str, tuple[str, ...]] = {
    "ff": ("SLICE_FFX",),
    "bram": ("RAMB18E1", "RAMB36E1"),
    "dsp": ("DSP48E1",),
    "io": ("PAD",),
}

#: How each family's canonical `lut` is counted (`lut_basis.method`). The stage
#: (`lut_basis.stage`) is always this flow's.
LUT_STAGE = "placed and routed"
LUT_METHODS = {
    "ecp5": "TRELLIS_COMB bels in nextpnr's utilization report",
    "xilinx": "unique occupied LUT locations (tile, site, A-D) in nextpnr's placement dump",
}

#: The nextpnr architectures for which this flow has a tested mapping of device, constraints and
#: output. `xilinx` stands for every 7-series family (`Nextpnr.io_family`).
NEXTPNR_FAMILIES = ("ecp5", "ice40", "nexus", "xilinx")

#: The executable of an architecture whose executable is not `nextpnr-<architecture>`.
EXECUTABLES = {"xilinx": "nextpnr-himbaechel"}

#: The time limit in seconds for the download of a board's pin constraints. It applies to the
#: connection and to each read, and not to the whole transfer. A stalled server fails the run and
#: does not hold the lock of the run directory.
BOARD_FILE_TIMEOUT_S = 60

_XILINX_LUT_BEL = re.compile(r"[A-D][56]LUT")


def xilinx_lut_locations(placement: Any) -> tuple[int, int]:
    """Count the LUT cells of a placement dump and the physical LUTs that they occupy.

    The dump is the file that nextpnr-himbaechel writes with `-o placement=`: a mapping from each
    cell to its tile, site, bel and bel type. A 7-series LUT has a 5LUT and a 6LUT position
    (`A5LUT`, `A6LUT`, ...). A fractured pair, a `LUT6_2` and a LUT that nextpnr inserted beside
    a carry input share one physical LUT. A RAM or shift-register LUT occupies its position like
    any other LUT. Return the number of placed LUT cells (positions) and the number of distinct
    `(tile, site, letter)` locations. Raise `ValueError` for a dump of another shape.
    """
    if not isinstance(placement, dict):
        raise ValueError("it is not a mapping of cells")
    cells = 0
    locations = set()
    for cell, where in placement.items():
        if not isinstance(where, dict) or not isinstance(where.get("type"), str):
            raise ValueError(f"cell {cell!r} has no bel type")
        if where["type"] != "SLICE_LUTX":
            continue
        tile, site, bel = (where.get(key) for key in ("tile", "site", "bel"))
        if not (isinstance(tile, str) and tile and isinstance(site, str) and site):
            raise ValueError(f"LUT cell {cell!r} has no tile or site")
        if not isinstance(bel, str) or not _XILINX_LUT_BEL.fullmatch(bel):
            raise ValueError(f"LUT cell {cell!r} is on an unknown bel {bel!r}")
        cells += 1
        locations.add((tile, site, bel[0]))
    return cells, len(locations)


#: The device names that nextpnr-ice40 takes as `--<device>`.
ICE40_DEVICES: Tuple[str, ...] = (
    ("lp384", "lp1k", "lp4k", "lp8k")
    + ("hx1k", "hx4k", "hx8k")
    + ("up3k", "up5k", "u1k", "u2k", "u4k")
)

#: Package names that nextpnr-ice40 spells differently from the ordering code.
ICE40_PACKAGE_NAMES = {"swg16": "swg16tr"}

#: A device name as nextpnr-nexus spells it (see `nextpnr-nexus --list-devices`).
NEXUS_DEVICE = re.compile(r"(LIFCL|LFD2NX)-\d+-\d[A-Z]+\d+[CI](ES2?)?", re.IGNORECASE)

#: Settings that apply to only some nextpnr architectures, each with the architectures that take it.
FAMILY_SETTINGS: Dict[str, Tuple[str, ...]] = {
    "lpf_allow_unconstrained": ("ecp5",),
    "out_of_context": ("ecp5",),
    "disable_router_lutperm": ("ecp5",),
    "override_basecfg": ("ecp5",),
    "allow_fabric_eclk": ("ecp5",),
    "no_promote_globals": ("ecp5", "ice40"),
    "pcf_allow_unconstrained": ("ice40",),
    "opt_timing": ("ice40",),
    "promote_logic": ("ice40",),
    "no_promote_ce": ("ice40",),
    "no_pack_lutff": ("nexus",),
    "no_post_place_opt": ("nexus",),
    "carry_lutff_ratio": ("nexus",),
    "estimate_delay_mult": ("nexus",),
    "chipdb": ("xilinx",),
    "prjxray_db": ("xilinx",),
    "placement": ("xilinx",),
    "delay_matrix": ("xilinx",),
    "hold_fix": ("xilinx",),
    "hold_detour_max": ("xilinx",),
}

#: Settings of the Xilinx backend. nextpnr takes them as `-o name=value` options.
XILINX_SETTINGS = tuple(name for name, families in FAMILY_SETTINGS.items() if "xilinx" in families)


class EcpPLL(Tool):
    class Clock(XedaBaseModel):
        name: Optional[str] = None
        mhz: float = Field(gt=0, description="Frequency in MHz.")
        phase: Optional[float] = None

    executable: str = "ecppll"
    module: str = "ecp5pll"
    reset: bool = False
    standby: bool = False
    highres: bool = False
    internal_feedback: bool = False
    internal_feedback_wake: bool = False
    clkin: Union[Clock, float] = 25.0
    clkouts: List[Union[Clock, float]]
    file: Optional[str] = None

    @classmethod
    def _fix_clock(cls, clock: Union[Clock, float], out_clk: Optional[int] = None) -> Clock:
        if isinstance(clock, float):
            clock = cls.Clock(mhz=clock)
        else:
            # Pydantic v2 retains nested model instances. Copy before assigning the generated
            # name so construction and assignment never mutate or retain a caller-owned clock.
            clock = clock.model_copy(deep=True)
        if not clock.name:
            clock.name = "clk_i" if out_clk is None else f"clk_o_{out_clk}"
        return clock

    @field_validator("clkin")
    @classmethod
    def _validate_clockin(cls, value):
        return cls._fix_clock(value)

    @field_validator("clkouts")
    @classmethod
    def _validate_clockouts(cls, value):
        new_value = []
        for i, v in enumerate(value):
            new_value.append(cls._fix_clock(v, i))
        return new_value

    @field_validator("file")
    @classmethod
    def _validate_outfile(cls, value, info):
        values = info.data if isinstance(info.data, dict) else {}
        if not value:
            # Use `.get`: `info.data` holds only the fields that validated, so a bad `module`
            # would raise a bare `KeyError` here and hide its own error.
            module = values.get("module")
            if module:
                value = module + ".v"
        return value

    def generate(self):
        def s(f: float) -> str:
            return f"{f:0.03f}"

        args = setting_flag(self.module)
        args += setting_flag(self.file)
        args += ["--clkin_name", self.clkin.name]  # type: ignore # pylint: disable=no-member
        args += ["--clkin", s(self.clkin.mhz)]  # type: ignore # pylint: disable=no-member
        if not 1 <= len(self.clkouts) <= 4:
            raise ValueError("At least 1 and at most 4 output clocks can be specified")
        for i, clk in enumerate(self.clkouts):
            assert isinstance(clk, self.Clock) and clk.name
            args += [f"--clkout{i}_name", clk.name]
            args += [f"--clkout{i}", s(clk.mhz)]
            if clk.phase:
                if i > 0:
                    args += [f"--phase{i}", s(clk.phase)]
                else:
                    raise ValueError("First output clock cannot have a phase difference!")
        args += setting_flag(self.highres)
        args += setting_flag(self.standby)
        args += setting_flag(self.internal_feedback)
        self.run(*args)


class Nextpnr(FpgaSynthFlow):
    """Place and route an FPGA design with nextpnr.

    The input `netlist` is a `JsonNetlist` design source or, by default, the netlist that
    `yosys_fpga` produces. The flow runs the nextpnr variant for `fpga.family`: `nextpnr-ecp5`,
    `nextpnr-ice40`, `nextpnr-nexus`, or openXC7's `nextpnr-himbaechel` for Xilinx 7-series
    (Artix, Kintex, Spartan, Virtex, Zynq). It then parses the JSON report of nextpnr for the
    achieved frequency, the slack and the resource utilization.

    A 7-series part needs the full part name (`xc7a100tcsg324-1`) and pin constraints (typed
    `Xdc` sources, or the pin constraints of a board). The chip database is generated once for
    each run root, unless `chipdb` names one. Canonical resource names (`lut`, `ff`, ...) are
    mapped for ECP5 and 7-series. Other families report raw bel-type counts. `lut` is counted at
    this stage by the method that `lut_basis.method` names. For 7-series it is the number of
    occupied physical LUTs, read from the placement dump. It is not certified comparable with the
    count of another toolchain. `fmax` is the timing of nextpnr itself: compare it only with other
    nextpnr runs. A family without a tested mapping of device, constraints and output is refused before
    any producer runs.
    """

    executables = tuple(EXECUTABLES.get(family, f"nextpnr-{family}") for family in NEXTPNR_FAMILIES)

    required_settings = {"fpga": FPGA_OR_BOARD_REQUIRED}

    #: Whether every constrained clock domain met its constraint, as the last
    #: `parse_reports()` found it. None if no report was parsed.
    _timing_met: Optional[bool] = None

    #: The design sources that this flow is judged on: a JSON netlist, the pin files and the SDC
    #: files. The HDL that `yosys_fpga` reads is an input of that flow only.
    design_source_types = frozenset(
        {
            SourceType.JsonNetlist,
            SourceType.Lpf,
            SourceType.Pcf,
            SourceType.Pdc,
            SourceType.Xdc,
            SourceType.Sdc,
        }
    )

    results_description = describe_results(
        "fmax",
        "wns",
        "clock_frequency",
        "clock_period",
        "clock_domains",
        "timing_met",
        "lut",
        "ff",
        "slice",
        "bram",
        "dsp",
        "io",
        **{
            "lut_basis.stage": MetricSpec(
                "The stage that the canonical `lut` count describes.", "text"
            ),
            "lut_basis.method": MetricSpec(
                "The method that gave the canonical `lut` count at that stage.", "text"
            ),
            "clock_port": MetricSpec(
                "Top-level port of the only reported clock domain, when that is certain.", "text"
            ),
            "device": MetricSpec("Xilinx 7-series: the part placed and routed.", "text"),
            "fabric": MetricSpec(
                "Xilinx 7-series: the die that nextpnr routed. Every `available` total "
                "describes this die (an xc7a35t is routed as an xc7a50t).",
                "text",
            ),
        },
    )

    class Settings(WithFpgaBoardSettings):
        removed_settings = {
            **WithFpgaBoardSettings.removed_settings,
            **{
                f"{kind}_cfg": f"`rtl.sources` with a typed pin file, in YAML "
                f"`{{file: pins.{kind}, type: {kind.capitalize()}}}`"
                for kind in ("lpf", "pcf", "pdc")
            },
            "yosys": "`flows.yosys_fpga.<key>`",
        }
        seed: Optional[int] = Field(
            None,
            description="Seed for nextpnr's placer. Different seeds give different results. "
            "A sweep over seeds is a common way to find a higher Fmax.",
            json_schema_extra=count(),
        )
        randomize_seed: bool = Field(
            False,
            description="Use a new random seed on every run. The results are not reproducible, "
            "so the flow runs again every time. Set `seed` to pin a seed.",
        )
        timing_allow_fail: bool = Field(
            False,
            description="Let the flow succeed even when timing constraints are not met.",
        )
        ignore_loops: bool = Field(
            False, description="Ignore combinational loops in timing analysis."
        )

        textcfg: Path = Field(
            Path("config.txt"),
            description="ECP5 configuration file in Trellis text format, which the bitstream "
            "packer (`fpga_pack`) reads. Always written, except by an `out_of_context` run, "
            "which writes none.",
            json_schema_extra=deliverable(),
        )
        asc: Path = Field(
            Path("config.asc"),
            description="iCE40 ASCII configuration written with `--asc`. Always written.",
            json_schema_extra=deliverable(),
        )
        fasm: Path = Field(
            Path("config.fasm"),
            description="Nexus or Xilinx 7-series FASM configuration. Always written.",
            json_schema_extra=deliverable(),
        )

        @field_validator("textcfg", "asc", "fasm", mode="before")
        @classmethod
        def _configuration_is_always_written(cls, value):
            """Refuse an empty value, because the configuration has no switch.

            The configuration is the declared output `config` of nextpnr. It is written in the
            same way whether nextpnr is requested or runs as the producer of a packer, so one
            run serves both. The setting only names the file and gives its delivery location."""
            if value is None or (isinstance(value, (str, Path)) and not str(value).strip()):
                raise ValueError(
                    "nextpnr always writes its configuration; give a file name or leave the "
                    "default"
                )
            return value

        out_of_context: bool = Field(
            False,
            description="Disable IO buffer insertion and global promotion and routing. Use it to "
            "build pre-routed blocks.",
        )
        lpf_allow_unconstrained: bool = Field(
            False,
            description="Do not require the LPF files to constrain all IOs.",
        )
        pcf_allow_unconstrained: bool = Field(
            False, description="Allow iCE40 IOs without a PCF pin assignment."
        )
        placer: Optional[str] = Field(
            None, description="nextpnr placer (`heap`, `sa`, or a backend-specific choice)."
        )
        router: Optional[str] = Field(None, description="nextpnr router (`router1` or `router2`).")
        cstrweight: Optional[float] = Field(
            None, description="Placer constraint weight.", json_schema_extra=ratio()
        )
        starttemp: Optional[float] = Field(
            None,
            description="Simulated-annealing starting temperature.",
            json_schema_extra=ratio(),
        )
        placer_heap_alpha: Optional[float] = Field(
            None, description="HeAP placer alpha.", json_schema_extra=ratio()
        )
        placer_heap_beta: Optional[float] = Field(
            None, description="HeAP placer density limit.", json_schema_extra=ratio()
        )
        placer_heap_critexp: Optional[int] = Field(
            None, description="HeAP placer criticality exponent.", json_schema_extra=ratio()
        )
        placer_heap_timingweight: Optional[int] = Field(
            None, description="HeAP placer timing weight.", json_schema_extra=ratio()
        )
        tmg_ripup: bool = Field(
            False, description="Enable experimental timing-driven router ripup."
        )
        no_tmdriv: bool = Field(False, description="Disable timing-driven placement.")
        ignore_rel_clk: bool = Field(False, description="Ignore clock-to-clock timing relations.")
        sdc: Optional[Path] = Field(
            None,
            description="SDC timing file appended after typed Sdc design sources. "
            "Clocks must not duplicate those in pin files, other SDC files, or flow settings.",
        )
        pre_pack: Optional[Path] = Field(None, description="Python hook before packing.")
        pre_place: Optional[Path] = Field(None, description="Python hook before placement.")
        pre_route: Optional[Path] = Field(None, description="Python hook before routing.")
        post_route: Optional[Path] = Field(None, description="Python hook after routing.")
        no_promote_globals: bool = Field(
            False, description="Disable global signal promotion (ECP5/iCE40)."
        )
        opt_timing: bool = Field(
            False, description="Enable experimental iCE40 post-placement timing optimization."
        )
        disable_router_lutperm: bool = Field(
            False, description="Disable ECP5 router LUT input permutation."
        )
        override_basecfg: Optional[Path] = Field(
            None, description="ECP5 Trellis base configuration override."
        )
        allow_fabric_eclk: bool = Field(
            False, description="Allow ECP5 ECLK routing in general fabric."
        )
        promote_logic: bool = Field(
            False, description="Promote iCE40 logic globals as well as clocks."
        )
        no_promote_ce: bool = Field(
            False, description="Disable iCE40 clock-enable global promotion."
        )
        no_pack_lutff: bool = Field(False, description="Disable Nexus LUT/FF clustering.")
        no_post_place_opt: bool = Field(
            False, description="Disable Nexus post-placement repacking."
        )
        carry_lutff_ratio: Optional[float] = Field(
            None,
            description="Nexus carry-chain LUT/FF clustering ratio.",
            json_schema_extra=ratio(),
        )
        estimate_delay_mult: Optional[float] = Field(
            None, description="Nexus estimated-delay multiplier.", json_schema_extra=ratio()
        )
        router2_alt_weights: bool = Field(
            False, description="Use alternative router2 congestion weights."
        )
        placer_heap_no_ctrl_set: bool = Field(
            False, description="Disable control-set awareness in HeAP."
        )
        extra_args: List[str] = Field(
            [], description="Extra command-line arguments appended to the nextpnr invocation."
        )
        py_script: Optional[Path] = Field(
            None,
            description="Python script run inside nextpnr (`--run`), for custom constraints or "
            "analysis. Requires a nextpnr built with Python support.",
        )
        write: Optional[Path] = Field(
            None,
            description="Write the post-routing design to this JSON file.",
            json_schema_extra=deliverable("outputs/{design}_routed.json"),
        )
        sdf: Optional[Path] = Field(
            None,
            description="Write post-routing timing to this SDF file, for timing-annotated "
            "netlist simulation.",
            json_schema_extra=deliverable("outputs/{design}.sdf"),
        )
        log: Optional[Path] = Field(
            Path("nextpnr.log"),
            description="File nextpnr writes its log to.",
            json_schema_extra=WORKING,
        )
        report: Optional[Path] = Field(
            Path("report.json"),
            description="File that nextpnr writes its JSON report of utilization and timing to. "
            "The flow reads its results from this file.",
            json_schema_extra=deliverable(),
        )
        detailed_timing_report: bool = Field(
            False,
            description="Ask nextpnr for a detailed timing report for each path. This report is "
            "unstable and can crash nextpnr.",
        )
        placed_svg: Optional[Path] = Field(
            None,
            description="Render the placed design to this SVG file.",
            json_schema_extra=deliverable("outputs/{design}_placed.svg"),
        )
        routed_svg: Optional[Path] = Field(
            None,
            description="Render the routed design to this SVG file.",
            json_schema_extra=deliverable("outputs/{design}_routed.svg"),
        )
        parallel_refine: bool = Field(
            False,
            description="Enable the parallel placement refinement of nextpnr. It is faster on "
            "many cores. Only some nextpnr builds have it.",
        )
        prjxray_db: Path | None = Field(None, description="Project X-Ray database root override.")
        chipdb: Path | None = Field(
            None,
            description="An existing Himbaechel Xilinx chip database file. Without it, Xeda "
            "prepares a chip database in a shared cache under the run root. The cache entry is "
            "named by the content of the files that make it.",
        )
        delay_matrix: Literal["build", "off"] = Field(
            "build",
            description="Xilinx interconnect delay model. `build` measures it for each tile "
            "offset (the default of the backend). `off` uses the tuned formula.",
        )
        hold_fix: bool | int = Field(
            False,
            description="Xilinx: fix hold-time violations after routing. `true` uses the "
            "default pass limit of the backend (8). A positive number sets the pass limit.",
            json_schema_extra=count(),
        )
        hold_detour_max: float | None = Field(
            None,
            ge=0,
            description="Xilinx: hold deficit in ns up to which hold fixing uses a routing "
            "detour instead of a feedthrough LUT.",
        )
        placement: Path | None = Field(
            None,
            description="Xilinx placement dump in JSON (cell to tile, site and bel). It is "
            "always written in the run directory, and the LUT count is read from it. The default "
            "is `outputs/<design>_placement.json`.",
            json_schema_extra=deliverable("outputs/{design}_placement.json", always=True),
        )

        @field_validator("hold_fix")
        @classmethod
        def _hold_fix_pass_limit(cls, value):
            if not isinstance(value, bool) and value < 1:
                raise ValueError("hold_fix is true, false, or a positive number of passes")
            return value

    class Inputs(FpgaSynthFlow.Inputs):
        netlist: Path = In(
            SourceType.JsonNetlist,
            producer="yosys_fpga",
            output="netlist",
            # The results cite the source locations of the netlist that this flow placed. A
            # netlist that differs only in them is the same design.
            equivalence=True,
            description="The JSON netlist to place: a design source or the `yosys_fpga` netlist.",
        )
        constraints: list[Path] = In(
            (SourceType.Lpf, SourceType.Pcf, SourceType.Pdc, SourceType.Xdc),
            optional=True,
            description="Pin constraints of the family, in source order.",
        )
        sdc: list[Path] = In(
            SourceType.Sdc,
            optional=True,
            description="SDC timing constraints, in source order.",
        )

    class Outputs(FpgaSynthFlow.Outputs):
        config: Path | None = Out(
            (SourceType.EcpConfig, SourceType.IceAsc, SourceType.Fasm),
            description="The routed configuration: ECP5 textcfg, iCE40 asc, or Nexus/Xilinx fasm.",
        )

    @staticmethod
    def runs_a_hook(settings: Flow.Settings) -> bool:
        """Return whether a Python hook may run inside nextpnr.

        A hook can read the sources of the design and the source locations of the netlist (a
        cell's `src` attribute) to decide where to place. `extra_args` can start a hook
        (`--pre-place=hook.py`, `--run`), so it counts."""
        hooks = ("pre_pack", "pre_place", "pre_route", "post_route", "py_script", "extra_args")
        return any(getattr(settings, hook, None) for hook in hooks)

    @classmethod
    def input_equivalence(cls, settings: Flow.Settings, name: str) -> bool:
        """Return whether `name` is compared by equivalence (the netlist is, unless a hook may run)."""
        if name == "netlist" and cls.runs_a_hook(settings):
            return False
        return super().input_equivalence(settings, name)

    @classmethod
    def design_scope(cls, settings: Flow.Settings) -> frozenset[SourceType] | None:
        """Return the source types that the flow is judged on, or None (all) if a hook may run.

        A hook can read any source."""
        return None if cls.runs_a_hook(settings) else super().design_scope(settings)

    @staticmethod
    def io_family(settings: Flow.Settings) -> str:
        """Return the group of the FPGA family that selects the file formats.

        The group is `xilinx` for every 7-series family, else the family name in lower case. This
        does not check that nextpnr supports the family."""
        fpga = getattr(settings, "fpga", None)
        family = ((fpga.family if fpga else None) or "").lower()
        if family in ("artix-7", "kintex-7", "spartan-7", "virtex-7", "zynq-7"):
            return "xilinx"
        return family

    @classmethod
    def input_types(cls, settings: Flow.Settings, name: str) -> tuple[SourceType, ...]:
        if name == "constraints":
            kind = {
                "ecp5": SourceType.Lpf,
                "ice40": SourceType.Pcf,
                "nexus": SourceType.Pdc,
                "xilinx": SourceType.Xdc,
            }.get(cls.io_family(settings))
            return (kind,) if kind else ()
        return super().input_types(settings, name)

    @classmethod
    def output_types(cls, settings: Flow.Settings, name: str) -> tuple[SourceType, ...]:
        if name == "config":
            kind = {
                "ecp5": SourceType.EcpConfig,
                "ice40": SourceType.IceAsc,
                "nexus": SourceType.Fasm,
                "xilinx": SourceType.Fasm,
            }.get(cls.io_family(settings))
            return (kind,) if kind else ()
        return super().output_types(settings, name)

    @classmethod
    def enable_output(cls, settings: Flow.Settings, name: str, *, design_name: str) -> None:
        if name != "config":
            return super().enable_output(settings, name, design_name=design_name)
        if getattr(settings, "fpga", None) is None:
            return  # no `fpga` yet: the required-settings check names what is missing
        family = cls.io_family(settings)
        if family == "ecp5" and getattr(settings, "out_of_context", False):
            raise ValueError("ECP5 out_of_context produces no configuration")
        if family not in ("ecp5", "ice40", "nexus", "xilinx"):
            raise ValueError(f"no configuration format for FPGA family {family!r}")
        # nothing to switch on: the family's configuration is always written

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Refuse an unsupported device or family before any producer runs."""
        super().check_settings_supported(settings)
        assert isinstance(settings, cls.Settings)
        cls.target_for_settings(settings)
        cls.config_for_settings(settings)

    def init(self) -> None:
        """Validate the device. The launcher supplies the declared netlist input."""
        self._target()

    def always_runs(self) -> Optional[str]:
        """Return why the flow always runs: a new random seed, or pin constraints fetched from a
        URL. No trace can verify a file fetched from a URL."""
        assert isinstance(self.settings, self.Settings)
        if self.settings.randomize_seed:
            return "it draws a new random seed"
        if self._constraint_url() is not None:
            return "its constraints are fetched from a URL"
        return super().always_runs()

    def _constraint_kind(self) -> Optional[str]:
        """Return the pin file format of the family (`lpf`, `pcf`, `pdc` or `xdc`), or None.

        This does not check that the family is supported."""
        assert isinstance(self.settings, self.Settings)
        return {"ecp5": "lpf", "ice40": "pcf", "nexus": "pdc", "xilinx": "xdc"}.get(
            self.io_family(self.settings)
        )

    def _constraint_url(self) -> Optional[str]:
        """Return the URL of the board's pin file, or None.

        A pin source of the family in the design takes precedence."""
        assert isinstance(self.settings, self.Settings)
        assert isinstance(self.inputs, self.Inputs)
        if hasattr(self, "_board_url"):
            return self._board_url
        kind = self._constraint_kind()
        if kind is None or self.inputs.constraints:
            return None
        board_data = self.settings.board_data()
        uri = board_data.get(kind) if board_data else None
        if not isinstance(uri, str):
            return None
        parsed = urlparse(uri)
        return uri if parsed.scheme and parsed.netloc else None

    def prepare_inputs(self) -> None:
        """Prepare the board's pin file and the chip database, outside the run directory."""
        self._prepare_board_inputs()
        if self.io_family(self.settings) == "xilinx":
            self._prepare_chipdb()

    def locate_prepared_inputs(self) -> Optional[List[Path]]:
        """Return the board's pin file and, for a Xilinx part, the chip database, where
        `prepare_inputs` leaves them. Nothing is made here: return None if one is not made yet."""
        located = []
        board = self._locate_board_input()
        if board.path is not None:
            if not board.path.is_file():
                if board.content is not None:
                    return None  # the cache entry of a packaged board file: not made yet
                raise FlowFatalError(self._board_file_missing(board.path))
            if board.content is not None:
                self._check_board_cache(board.path, board.content)
            located.append(board.path)
        if self.io_family(self.settings) == "xilinx":
            chipdb = self._locate_chipdb()
            if chipdb is None:
                return None
            located.append(chipdb)
        return located

    def _xilinx_layout_and_selection(self) -> tuple[XilinxLayout, XilinxSelection]:
        assert isinstance(self.settings, self.Settings)
        assert self.settings.fpga is not None
        executable = which("nextpnr-himbaechel")
        if executable is None:
            raise FlowFatalError("nextpnr-himbaechel is missing on PATH; install openXC7 1.0.")
        layout = find_xilinx_layout(
            Path(executable),
            prjxray_db=(
                self.normalize_path_to_design_root(self.settings.prjxray_db)
                if self.settings.prjxray_db is not None
                else None
            ),
            chipdb=(
                self.normalize_path_to_design_root(self.settings.chipdb)
                if self.settings.chipdb is not None
                else None
            ),
        )
        return layout, select_xilinx(self.settings.fpga.part or "", layout)

    def _locate_chipdb(self) -> Optional[Path]:
        """Return where the chip database is, or None if it is not made yet.

        After `prepare_inputs`, this reuses the identity of the installation that the preparation
        computed, because hashing the installation is most of the cost. A launch computes the
        identity once. A dry run prepares nothing, so it computes the identity here."""
        layout, selection = self._xilinx_layout_and_selection()
        return locate_chipdb(
            layout, selection, self.run_directory, getattr(self, "_chipdb_identity", None)
        )

    def _prepare_chipdb(self) -> None:
        layout, selection = self._xilinx_layout_and_selection()
        self._xilinx_layout = layout
        self._xilinx_selection = selection
        if layout.chipdb is None:
            self._chipdb_identity = chipdb_identity(layout, selection)
        self._chipdb = prepare_chipdb(
            layout, selection, self.run_directory, getattr(self, "_chipdb_identity", None)
        )
        self.implicit_inputs.append(self._chipdb)

    def _locate_board_input(self) -> "_BoardFallback":
        """Return where the board's pin file is, when the design supplies none.

        It is a local file, a board file of the package, or a URL, which is fetched when the flow
        runs. A package that is not on the file system gives the bytes of the file as `content`,
        and the file is copied once into the cache of the run root. Nothing is written here."""
        assert isinstance(self.settings, self.Settings)
        assert isinstance(self.inputs, self.Inputs)
        kind = self._constraint_kind()
        if list(self.inputs.constraints or []) or kind is None:
            return _BoardFallback()
        board_data = self.settings.board_data()
        name = board_data.get(kind) if board_data else None
        if not isinstance(name, str) or not name:
            if kind == "xdc":
                raise FlowFatalError(
                    "Xilinx nextpnr needs typed Xdc files in rtl.sources or a board with an xdc fallback."
                )
            return _BoardFallback()
        parsed = urlparse(name)
        if parsed.scheme and parsed.netloc:
            return _BoardFallback(url=name)  # always runs; fetched after freshness
        if self.settings.custom_boards_file:
            return _BoardFallback(path=self.settings.custom_boards_file.parent / name)
        resource = files("xeda.data").joinpath(name)
        if isinstance(resource, Path):
            return _BoardFallback(path=resource)
        content = resource.read_bytes()
        digest = hashlib.sha3_256(content).hexdigest()[:32]
        root = self.run_directory.run_root
        if root is None:
            raise FlowFatalError(
                "A board file in an archive needs a run root that Xeda owns. "
                "Give a claimed scratch `RunDirectory` when you build a flow directly."
            )
        return _BoardFallback(
            path=root / ".cache" / "board-files" / digest / Path(name).name, content=content
        )

    @staticmethod
    def _check_board_cache(path: Path, content: bytes) -> None:
        """Refuse a cached board file that differs from the content of the package."""
        if path.read_bytes() != content:
            raise FlowFatalError(f"Corrupt board file cache entry {path}; remove it and run again.")

    def _board_file_missing(self, path: Path) -> str:
        assert isinstance(self.settings, self.Settings)
        kind = self._constraint_kind()
        return f"Board {self.settings.board!r} {kind} fallback does not exist: {path}"

    def _prepare_board_inputs(self) -> None:
        """Choose the board's pin file. This does not write the run directory."""
        assert isinstance(self.settings, self.Settings)
        assert isinstance(self.inputs, self.Inputs)
        self._pin_inputs = list(self.inputs.constraints or [])
        self._board_url = None
        board = self._locate_board_input()
        if board.url is not None:
            self._board_url = board.url
            return
        path = board.path
        if path is None:
            return
        if board.content is not None:
            root = self.run_directory.run_root
            assert root is not None
            cache = RunDirectory.claimed(path.parent, root)
            cache.path.mkdir(parents=True, exist_ok=True)
            path = cache.writable(path.name)
            if path.is_file():
                self._check_board_cache(path, board.content)
            else:
                with replacing_file(path, "wb") as stream:
                    stream.write(board.content)
        if not path.is_file():
            raise FlowFatalError(self._board_file_missing(path))
        self._pin_inputs = [path]
        self.implicit_inputs.append(path)

    def _merged_constraints(self) -> tuple[Path | None, Path | None, float | None]:
        """Merge the pin and SDC constraints, reconcile their clocks and write both files.

        This runs after the launcher records the inputs, so both files are outputs. Return the
        pin file, the SDC file (None for a file with no text) and the frequency hint."""
        assert isinstance(self.settings, self.Settings)
        assert isinstance(self.inputs, self.Inputs)
        if not hasattr(self, "_pin_inputs"):
            raise FlowFatalError(
                "Call prepare_inputs() after assigning inputs and before running nextpnr."
            )
        paths = self._pin_inputs
        if self._board_url:
            scratch = self.run_directory.writable("board-download.constraints")
            try:
                # Fetch into a file of the run directory, never a system temporary file.
                with (
                    urlopen(self._board_url, timeout=BOARD_FILE_TIMEOUT_S) as response,
                    replacing_file(scratch, "wb") as stream,
                ):
                    stream.write(response.read())
            except (OSError, URLError) as e:
                raise FlowFatalError(
                    f"Unable to retrieve constraints from {self._board_url}: {e}"
                ) from e
            paths = [scratch]
        pins = merge_constraints(paths)
        if self._board_url:
            pins = Constraints().append(pins.text, self._board_url)
        timing_paths = list(self.inputs.sdc or [])
        if self.settings.sdc:
            timing_paths.append(self.normalize_path_to_design_root(self.settings.sdc))
        sdc = merge_constraints(timing_paths)
        uses: list[ClockUse] = []
        pins, frequency, timed = reconcile_clocks(
            pins,
            sdc,
            self.settings.clocks,
            family=self.io_family(self.settings),
            netlist=self.inputs.netlist,
            top=self.design.rtl.top or "",
            main_clock=self.settings.main_clock,
            uses=uses,
        )
        if not timed:
            log.warning(
                "No physical clock period or frequency in the flow settings or constraint files. "
                "nextpnr uses its default of 12 MHz."
            )
        self._pin_constraints, self._sdc_constraints = pins, sdc

        def write(merged: Constraints, name: str) -> Path | None:
            if not merged.text:
                return None
            path = self.run_directory.writable(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            with replacing_file(path) as stream:
                stream.write(merged.text)
            return path

        return (
            write(pins, f"constraints.{self._constraint_kind()}"),
            write(sdc, "constraints.sdc"),
            frequency,
        )

    def _error_lines(self) -> list[str]:
        """Return the error lines of the log that this run wrote. An older log counts as none."""
        assert isinstance(self.settings, self.Settings)
        path = self.settings.log
        if path is None:
            return []
        path = self.report_file(path if path.is_absolute() else self.run_path / path, optional=True)
        if path is None:
            return []
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            return []  # keep the tool failure when the log cannot be read
        return [line for line in lines if line.lstrip().startswith("ERROR:")]

    def _constraint_diagnostic(self, errors: list[str]) -> str | None:
        """Translate the constraint-file lines among nextpnr's `errors` to their origins.

        A warning is never the cause of a failure: the parsers warn about options they ignore.
        Each line gets its own origin. If the line names a file, that file is the origin.
        Otherwise (nextpnr's parsers also emit an unqualified '(on line N)'), the only constraint
        input is the origin. With several inputs the origin is unknown, and the line is reported
        without a file. Python hook tracebacks ('File ... line N') are never constraint-file
        diagnostics."""
        messages = []
        for line in errors:
            source = None
            if "constraints.sdc" in line:
                source = self._sdc_constraints
            elif f"constraints.{self._constraint_kind()}" in line:
                source = self._pin_constraints
            elif re.search(r"\(on line\s+\d+\)", line, re.IGNORECASE):
                candidates = [m for m in (self._pin_constraints, self._sdc_constraints) if m.text]
                if len(candidates) == 1:
                    source = candidates[0]
                elif candidates:
                    unknown = line.strip().removeprefix("ERROR:").strip()
                    messages.append(
                        f"{unknown} (in the pin or the SDC input; nextpnr does not say which)"
                    )
                    continue
            if source is not None:
                translated = source.diagnostic(line)
                if translated != line:
                    messages.append(translated)
        return "\n".join(messages) if messages else None

    def _failure(self, error: NonZeroExitCode) -> Exception:
        """Return the exception that reports a failed nextpnr, judged by the errors in its log.

        It is a constraint error at its original file and line, a timing failure, or the failure
        of the tool itself (`NonZeroExitCode`). In the last case the message quotes what nextpnr
        reported, as the other two do: the error lines of this run, each once, up to
        `ERRORS_SHOWN` of them."""
        errors = self._error_lines()
        diagnostic = self._constraint_diagnostic(errors)
        if diagnostic:
            return FlowFatalError(f"nextpnr constraint error: {diagnostic}")
        missed = [
            line.strip().removeprefix("ERROR:").strip()
            for line in errors
            if "Max frequency for clock" in line and "FAIL" in line
        ]
        if missed:
            return FlowFatalError(
                "nextpnr: timing constraints are not met: "
                + "; ".join(dict.fromkeys(missed))
                + " (`timing_allow_fail` keeps the result anyway)"
            )
        reported = list(
            dict.fromkeys(line.strip().removeprefix("ERROR:").strip() for line in errors)
        )
        if not reported or type(error) is not NonZeroExitCode:  # a timeout keeps its own words
            return error
        shown = reported[:ERRORS_SHOWN]
        more = len(reported) - len(shown)
        assert isinstance(self.settings, self.Settings)
        rest = f" (and {more} more in {self.settings.log})" if more else ""
        return NonZeroExitCode(
            error.command_args, error.exit_code, f"nextpnr error: {'; '.join(shown)}{rest}"
        )

    def _target(self) -> tuple[str, list[str]]:
        """Return the validated architecture and the device arguments of this flow."""
        assert isinstance(self.settings, self.Settings)
        return self.target_for_settings(self.settings)

    @classmethod
    def target_for_settings(cls, ss: Settings) -> tuple[str, list[str]]:
        """Select a supported architecture and device. This runs no tool and writes no file."""
        fpga = ss.fpga
        assert fpga is not None, "checked at launch (`required_settings`)"
        family = cls.io_family(ss)
        if family not in NEXTPNR_FAMILIES:
            raise FlowSettingsException(
                f"nextpnr has no tested device, constraint and output mapping for "
                f"fpga.family={fpga.family or None!r}; supported are "
                f"{', '.join(NEXTPNR_FAMILIES)} (7-series)."
            )
        # Settings of another architecture that have a value other than their default.
        misplaced = [
            name
            for name, families in FAMILY_SETTINGS.items()
            if family not in families
            and getattr(ss, name) != type(ss).model_fields[name].get_default()
        ]
        if misplaced:
            executable = EXECUTABLES.get(family, f"nextpnr-{family}")
            raise FlowSettingsException(f"{executable} does not take {', '.join(misplaced)}.")
        if family == "xilinx":
            # The part selects the package pins and speed file; the database maps it to a die.
            if not (fpga.part and fpga.device and fpga.package and fpga.pins and fpga.speed):
                raise FlowSettingsException(
                    "nextpnr-himbaechel needs the full part in fpga.part, with its package and "
                    f"speed grade, e.g. xc7a100tcsg324-1, not {fpga.part or fpga.device or None!r}."
                )
            # `--device` is the part as the Project X-Ray database spells it, known only once
            # the installation is read (`prepare_inputs`): nextpnr matches it case-sensitively.
            return family, []
        if family == "ecp5":
            if not fpga.capacity:
                raise FlowSettingsException(
                    "nextpnr-ecp5 needs fpga.capacity (e.g. 25k), or an fpga.part to read it from."
                )
            device_type = (fpga.type or "u").lower()
            prefix = "" if device_type == "u" else f"{device_type}-"
            args = [f"--{prefix}{fpga.capacity.lower()}"]
            args += setting_flag(fpga.speed, name="speed")
            if fpga.package:
                package = fpga.package.upper()
                package = {"BG": "CABGA", "MG": "CSFBGA"}.get(package, package)
                if not re.search(r"\d$", package):
                    if not fpga.pins:
                        raise FlowSettingsException(
                            f"nextpnr-ecp5 needs fpga.pins with fpga.package={fpga.package!r}."
                        )
                    package += str(fpga.pins)
                args += setting_flag(package, name="package")
            return family, args
        if family == "ice40":
            device = (fpga.device or "").lower()
            device = "u" + device.removeprefix("ice5lp") if device.startswith("ice5lp") else device
            device = device.removeprefix("ice40")
            if not device and fpga.type and fpga.capacity:
                device = f"{fpga.type}{fpga.capacity}".lower()
            if device not in ICE40_DEVICES:
                raise FlowSettingsException(
                    f"nextpnr-ice40 has no device {device or None!r}: set fpga.part (e.g. "
                    "iCE40UP5K-SG48I), fpga.device (e.g. iCE40HX8K), or fpga.type and "
                    f"fpga.capacity; its devices are {', '.join(ICE40_DEVICES)}."
                )
            args = [f"--{device}"]
            if fpga.package:
                package = fpga.package.lower()
                args += setting_flag(ICE40_PACKAGE_NAMES.get(package, package), name="package")
            return family, args
        device = fpga.part or fpga.device or ""
        if not NEXUS_DEVICE.fullmatch(device):
            raise FlowSettingsException(
                f"nextpnr-nexus needs the whole device in fpga.part, e.g. LIFCL-40-9BG400C, not "
                f"{device or None!r}; `nextpnr-nexus --list-devices` lists them."
            )
        return family, [f"--device={device.upper()}"]

    @classmethod
    def config_for_settings(cls, ss: Settings) -> tuple[str, Path] | None:
        """Return the name of the configuration setting and its file, or None if there is none.

        An ECP5 `out_of_context` run has no configuration."""
        family = cls.io_family(ss)
        if family == "ecp5" and ss.out_of_context:
            return None
        name = {"ecp5": "textcfg", "ice40": "asc", "nexus": "fasm", "xilinx": "fasm"}.get(family)
        path = getattr(ss, name) if name else None
        return (name, path) if name and path else None

    def run(self) -> None:
        """Place and route the netlist handed over as the input `netlist`."""
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        inputs, declared = self.inputs, self.outputs
        assert isinstance(inputs, self.Inputs) and isinstance(declared, self.Outputs)
        netlist_json = inputs.netlist
        config = self.config_for_settings(ss)
        if config:
            declared.config = self.run_path / config[1]
        fpga_family, target_args = self._target()
        next_pnr = NextpnrTool(executable=EXECUTABLES.get(fpga_family, f"nextpnr-{fpga_family}"))
        xilinx = fpga_family == "xilinx"

        if not netlist_json.exists():
            raise FlowFatalError(f"The JSON netlist {netlist_json} does not exist.")

        pin_file, sdc_file, frequency = self._merged_constraints()
        placement: Path | None = None  # the Xilinx placement dump, which only that backend writes
        if xilinx:
            if not hasattr(self, "_chipdb"):
                raise FlowFatalError(
                    "Call prepare_inputs() after assigning inputs and before running nextpnr."
                )
            # The Xilinx backend takes its files as `-o name=value` options of the uarch. It
            # takes no `--freq`: its clocks are the `create_clock` commands of the XDC and SDC
            # files.
            placement = self._placement_dump()
            self.run_directory.inside(placement).parent.mkdir(parents=True, exist_ok=True)
            args: list[Any] = ["--device", self._xilinx_selection.name]
            args += ["--chipdb", self._chipdb, "--json", netlist_json]
            if pin_file:
                args += ["-o", f"xdc={pin_file}"]
            if config:
                args += ["-o", f"fasm={config[1]}"]
            args += ["-o", f"placement={placement}"]
            if ss.delay_matrix != "build":
                args += ["-o", f"delay-matrix={ss.delay_matrix}"]
            if ss.hold_fix is True:
                args += ["-o", "hold-fix"]  # the backend's default pass limit
            elif ss.hold_fix:
                args += ["-o", f"hold-fix={ss.hold_fix}"]
            if ss.hold_detour_max is not None:
                args += ["-o", f"hold-detour-max={ss.hold_detour_max}"]
        else:
            constraint_name = {"ecp5": "lpf", "ice40": "pcf", "nexus": "pdc"}[fpga_family]
            args = setting_flag(pin_file, name=constraint_name)
            args += setting_flag(netlist_json, name="json")
            args += target_args
        args += setting_flag(frequency, name="freq")
        args += setting_flag(sdc_file, name="sdc")
        args += setting_flag(self.design.rtl.top)
        args += setting_flag(ss.seed)
        # Settings of another architecture were rejected by `_target`.
        for name in FAMILY_SETTINGS:
            if name in XILINX_SETTINGS:
                continue
            value = getattr(ss, name)
            if isinstance(value, Path):
                value = self.normalize_path_to_design_root(value)
            args += setting_flag(value, name=name)
        args += setting_flag(ss.debug)
        args += setting_flag(ss.verbose > 0, name="verbose")  # nextpnr has one level
        args += setting_flag(ss.is_quiet, name="quiet")
        args += setting_flag(ss.randomize_seed)
        args += setting_flag(ss.timing_allow_fail)
        args += setting_flag(ss.ignore_loops)
        outputs: Tuple[str, ...] = ("write", "sdf", "log", "report", "placed_svg", "routed_svg")
        for name in outputs if xilinx or not config else (config[0], *outputs):
            args += setting_flag(getattr(ss, name), name=name)
        if config:
            outputs = (config[0], *outputs)
        args += setting_flag(ss.nthreads, name="threads")
        args += setting_flag(ss.detailed_timing_report)
        args += setting_flag(ss.parallel_refine)
        for name in (
            "placer",
            "router",
            "cstrweight",
            "starttemp",
            "placer_heap_alpha",
            "placer_heap_beta",
            "placer_heap_critexp",
            "placer_heap_timingweight",
        ):
            args += setting_flag(getattr(ss, name), name=name)
        # Input files are named relative to the design root.
        for name in ("pre_pack", "pre_place", "pre_route", "post_route", "py_script"):
            value = getattr(ss, name)
            if value:
                flag = "run" if name == "py_script" else name
                args += setting_flag(self.normalize_path_to_design_root(value), name=flag)
        args += setting_flag(ss.tmg_ripup)
        args += setting_flag(ss.no_tmdriv)
        args += setting_flag(ss.ignore_rel_clk)
        args += setting_flag(ss.router2_alt_weights)
        args += setting_flag(ss.placer_heap_no_ctrl_set)
        if ss.extra_args:
            args += ss.extra_args
        try:
            next_pnr.run(*args, env={"PYTHONDONTWRITEBYTECODE": "1"})
        except NonZeroExitCode as e:
            failure = self._failure(e)
            if failure is e:
                raise
            raise failure from e
        if config and (declared.config is None or not self.wrote_output(declared.config)):
            raise FlowFatalError(
                f"nextpnr did not write enabled {config[0]} configuration {declared.config}."
            )
        for name in outputs:
            path = getattr(ss, name)
            if name != "report" and path:
                path = path if path.is_absolute() else self.run_path / path
                if path.is_file() and self.wrote_output(path):  # never an earlier run's
                    self.artifacts[name] = path
        if placement is not None and self.wrote_output(self.run_path / placement):
            self.artifacts["placement"] = self.run_path / placement

    def parse_reports(self) -> bool:
        """Parse nextpnr's JSON report (`--report`).

        The report is written by architecture-independent nextpnr code and always holds `fmax`,
        `utilization` and `critical_paths`, plus `detailed_net_timings` when
        `detailed_timing_report` is set. It is written even when timing fails.
        """
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        self._timing_met = None
        if not ss.report:
            log.warning(
                "The setting `report` is empty, so nextpnr wrote no report and Xeda parsed no "
                "timing or utilization results."
            )
            return True
        report_path = Path(ss.report)
        if not report_path.is_absolute():
            report_path = self.run_path / report_path
        if self.report_file(report_path) is None:  # none, or a previous run's
            log.error("nextpnr wrote no report %s", report_path)
            return False
        try:
            report = json.loads(report_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.error("Failed to read nextpnr report %s: %s", report_path, e)
            return False
        self.artifacts["report"] = str(report_path)

        self._timing_met = self._parse_fmax(report.get("fmax") or {})
        self._parse_utilization(report.get("utilization") or {})
        if self.io_family(ss) == "xilinx":
            selection = getattr(self, "_xilinx_selection", None)
            if selection is not None:
                self.results["device"] = selection.name
                self.results["fabric"] = selection.fabric
        self._parse_critical_paths(report.get("critical_paths") or [])
        detailed = report.get("detailed_net_timings")
        if detailed is not None:
            self.results["_detailed_net_timings"] = detailed

        return True

    def check_results(self) -> bool:
        """Reject the run if a constrained clock domain missed its constraint, unless
        `timing_allow_fail` is set.

        nextpnr exits non-zero when timing fails, unless `timing_allow_fail` is set. This check
        is mostly a backstop. It also covers a backend that reports a missed constraint and still
        exits with 0."""
        ok = super().check_results()
        assert isinstance(self.settings, self.Settings)
        if self._timing_met is not False or self.settings.timing_allow_fail:
            return ok
        target = self.results.get("clock_frequency")
        self.reject(
            f"timing not met: Fmax {self.results.get('fmax')} MHz"
            + (f" is below the target {target} MHz" if target is not None else "")
            + f" (worst slack {self.results.get('wns')} ns)"
        )
        return False

    def _parse_fmax(self, fmax: Dict[str, Any]) -> bool:
        """Record achieved frequency and derived slack per clock domain.

        nextpnr reports frequencies and no slack. The slack is the constrained clock period minus
        the achieved clock period. Return whether every constrained domain met its constraint.
        """
        domains: Dict[str, Dict[str, Any]] = {}
        # Rounding is for display only: rounding before the sign test turns a slack of
        # -0.0000004 ns into -0.0, and `-0.0 >= 0` is True, which reports a violation as met.
        raw_slacks: Dict[str, float] = {}
        for domain, values in fmax.items():
            achieved = values.get("achieved")
            constraint = values.get("constraint")
            entry: Dict[str, Any] = {"achieved_mhz": achieved, "constraint_mhz": constraint}
            if achieved and constraint:
                raw = 1000.0 / constraint - 1000.0 / achieved
                raw_slacks[domain] = raw
                entry["slack_ns"] = round(raw, _SLACK_DECIMALS)
            domains[domain] = entry
        self.results["_fmax"] = domains
        self.results["clock_domains"] = len(domains)

        constrained = [v for v in domains.values() if v.get("slack_ns") is not None]
        if not constrained:
            if domains:
                log.warning(
                    "nextpnr reported %d clock domain(s), and none is constrained, so Xeda "
                    "derived no Fmax or slack. Set `clock.period` (or `clocks`), or constrain "
                    "the clocks in an LPF or SDC file.",
                    len(domains),
                )
            return True
        # Fmax is the lowest achieved frequency: the frequency at which every domain is still
        # satisfied. `wns` comes from the domain with the least slack, which in a multi-domain
        # design can be another domain.
        self.results["fmax"] = round(min(v["achieved_mhz"] for v in constrained), 3)
        wns = min(raw_slacks.values())
        self.results["wns"] = round(wns, _SLACK_DECIMALS)
        self.results["timing_met"] = wns >= 0
        if len(domains) == 1:
            port = self._clock_port(next(iter(domains)))
            if port is not None:
                self.results["clock_port"] = port
        if len(constrained) == 1:
            # `clock_frequency` and `clock_period` describe one domain, so they are reported
            # only when one domain is constrained.
            only = constrained[0]
            self.results["clock_frequency"] = only["constraint_mhz"]
            self.results["clock_period"] = round(1000.0 / only["constraint_mhz"], 3)
        if wns < 0:
            log.error(
                "Timing not met: worst slack is %s ns (target %s MHz, achieved %s MHz)",
                wns,
                self.results.get("clock_frequency"),
                self.results.get("fmax"),
            )
        return bool(wns >= 0)

    def _clock_port(self, domain: str) -> str | None:
        """The top-level port of the only reported clock domain, when that is certain.

        nextpnr names a domain after its clock net. That net is the port itself only when no
        buffer sits between them (a 7-series domain is the output net of the BUFG). The port is
        known only when the domain name is a port of the top module in the placed netlist. Any
        other domain keeps its raw name. A launch that constrains one clock on a port does not
        prove the port: the reported domain can be a clock generated from it (an MMCM output).
        """
        try:
            netlist = json.loads(Path(self.inputs.netlist).read_text())  # type: ignore[attr-defined]
            ports = netlist["modules"][self.design.rtl.top or ""]["ports"]
            if domain in ports:
                return domain
        except (AttributeError, KeyError, OSError, TypeError, ValueError):
            pass  # no netlist to consult (a flow built for its report alone)
        return None

    def _placement_dump(self) -> Path:
        """Return where the Xilinx placement dump goes: the `placement` setting, else its
        conventional name. It is always written, because the canonical LUT count is read from it."""
        assert isinstance(self.settings, self.Settings)
        if self.settings.placement is not None:
            return self.settings.placement
        conventional = self.settings.conventional_output("placement", self.design.name)
        assert conventional is not None  # the field is a deliverable with a conventional name
        return Path(conventional)

    def _count_xilinx_luts(self, positions: Any) -> None:
        """Report `lut` as the physical LUTs this run's placement dump shows occupied.

        `positions` is the `SLICE_LUTX` count of the report, and the LUT cells of the dump must
        equal it. A dump that is missing, left by an earlier run, unreadable or in disagreement
        gives no count.
        """
        assert isinstance(self.settings, self.Settings)
        path = self.run_path / self._placement_dump()
        try:
            if self.report_file(path, optional=True) is None:
                raise ValueError("nextpnr did not write it")
            cells, locations = xilinx_lut_locations(json.loads(path.read_text()))
            if cells != positions:
                raise ValueError(f"it places {cells} LUT cells, the report counts {positions}")
        except (OSError, ValueError) as e:
            log.warning("No `lut` result: placement dump %s cannot be counted: %s", path, e)
            return
        self.results["lut"] = locations
        self.results["lut_basis.stage"] = LUT_STAGE
        self.results["lut_basis.method"] = LUT_METHODS["xilinx"]

    def _parse_utilization(self, utilization: Dict[str, Any]) -> None:
        """Record the usage of each bel type, and canonical resource names for a known family."""
        detail: Dict[str, Dict[str, Any]] = {}
        for cell, counts in utilization.items():
            used = counts.get("used") or 0
            available = counts.get("available") or 0
            detail[cell] = {
                "used": used,
                "available": available,
                "utilization_percent": round(100.0 * used / available, 2) if available else None,
            }
            if used:
                # Raw bel-type counts are always correct, whatever the family is.
                self.results[cell] = used
        self.results["_utilization"] = detail

        assert isinstance(self.settings, self.Settings)
        family = self.io_family(self.settings)
        resources = {"ecp5": ECP5_RESOURCES, "xilinx": XILINX_RESOURCES}.get(family)
        if resources is None:
            if detail:
                log.debug(
                    "No canonical resource mapping for fpga.family=%r; reporting raw nextpnr "
                    "bel-type counts only.",
                    family or None,
                )
            return
        for canonical, cell_types in resources.items():
            present = [c for c in cell_types if c in detail]
            if present:
                self.results[canonical] = sum(detail[c]["used"] for c in present)
        if family == "xilinx":
            if "SLICE_LUTX" in detail:
                self._count_xilinx_luts(detail["SLICE_LUTX"]["used"])
        elif "lut" in self.results:
            self.results["lut_basis.stage"] = LUT_STAGE
            self.results["lut_basis.method"] = LUT_METHODS[family]

    def _parse_critical_paths(self, critical_paths: List[Dict[str, Any]]) -> None:
        """Summarize each reported critical path.

        The full detail of each stage stays in the report file, which is an artifact."""
        summary = []
        for path in critical_paths:
            stages = path.get("path") or []
            summary.append(
                {
                    "from": path.get("from"),
                    "to": path.get("to"),
                    "delay_ns": round(sum(stage.get("delay") or 0.0 for stage in stages), 3),
                    "stages": len(stages),
                }
            )
        self.results["_critical_paths"] = summary
