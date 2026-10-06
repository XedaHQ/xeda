import contextlib
import gzip
import hashlib
import logging
import re
import tempfile
from pathlib import Path
from typing import List, Literal, Optional, Tuple, Union

from ...dataclass import WORKING, Field, XedaBaseModel, field_validator, model_validator
from ...design import SourceType
from ...flow import Flow, FlowSettingsError, Out, SynthFlow, describe_results
from ...platforms import AsicsPlatform
from ...run_dir import RunDirectory
from ...utils import replacing_file, unique
from ..ghdl import GhdlSynth
from .common import YosysBase, append_flag, process_parameters

log = logging.getLogger(__name__)


ORIGINAL_PIN_PATTERN = re.compile(r"(.*original_pin.*)")
EXCLAIM_PATTERN = re.compile(r":\s+(!.*)\s+;")


def merge_files(in_files, out_file, add_newline=False):
    with replacing_file(out_file) as out_f:
        for in_file in in_files:
            with open(in_file) as in_f:
                out_f.write(in_f.read())
            if add_newline:
                out_f.write("\n")


def clean_ascii(s: str) -> str:
    return s.encode("ascii", "ignore").decode("ascii")


def preproc_lib_content(content: str, dont_use_cells: List[str]) -> str:
    # set dont_use
    pattern1 = r"(^\s*cell\s*\(\s*([\"]*" + '["]*|["]*'.join(dont_use_cells) + r"[\"]*)\)\s*\{)"
    content, count = re.subn(pattern1, r"\1\n    dont_use : true;", content, flags=re.MULTILINE)
    if count:
        log.info("Marked %d cells as dont_use", count)

    # # Yosys-abc throws an error if original_pin is found within the liberty file.
    # The following substitution is *very* slow and therefore has been disabled.
    # Possibly sub-optimal/incorrect regex.
    # content, count = re.subn(ORIGINAL_PIN_PATTERN, r"/* \1 */;", content)
    # if count:
    #     log.info("Commented %d lines containing 'original_pin", count)

    # Yosys does not like properties that start with : !, without quotes
    content, count = re.subn(EXCLAIM_PATTERN, r': "\1" ;', content)
    if count:
        log.info("Replaced %d malformed functions", count)
    return content


_CELL_PATTERN = re.compile(r"^\s*cell\s*\(\s*(\w+)\s*\)")
_LIBRARY_PATTERN = re.compile(r"^\s*library\s*\(\s*(\S+)\s*\)")


def merge_libs(in_files, out_file, new_lib_name=None):
    log.info(
        "Merging libraries %s into library %s (%s)",
        ", ".join(str(f) for f in in_files),
        new_lib_name,
        out_file,
    )

    all_cell_names = set()
    skip_this_cell = False

    with replacing_file(out_file) as out:
        with open(in_files[0]) as hf:
            for line in hf.readlines():
                match_library = _LIBRARY_PATTERN.match(line)
                if match_library:
                    if not new_lib_name:
                        new_lib_name = match_library.group(1)
                    out.write(f"library ({new_lib_name}) {{\n")
                elif re.search(_CELL_PATTERN, line):
                    break
                else:
                    out.write(line)
        for f in in_files:
            with open(f) as f:
                flag = 0
                for line in f.readlines():
                    cell_match = _CELL_PATTERN.match(line)
                    if cell_match:
                        if flag != 0:
                            raise Exception("Error! new cell before finishing previous one.")
                        flag = 1
                        cell_name = cell_match.group(1)
                        if cell_name in all_cell_names:
                            log.warning("Skipping duplicate cell %s", cell_name)
                            skip_this_cell = True
                        else:
                            all_cell_names.add(cell_name)
                            out.write("\n" + line)
                            skip_this_cell = False
                    elif flag > 0:
                        flag += len(re.findall(r"\{", line))
                        flag -= len(re.findall(r"\}", line))
                        if not skip_this_cell:
                            out.write(line)
        out.write("\n}\n")


