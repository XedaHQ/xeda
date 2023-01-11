import json
import logging
import re
from pathlib import Path
from typing import List, Literal, Optional, Tuple, Union

from ...dataclass import Field, XedaBaseModel, validator
from ...design import SourceType
from ...flow import SynthFlow
from ...tool import Docker, Tool
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
    # Yosys-abc throws an error if original_pin is found within the liberty file.
    content, count = re.subn(ORIGINAL_PIN_PATTERN, r"/* \1 */;", content)
    if count:
        log.info("Commented %d lines containing 'original_pin", count)
    # Yosys does not like properties that start with : !, without quotes
    content, count = re.subn(EXCLAIM_PATTERN, r': "\1" ;', content)
    if count:
        log.info("Replaced %d malformed functions", count)
    return content


_CELL_PATTERN = re.compile(r"^\s*cell\s*\(")
_LIBRARY_PATTERN = re.compile(r"^\s*library\s*\(\s*(\S+)\s*\)")


def merge_libs(in_files, out_file, new_lib_name=None):
    log.info(
        "Merging libraries %s into library %s (%s)",
        ", ".join(str(f) for f in in_files),
        new_lib_name,
        out_file,
    )

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
                    if re.search(_CELL_PATTERN, line):
                        if flag != 0:
                            raise Exception("Error! new cell before finishing previous one.")
                        flag = 1
                        out.write("\n" + line)
                    elif flag > 0:
                        flag += len(re.findall(r"\{", line))
                        flag -= len(re.findall(r"\}", line))
                        out.write(line)
        out.write("\n}\n")


def preproc_libs(in_files, merged_file, dont_use_cells: List[str], new_lib_name=None):
    merged_content = ""
    proc_files = []
    temp_path = Path("processed_libs")
    temp_path.mkdir(parents=True, exist_ok=True)
    for in_file in in_files:
        in_file = Path(in_file)
        log.info("Pre-processing liberty file: %s", in_file)
        with open(in_file, encoding="utf-8") as f:
            content = clean_ascii(f.read())
        merged_content += preproc_lib_content(content, dont_use_cells)
        out_file = temp_path / in_file.with_stem(in_file.stem + "-mod").name
        log.info("Writing pre-processed file: %s", out_file)
        with open(out_file, "w") as f:
            f.write(merged_content)
        proc_files.append(out_file)
    merge_libs(proc_files, merged_file, new_lib_name)
    log.info("Merged lib: %s", str(merged_file))


class HiLoMap(XedaBaseModel):
    hi: Tuple[str, str]
    lo: Tuple[str, str]
    singleton: bool = True


class Yosys(YosysBase, SynthFlow):
    """
    Yosys Open SYnthesis Suite: ASICs and generic gate/LUT synthesis
    """

    class Settings(YosysBase.Settings, SynthFlow.Settings):
        liberty: List[Path] = []
        dff_liberty: Optional[str] = None
        dont_use_cells: List[str] = []
        gates: Optional[str] = None
        lut: Optional[str] = None
        retime: bool = Field(False, description="Enable flip-flop retiming")
        sta: bool = Field(
            False,
            description="Run a simple static timing analysis (requires `flatten`)",
        )
        dff: bool = Field(True, description="Run abc/abc9 with -dff option")
        synth_flags: List[str] = []
        abc_flags: List[str] = []
        show_netlist: bool = False
        show_netlist_flags: List[str] = ["-stretch", "-enum"]
        post_synth_opt: bool = Field(
            True,
            description="run additional optimization steps after synthesis if complete",
        )
        optimize: Optional[Literal["speed", "area"]] = Field(
            "area", description="Optimization target"
        )
        stop_after: Optional[Literal["rtl"]]
        adder_map: Optional[str] = None
        clockgate_map: Optional[str] = None
        other_maps: List[str] = []
        abc_constr: List[str] = []
        abc_script: Union[None, Path, List[str]] = None
        hilomap: Optional[HiLoMap] = None
        insbuf: Optional[Tuple[str, str, str]] = None
        merge_libs_to: Optional[str] = None

        @validator("liberty")
        def _str_to_list(value):
            if not isinstance(value, (list, tuple)):
                return [value]
            return value

    def run(self) -> None:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings

        yosys = Tool(
            executable="yosys",
            docker=Docker(image="hdlc/impl"),  # pyright: reportGeneralTypeIssues=none
        )

        self.artifacts.timing_report = "timing.rpt"
        self.artifacts.utilization_report = (
            "utilization.json" if yosys.version_gte(0, 21) else "utilization.rpt"
        )
        if ss.rtl_json:
            self.artifacts.rtl_json = ss.rtl_json
        if ss.rtl_vhdl:
            self.artifacts.rtl_vhdl = ss.rtl_vhdl
        if ss.rtl_verilog:
            self.artifacts.rtl_verilog = ss.rtl_verilog
        if not ss.stop_after:  # FIXME
            self.artifacts.netlist_verilog = "netlist.v"
            self.artifacts.netlist_json = "netlist.json"

        if ss.sta:
            ss.flatten = True
        if ss.flatten:
            append_flag(ss.synth_flags, "-flatten")

        if ss.dff:
            append_flag(ss.abc_flags, "-dff")
        if ss.gates:
            append_flag(ss.abc_flags, f"-g {ss.gates}")
        elif ss.lut:
            append_flag(ss.abc_flags, f"-lut {ss.lut}")

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
        if ss.top_is_vhdl is True or (
            ss.top_is_vhdl is None and self.design.rtl.sources[-1].type is SourceType.Vhdl
        ):
            # generics were already handled by GHDL and the synthesized design is no longer parametric
            self.design.rtl.parameters = {}

        script_path = self.copy_from_template(
            "yosys_synth.tcl",
            lstrip_blocks=True,
            trim_blocks=True,
            ghdl_args=GhdlSynth.synth_args(ss.ghdl, self.design),
            parameters=process_parameters(self.design.rtl.parameters),
            defines=[f"-D{k}" if v is None else f"-D{k}={v}" for k, v in ss.defines.items()],
            abc_constr_file=abc_constr_file,
            abc_script_file=abc_script_file,
        )
        log.info("Yosys script: %s", script_path.absolute())
        args = ["-c", script_path]
        if ss.log_file:
            args.extend(["-L", ss.log_file])
        if not ss.verbose:  # reduce noise unless verbose
            args.extend(["-T", "-Q"])
            if not ss.debug and not ss.verbose:
                args.append("-q")
        self.results["_tool"] = yosys.info  # TODO where should this go?
        log.info("Logging yosys output to %s", ss.log_file)
        yosys.run(*args)

    def parse_reports(self) -> bool:
        assert isinstance(self.settings, self.Settings)
        if self.artifacts.utilization_report.endswith(".json"):
            try:
                with open(self.artifacts.utilization_report, "r") as f:
                    content = f.read()
                i = content.find("{")  # yosys bug (FIXED)
                if i >= 0:
                    content = content[i:]
                utilization = json.loads(content)
                mod_util = utilization.get("modules")
                if mod_util:
                    self.results["_module_utilization"] = mod_util
                design_util = utilization.get("design")
                if design_util:
                    num_cells_by_type = design_util.get("num_cells_by_type")
                    if num_cells_by_type:
                        design_util = {
                            **{k: v for k, v in design_util.items() if k != "num_cells_by_type"},
                            **num_cells_by_type,
                        }
                    self.results["design_utilization"] = design_util

            except json.decoder.JSONDecodeError as e:
                log.error("Failed to decode JSON %s: %s", self.artifacts.utilization_report, e)

        return True
