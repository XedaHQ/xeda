import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import urlretrieve

from ..board import FPGA_OR_BOARD_REQUIRED, WithFpgaBoardSettings
from ..dataclass import Field, XedaBaseModel, field_validator
from ..flow import (
    FlowFatalError,
    FlowSettingsException,
    FpgaSynthFlow,
    describe_results,
)
from ..tool import Tool
from ..utils import setting_flag
from .yosys import YosysFpga

__all__ = ["Nextpnr"]

log = logging.getLogger(__name__)


class NextpnrTool(Tool):
    """nextpnr, whose version banner goes to stderr rather than stdout.

    The banner looks like::

        "nextpnr-ecp5" -- Next Generation Place and Route (Version nextpnr-0.11.1-3-g930fef44)

    The generic patterns in `Tool` do not match that shape, so without this the reported version
    is empty. `Tool` already retries version detection with stderr folded in.
    """

    version_regexps: List[Union[re.Pattern, str]] = [
        re.compile(r"Version\s+nextpnr-(?P<version>\d+(?:\.\d+)*)")
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

#: The nextpnr architectures this flow has a tested device, constraint and output mapping for.
NEXTPNR_FAMILIES = ("ecp5", "ice40", "nexus")

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
    "lpf_cfg": ("ecp5",),
    "lpf_allow_unconstrained": ("ecp5",),
    "out_of_context": ("ecp5",),
    "disable_router_lutperm": ("ecp5",),
    "override_basecfg": ("ecp5",),
    "allow_fabric_eclk": ("ecp5",),
    "no_promote_globals": ("ecp5", "ice40"),
    "pcf_cfg": ("ice40",),
    "pcf_allow_unconstrained": ("ice40",),
    "opt_timing": ("ice40",),
    "promote_logic": ("ice40",),
    "no_promote_ce": ("ice40",),
    "pdc_cfg": ("nexus",),
    "no_pack_lutff": ("nexus",),
    "no_post_place_opt": ("nexus",),
    "carry_lutff_ratio": ("nexus",),
    "estimate_delay_mult": ("nexus",),
}


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

    Synthesis is delegated to the `yosys_fpga` dependency flow; this flow places and routes the
    resulting JSON netlist with the nextpnr variant matching `fpga.family`, then parses nextpnr's
    JSON report for achieved frequency, slack and resource utilization. Use the `openfpgaloader`
    flow to pack and program the result onto a board.

    ECP5 and iCE40 are exercised by real-tool tests; Nexus has verified command construction.
    The report parser works across architectures, but canonical resource names (`lut`, `ff`, ...)
    are currently mapped from ECP5 bel types only. Other families report raw bel-type counts.
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
    )

    class Settings(WithFpgaBoardSettings):
        lpf_cfg: Optional[Path] = Field(
            None,
            description="Lattice LPF pin-constraint file. Taken from the board database when "
            "`board` is set and this is unset.",
        )
        pcf_cfg: Optional[Path] = Field(
            None, description="iCE40 PCF pin-constraint file, or the board's `pcf` when unset."
        )
        pdc_cfg: Optional[Path] = Field(
            None,
            description="Nexus PDC pin-constraint file, or the board's `pdc` when unset. "
            "nextpnr-nexus requires every IO to be constrained.",
        )
        seed: Optional[int] = Field(
            None,
            description="Seed for nextpnr's placer. Different seeds give different results; "
            "sweeping the seed is a common way to squeeze out extra Fmax.",
        )
        randomize_seed: bool = Field(
            False,
            description="Use a fresh random seed on every run. Makes results non-reproducible; "
            "set `seed` instead to pin one.",
        )
        timing_allow_fail: bool = Field(
            False,
            description="Let the flow succeed even when timing constraints are not met.",
        )
        ignore_loops: bool = Field(
            False, description="ignore combinational loops in timing analysis"
        )

        textcfg: Optional[Path] = Field(
            Path("config.txt"),
            description="ECP5 routed design written as a Trellis textual configuration file, "
            "which the bitstream packer (and the `openfpgaloader` flow) consumes.",
        )
        asc: Optional[Path] = Field(
            Path("config.asc"), description="iCE40 ASCII configuration written with `--asc`."
        )
        fasm: Optional[Path] = Field(
            Path("config.fasm"), description="Nexus FASM configuration written with `--fasm`."
        )
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
        sdc: Optional[Path] = Field(None, description="Generic SDC timing constraints file.")
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
            None, description="Write the post-routing design to this JSON file."
        )
        sdf: Optional[Path] = Field(
            None,
            description="Write post-routing timing to this SDF file, for timing-annotated "
            "netlist simulation.",
        )
        log: Optional[Path] = Field(
            Path("nextpnr.log"), description="File nextpnr writes its log to."
        )
        report: Optional[Path] = Field(
            Path("report.json"),
            description="File nextpnr writes its JSON utilization/timing report to. This is what "
            "the flow parses its results from.",
        )
        detailed_timing_report: bool = Field(
            False,
            description="Ask nextpnr for a detailed per-path timing report. Known to be unstable "
            "and may crash nextpnr.",
        )
        placed_svg: Optional[Path] = Field(
            None, description="Render the placed design to this SVG file."
        )
        routed_svg: Optional[Path] = Field(
            None, description="Render the routed design to this SVG file."
        )
        parallel_refine: bool = Field(
            False,
            description="Enable nextpnr's parallel placement refinement. Faster on many cores, "
            "and only available in some nextpnr builds.",
        )
        yosys: YosysFpga.Settings = Field(
            default_factory=YosysFpga.Settings,
            description="Settings for the `yosys_fpga` dependency that synthesizes the design. "
            "`fpga` and `clocks` are propagated automatically.",
        )

        dependency_settings = {"yosys": ("fpga", "clocks")}

    def init(self) -> None:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        yosys = ss.resolve_dependency("yosys")  # adopts an `fpga` given only for yosys_fpga
        self._target()  # rejects an unsupported target before its synthesis runs
        self.add_dependency(YosysFpga, yosys)

    def _target(self) -> Tuple[str, List[str]]:
        """The nextpnr architecture for `fpga`, and the arguments selecting its device.

        Rejects a target this flow has no tested device, constraint and output mapping for, and
        a setting only another architecture takes.
        """
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        fpga = ss.fpga
        assert fpga is not None, "checked at launch (`required_settings`)"
        family = (fpga.family or "").lower()
        if family not in NEXTPNR_FAMILIES:
            raise FlowSettingsException(
                f"nextpnr has no tested device, constraint and output mapping for "
                f"fpga.family={family or None!r}; supported are {', '.join(NEXTPNR_FAMILIES)}."
            )
        misplaced = [
            name
            for name, families in FAMILY_SETTINGS.items()
            if family not in families
            and getattr(ss, name) is not None
            and getattr(ss, name) is not False
        ]
        if misplaced:
            raise FlowSettingsException(f"nextpnr-{family} does not take {', '.join(misplaced)}.")
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

    def run(self) -> None:
        """Place and route the netlist produced by Yosys."""
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        yosys_flow = self.completed_dependencies[0]
        assert isinstance(yosys_flow, YosysFpga)
        assert isinstance(yosys_flow.settings, YosysFpga.Settings)
        assert yosys_flow.settings.netlist_json
        netlist_json = yosys_flow.run_path / yosys_flow.settings.netlist_json
        fpga_family, target_args = self._target()
        next_pnr = NextpnrTool(executable=f"nextpnr-{fpga_family}")

        if not netlist_json.exists():
            raise FlowFatalError(f"netlist json file {netlist_json} does not exist!")

        args = setting_flag(netlist_json, name="json")
        args += setting_flag(ss.clock_period and (1000 / ss.clock_period), name="freq")
        args += setting_flag(self.design.rtl.top)
        args += setting_flag(ss.seed)
        args += target_args
        # Settings of another architecture were rejected by `_target`.
        for name in FAMILY_SETTINGS:
            value = getattr(ss, name)
            if name in ("lpf_cfg", "pcf_cfg", "pdc_cfg"):
                continue  # the constraint file is passed below
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
        if fpga_family == "ecp5" and not ss.out_of_context:
            outputs = ("textcfg", *outputs)
        elif fpga_family == "ice40":
            outputs = ("asc", *outputs)
        elif fpga_family == "nexus":
            outputs = ("fasm", *outputs)
        for name in outputs:
            args += setting_flag(getattr(ss, name), name=name)
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
        for name in ("sdc", "pre_pack", "pre_place", "pre_route", "post_route", "py_script"):
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
        constraint_name = {"ecp5": "lpf", "ice40": "pcf", "nexus": "pdc"}[fpga_family]
        with self._constraint_file(constraint_name) as constraint:
            next_pnr.run(*setting_flag(constraint, name=constraint_name), *args)
        for name in outputs:
            path = getattr(ss, name)
            if name != "report" and path:
                path = path if path.is_absolute() else self.run_path / path
                if path.is_file():
                    self.artifacts[name] = path

    @contextmanager
    def _constraint_file(self, kind: str) -> Iterator[Path | str | None]:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        explicit = getattr(ss, f"{kind}_cfg")
        board_data = None if explicit else ss.board_data()
        if not board_data or kind not in board_data:
            yield self.normalize_path_to_design_root(explicit) if explicit else None
            return
        uri = board_data[kind]
        r = urlparse(uri)
        if r.scheme and r.netloc:
            try:
                lpf, _ = urlretrieve(uri)
            except HTTPError as e:
                log.critical("Unable to retrieve file from %s (HTTP Error %d)", uri, e.code)
                raise FlowFatalError("Unable to retrieve LPF file") from None
            yield lpf
        else:
            with ss.board_file(uri) as lpf_path:
                yield lpf_path

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
        if not report_path.exists():
            log.error("nextpnr report file %s does not exist!", report_path)
            return False
        try:
            report = json.loads(report_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.error("Failed to read nextpnr report %s: %s", report_path, e)
            return False
        self.artifacts["report"] = str(report_path)

        timing_met = self._parse_fmax(report.get("fmax") or {})
        self._parse_utilization(report.get("utilization") or {})
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
        fpga = self.settings.fpga
        family = (fpga.family or "").lower() if fpga else ""
        if family != "ecp5":
            if detail:
                log.debug(
                    "No canonical resource mapping for fpga.family=%r; reporting raw nextpnr "
                    "bel-type counts only.",
                    family or None,
                )
            return
        for canonical, cell_types in ECP5_RESOURCES.items():
            present = [c for c in cell_types if c in detail]
            if present:
                self.results[canonical] = sum(detail[c]["used"] for c in present)

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