def preproc_libs(
    in_files,
    merged_file,
    dont_use_cells: List[str],
    new_lib_name=None,
    use_temp_folder=True,
    run_directory: Optional[RunDirectory] = None,
):
    """Mark selected Liberty cells as `dont_use`, then merge the inputs. A file it writes where
    a flow runs goes through the flow's `run_directory` (`RunDirectory.writable`)."""
    if run_directory is not None:
        run_directory.writable(merged_file)
    in_files = unique(in_files)
    proc_files: list[Path] = []
    if len(in_files) > 1:
        log.info(f"Processing {len(in_files)} libraries")
    tmp_context: Union[tempfile.TemporaryDirectory[str], contextlib.ExitStack]
    if use_temp_folder:
        tmp_context = tempfile.TemporaryDirectory(prefix="xeda.yosys.processed_libs")
    else:
        tmp_context = contextlib.ExitStack()  # no-op
    with tmp_context as tmp:
        if use_temp_folder:
            assert isinstance(tmp, str)
            temp_path = Path(tmp)
        else:
            temp_path = Path("processed_libs")
            temp_path.mkdir(parents=True, exist_ok=True)
        for in_file in in_files:
            in_file = Path(in_file)
            log.info("Pre-processing liberty file: %s", in_file)
            suffix = in_file.suffix
            if suffix and suffix[1:] in ["gz", "gzip"]:
                ctx = gzip.open(in_file, "rt")
                suffix = ".".join(in_file.suffixes[:-1]) if len(in_file.suffixes) > 1 else ".lib"
            else:
                ctx = open(in_file, encoding="utf-8")
            with ctx as f:
                content = clean_ascii(f.read())
            # Named after the library's stem, unless another library already took that name:
            # `a/cells.lib` and `b/cells.lib` are two libraries, not one written twice.
            out_file = temp_path / f"{in_file.stem}-mod{suffix}"
            if out_file in proc_files:
                digest = hashlib.sha256(str(in_file.absolute()).encode()).hexdigest()[:8]
                out_file = temp_path / f"{in_file.stem}-{digest}-mod{suffix}"
            log.info("Writing pre-processed file: %s", out_file)
            if run_directory is not None and not use_temp_folder:
                run_directory.writable(out_file)
            with replacing_file(out_file) as f:
                f.write(preproc_lib_content(content, dont_use_cells))
            proc_files.append(out_file)
        merge_libs(proc_files, merged_file, new_lib_name)
    log.info("Merged lib: %s", str(Path(merged_file).absolute()))


def abc_opt_script(opt: Optional[str]) -> Optional[str]:
    """The abc mapping script for `optimize`, as the `abc_script` setting of `Yosys` takes it.

    The scripts are OpenROAD-flow-scripts' `abc_area.script` and `abc_speed.script`, written
    inline (``+cmd;cmd;...``, which `Yosys.Settings` adapts for `abc -script`). `None` keeps
    yosys's own default mapping script.
    """
    if opt is None:
        return None
    if opt == "area":
        scr = ["strash", "dch", "map -B 0.9", "topo", "stime -c", "buffer -c"]
    else:
        scr = [
            # fmt: off
            "&get -n", "&st", "&dch", "&nf", "&put", "&get -n", "&st", "&syn2", "&if -g -K 6", "&synch2", "&nf",
            "&put", "&get -n", "&st", "&syn2", "&if -g -K 6", "&synch2", "&nf", "&put", "&get -n", "&st", "&syn2",
            "&if -g -K 6", "&synch2", "&nf", "&put", "&get -n", "&st", "&syn2", "&if -g -K 6", "&synch2", "&nf",
            "&put", "&get -n", "&st", "&syn2", "&if -g -K 6", "&synch2", "&nf", "&put", "buffer -c", "topo", "stime -c",
            # fmt: on
        ]
    return "+" + ";".join([*scr, "upsize -c", "dnsize -c"])


class HiLoMap(XedaBaseModel):
    hi: Tuple[str, str]
    lo: Tuple[str, str]
    singleton: bool = True


