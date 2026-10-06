import hashlib
import json
import logging
import re
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
    describe_results,
)
from ..tool import NonZeroExitCode, Tool
from ..run_dir import RunDirectory
from ..utils import replacing_file, setting_flag
from .nextpnr_constraints import ClockUse, Constraints, merge_constraints, reconcile_clocks
from .xilinx import find_xilinx_layout, prepare_chipdb, select_xilinx

__all__ = ["Nextpnr"]

log = logging.getLogger(__name__)


class NextpnrTool(Tool):
    """nextpnr, whose version banner goes to stderr rather than stdout.

    The banner looks like::

        "nextpnr-ecp5" -- Next Generation Place and Route (Version nextpnr-0.11.1-3-g930fef44)
        "nextpnr-himbaechel" -- Next Generation Place and Route (Version 1.0.0-7-g334d1b18)

    The generic patterns in `Tool` do not match that shape, so without this the reported version
    is empty. `Tool` already retries version detection with stderr folded in.
    """

    version_regexps: List[Union[re.Pattern, str]] = [
        re.compile(r"Version\s+(?:nextpnr-)?(?P<version>\d+(?:\.\d+)*)")
    ]


#: Canonical resource name -> the nextpnr bel types that implement it, for ECP5.
#:
#: Verified against the nextpnr-ecp5 0.11.1 shipped in oss-cad-suite. `TRELLIS_COMB` is one LUT4
#: and `TRELLIS_FF` one flip-flop, so both map cleanly. Older nextpnr reported `TRELLIS_SLICE`
#: instead -- two LUT4s and two FFs per slice -- which cannot be split, so it is reported as
#: `slice` and leaves `lut`/`ff` unset rather than being counted wrongly.
#: Decimal places slack is rounded to for reporting. Pass/fail is always decided on the
#: unrounded value.
_SLACK_DECIMALS = 6

ECP5_RESOURCES: Dict[str, Tuple[str, ...]] = {
    "lut": ("TRELLIS_COMB",),
    "ff": ("TRELLIS_FF",),
    "slice": ("TRELLIS_SLICE",),
    "bram": ("DP16KD",),
    "dsp": ("MULT18X18D", "ALU54B"),
    "io": ("TRELLIS_IO",),
}

#: Canonical resource name -> the bel types that implement it, for Xilinx 7-series
#: (nextpnr-himbaechel 1.0.0, uarch `xilinx`). `lut` is not here: `SLICE_LUTX` counts 5LUT and
#: 6LUT *positions*, two per physical LUT, so it is counted from the placement dump instead
#: (`xilinx_lut_locations`). Block RAM counts primitives; `CARRY4` stays a raw count.
XILINX_RESOURCES: dict[str, tuple[str, ...]] = {
    "ff": ("SLICE_FFX",),
    "bram": ("RAMB18E1", "RAMB36E1"),
    "dsp": ("DSP48E1",),
    "io": ("PAD",),
}

#: How each family's canonical `lut` is counted (`LUT:METHOD`); the stage is always this flow's.
LUT_STAGE = "placed and routed"
LUT_METHODS = {
    "ecp5": "TRELLIS_COMB bels in nextpnr's utilization report",
    "xilinx": "unique occupied LUT locations (tile, site, A-D) in nextpnr's placement dump",
}

#: The nextpnr architectures this flow has a tested device, constraint and output mapping for;
#: `xilinx` is every 7-series family (`Nextpnr.io_family`).
NEXTPNR_FAMILIES = ("ecp5", "ice40", "nexus", "xilinx")

#: The executable of an architecture that is not `nextpnr-<architecture>`.
EXECUTABLES = {"xilinx": "nextpnr-himbaechel"}

#: Where the placement dump goes when the `placement` setting names no file: it is always
#: written, since the canonical LUT count is read from it.
XILINX_PLACEMENT = Path("placement.json")

#: Seconds a board's pin-constraint download may wait on its server, to connect and for each
#: read (not the whole transfer): a stalled server fails the run instead of holding its lock.
BOARD_FILE_TIMEOUT_S = 60

_XILINX_LUT_BEL = re.compile(r"[A-D][56]LUT")


