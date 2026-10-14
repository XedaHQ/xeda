import contextlib
import gzip
import hashlib
import logging
import re
import tempfile
from pathlib import Path
from typing import ClassVar, Dict, List, Literal, Optional, Tuple, Union

from ...dataclass import WORKING, Field, XedaBaseModel
from ...design import SourceType
from ...flow import Flow, FlowSettingsError, Out, SynthFlow, describe_results
from ...flow.asic import AsicPlatformFlow, AsicPlatformSettings
from ...run_dir import RunDirectory
from ...utils import replacing_file, unique
from ..ghdl import GhdlSynth
from .common import (
    STOP_AFTER_DESCRIPTION,
    YosysSynthesisBase,
    append_flag,
    process_parameters,
    stop_after_conflicts,
)

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


def dont_use_pattern(name: str) -> str:
    """Return the regular expression that matches the cells `name` names. A `*` stands for any
    characters of a cell name, a `?` for one, and every other character for itself."""
    wildcards = {"*": r"[^\s\"()]*", "?": r"[^\s\"()]"}
    return "".join(wildcards.get(char) or re.escape(char) for char in name)


def preproc_lib_content(content: str, dont_use_cells: List[str]) -> str:
    # mark the cells that abc must not use
    if dont_use_cells:
        names = "|".join(dont_use_pattern(name) for name in dont_use_cells)
        pattern1 = r"(^\s*cell\s*\(\s*[\"]*(?:" + names + r")[\"]*\s*\)\s*\{)"
        content, count = re.subn(pattern1, r"\1\n    dont_use : true;", content, flags=re.MULTILINE)
        if count:
            log.info("Marked %d cells as dont_use", count)

    # Yosys-abc fails on `original_pin` in a liberty file. The substitution below is very slow,
    # so it is disabled. Its regex may be wrong.
    # content, count = re.subn(ORIGINAL_PIN_PATTERN, r"/* \1 */;", content)
    # if count:
    #     log.info("Commented %d lines containing 'original_pin", count)

    # Yosys rejects an unquoted function that starts with `!`, so quote it.
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
    """Mark selected Liberty cells as `dont_use`, then merge the libraries.

    With a `run_directory`, each file written in a flow's run directory goes through
    `RunDirectory.writable`.
    """
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
            # Name the file after the library's stem. When another library already has that name,
            # add a digest of the path: `a/cells.lib` and `b/cells.lib` are two libraries.
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
    """Return the abc mapping script for `optimize`, in the form of the `abc_script` setting.

    The scripts are the `abc_area.script` and `abc_speed.script` of OpenROAD-flow-scripts. They are
    written inline as `+cmd;cmd;...`, and `Yosys.Settings` adapts that text for `abc -script`.
    `None` selects yosys's own default mapping script.
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


#: The roles of the platform files that `yosys` opens: the Liberty files, and the mapping files.
#: The derivation of the platform model adds the LEF files.
PLATFORM_ROLES = ("liberty", "latch_map", "clkgate_map", "adder_map")


class Yosys(AsicPlatformFlow, YosysSynthesisBase, SynthFlow):
    """Synthesize a design with Yosys to a standard-cell, gate or LUT netlist."""

    results_description = describe_results(
        "area",
        **{
            "cells": "Total number of cells in the synthesized netlist.",
            "platform": "The platform that the netlist is mapped to, when the run maps to one: "
            "`name`, the PDK `provider` and `version`, the `corner`, and `sources`, the layer "
            "(derived, upstream, residue or user) that gave each value of the platform model.",
        },
    )

    class Settings(AsicPlatformSettings, YosysSynthesisBase.Settings, SynthFlow.Settings):
        removed_settings: ClassVar[Dict[str, str]] = {
            **YosysSynthesisBase.Settings.removed_settings,
            "retime": "`abc_dff` (on by default), which passes `-dff` to `abc`, and `abc_flags` "
            "for more options of `abc`; yosys's `synth` has no retime option",
            "netlist_verilog_extmem": "`netlist_verilog_flags: [-extmem]`, which `write_verilog` gets",
        }
        nosynth: bool = Field(False, description="Do not run `synth`.")
        liberty: List[Path] = Field(
            [],
            alias="library",
            description="Standard cell (liberty) libraries to map to. Without them, the Liberty "
            "files of the platform are merged into one library and used.",
        )
        dff_liberty: Optional[Path] = Field(
            None, alias="dff_library", description="Additional liberty file for mapping flip-flops."
        )
        optimize: Optional[Literal["speed", "area"]] = Field(
            "area",
            description="Optimization target when mapping to a liberty library. It selects the "
            "abc mapping script of OpenROAD-flow-scripts for that target and turns on "
            "post-synthesis optimization unless `post_synth_opt` says otherwise. `null` keeps "
            "yosys's default mapping script. An explicit `abc_script` replaces the selected "
            "script.",
        )
        abc_driver_cell: Optional[str] = Field(
            None,
            description="Cell abc assumes drives the primary inputs when mapping to a liberty "
            "library. Defaults to the cell of the platform model.",
        )
        abc_load_in_ff: Optional[float] = Field(
            None,
            description="Wire load abc assumes when mapping to a liberty library, in units of "
            "flip-flop input capacitance. Defaults to the load of the platform model.",
        )
        netlist_attrs: Optional[bool] = Field(
            None,
            description="Include cell and wire attributes in the written Verilog netlist. Unset: "
            "included, except when mapping to a liberty library (that netlist is written for "
            "place and route).",
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
            '["AND", "OR", "NOT"]. Accepts a comma-separated string. Refused together with '
            "`platform` or `platform_file`.",
        )
        lut: Optional[str] = Field(
            None,
            description='Map to LUTs of this size instead of standard cells, e.g. "4" or a '
            '"<width>:<cost>" pair. Refused together with `platform` or `platform_file`.',
        )
        stop_after: Optional[Literal["rtl"]] = Field(None, description=STOP_AFTER_DESCRIPTION)
        adder_map: Optional[Path] = Field(
            None, description="Verilog file with technology-specific adder cell mappings."
        )
        clockgate_map: Optional[Path] = Field(
            None, description="Verilog file with technology-specific clock-gating cell mappings."
        )
        other_maps: Optional[List[Path]] = Field(
            None,
            description="Additional Verilog files with technology-specific cell mappings. Unset: "
            "the latch mapping of the platform when mapping to a liberty library, none otherwise; "
            "an empty list means none.",
        )
        hilomap: Optional[HiLoMap] = Field(
            None,
            description="Tie-high/tie-low cells used to drive constant nets, for technologies "
            "that forbid connecting logic directly to the rails.",
        )
        insbuf: Union[None, Tuple[str, str, str], List[str]] = Field(
            None,
            description="Buffer cell inserted on otherwise undriven or directly connected wires, "
            "as (cell_name, input_port, output_port).",
        )
        merge_libs_to: Optional[Path] = Field(
            None,
            description="Merge all liberty libraries into this single file before synthesis, "
            "which some yosys versions require when several corners are given.",
            json_schema_extra=WORKING,
        )

        def uses_platform(self) -> bool:
            """Return whether abc maps to the liberty library of a platform.

            That is the case with `platform` or `platform_file`. The platform then supplies the
            mapping inputs that no setting gives."""
            return self.platform is not None or self.platform_file is not None

        def maps_to_liberty(self) -> bool:
            """Return whether abc maps to a liberty library, from `liberty` or the platform.

            It does not when `gates` or `lut` is set. The platform-derived settings act only
            when it does.
            """
            return not self.gates and not self.lut and (bool(self.liberty) or self.uses_platform())

        def abc_constraints(self) -> List[str]:
            """Return the abc constraint lines.

            With a liberty library, the first lines are the driver cell and the load
            (`abc_driver_cell` and `abc_load_in_ff`). `abc_constr` follows.
            """
            lines = []
            if self.maps_to_liberty():
                if self.abc_driver_cell:
                    lines.append(f"set_driving_cell {self.abc_driver_cell}")
                if self.abc_load_in_ff is not None:
                    lines.append(f"set_load {self.abc_load_in_ff}")
            return [*lines, *self.abc_constr]

    class Outputs(SynthFlow.Outputs):
        netlist: Path | None = Out(
            SourceType.VerilogNetlist,
            enabled_by="netlist_verilog",
            description="The synthesized gate-level Verilog netlist, written at "
            "`netlist_verilog`, for a place and route tool.",
        )

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Refuse settings the run cannot honor, before anything runs.

        These are a result that a run stopped by `stop_after` does not write
        (`stop_after_conflicts`), a platform together with `gates` or `lut`, and the abc cell
        settings when abc maps to no cells.
        """
        super().check_settings_supported(settings)
        assert isinstance(settings, cls.Settings)
        problems = stop_after_conflicts(settings)
        if settings.uses_platform():
            selector = "platform" if settings.platform is not None else "platform_file"
            for name in ("gates", "lut"):
                if getattr(settings, name):
                    problems.append(
                        (
                            name,
                            f"`{name}` and `{selector}` cannot both be set: `{selector}` maps to "
                            f"the standard cells of the platform and `{name}` maps to generic "
                            "cells. Set only one of them",
                        )
                    )
        if not settings.maps_to_liberty():
            for name in ("abc_driver_cell", "abc_load_in_ff"):
                if getattr(settings, name) is not None:
                    problems.append(
                        (
                            name,
                            f"`{name}` acts on mapping to a liberty library: give `platform`, "
                            "`platform_file` or `liberty` too",
                        )
                    )
        if problems:
            raise FlowSettingsError(
                [(key, message, None, "value_error") for key, message in problems], cls.Settings
            )

    def init(self):
        """Resolve the Liberty files that the user gave against the design root."""
        super().init()
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        ss.liberty = [self.normalize_path_to_design_root(lib) for lib in ss.liberty]
        if ss.dff_liberty:
            ss.dff_liberty = self.normalize_path_to_design_root(ss.dff_liberty)

    def prepare_inputs(self) -> None:
        """Build the platform model, when abc maps to the liberty library of a platform."""
        super().prepare_inputs()
        assert isinstance(self.settings, self.Settings)
        if self.settings.uses_platform():
            self.prepare_platform(PLATFORM_ROLES)

    def locate_prepared_inputs(self) -> Optional[List[Path]]:
        """Return the ABC script file, then the files of the platform that `prepare_inputs` reads."""
        assert isinstance(self.settings, self.Settings)
        located = super().locate_prepared_inputs()
        if located is not None and self.settings.uses_platform():
            located = [*located, *self.locate_platform_inputs(PLATFORM_ROLES)]
        return located

    def _derive_from_platform(self) -> None:
        """Complete the settings that mapping to a liberty library needs and the user left unset.

        From the platform model: its maps and cells, and the driver cell and the load of abc. For
        a liberty mapping: flattening, the abc script that `optimize` selects, post-synthesis
        optimization, and a gate-level netlist (without attributes and hexadecimal constants).
        Without a liberty mapping, the netlist keeps attributes and hexadecimal constants. A
        setting that the user gave is never replaced. Only an unset (`None`) setting is derived,
        so `post_synth_opt: false` stays beside the default `optimize: area`.
        """
        ss = self.settings
        assert isinstance(ss, self.Settings)
        mapping = ss.maps_to_liberty()
        if ss.netlist_attrs is None:
            ss.netlist_attrs = not mapping
        if ss.netlist_hex is None:
            ss.netlist_hex = not mapping
        if ss.post_synth_opt is None:
            ss.post_synth_opt = mapping and ss.optimize is not None
        model = self.platform_model
        if ss.other_maps is None:
            latch_map = model.files.get("latch_map") if mapping and model else None
            ss.other_maps = latch_map[:1] if latch_map else []
        if not mapping:
            return
        if model:
            for name, role in (
                ("adder_map", "adder_map"),
                ("clockgate_map", "clkgate_map"),
            ):
                if getattr(ss, name) is None and model.files.get(role):
                    setattr(ss, name, model.files[role][0])
            if ss.hilomap is None and model.tie_high and model.tie_low:
                ss.hilomap = HiLoMap(
                    hi=(model.tie_high.cell, model.tie_high.port),
                    lo=(model.tie_low.cell, model.tie_low.port),
                )
            if ss.insbuf is None and model.min_buffer:
                buffer = model.min_buffer
                ss.insbuf = (buffer.cell, buffer.input, buffer.output)
            if ss.abc_driver_cell is None:
                ss.abc_driver_cell = model.abc_driver_cell
            if ss.abc_load_in_ff is None:
                ss.abc_load_in_ff = model.abc_load_in_ff
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

        # Merge the platform's liberty set into one library, as the place and route that reads
        # the netlist does. Then abc and `dfflibmap` get one file.
        model = self.platform_model
        assert model is not None or not ss.uses_platform(), "prepare_inputs() builds the model"
        liberty_from_platform = model is not None and not ss.liberty
        if model is not None and liberty_from_platform:
            ss.liberty = list(model.files.get("liberty", []))
        self._derive_from_platform()
        # List a timing report only when `sta` writes one, as `yosys_fpga` does. A remote run is
        # asked for every listed artifact and cannot send one that was never written.
        timing_report = ss.reports_dir / "timing.rpt"
        utilization_report = ss.reports_dir / "utilization.json"
        self.artifacts.timing_report = timing_report if ss.sta else None
        # `stop_after: rtl` skips the stage that writes this report, so a stopped run lists none
        self.artifacts.utilization_report = utilization_report if ss.stop_after != "rtl" else None
        # an earlier run's reports must not stand for this run's
        self.run_directory.remove(utilization_report, timing_report)
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
                raise FileNotFoundError(f"Liberty file {lib} does not exist.")

        dont_use = self.dont_use_cells()
        if ss.liberty and (liberty_from_platform or dont_use or ss.merge_libs_to):
            merge_libs_to = ss.merge_libs_to or Path("merged_lib")
            merged_lib_file = Path(f"{merge_libs_to}.lib")
            preproc_libs(
                ss.liberty,
                merged_lib_file,
                dont_use,
                (
                    f"{model.name}_merged"
                    if liberty_from_platform and model is not None
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
        platform = self.platform_result()
        if platform is not None:
            self.results["platform"] = platform
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