class Yosys(YosysBase, SynthFlow):
    """
    Yosys Open SYnthesis Suite: ASICs and generic gate/LUT synthesis
    """

    results_description = describe_results(
        "area",
        **{
            "cells": "Total number of cells in the synthesized netlist.",
        },
    )

    class Settings(YosysBase.Settings, SynthFlow.Settings):
        platform: Optional[AsicsPlatform] = Field(
            None,
            description="ASIC platform (PDK) to map to: a bundled platform name (see "
            "`xeda list-platforms`) or a path to a config.toml. Supplies whatever mapping to it "
            "needs that is not set explicitly: its corner's liberty set, merged into one library "
            "with its dont-use cells marked, its flip-flop library, its mapping files, tie and "
            "buffer cells, and abc's driver cell and load.",
        )
        corner: Optional[Union[str, List]] = Field(
            None,
            description='Corner of the platform whose liberty set is mapped to, e.g. "tt". '
            "Defaults to the platform's own default corner. Needs `platform`.",
        )
        liberty: List[Path] = Field(
            [], alias="library", description="Standard cell (liberty) libraries to use"
        )
        dff_liberty: Optional[Path] = Field(
            None, alias="dff_library", description="Additional liberty file for mapping flip-flops"
        )
        dont_use_cells: List[str] = Field(
            [],
            description="Standard cells abc must not use, in addition to the platform's own "
            "dont-use list.",
        )
        optimize: Optional[Literal["speed", "area"]] = Field(
            "area",
            description="Optimization target when mapping to a liberty library: selects "
            "OpenROAD-flow-scripts' abc mapping script for it, and post-synthesis optimization "
            "unless `post_synth_opt` says otherwise. `null` keeps yosys's default mapping script. "
            "An explicit `abc_script` replaces the selected script.",
        )
        abc_driver_cell: Optional[str] = Field(
            None,
            description="Cell abc assumes drives the primary inputs when mapping to a liberty "
            "library. Defaults to the platform's.",
        )
        abc_load_in_ff: Optional[float] = Field(
            None,
            description="Wire load abc assumes when mapping to a liberty library, in units of "
            "flip-flop input capacitance. Defaults to the platform's.",
        )
        netlist_attrs: Optional[bool] = Field(
            None,
            description="Include cell and wire attributes in the written Verilog netlist. Unset: "
            "included, except when mapping to a liberty library, whose gate-level netlist is "
            "written for a place and route.",
        )
        post_synth_opt: Optional[bool] = Field(
            None,
            description="Run additional optimization after synthesis. Unset: on when mapping to "
            "a liberty library with an `optimize` target, off otherwise.",
        )
        netlist_hex: Optional[bool] = Field(
            None,
            description="Write constants in the netlist in hexadecimal. Unset: hexadecimal, "
            "except when mapping to a liberty library.",
        )
        gates: Optional[List[str]] = Field(
            None,
            description="Map to this generic gate set instead of a liberty library, e.g. "
            '["AND", "OR", "NOT"]. Accepts a comma-separated string.',
        )
        lut: Optional[str] = Field(
            None,
            description='Map to LUTs of this size instead of standard cells, e.g. "4" or a '
            '"<width>:<cost>" pair.',
        )
        stop_after: Optional[Literal["rtl"]] = Field(
            None,
            description='Stop the flow after this stage. "rtl" elaborates the design and writes '
            "the RTL outputs without synthesizing.",
        )
        adder_map: Optional[Path] = Field(
            None, description="Verilog file with technology-specific adder cell mappings."
        )
        clockgate_map: Optional[Path] = Field(
            None, description="Verilog file with technology-specific clock-gating cell mappings."
        )
        other_maps: Optional[List[Path]] = Field(
            None,
            description="Additional Verilog files with technology-specific cell mappings. Unset: "
            "the platform's latch mapping when mapping to a liberty library, none otherwise; an "
            "empty list means none.",
        )
        hilomap: Optional[HiLoMap] = Field(
            None,
            description="Tie-high/tie-low cells used to drive constant nets, for technologies "
            "that forbid connecting logic directly to the rails.",
        )
        insbuf: Union[None, Tuple[str, str, str], List[str]] = Field(
            None,
            description="Buffer cell inserted on otherwise undriven or directly-connected wires, "
            "as (cell_name, input_port, output_port).",
        )
        merge_libs_to: Optional[Path] = Field(
            None,
            description="Merge all liberty libraries into this single file before synthesis, "
            "which some yosys versions require when several corners are given.",
            json_schema_extra=WORKING,
        )

        @field_validator("platform", mode="before")
        @classmethod
        def _validate_platform(cls, value):
            value = AsicsPlatform.from_setting(value)
            if isinstance(value, AsicsPlatform):
                # `corner` is selected on it below: never on a caller's own platform
                value = value.model_copy(deep=True)
            return value

        @model_validator(mode="after")
        def _select_platform_corner(self):
            if self.corner:
                if self.platform is None:
                    raise ValueError(
                        "`corner` selects one of a platform's corners: give `platform` too"
                    )
                corner = self.corner[0] if isinstance(self.corner, list) else self.corner
                self.platform.select_corner(corner)
            return self

        def maps_to_liberty(self) -> bool:
            """Whether abc maps to a liberty library -- `liberty`, or a `platform`'s -- rather
            than to generic gates or LUTs: what the platform-derived settings act on."""
            return not self.gates and not self.lut and (bool(self.liberty) or bool(self.platform))

        def dont_use(self) -> List[str]:
            """The cells marked `dont_use`: the platform's own, then `dont_use_cells`."""
            platform_s = self.platform.dont_use_cells if self.platform else []
            return unique([*platform_s, *self.dont_use_cells])

        def abc_constraints(self) -> List[str]:
            """abc's constraints: the driver cell and load (`abc_driver_cell`, `abc_load_in_ff`,
            else the platform's) when mapping to a liberty library, then `abc_constr`."""
            lines = []
            if self.maps_to_liberty():
                platform = self.platform
                driver = self.abc_driver_cell or (platform.abc_driver_cell if platform else None)
                load = self.abc_load_in_ff
                if load is None and platform:
                    load = platform.abc_load_in_ff
                if driver:
                    lines.append(f"set_driving_cell {driver}")
                if load is not None:
                    lines.append(f"set_load {load}")
            return [*lines, *self.abc_constr]

    class Outputs(SynthFlow.Outputs):
        netlist: Path | None = Out(
            SourceType.VerilogNetlist,
            enabled_by="netlist_verilog",
            description="The synthesized gate-level Verilog netlist, written at "
            "`netlist_verilog`, for a place and route such as openroad.",
        )

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Refuse settings the run cannot honor, before anything runs: a netlist asked of a run
        that stops before writing one, and abc's cell settings where nothing maps to cells."""
        assert isinstance(settings, cls.Settings)
        problems = []
        if settings.stop_after == "rtl" and settings.netlist_verilog:
            problems.append(
                (
                    "stop_after",
                    "`stop_after: rtl` writes no netlist, and `netlist_verilog` asks for one: "
                    "set `netlist_verilog` to null (`-s netlist_verilog=`)",
                )
            )
        if not settings.maps_to_liberty():
            for name in ("abc_driver_cell", "abc_load_in_ff"):
                if getattr(settings, name) is not None:
                    problems.append(
                        (
                            name,
                            f"`{name}` acts on mapping to a liberty library: give `platform` or "
                            "`liberty` too",
                        )
                    )
        if problems:
            raise FlowSettingsError(
                [(key, message, None, "value_error") for key, message in problems], cls.Settings
            )

    def _derive_from_platform(self) -> None:
        """Complete the settings that mapping to a liberty library needs and that were not set:
        from the `platform`, its maps and cells; and, for any liberty mapping, flattening, the
        abc script `optimize` selects with post-synthesis optimization, and a gate-level netlist
        (no attributes, no hexadecimal constants). An explicit setting is never replaced: an
        unset (`None`) one is what is derived, so `post_synth_opt: false` stands beside the
        default `optimize: area`."""
        ss = self.settings
        assert isinstance(ss, self.Settings)
        mapping = ss.maps_to_liberty()
        if ss.netlist_attrs is None:
            ss.netlist_attrs = not mapping
        if ss.netlist_hex is None:
            ss.netlist_hex = not mapping
        if ss.post_synth_opt is None:
            ss.post_synth_opt = mapping and ss.optimize is not None
        platform = ss.platform
        if ss.other_maps is None:
            latch_map = platform.latch_map_file if mapping and platform else None
            ss.other_maps = [latch_map] if latch_map else []
        if not mapping:
            return
        if platform:
            if ss.adder_map is None:
                ss.adder_map = platform.adder_map_file
            if ss.clockgate_map is None:
                ss.clockgate_map = platform.clkgate_map_file
            if (
                ss.hilomap is None
                and platform.tiehi_cell
                and platform.tiehi_port
                and platform.tielo_cell
                and platform.tielo_port
            ):
                ss.hilomap = HiLoMap(
                    hi=(platform.tiehi_cell, platform.tiehi_port),
                    lo=(platform.tielo_cell, platform.tielo_port),
                )
            if ss.insbuf is None and platform.min_buf_cell:
                ss.insbuf = (
                    platform.min_buf_cell,
                    platform.min_buf_ports[0],
                    platform.min_buf_ports[1],
                )
        if ss.flatten is None:
            ss.flatten = True
        if ss.optimize is not None and ss.abc_script is None:
            ss.abc_script = abc_opt_script(ss.optimize)
        ss.abc_constr = ss.abc_constraints()

    def run(self) -> None:
        assert isinstance(self.settings, self.Settings)
        # TODO factor out common code
        ss = self.settings
        self.prepare_output_parents()
        declared = self.outputs
        assert isinstance(declared, self.Outputs)
        if ss.netlist_verilog:
            declared.netlist = self.run_path / ss.netlist_verilog

        # the platform's liberty set is merged into one library, as the place and route that
        # reads the netlist merges its own: abc and `dfflibmap` are handed one file
        liberty_from_platform = bool(ss.platform) and not ss.liberty
        if ss.platform:
            if not ss.liberty:
                ss.liberty = ss.platform.default_corner_settings.lib_files
            if not ss.dff_liberty:
                ss.dff_liberty = ss.platform.default_corner_settings.dff_lib_file
        self._derive_from_platform()

        ss.liberty = [self.normalize_path_to_design_root(lib) for lib in ss.liberty]
        if ss.dff_liberty:
            ss.dff_liberty = self.normalize_path_to_design_root(ss.dff_liberty)

        # a timing report only where `sta` writes one, as `yosys_fpga` lists it: an artifact this
        # run never writes is one a remote run is asked for and cannot send
        timing_report = ss.reports_dir / "timing.rpt"
        self.artifacts.timing_report = timing_report if ss.sta else None
        self.artifacts.utilization_report = ss.reports_dir / "utilization.json"
        # a previous run's reports must not pass for this run's
        self.run_directory.remove(self.artifacts.utilization_report, timing_report)
        if ss.gates:
            append_flag(ss.abc_flags, f"-g {','.join(ss.gates)}")
        elif ss.lut:
            append_flag(ss.abc_flags, f"-lut {ss.lut}")
        elif ss.liberty and ss.netlist_expr is None:
            ss.netlist_expr = False
        if ss.flatten:
            append_flag(ss.synth_flags, "-flatten")

        for lib in ss.liberty:
            if not lib.exists():
                raise FileNotFoundError(f"Specified liberty: {lib} does not exist!")

        dont_use = ss.dont_use()
        if ss.liberty and (liberty_from_platform or dont_use or ss.merge_libs_to):
            merge_libs_to = ss.merge_libs_to or Path("merged_lib")
            merged_lib_file = Path(f"{merge_libs_to}.lib")
            preproc_libs(
                ss.liberty,
                merged_lib_file,
                dont_use,
                (
                    f"{ss.platform.name}_merged"
                    if liberty_from_platform and ss.platform
                    else ss.merge_libs_to
                ),
                run_directory=self.run_directory,
            )
            ss.merge_libs_to = merge_libs_to
            ss.liberty = [merged_lib_file]

        abc_constr_file = None
        if ss.abc_constr:
            abc_constr_file = "abc.constr"
            with replacing_file(self.run_directory.writable(abc_constr_file)) as f:
                f.write("\n".join(ss.abc_constr) + "\n")

        script_path = self.copy_from_template(
            f"yosys_synth{self.script_ext}",
            lstrip_blocks=True,
            trim_blocks=False,
            ghdl_args=GhdlSynth.synth_args(ss.ghdl, self.design, one_shot_elab=False),
            parameters=process_parameters(self.design.rtl.parameters),
            defines=[f"-D{k}" if v is None else f"-D{k}={v}" for k, v in ss.defines.items()],
            abc_constr_file=abc_constr_file,
        )
        log.info("Yosys script: %s", script_path.absolute())
        args = [self.script_flag, script_path]
        if ss.log_file:
            log.info("Logging yosys output to %s", ss.log_file)
            args += ["-L", ss.log_file]
        depfile = self.run_path / "yosys.d"
        args += ["-E", depfile]
        self.depfiles.append(depfile)
        self.yosys.run(*args)

    def parse_reports(self) -> bool:
        assert isinstance(self.settings, self.Settings)
        if not self.artifacts.utilization_report:
            return True
        utilization = self.get_utilization()
        if not utilization:
            return False
        mod_util = utilization.get("modules")
        if mod_util:
            self.results["_hierarchical_utilization"] = mod_util
        design_util = utilization.get("design")
        if design_util:
            num_cells_by_type = design_util.get("num_cells_by_type", {})
            self.results.update(**num_cells_by_type)
            num_cells = design_util.get("num_cells")
            if num_cells is not None:
                self.results["cells"] = num_cells
            area = design_util.get("area")
            if area:
                self.results["area"] = area
            self.results["_utilization"] = design_util
        return True