def xilinx_lut_locations(placement: Any) -> tuple[int, int]:
    """The LUT cells of a nextpnr-himbaechel placement dump (`-o placement=`, cell -> its tile,
    site, bel and bel type) and the physical LUTs they occupy.

    A 7-series LUT has a 5LUT and a 6LUT position (`A5LUT`, `A6LUT`, ...): a fractured pair, a
    `LUT6_2`, or a LUT nextpnr inserted beside a carry input shares one physical LUT, and a
    RAM or shift-register LUT occupies its position like any other. Returns the number of
    placed LUT cells (positions) and of distinct `(tile, site, letter)` locations; raises
    `ValueError` for a dump that is not of that shape, rather than count what it cannot read.
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


#: nextpnr-ice40's device flags.
ICE40_DEVICES: Tuple[str, ...] = (
    ("lp384", "lp1k", "lp4k", "lp8k")
    + ("hx1k", "hx4k", "hx8k")
    + ("up3k", "up5k", "u1k", "u2k", "u4k")
)

#: Packages nextpnr-ice40 names differently from their ordering code.
ICE40_PACKAGE_NAMES = {"swg16": "swg16tr"}

#: A device as nextpnr-nexus names it (`nextpnr-nexus --list-devices`).
NEXUS_DEVICE = re.compile(r"(LIFCL|LFD2NX)-\d+-\d[A-Z]+\d+[CI](ES2?)?", re.IGNORECASE)

#: Settings that are options of only some nextpnr architectures, and of which.
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

#: Settings of the Xilinx backend, given to it as `-o name=value` rather than as flags.
XILINX_SETTINGS = tuple(name for name, families in FAMILY_SETTINGS.items() if "xilinx" in families)


class EcpPLL(Tool):
    class Clock(XedaBaseModel):
        name: Optional[str] = None
        mhz: float = Field(gt=0, description="frequency in MHz")
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
            # `.get`, not `[...]`: `info.data` carries only the fields that validated, so a bad
            # `module` would otherwise surface as a bare `KeyError` instead of its own error.
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
    """Place and route an FPGA design with nextpnr, the portable open-source PnR tool.

    Its `netlist` input is a `JsonNetlist` design source or, by default, the recorded netlist
    synthesized by `yosys_fpga`. This flow places and routes that input with the nextpnr
    variant matching `fpga.family` -- `nextpnr-ecp5`, `nextpnr-ice40`, `nextpnr-nexus`, or
    openXC7's `nextpnr-himbaechel` for Xilinx 7-series (Artix, Kintex, Spartan, Virtex, Zynq) --
    then parses nextpnr's JSON report for achieved frequency, slack and resource utilization.

    A 7-series target needs the full part (`xc7a100tcsg324-1`) and pin constraints (typed `Xdc`
    sources, or a board's); its chip database is generated once per run root unless `chipdb`
    names one. Canonical resource names (`lut`, `ff`, ...) are mapped for ECP5 and 7-series;
    other families report raw bel-type counts. `lut` is counted at this stage by the method
    `LUT:METHOD` names -- for 7-series, the physical LUTs occupied, from the placement dump --
    and is not certified comparable with another toolchain's.
    A target without a tested device/constraint/output mapping is rejected before synthesis.
    """

    required_settings = {"fpga": FPGA_OR_BOARD_REQUIRED}

    results_description = describe_results(
        "Fmax",
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
            "LUT:STAGE": "Stage the canonical `lut` count describes.",
            "LUT:METHOD": "How the canonical `lut` count was taken at that stage.",
            "clock_port": "Top-level port of the one reported clock domain, when it is certain.",
            "device": "Xilinx 7-series: the part placed and routed.",
            "fabric": "Xilinx 7-series: the die routed, which every `available` total describes "
            "(an xc7a35t is routed as an xc7a50t).",
        },
    )

    class Settings(WithFpgaBoardSettings):
        removed_settings = {
            **WithFpgaBoardSettings.removed_settings,
            **{
                f"{kind}_cfg": f'rtl.sources with a typed pin file: {{ file = "pins.{kind}", type = "{kind.capitalize()}" }}'
                for kind in ("lpf", "pcf", "pdc")
            },
            "yosys": "`flows.yosys_fpga.<key>`",
        }
        seed: Optional[int] = Field(
            None,
            description="Seed for nextpnr's placer. Different seeds give different results; "
            "sweeping the seed is a common way to squeeze out extra Fmax.",
        )
        randomize_seed: bool = Field(
            False,
            description="Use a fresh random seed on every run. Makes results non-reproducible, "
            "so the flow then always runs and is never reused; set `seed` instead to pin one.",
        )
        timing_allow_fail: bool = Field(
            False,
            description="Let the flow succeed even when timing constraints are not met.",
        )
        ignore_loops: bool = Field(
            False, description="ignore combinational loops in timing analysis"
        )

        textcfg: Path = Field(
            Path("config.txt"),
            description="ECP5 routed design written as a Trellis textual configuration file, "
            "which the bitstream packer (`fpga_pack`) consumes. Always written, except by an "
            "`out_of_context` run, which has none.",
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
            """There is no switch: the configuration is nextpnr's declared output `config`,
            the same whether nextpnr is requested or produces for a packer, so one run serves
            both. The setting only names (or delivers) the file."""
            if value is None or (isinstance(value, (str, Path)) and not str(value).strip()):
                raise ValueError(
                    "nextpnr always writes its configuration; give a file name or leave the "
                    "default"
                )
            return value

        out_of_context: bool = Field(
            False,
            description="disable IO buffer insertion and global promotion/routing, for building pre-routed blocks",
        )
        lpf_allow_unconstrained: bool = Field(
            False,
            description="don't require LPF file(s) to constrain all IOs",
        )
        pcf_allow_unconstrained: bool = Field(
            False, description="Allow iCE40 IOs without a PCF pin assignment."
        )
        placer: Optional[str] = Field(
            None, description="nextpnr placer (`heap`, `sa`, or a backend-specific choice)."
        )
        router: Optional[str] = Field(None, description="nextpnr router (`router1` or `router2`).")
        cstrweight: Optional[float] = Field(None, description="Placer constraint weight.")
        starttemp: Optional[float] = Field(
            None, description="Simulated-annealing starting temperature."
        )
        placer_heap_alpha: Optional[float] = Field(None, description="HeAP placer alpha.")
        placer_heap_beta: Optional[float] = Field(None, description="HeAP placer density limit.")
        placer_heap_critexp: Optional[int] = Field(
            None, description="HeAP placer criticality exponent."
        )
        placer_heap_timingweight: Optional[int] = Field(
            None, description="HeAP placer timing weight."
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
            None, description="Nexus carry-chain LUT/FF clustering ratio."
        )
        estimate_delay_mult: Optional[float] = Field(
            None, description="Nexus estimated-delay multiplier."
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
            description="File nextpnr writes its JSON utilization/timing report to. This is what "
            "the flow parses its results from.",
            json_schema_extra=deliverable(),
        )
        detailed_timing_report: bool = Field(
            False,
            description="Ask nextpnr for a detailed per-path timing report. Known to be unstable "
            "and may crash nextpnr.",
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
            description="Enable nextpnr's parallel placement refinement. Faster on many cores, "
            "and only available in some nextpnr builds.",
        )
        prjxray_db: Path | None = Field(None, description="Project X-Ray database root override.")
        chipdb: Path | None = Field(
            None,
            description="An existing Himbaechel Xilinx chip database file; otherwise prepare "
            "a content-identified shared cache under the run root.",
        )
        delay_matrix: Literal["build", "off"] = Field(
            "build",
            description="Xilinx interconnect delay model: `build` measures it per tile offset "
            "(the backend's default), `off` uses the tuned formula.",
        )
        hold_fix: bool | int = Field(
            False,
            description="Xilinx: fix hold-time violations after routing. `true` uses the "
            "backend's default pass limit (8); a positive number is the pass limit.",
        )
        hold_detour_max: float | None = Field(
            None,
            ge=0,
            description="Xilinx: hold deficit in ns up to which hold fixing uses a routing "
            "detour instead of a feedthrough LUT.",
        )
        placement: Path | None = Field(
            None,
            description="Xilinx placement dump (JSON: cell to tile, site and bel). It is always "
            "written in the run directory, where the LUT count is read from it.",
            json_schema_extra=deliverable("outputs/{design}_placement.json"),
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
            description="The JSON netlist to place: a design source or yosys_fpga's netlist.",
        )
        constraints: list[Path] = In(
            (SourceType.Lpf, SourceType.Pcf, SourceType.Pdc, SourceType.Xdc),
            optional=True,
            description="Family pin constraints in design-source order.",
        )
        sdc: list[Path] = In(
            SourceType.Sdc,
            optional=True,
            description="SDC timing constraints in design-source order.",
        )

    class Outputs(FpgaSynthFlow.Outputs):
        config: Path | None = Out(
            (SourceType.EcpConfig, SourceType.IceAsc, SourceType.Fasm),
            description="The routed configuration: ECP5 textcfg, iCE40 asc, or Nexus/Xilinx fasm.",
        )

    @staticmethod
    def io_family(settings: Flow.Settings) -> str:
        """Classify formats independently of which targets can currently be launched."""
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
    def enable_output(cls, settings: Flow.Settings, name: str) -> None:
        if name != "config":
            return super().enable_output(settings, name)
        if getattr(settings, "fpga", None) is None:
            return  # no target yet: the required-settings check names what is missing
        family = cls.io_family(settings)
        if family == "ecp5" and getattr(settings, "out_of_context", False):
            raise ValueError("ECP5 out_of_context produces no configuration")
        if family not in ("ecp5", "ice40", "nexus", "xilinx"):
            raise ValueError(f"no configuration format for FPGA family {family!r}")
        # nothing to switch on: the family's configuration is always written

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Reject unsupported targets before any producer runs."""
        assert isinstance(settings, cls.Settings)
        cls.target_for_settings(settings)
        cls.config_for_settings(settings)

    def init(self) -> None:
        """Validate the target; the launcher supplies the declared netlist input."""
        self._target()

    def always_runs(self) -> Optional[str]:
        """A fresh random seed, or pin constraints fetched from a URL -- which no trace can
        verify until the board database pins them by hash."""
        assert isinstance(self.settings, self.Settings)
        if self.settings.randomize_seed:
            return "it draws a new random seed"
        if self._constraint_url() is not None:
            return "its constraints are fetched from a URL"
        return super().always_runs()

    def _constraint_kind(self) -> Optional[str]:
        """The selected family's pin format, independent of launch support."""
        assert isinstance(self.settings, self.Settings)
        return {"ecp5": "lpf", "ice40": "pcf", "nexus": "pdc", "xilinx": "xdc"}.get(
            self.io_family(self.settings)
        )

    def _constraint_url(self) -> Optional[str]:
        """The board URL, selected only when no typed family pin source is supplied."""
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
        """Prepare stable board/chipdb inputs before freshness, outside the run directory."""
        self._prepare_board_inputs()
        if self.io_family(self.settings) == "xilinx":
            self._prepare_chipdb()

    def _prepare_chipdb(self) -> None:
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
        selection = select_xilinx(self.settings.fpga.part or "", layout)
        self._xilinx_layout = layout
        self._xilinx_selection = selection
        self._chipdb = prepare_chipdb(layout, selection, self.run_directory)
        self.implicit_inputs.append(self._chipdb)

    def _prepare_board_inputs(self) -> None:
        """Select board fallback paths before freshness, without writing the run directory."""
        assert isinstance(self.settings, self.Settings)
        assert isinstance(self.inputs, self.Inputs)
        self._pin_inputs = list(self.inputs.constraints or [])
        self._board_url = None
        kind = self._constraint_kind()
        if self._pin_inputs or kind is None:
            return
        board_data = self.settings.board_data()
        name = board_data.get(kind) if board_data else None
        if not isinstance(name, str) or not name:
            if kind == "xdc":
                raise FlowFatalError(
                    "Xilinx nextpnr needs typed Xdc files in rtl.sources or a board with an xdc fallback."
                )
            return
        parsed = urlparse(name)
        if parsed.scheme and parsed.netloc:
            self._board_url = name
            return  # always-run; fetch only after freshness/snapshot, inside run-local scratch
        if self.settings.custom_boards_file:
            path = self.settings.custom_boards_file.parent / name
        else:
            resource = files("xeda.data").joinpath(name)
            if isinstance(resource, Path):
                path = resource
            else:
                content = resource.read_bytes()
                digest = hashlib.sha3_256(content).hexdigest()[:32]
                root = self.run_directory.run_root
                if root is None:
                    raise FlowFatalError(
                        "An archive board fallback needs an owned run root; supply a claimed scratch RunDirectory when constructing a flow directly."
                    )
                cache = RunDirectory.claimed(root / ".cache" / "board-files" / digest, root)
                cache.path.mkdir(parents=True, exist_ok=True)
                path = cache.writable(Path(name).name)
                if path.is_file():
                    if path.read_bytes() != content:
                        raise FlowFatalError(
                            f"Corrupt board file cache entry {path}; remove it and rerun."
                        )
                else:
                    with replacing_file(path, "wb") as stream:
                        stream.write(content)
        if not path.is_file():
            raise FlowFatalError(
                f"Board {self.settings.board!r} {kind} fallback does not exist: {path}"
            )
        self._pin_inputs = [path]
        self.implicit_inputs.append(path)

    def _merged_constraints(self) -> tuple[Path | None, Path | None, float | None]:
        """Write ordered pin and SDC merges after the launch snapshot, reconciling clocks."""
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
                # Fetch into our atomic guarded output, never a system temporary.
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
                "No physical clock period/frequency in flow settings or constraint files; nextpnr uses its 12 MHz default."
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
        """The lines nextpnr emitted as errors, from a log written by this execution only."""
        assert isinstance(self.settings, self.Settings)
        path = self.settings.log
        if path is None:
            return []
        path = self.report_file(path if path.is_absolute() else self.run_path / path)
        if path is None:
            return []
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            return []  # preserve the tool failure when its log cannot be read
        return [line for line in lines if line.lstrip().startswith("ERROR:")]

    def _constraint_diagnostic(self, errors: list[str]) -> str | None:
        """Translate the constraint-file lines among nextpnr's `errors` to their origins. A
        warning is never a failure's cause: the parsers warn about options they ignore. Each
        line is attributed on its own: by the file it names, else (nextpnr's parsers also emit
        an unqualified '(on line N)') by the only constraint input there is. With several
        inputs its origin is unknown and the line is reported without a file. Python hook
        tracebacks ('File ... line N') are never constraint-file diagnostics."""
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
        """What a failed nextpnr is reported as: a constraint error at its original file and
        line, a timing failure, or the tool's failure itself -- by the errors in its log."""
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
        return error

    def _target(self) -> tuple[str, list[str]]:
        """The validated architecture and device arguments for this instance."""
        assert isinstance(self.settings, self.Settings)
        return self.target_for_settings(self.settings)

    @classmethod
    def target_for_settings(cls, ss: Settings) -> tuple[str, list[str]]:
        """Select a supported architecture and device without tools or filesystem writes."""
        fpga = ss.fpga
        assert fpga is not None, "checked at launch (`required_settings`)"
        family = cls.io_family(ss)
        if family not in NEXTPNR_FAMILIES:
            raise FlowSettingsException(
                f"nextpnr has no tested device, constraint and output mapping for "
                f"fpga.family={fpga.family or None!r}; supported are "
                f"{', '.join(NEXTPNR_FAMILIES)} (7-series)."
            )
        # A setting of another architecture, given any value but its default.
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
        """Select an enabled family configuration, including ECP5 out-of-context runs."""
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
            raise FlowFatalError(f"netlist json file {netlist_json} does not exist!")

        pin_file, sdc_file, frequency = self._merged_constraints()
        placement: Path | None = None  # the Xilinx placement dump, which only that backend writes
        if xilinx:
            if not hasattr(self, "_chipdb"):
                raise FlowFatalError(
                    "Call prepare_inputs() after assigning inputs and before running nextpnr."
                )
            # The Xilinx backend takes its files as `-o name=value` options of the uarch, and
            # never `--freq`: its clocks are the `create_clock`s of the XDC and SDC files.
            placement = ss.placement or XILINX_PLACEMENT
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

    # ------------------------------------------------------------------ report parsing

    def parse_reports(self) -> bool:
        """Parse nextpnr's JSON report (`--report`).

        The report is written by architecture-independent nextpnr code and always holds `fmax`,
        `utilization` and `critical_paths`, plus `detailed_net_timings` when
        `detailed_timing_report` is set. It is written even when timing fails.
        """
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        if not ss.report:
            log.warning(
                "Setting 'report' is empty, so nextpnr wrote no report and no timing or "
                "utilization results could be parsed."
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

        timing_met = self._parse_fmax(report.get("fmax") or {})
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

        # nextpnr itself exits non-zero when timing fails unless `timing_allow_fail` is set, so
        # this is mostly a backstop -- but it also covers a backend that reports a missed
        # constraint without failing.
        return timing_met or ss.timing_allow_fail

    def _parse_fmax(self, fmax: Dict[str, Any]) -> bool:
        """Record achieved frequency and derived slack per clock domain.

        nextpnr reports frequencies, not slack, so slack is derived as the difference between the
        constrained and the achieved clock period. Returns whether every constrained domain met
        its constraint.
        """
        domains: Dict[str, Dict[str, Any]] = {}
        # Rounding is for display only: rounding before the sign test turns a slack of
        # -0.00025 ns into -0.0, and `-0.0 >= 0` is True, reporting a violation as met.
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
                    "nextpnr reported %d clock domain(s) but none of them was constrained, so "
                    "no Fmax or slack could be derived. Set 'clock.period' (or 'clocks'), or "
                    "constrain the clocks in an LPF/SDC file.",
                    len(domains),
                )
            return True
        # Fmax is the lowest achieved frequency: the frequency at which *every* domain is still
        # satisfied. wns comes from whichever domain has the least slack, which in a
        # multi-domain design need not be the same one.
        self.results["Fmax"] = round(min(v["achieved_mhz"] for v in constrained), 3)
        wns = min(raw_slacks.values())
        self.results["wns"] = round(wns, _SLACK_DECIMALS)
        self.results["timing_met"] = wns >= 0
        if len(domains) == 1:
            port = self._clock_port(next(iter(domains)))
            if port is not None:
                self.results["clock_port"] = port
        if len(constrained) == 1:
            # clock_frequency/clock_period are per-domain, so only report them when there is no
            # ambiguity about which domain they describe.
            only = constrained[0]
            self.results["clock_frequency"] = only["constraint_mhz"]
            self.results["clock_period"] = round(1000.0 / only["constraint_mhz"], 3)
        if wns < 0:
            log.error(
                "Timing not met: worst slack is %s ns (target %s MHz, achieved %s MHz)",
                wns,
                self.results.get("clock_frequency"),
                self.results.get("Fmax"),
            )
        return bool(wns >= 0)

    def _clock_port(self, domain: str) -> str | None:
        """The top-level port of the only reported clock domain, when that is certain.

        nextpnr names a domain for its clock net, which is the port's own only when no buffer
        sits between them (a 7-series domain is the BUFG's output net). The port is known only
        when the domain is itself a port of the top module in the placed netlist; anything else
        keeps the raw name. That the launch constrained one clock, on a port, proves nothing:
        the reported domain may be a clock generated from it (an MMCM's output).
        """
        try:
            netlist = json.loads(Path(self.inputs.netlist).read_text())  # type: ignore[attr-defined]
            ports = netlist["modules"][self.design.rtl.top or ""]["ports"]
            if domain in ports:
                return domain
        except (AttributeError, KeyError, OSError, TypeError, ValueError):
            pass  # no netlist to consult (a flow built for its report alone)
        return None

    def _count_xilinx_luts(self, positions: Any) -> None:
        """Report `lut` as the physical LUTs this run's placement dump shows occupied.

        `positions` is the report's `SLICE_LUTX` count, which the dump's LUT cells must equal:
        a dump that is missing, a previous run's, unreadable or disagreeing gives no count.
        """
        assert isinstance(self.settings, self.Settings)
        path = self.run_path / (self.settings.placement or XILINX_PLACEMENT)
        try:
            if self.report_file(path) is None:
                raise ValueError("nextpnr did not write it")
            cells, locations = xilinx_lut_locations(json.loads(path.read_text()))
            if cells != positions:
                raise ValueError(f"it places {cells} LUT cells, the report counts {positions}")
        except (OSError, ValueError) as e:
            log.warning("No `lut` result: placement dump %s cannot be counted: %s", path, e)
            return
        self.results["lut"] = locations
        self.results["LUT:STAGE"] = LUT_STAGE
        self.results["LUT:METHOD"] = LUT_METHODS["xilinx"]

    def _parse_utilization(self, utilization: Dict[str, Any]) -> None:
        """Record per-bel-type usage, plus canonical resource names where the family is known."""
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
                # Raw bel-type counts are always correct, whatever the target family is.
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
            self.results["LUT:STAGE"] = LUT_STAGE
            self.results["LUT:METHOD"] = LUT_METHODS[family]

    def _parse_critical_paths(self, critical_paths: List[Dict[str, Any]]) -> None:
        """Summarize each reported critical path; the full stage-by-stage detail stays in the
        report file, which is recorded as an artifact."""
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
