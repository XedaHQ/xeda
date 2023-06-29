import json
import logging
import re
from functools import cached_property
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...dataclass import Field, validator
from ...design import SourceType
from ...flow import Flow
from ...flows.ghdl import GhdlSynth
from ...tool import Docker, Tool
from ...utils import hierarchical_merge, unique

log = logging.getLogger(__name__)


def append_flag(flag_list: List[str], flag: str) -> List[str]:
    if flag not in flag_list:
        flag_list.append(flag)
    return flag_list


class YosysBase(Flow):
    """Synthesize the design using Yosys Open SYnthesis Suite"""

    class Settings(Flow.Settings):
        log_file: Optional[str] = "yosys.log"
        plugins: List[str] = []
        flatten: bool = Field(True, description="flatten design")
        read_verilog_flags: List[str] = [
            "-noautowire",
            "-sv",
        ]
        read_systemverilog_flags: List[str] = []
        check_assert: bool = True
        rtl_verilog: Optional[Path] = None  # "rtl.v"
        rtl_vhdl: Optional[Path] = None  # "rtl.vhdl"
        rtl_json: Optional[Path] = None  # "rtl.json"
        show_rtl: bool = False
        show_rtl_flags: List[str] = [
            "-stretch",
            "-enum",
            "-width",
        ]
        ghdl: GhdlSynth.Settings = GhdlSynth.Settings()  # type: ignore
        use_uhdm_plugin: bool = False
        verilog_lib: List[str] = []
        splitnets: bool = False
        splitnets_driver: bool = False
        splitnets_ports: bool = False
        rmports: bool = Field(False, description="Remove unused or un-driven ports.")
        set_attribute: Dict[str, Any] = Field(
            {},
            description="Set attributes from a dict (or a json file) orgranized as attr_name -> (object_path -> attr_value)",
        )
        set_mod_attribute: Dict[str, Dict[str, Any]] = {}  # attr -> (path -> value)
        prep: Optional[List[str]] = None
        keep_hierarchy: List[str] = []
        defines: Dict[str, Any] = {}
        black_box: List[str] = []
        synth_flags: List[str] = []
        nosynth: bool = Field(False, description="Do not run `synth`.")
        noabc: bool = Field(
            False, description="Do not run `abc` step, also pass `-noabc` to `synth`."
        )
        abc_dff: bool = Field(True, description="Run abc/abc9 with -dff option")
        abc_flags: List[str] = []
        top_is_vhdl: Optional[bool] = Field(
            None,
            description="set to `true` to specify top module is VHDL, or `false` to override detection based on last source.",
        )
        post_synth_rename: List[str] = []
        netlist_verilog: Optional[Path] = Field(Path("netlist.v"), alias="netlist")
        netlist_attrs: Optional[bool] = True
        netlist_expr: Optional[bool] = None
        netlist_dec: Optional[bool] = False
        netlist_hex: Optional[bool] = False
        netlist_blackboxes: Optional[bool] = False
        netlist_simple_lhs: Optional[bool] = False
        netlist_verilog_flags: List[str] = []
        netlist_verilog_extmem: List[str] = []
        netlist_src_attrs: bool = True
        netlist_unset_attributes: List[str] = []
        netlist_json: Optional[Path] = Field(Path("netlist.json"), alias="json_netlist")
        netlist_dot: Optional[str] = None  # prefix
        netlist_dot_flags: List[str] = ["-stretch", "-enum", "-width"]
        write_blif: Optional[Path] = None
        retime: bool = Field(False, description="Enable flip-flop retiming")
        sta: bool = Field(
            False,
            description="Run a simple static timing analysis (implies `flatten`)",
        )
        post_synth_opt: bool = Field(
            True,
            description="run additional optimization steps after synthesis if complete",
        )
        ltp: bool = Field(False, description="Print the longest topological path in the design.")

        @validator("netlist_verilog_flags", pre=False, always=True)
        def _validate_netlist_flags(cls, value, values):
            def add_remove(key, flag, neg_flag=True):
                if values.get(key) is (not neg_flag):
                    value.append(flag)
                elif values.get(key) is neg_flag and flag in value:
                    value.remove(flag)

            if value is None:
                value = []
            add_remove("netlist_dec", "-nodec")
            add_remove("netlist_hex", "-nohex")
            add_remove("netlist_expr", "-noexpr")
            add_remove("netlist_attrs", "-noattr")
            add_remove("netlist_blackboxes", "-blackboxes", False)
            add_remove("netlist_simple_lhs", "-simple-lhs", False)
            return unique(value)

        @validator("netlist_unset_attributes", pre=False, always=True)
        def _validate_netlist_unset_attributes(cls, value, values):
            if values.get("netlist_attrs") is True and values.get("netlist_src_attrs") is False:
                value.append("src")
            return unique(value)

        @validator("verilog_lib", pre=True, always=True)
        def validate_verilog_lib(cls, value):
            if isinstance(value, str):
                value = [value]
            value = [str(Path(v).resolve(strict=True)) for v in value]
            return value

        @validator("set_attribute", "set_mod_attribute", pre=True, always=True)
        def validate_set_attributes(cls, value):
            def format_attribute_value(v) -> Any:
                if isinstance(v, str):
                    # try:
                    #     return int(v)
                    # except ValueError:
                    v = v.replace("[", "\\[")
                    v = v.replace("\\\\[", "\\[")
                    v = v.replace("]", "\\]")
                    v = v.replace("\\\\]", "\\]")
                    # String values must be passed in double quotes
                    if v.startswith('"') and v.endswith('"'):
                        # escape double-quotes for TCL
                        return "\\" + v + "\\"
                    elif not v.startswith('\\"') and not v.endswith('\\"'):  # conservative
                        return f'\\"{v}\\"'
                return v

            if value:
                if isinstance(value, (str, Path)):
                    attr_file = Path(value)
                    if attr_file.suffix.endswith(".json"):
                        try:
                            log.info("Parsing %s as JSON file", attr_file)
                            with open(attr_file) as f:
                                value = {**json.load(f)}
                        except json.JSONDecodeError as e:
                            raise ValueError(
                                f"Decoding of JSON file {attr_file} failed: {e.args}"
                            ) from e
                        except TypeError as e:
                            raise ValueError(f"JSON TypeError: {e.args}") from e
                    else:
                        raise ValueError(f"Unsupported extension for JSON file: {value}")
                for attr, attr_val in value.items():
                    if isinstance(attr_val, dict):
                        for path, v in attr_val.items():
                            value[attr][path] = format_attribute_value(v)
                    else:
                        value[attr] = format_attribute_value(attr_val)
            return value

    def init(self):
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        if ss.keep_hierarchy:
            kh = "keep_hierarchy"
            if kh not in ss.set_mod_attribute:
                ss.set_mod_attribute[kh] = {}
            for mod in ss.keep_hierarchy:
                ss.set_mod_attribute[kh][mod] = 1

        if ss.top_is_vhdl is True or (
            ss.top_is_vhdl is None and self.design.rtl.sources[-1].type is SourceType.Vhdl
        ):
            # generics were already handled by GHDL and the synthesized design is no longer parametric
            self.design.rtl.parameters = {}
        if ss.sta or ss.ltp:
            ss.flatten = True  # design must be flattened
        if ss.flatten:
            append_flag(ss.synth_flags, "-flatten")
        if ss.flatten:
            append_flag(ss.synth_flags, "-flatten")
        if ss.abc_dff:
            append_flag(ss.abc_flags, "-dff")
        ss.set_attribute = hierarchical_merge(self.design.rtl.attributes, ss.set_attribute)

        if ss.rtl_json:
            ss.rtl_json.parent.mkdir(parents=True, exist_ok=True)
            self.artifacts.rtl_json = ss.rtl_json
        if ss.rtl_vhdl:
            ss.rtl_vhdl.parent.mkdir(parents=True, exist_ok=True)
            self.artifacts.rtl_vhdl = ss.rtl_vhdl
        if ss.rtl_verilog:
            ss.rtl_verilog.parent.mkdir(parents=True, exist_ok=True)
            self.artifacts.rtl_verilog = ss.rtl_verilog
        if ss.netlist_verilog:
            ss.netlist_verilog.parent.mkdir(parents=True, exist_ok=True)
            self.artifacts.netlist_verilog = ss.netlist_verilog
        if ss.netlist_json:
            ss.netlist_json.parent.mkdir(parents=True, exist_ok=True)
            self.artifacts.netlist_json = ss.netlist_json

    @cached_property
    def yosys(self):
        default_args = []
        ss = self.settings
        if ss.quiet or (not ss.verbose and not ss.debug):
            default_args += ["-T", "-Q"]
        if ss.quiet:
            default_args += ["-q"]
        if ss.debug:
            default_args += ["-g"]
        return Tool(
            executable="yosys",
            docker=Docker(image="hdlc/impl"),  # type: ignore
            version_flag="-V",
            minimum_version=(0, 21),
            default_args=default_args,
        )

    def get_utilization(self) -> Optional[dict]:
        report = Path(self.artifacts.utilization_report)
        if not report.exists():
            return None
        try:
            with open(report, "r") as f:
                content = f.read()
            json_start = content.find("{")
            if json_start > 0:
                content = content[json_start:]
            return json.loads(content)
        except json.decoder.JSONDecodeError as e:
            log.error("Failed to decode JSON %s: %s", str(report), e)
            return None


def process_parameters(parameters: Dict[str, Any]) -> Dict[str, str]:
    out = dict()
    for k, v in parameters.items():
        if isinstance(v, bool):
            v = f"1'b{int(v)}"
        elif isinstance(v, str) and not re.match(r"\d+'b[01]+", v):
            v = '\\"' + v + '\\"'
        out[k] = str(v)
    return out
