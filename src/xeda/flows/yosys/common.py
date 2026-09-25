import hashlib
import json
import logging
import os
import re
from functools import cached_property
from glob import escape as glob_escape
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from ...dataclass import Field, field_validator
from ...design import SourceType
from ...flow import Flow, FlowException
from ...flows.ghdl import GhdlSynth
from ...tool import Docker, Tool
from ...utils import hierarchical_merge, tcl_word, unique

log = logging.getLogger(__name__)
YOSYS_DOCKER_IMAGE = "hdlc/impl"


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


def frontend_name(value: Any) -> str:
    """A file name as yosys has to be given it to read that very file, where yosys expands the
    name as a glob pattern.

    yosys' own frontends -- `read_verilog`, `read_liberty`, and `techmap -map`, which reads its
    maps through one -- and `dfflibmap -liberty` expand a file name as a glob pattern, falling
    back to the name itself only when nothing matches: `top[1].v` read `top1.v` whenever one
    existed. Escaped (`top[[]1].v`), the pattern matches only the file itself. The other
    commands take a name as it is (`abc` and `stat` with `-liberty`, the plugins, the writers),
    and an escaped one names no file there. `test_yosys_reads_every_input_by_its_own_name` pins
    which is which.
    """
    return glob_escape(str(value))


def ys_path(value: Any) -> str:
    """A path as one `.ys` token, for the arguments yosys unquotes itself.

    yosys splits a script line at whitespace, keeping a double-quoted token whole (quotes
    included); its frontends and backends, `tee -o`, `stat`/`dfflibmap`/`abc -liberty`, `abc
    -script`/`-constr` and `techmap -map` then strip those quotes. So a path containing
    whitespace is quoted, and any other is written as is. A double quote inside the path cannot
    be expressed at all.
    """
    text = str(value)
    if '"' in text:
        raise FlowException(f"A yosys script cannot name the path {text!r}.")
    return f'"{text}"' if _NOT_A_YS_TOKEN.search(text) else text


PATH_ALIASES_DIR = "path_aliases"
"""Directory, in the run directory, of the whitespace-free aliases `YosysBase.verbatim_path`
creates."""

