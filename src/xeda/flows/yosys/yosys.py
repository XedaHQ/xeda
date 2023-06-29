import contextlib
import gzip
import logging
import os
import re
from pathlib import Path
import tempfile
from typing import List, Literal, Optional, Tuple, Union

from ...dataclass import Field, XedaBaseModel, validator
from ...flow import SynthFlow
from ...platforms import AsicsPlatform
from ...utils import unique
from ..ghdl import GhdlSynth
from .common import YosysBase, append_flag, process_parameters

log = logging.getLogger(__name__)


ORIGINAL_PIN_PATTERN = re.compile(r"(.*original_pin.*)")
EXCLAIM_PATTERN = re.compile(r":\s+(!.*)\s+;")


def merge_files(in_files, out_file, add_newline=False):
    with open(out_file, "w") as out_f:
        for in_file in in_files:
            with open(in_file, "r") as in_f:
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

    with open(out_file, "w") as out:
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
            with open(f, "r") as f:
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
    in_files, merged_file, dont_use_cells: List[str], new_lib_name=None, use_temp_folder=True
):
    in_files = unique(in_files)
    merged_content = ""
    proc_files = []
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
            merged_content += preproc_lib_content(content, dont_use_cells)
            out_file = temp_path / in_file.with_name(in_file.stem + "-mod" + suffix).name
            log.info("Writing pre-processed file: %s", out_file)
            with open(out_file, "w") as f:
                f.write(merged_content)
            proc_files.append(out_file)
        merge_libs(proc_files, merged_file, new_lib_name)
    log.info("Merged lib: %s", str(Path(merged_file).absolute()))


class HiLoMap(XedaBaseModel):
    hi: Tuple[str, str]
    lo: Tuple[str, str]
    singleton: bool = True


class Yosys(YosysBase, SynthFlow):
    """
    Yosys Open SYnthesis Suite: ASICs and generic gate/LUT synthesis
    """

    class Settings(YosysBase.Settings, SynthFlow.Settings):
        platform: Optional[AsicsPlatform] = None
        liberty: List[Path] = Field(
            [], alias="library", description="Standard cell (liberty) libraries to use"
        )
        dff_liberty: Optional[Path] = Field(
            None, alias="dff_library", description="Additional liberty file for mapping flip-flops"
        )
        dont_use_cells: List[str] = []
        gates: Optional[List[str]] = None
        lut: Optional[str] = None
        optimize: Optional[Literal["speed", "area", "area+speed"]] = Field(
            "area", description="Optimization target"
        )
        stop_after: Optional[Literal["rtl"]]
        adder_map: Optional[str] = None
        clockgate_map: Optional[str] = None
        other_maps: List[str] = []
        abc_constr: List[str] = []
        abc_script: Union[None, Path, List[str]] = None
        hilomap: Optional[HiLoMap] = None
        insbuf: Union[None, Tuple[str, str, str], List[str]] = None
        merge_libs_to: Optional[str] = None

        @validator("liberty", pre=True, always=True)
        def _validate_liberty(cls, value):
            if isinstance(value, (Path, str)):
                value = [value]
            return value

        @validator("platform", pre=True, always=True)
        def _validate_platform(cls, value):
            if isinstance(value, str) and not value.endswith(".toml"):
                return AsicsPlatform.from_resource(value)
            elif isinstance(value, (str, Path)):
                return AsicsPlatform.from_toml(value)
            return value

        @validator("gates", pre=True, always=True)
        def _validate_commasep_to_list(cls, value):
            if isinstance(value, str):
                value = [x.strip() for x in value.split(",")]
            return value

    def run(self) -> None:
        assert isinstance(self.settings, self.Settings)
        # TODO factor out common code
        ss = self.settings

        def set_file_path(p):
            if not p or os.path.isabs(p):
                return p
            return self.design.root_path / p

        if ss.platform:
            if not ss.liberty:
                ss.liberty = ss.platform.default_corner_settings.lib_files
            if not ss.dff_liberty:
                ss.dff_liberty = ss.platform.default_corner_settings.dff_lib_file

        ss.liberty = [set_file_path(lib) for lib in ss.liberty]
        ss.dff_liberty = set_file_path(ss.dff_liberty)
        if isinstance(ss.abc_script, Path):
            ss.abc_script = set_file_path(ss.abc_script)

        self.artifacts.timing_report = ss.reports_dir / "timing.rpt"
        self.artifacts.utilization_report = ss.reports_dir / "utilization.json"
        if os.path.exists(self.artifacts.utilization_report):
            os.remove(self.artifacts.utilization_report)
        if os.path.exists(self.artifacts.timing_report):
            os.remove(self.artifacts.timing_report)
        if ss.gates:
            append_flag(ss.abc_flags, f"-g {','.join(ss.gates)}")
        elif ss.lut:
            append_flag(ss.abc_flags, f"-lut {ss.lut}")
        elif ss.liberty and ss.netlist_expr is None:
            ss.netlist_expr = False

        for lib in ss.liberty:
            if not lib.exists():
                raise FileNotFoundError(f"Specified liberty: {lib} does not exist!")

        if ss.liberty and (ss.dont_use_cells or ss.merge_libs_to):
            merge_libs_to = ss.merge_libs_to or "merged_lib"
            merged_lib_file = Path(f"{merge_libs_to}.lib")
            preproc_libs(
                ss.liberty,
                merged_lib_file,
                ss.dont_use_cells,
                ss.merge_libs_to,
            )
            ss.merge_libs_to = merge_libs_to
            ss.liberty = [merged_lib_file]

        abc_constr_file = None
        if ss.abc_constr:
            abc_constr_file = "abc.constr"
            with open(abc_constr_file, "w") as f:
                f.write("\n".join(ss.abc_constr) + "\n")
        abc_script_file = None
        if ss.abc_script:
            if isinstance(ss.abc_script, list):
                abc_script_file = "abc.script"
                with open(abc_script_file, "w") as f:
                    f.write("\n".join(ss.abc_script) + "\n")
            else:
                abc_script_file = str(ss.abc_script)

        script_path = self.copy_from_template(
            "yosys_synth.tcl",
            lstrip_blocks=True,
            trim_blocks=False,
            ghdl_args=GhdlSynth.synth_args(ss.ghdl, self.design),
            parameters=process_parameters(self.design.rtl.parameters),
            defines=[f"-D{k}" if v is None else f"-D{k}={v}" for k, v in ss.defines.items()],
            abc_constr_file=abc_constr_file,
            abc_script_file=abc_script_file,
        )
        log.info("Yosys script: %s", script_path.absolute())
        args = ["-c", script_path]
        if ss.log_file:
            log.info("Logging yosys output to %s", ss.log_file)
            args += ["-L", ss.log_file]
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
            area = design_util.get("area")
            if area:
                self.results["area"] = area
            self.results["_utilization"] = design_util
        return True
