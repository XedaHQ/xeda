import json
import logging
import re
from functools import cached_property
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from ...dataclass import Field, field_validator
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


def ys_escape(value: Any) -> str:
    """Render a canonical value into a yosys (`.ys`) script.

    Yosys' own script tokenizer needs no escaping: it strips grouping double
    quotes itself and never performs substitution on brackets.
    """
    return str(value)


def tcl_escape(value: Any) -> str:
    """Render a canonical value into a TCL (`.tcl`) script.

    Brackets would trigger TCL command substitution and double quotes would
    terminate the enclosing word, so both have to be backslash-escaped.
    """
    s = str(value)
    s = s.replace("[", "\\[").replace("]", "\\]")
    return s.replace('"', '\\"')


class YosysBase(Flow):
    """Synthesize the design using Yosys Open SYnthesis Suite"""

    class Settings(Flow.Settings):
        log_file: Optional[str] = Field(
            "yosys.log",
            description="File yosys writes its full log to, relative to the run directory. "
            "Set to null to log only to the console.",
        )
        script_format: Literal["ys", "tcl"] = Field(
            "ys",
            description="Format of the generated yosys script. Every yosys build accepts `ys`,"
            " while `tcl` requires a build with TCL support (e.g. not available in oss-cad-suite).",
        )
        plugins: List[str] = Field(
            [],
            description='Yosys plugins to load with `plugin -i`, e.g. "ghdl" for VHDL input or '
            '"slang" for SystemVerilog.',
        )
        flatten: bool = Field(False, description="flatten design")
        read_verilog_flags: List[str] = Field(
            ["-noautowire", "-sv"],
            description="Flags passed to yosys' `read_verilog` for each Verilog source.",
        )
        read_systemverilog_flags: List[str] = Field(
            [],
            description="Flags passed to the SystemVerilog front-end selected by `systemverilog`.",
        )
        check_assert: bool = Field(
            True,
            description="Run yosys' `check -assert`, failing the flow on structural problems such "
            "as combinational loops or multiply-driven wires.",
        )
        rtl_verilog: Optional[Path] = Field(
            None,
            description="Write the elaborated (pre-synthesis) design to this Verilog file. "
            "Useful for inspecting what yosys read.",
        )
        rtl_json: Optional[Path] = Field(
            None, description="Write the elaborated (pre-synthesis) design to this JSON file."
        )
        rtl_graph: Optional[Path] = Field(
            None, description="Render a `show` graph of the elaborated design to this file."
        )
        rtl_graph_flags: List[str] = Field(
            [
                "-notitle",
                "-stretch",
                "-width",
                "-enum",
                "-href",
                "-color maroon3 t:*dff",
            ],
            description="Flags passed to yosys' `show` when rendering `rtl_graph`.",
        )
        ghdl: Optional[GhdlSynth.Settings] = Field(
            None,
            description="Settings for the ghdl-yosys plugin, used to read VHDL sources. "
            "Set automatically when the design has VHDL sources.",
        )
        systemverilog: Literal["default", "uhdm", "slang"] = Field(
            "slang",
            description="Front-end used for SystemVerilog sources: yosys' built-in reader "
            "(`default`), Surelog/UHDM (`uhdm`), or the slang plugin (`slang`).",
        )
        use_slang_plugin: bool = Field(
            True,
            description='Load the slang plugin when `systemverilog` is "slang". Disable if slang '
            "is compiled into your yosys build.",
        )
        verilog_lib: List[str] = Field(
            [],
            description="Verilog library files read with `-lib`: their modules are used only when "
            "instantiated, and are otherwise treated as black boxes.",
        )
        splitnets: bool = Field(
            False, description="Run `splitnets`, splitting multi-bit nets into individual bits."
        )
        splitnets_driver: bool = Field(
            False, description="Pass `-driver` to `splitnets`, so nets are split by their driver."
        )
        splitnets_ports: bool = Field(
            False, description="Pass `-ports` to `splitnets`, so module ports are split too."
        )
        rmports: bool = Field(False, description="Remove unused or un-driven ports.")
        set_attribute: Dict[str, Any] = Field(
            {},
            description="Set attributes from a dict (or a json file) orgranized as attr_name -> (object_path -> attr_value)",
        )
        set_mod_attribute: Dict[str, Dict[str, Any]] = Field(
            {},
            description="Module attributes to set, organized as attr_name -> (module_path -> "
            "attr_value).",
        )
        prep: Optional[List[str]] = Field(
            None,
            description="Run yosys' `prep` with these flags instead of the default elaboration "
            "sequence.",
        )
        keep_hierarchy: List[str] = Field(
            [],
            description="Modules that must not be flattened, marked with the `keep_hierarchy` "
            "attribute. See also `flatten`.",
        )
        defines: Dict[str, Any] = Field(
            {},
            description="Verilog preprocessor macros passed as `-D<name>=<value>`, in addition to "
            "the design's own `defines`.",
        )
        black_box: List[str] = Field(
            [],
            description="Modules to treat as black boxes: their contents are discarded and only "
            "their interface is kept.",
        )
        synth_flags: List[str] = Field(
            [], description="Extra flags passed to yosys' `synth` command."
        )
        nosynth: bool = Field(False, description="Do not run `synth`.")
        noabc: bool = Field(
            False, description="Do not run `abc` step, also pass `-noabc` to `synth`."
        )
        abc_dff: bool = Field(True, description="Run abc/abc9 with -dff option")
        abc_flags: List[str] = Field([], description="Extra flags passed to yosys' `abc`/`abc9`.")
        abc_constr: List[str] = Field(
            [],
            description='Lines written to abc\'s constraint file, e.g. "set_driving_cell ..." or '
            '"set_load ...".',
        )
        abc_script: Optional[str] = Field(
            None,
            description="Custom abc script, replacing the default mapping script. Advanced: a "
            "wrong script silently produces a poor or invalid netlist.",
        )
        top_is_vhdl: Optional[bool] = Field(
            None,
            description="set to `true` to specify top module is VHDL, or `false` to override detection based on last source.",
        )
        post_synth_rename: List[str] = Field(
            [], description='Flags passed to yosys\' `rename` after synthesis, e.g. ["-hide"].'
        )
        netlist_verilog: Optional[Path] = Field(
            Path("netlist.v"),
            alias="netlist",
            description="Write the synthesized gate-level netlist to this Verilog file. Set to "
            "null to skip.",
        )
        netlist_attrs: Optional[bool] = Field(
            True, description="Include cell and wire attributes in the written Verilog netlist."
        )
        netlist_expr: Optional[bool] = Field(
            None,
            description="Write expressions in the netlist instead of always fully mapping them. "
            "Null leaves yosys' default.",
        )
        netlist_dec: Optional[bool] = Field(
            False, description="Write constants in the netlist in decimal."
        )
        netlist_hex: Optional[bool] = Field(
            True, description="Write constants in the netlist in hexadecimal."
        )
        netlist_blackboxes: Optional[bool] = Field(
            False, description="Include black-box modules in the written netlist."
        )
        netlist_simple_lhs: Optional[bool] = Field(
            False,
            description="Write only simple left-hand sides in the netlist, for tools that cannot "
            "parse concatenations on the left of an assignment.",
        )
        netlist_verilog_flags: List[str] = Field(
            [],
            description="Extra flags for `write_verilog`, in addition to those the `netlist_*` "
            "switches above imply.",
        )
        netlist_verilog_extmem: List[str] = Field(
            [],
            description="Write memory contents to external files rather than inline, using these "
            "`-extmem` arguments.",
        )
        netlist_src_attrs: bool = Field(
            False,
            description="Keep `src` attributes (source file and line) in the written netlist.",
        )
        netlist_unset_attributes: List[str] = Field(
            [], description="Attributes to strip from the design before writing the netlist."
        )
        netlist_json: Optional[Path] = Field(
            Path("netlist.json"),
            alias="json_netlist",
            description="Write the synthesized netlist to this JSON file. Required by downstream "
            "flows such as `nextpnr`.",
        )
        netlist_graph: Optional[Path] = Field(
            None, description="Render a `show` graph of the synthesized netlist to this file."
        )
        netlist_graph_flags: List[str] = Field(
            ["-stretch", "-enum", "-width", "-href", "-color maroon3 t:*dff"],
            description="Flags passed to yosys' `show` when rendering `netlist_graph`.",
        )
        write_blif: Optional[Path] = Field(
            None, description="Write the synthesized netlist to this BLIF file."
        )
        retime: bool = Field(False, description="Enable flip-flop retiming")
        sta: bool = Field(
            False,
            description="Run a simple static timing analysis (implies `flatten`)",
        )
        post_synth_opt: bool = Field(
            False,
            description="run additional optimization steps after synthesis if complete",
        )
        ltp: bool = Field(False, description="Print the longest topological path in the design.")

        def write_verilog_flags(self) -> List[str]:
            """`write_verilog` flags: `netlist_verilog_flags` plus those the `netlist_*` switches
            imply (a switch left `None` adds nothing). Computed when the script is written, so a
            switch set after construction -- as `init` does for `netlist_expr` -- takes effect."""
            flags = list(self.netlist_verilog_flags)
            for switch, flag, flag_when in (
                (self.netlist_dec, "-nodec", False),
                (self.netlist_hex, "-nohex", False),
                (self.netlist_expr, "-noexpr", False),
                (self.netlist_attrs, "-noattr", False),
                (self.netlist_blackboxes, "-blackboxes", True),
                (self.netlist_simple_lhs, "-simple-lhs", True),
            ):
                if switch is flag_when:
                    flags.append(flag)
                elif switch is (not flag_when) and flag in flags:
                    flags.remove(flag)
            return unique(flags)

        def attributes_to_unset(self) -> List[str]:
            """`netlist_unset_attributes`, plus `src` when attributes are written without it."""
            attributes = list(self.netlist_unset_attributes)
            if self.netlist_attrs is True and self.netlist_src_attrs is False:
                attributes.append("src")
            return unique(attributes)

        @field_validator("abc_script", mode="before")
        @classmethod
        def validate_abc_script(cls, value):
            if isinstance(value, str) and value.startswith("+"):
                return value.replace(" ", ",")
            return value

        @field_validator("verilog_lib", mode="before")
        @classmethod
        def validate_verilog_lib(cls, value):
            if not isinstance(value, (list, tuple, set)):
                raise ValueError(
                    f"'verilog_lib' must be a path or a list of paths, "
                    f"got {type(value).__name__}: {value!r}"
                )
            resolved = []
            for v in value:
                try:
                    resolved.append(str(Path(v).resolve(strict=True)))
                except FileNotFoundError:
                    raise ValueError(f"'verilog_lib' file not found: {v}") from None
            return resolved

        @field_validator("set_attribute", "set_mod_attribute", mode="before")
        @classmethod
        def validate_set_attributes(cls, value):
            def format_attribute_value(v) -> Any:
                """Normalize to a canonical, *unescaped* form.

                String values must reach yosys wrapped in double quotes. Any
                script-specific escaping on top of that is applied at render
                time by the `esc` template filter, as it differs between the
                `.ys` and `.tcl` templates.
                """
                if isinstance(v, str):
                    if v.startswith('\\"') and v.endswith('\\"'):
                        v = v[2:-2]  # accept legacy TCL-escaped values
                    if not (v.startswith('"') and v.endswith('"')):
                        v = f'"{v}"'
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
                if not isinstance(value, dict):
                    raise ValueError(
                        "expected a mapping of attribute -> value (or -> {path: value}), or a "
                        f"path to a .json file of one; got {type(value).__name__}: {value!r}"
                    )
                for attr, attr_val in value.items():
                    if isinstance(attr_val, dict):
                        for path, v in attr_val.items():
                            value[attr][path] = format_attribute_value(v)
                    else:
                        value[attr] = format_attribute_value(attr_val)
            return value

    @property
    def script_ext(self) -> str:
        """Extension of the script template to render (`.ys` or `.tcl`)."""
        assert isinstance(self.settings, self.Settings)
        return "." + self.settings.script_format

    @property
    def script_flag(self) -> str:
        """yosys CLI flag for running the generated script."""
        assert isinstance(self.settings, self.Settings)
        return "-s" if self.settings.script_format == "ys" else "-c"

    def init(self):
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        # values are stored canonically (unescaped); escape them for the target script language
        self.add_template_filter(
            "esc", tcl_escape if ss.script_format == "tcl" else ys_escape, replace_existing=True
        )
        if ss.ghdl is None:
            ss.ghdl = GhdlSynth.Settings()
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
        if ss.abc_dff:
            append_flag(ss.abc_flags, "-dff")
        ss.set_attribute = hierarchical_merge(self.design.rtl.attributes, ss.set_attribute)

        if ss.rtl_json:
            ss.rtl_json.parent.mkdir(parents=True, exist_ok=True)
            self.artifacts.rtl_json = ss.rtl_json
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
            with open(report) as f:
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
            # canonical (unescaped); the `esc` template filter escapes for the target language
            v = '"' + v + '"'
        out[k] = str(v)
    return out