_NOT_A_YS_TOKEN = re.compile(r'[\s"]+')
"""What keeps a path from being one verbatim `.ys` token: whitespace, or a double quote."""


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
        def validate_verilog_lib(cls, value, info):
            if not isinstance(value, (list, tuple, set)):
                raise ValueError(
                    f"'verilog_lib' must be a path or a list of paths, "
                    f"got {type(value).__name__}: {value!r}"
                )
            resolved = []
            context = info.context if isinstance(info.context, dict) else {}
            design_root = context.get("design_root")
            for v in value:
                try:
                    path = Path(v)
                    if not path.is_absolute() and design_root:
                        path = design_root / path
                    resolved.append(str(path.resolve(strict=True)))
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

    def add_template_helpers(self) -> None:
        """The filters and globals every yosys template uses. One place, called by `init()`,
        so that anything rendering a template without `init()` -- a test -- gets all of them,
        not whichever it thought to add by hand."""
        assert isinstance(self.settings, self.Settings)
        tcl = self.settings.script_format == "tcl"
        # values are stored canonically (unescaped); escape them for the target script language
        self.add_template_filter("esc", tcl_escape if tcl else ys_escape, replace_existing=True)
        # Every path a template emits goes through one of these filters: `read_path` for a file
        # yosys reads by a name it expands as a pattern (`frontend_name`), `path` for any other
        # argument yosys unquotes, `verbatim_path` for one it passes on as is.
        path = tcl_word if tcl else ys_path
        self.add_template_filter("path", path, replace_existing=True)
        self.add_template_filter(
            "read_path", lambda value: path(frontend_name(value)), replace_existing=True
        )
        self.add_template_filter(
            "verbatim_path", tcl_word if tcl else self.verbatim_path, replace_existing=True
        )
        self.add_template_filter(
            "ghdl_arg", tcl_word if tcl else self.ghdl_arg, replace_existing=True
        )
        self.jinja_env.globals["top_is_vhdl"] = self.top_is_vhdl

    def init(self):
        """Prepare template helpers and Yosys synthesis settings."""
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        self.add_template_helpers()
        if ss.abc_script and not ss.abc_script.startswith("+"):
            ss.abc_script = str(self.normalize_path_to_design_root(ss.abc_script))
        for name in ("adder_map", "clockgate_map"):
            value = getattr(ss, name, None)
            if value:
                setattr(ss, name, str(self.normalize_path_to_design_root(value)))
        if hasattr(ss, "other_maps"):
            ss.other_maps = [str(self.normalize_path_to_design_root(p)) for p in ss.other_maps]
        if ss.ghdl is None:
            ss.ghdl = GhdlSynth.Settings()
        if ss.keep_hierarchy:
            kh = "keep_hierarchy"
            if kh not in ss.set_mod_attribute:
                ss.set_mod_attribute[kh] = {}
            for mod in ss.keep_hierarchy:
                ss.set_mod_attribute[kh][mod] = 1

        if ss.sta or ss.ltp:
            ss.flatten = True  # design must be flattened
        # The generic `synth` and FPGA `synth_<family>` passes have different flatten
        # switches. Device-specific flags are assembled by YosysFpga.Settings.
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

    def top_is_vhdl(self) -> bool:
        """Whether the top unit is VHDL: `top_is_vhdl`, or else whether the last source is.

        A VHDL top's generics reach yosys through GHDL (`-g<name>=<value>`), and the top GHDL
        elaborates has no parameters left to `chparam`. The design's own parameters stay as they
        are: every flow of a run shares the design, and it has already been hashed.
        """
        assert isinstance(self.settings, self.Settings)
        if self.settings.top_is_vhdl is not None:
            return self.settings.top_is_vhdl
        sources = self.design.rtl.sources
        return bool(sources) and sources[-1].type is SourceType.Vhdl

    def verbatim_path(self, path: str | os.PathLike[str]) -> str:
        """`path` as a `.ys` token for an argument yosys takes verbatim, quotes included.

        `read_verilog -I<dir>`, `show -prefix`, and the ghdl and slang plugins' arguments are not
        unquoted, and yosys splits a script line at whitespace, so no quoting passes them a path
        containing a space. Such a path is named instead through a whitespace-free symbolic link
        in the run directory (`path_aliases/`). A path that does not exist yet -- an output --
        is named through an alias of its directory.
        """
        text = str(path)
        if not _NOT_A_YS_TOKEN.search(text):
            return text
        # Scripts are rendered, and yosys runs, in the run directory: relative paths are
        # relative to it, and so is the alias.
        target = Path(text).absolute()
        if target.exists():
            return self._path_alias(target)
        if _NOT_A_YS_TOKEN.search(target.name):
            raise FlowException(
                f"A yosys script cannot name {text!r}: the command takes the path verbatim, and "
                "its file name contains whitespace. Choose a name without spaces."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        return f"{self._path_alias(target.parent)}/{target.name}"

    @staticmethod
    def _path_alias(target: Path) -> str:
        """The name, relative to the run directory, of a whitespace-free symbolic link there to
        the absolute path `target`: `path_aliases/<digest of that path>/<its name>`, so one target
        always gets the same alias, two never share one, and the name keeps its extension (the
        ghdl plugin, for one, decides by it what a file is)."""
        digest = hashlib.sha256(str(target).encode()).hexdigest()[:12]
        alias = Path(PATH_ALIASES_DIR) / digest / _NOT_A_YS_TOKEN.sub("_", target.name)
        alias.parent.mkdir(parents=True, exist_ok=True)
        if alias.is_symlink():
            if os.readlink(alias) == str(target):
                return alias.as_posix()
            alias.unlink()
        try:
            alias.symlink_to(target, target_is_directory=target.is_dir())
        except OSError as e:
            raise FlowException(
                f"A yosys script cannot name {str(target)!r}, which contains whitespace, and "
                f"creating a whitespace-free link to it failed: {e}"
            ) from e
        return alias.as_posix()

    def ghdl_arg(self, arg: str) -> str:
        """One of the ghdl plugin's arguments as a `.ys` token: it takes them all verbatim, so a
        source file, or the directory of a `-P<dir>` library path, is named by `verbatim_path`."""
        if arg.startswith("-P"):
            return "-P" + self.verbatim_path(arg[2:])
        if arg.startswith("-"):
            return arg
        return self.verbatim_path(arg)

    @cached_property
    def yosys(self):
        """Create the Yosys tool with output and debug options."""
        default_args = []
        ss = self.settings
        if ss.is_quiet or (not ss.verbose and not ss.debug):
            default_args += ["-T", "-Q"]
        if ss.is_quiet:
            default_args += ["-q"]
        if ss.debug:
            default_args += ["-g"]
        return Tool(
            executable="yosys",
            docker=Docker(image=YOSYS_DOCKER_IMAGE),  # type: ignore
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
