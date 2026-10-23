import hashlib
import json
import logging
import os
import re
from functools import cached_property
from glob import escape as glob_escape
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Literal, Optional, Tuple

from ...dataclass import WORKING, Field, deliverable, field_validator
from ...design import LANGUAGE_TYPES, Design, SourceType
from ...flow import Flow, FlowException, FlowSettingsError, FlowSettingsException, NoReplacement
from ...flows.ghdl import GhdlSynth
from ...tool import Docker, Tool
from ...utils import ToolException, hierarchical_merge, tcl_word, unique
from .flag_hatches import SLANG_HATCHES, WRITE_VERILOG_SWITCHES, flag_problems

log = logging.getLogger(__name__)
YOSYS_DOCKER_IMAGE = "hdlc/impl"

#: A yosys release as (major, minor), which selects the flags of the synthesis passes.
YosysRelease = Tuple[int, int]

#: The oldest yosys release that the mapping from settings to FPGA synthesis pass options supports.
MINIMUM_YOSYS: YosysRelease = (0, 63)

#: The newest yosys release whose synthesis passes were checked against the flags that Xeda
#: generates. Xeda treats a newer yosys, or one with an unreadable version, as this release.
NEWEST_CHECKED_YOSYS: YosysRelease = (0, 69)


#: The design sources that the shared read templates (`read_files.ys`, `read_files.tcl`) hand to
#: yosys: Verilog and SystemVerilog to a reader (the one of yosys or of a plugin), VHDL to the GHDL
#: plugin, and a header by its directory, as an include directory (`-I`).
READ_SOURCE_TYPES = frozenset(
    {
        SourceType.Verilog,
        SourceType.SystemVerilog,
        SourceType.Vhdl,
        SourceType.VerilogHeader,
        SourceType.SVHeader,
    }
)


#: What the stages after the RTL stage write, by the setting that asks for it. `stop_after: rtl`
#: skips those stages, so none of these files exists when the run ends.
AFTER_RTL = {
    "netlist_json": "a JSON netlist",
    "netlist_verilog": "a Verilog netlist",
    "netlist_graph": "a netlist graph",
    "write_blif": "a BLIF netlist",
    "sta": "a timing report",
    "ltp": "a longest-path report",
}

#: The description of `stop_after`, a setting of `yosys` and `yosys_fpga` but not of `yosys_sim`.
STOP_AFTER_DESCRIPTION = (
    'Stop the flow after this stage. "rtl" elaborates the design and writes the RTL outputs '
    "without synthesizing, so it writes no netlist and no report. A setting that asks for one is "
    "refused: `netlist_json`, `netlist_verilog`, `netlist_graph`, `write_blif`, `sta` and `ltp` "
    "must be null or false."
)


def stop_after_conflicts(settings: Any) -> List[Tuple[str, str]]:
    """Return each setting that asks for a result of a stage that the run stops before.

    Each item is `(setting, message)`. The message says why the setting is refused and how to
    turn it off. A stop that the user asked for succeeds with the outputs it wrote (the RTL
    outputs). So the check refuses what the run cannot write when it checks the settings, before
    anything runs. Otherwise the run would fail, or list a result that does not exist. `yosys`
    and `yosys_fpga` use one rule: the value of the setting decides, whether or not the user
    wrote it, because a default is a request too.
    """
    if getattr(settings, "stop_after", None) != "rtl":  # `yosys_sim` has no `stop_after`
        return []
    problems = []
    for name, what in AFTER_RTL.items():
        value = getattr(settings, name)
        if value:
            off = (
                ("false", f"-s {name}=false")
                if isinstance(value, bool)
                else ("null", f"-s {name}=")
            )
            problems.append(
                (
                    name,
                    f"`stop_after: rtl` stops before synthesis, which writes {what}, and `{name}` "
                    f"asks for it: set `{name}` to {off[0]} (`{off[1]}`)",
                )
            )
    return problems


def refuse_output_after_rtl(flow: type[Flow], settings: Flow.Settings, name: str) -> None:
    """Refuse the request of a consumer for an output of a flow that `stop_after` stops.

    A flow stopped after the RTL (`stop_after: rtl`) writes no netlist, so the request has no
    answer. The advice of `stop_after_conflicts`, to turn the netlist settings off, would be
    circular here: this request turns them on. So the message says that no consumer can follow
    the stopped flow. `flow` is the class that gets the request: `yosys` and `yosys_fpga` call
    this function from their `enable_output`, since each declares `stop_after`.
    """
    if getattr(settings, "stop_after", None) == "rtl":
        raise ValueError(
            f"a flow that takes the {name} cannot follow a stopped {flow.name}: "
            f"`stop_after: rtl` ends it before it writes one. Remove `stop_after`, or leave "
            "out the flow that takes it"
        )


def yosys_release(yosys: Tool) -> YosysRelease:
    """Return the release of `yosys`, for example (0, 69) for "Yosys 0.69+152 (git sha1 ...)"."""
    parts = [re.match(r"\d+", part) for part in yosys.version[:2]]
    if len(parts) == 2 and parts[0] and parts[1]:
        release = (int(parts[0].group()), int(parts[1].group()))
        return min(release, NEWEST_CHECKED_YOSYS)
    log.warning(
        "Could not read the yosys version (%r); choosing flags for yosys %d.%d.",
        yosys.version_str,
        *NEWEST_CHECKED_YOSYS,
    )
    return NEWEST_CHECKED_YOSYS


def yosys_data_dir(yosys: Tool) -> Path:
    """Return the directory that `+/` stands for in yosys, as the installed `yosys` reports it.

    The flow asks `yosys-config --datdir`, the program that `yosys_sim` also asks for the
    include directory. It never reads an environment variable and never guesses the layout. In a
    container, the path is the container's.
    """
    reported = yosys.derive("yosys-config", sibling=True, source_name="yosys").probe_stdout(
        "--datdir"
    )
    if not reported or not reported.strip():
        raise ToolException("`yosys-config --datdir` reported no data directory.")
    return Path(reported.strip())


def same_file(first: Path, second: Path) -> bool:
    """Return whether two paths name the same file.

    A link, a different spelling, another letter case on a case-insensitive file system, or a
    hard link can make two paths name one file. If the paths do not both exist, the function
    compares their resolved, case-normalized spelling.
    """
    try:
        return first.samefile(second)
    except OSError:
        return os.path.normcase(os.path.realpath(first)) == os.path.normcase(
            os.path.realpath(second)
        )


def append_flag(flag_list: List[str], flag: str) -> List[str]:
    if flag not in flag_list:
        flag_list.append(flag)
    return flag_list


def ys_escape(value: Any) -> str:
    """Return a canonical value as text for a yosys (`.ys`) script.

    The yosys script tokenizer needs no escaping. It removes grouping double quotes itself and
    does not substitute brackets.
    """
    return str(value)


def tcl_escape(value: Any) -> str:
    """Return a canonical value as text for a Tcl (`.tcl`) script.

    A bracket starts a Tcl command substitution and a double quote ends the enclosing word, so
    both get a backslash.
    """
    s = str(value)
    s = s.replace("[", "\\[").replace("]", "\\]")
    return s.replace('"', '\\"')


def frontend_name(value: Any) -> str:
    """Return a file name in the form that makes yosys read that very file.

    The yosys frontends `read_verilog` and `read_liberty`, `techmap -map` (which reads its maps
    through a frontend) and `dfflibmap -liberty` expand a file name as a glob pattern. They use
    the name itself only when nothing matches, so `top[1].v` reads `top1.v` whenever that file
    exists. Escaped (`top[[]1].v`), the pattern matches only the file itself. The other commands
    take a name as it is (`abc` and `stat` with `-liberty`, the plugins, the writers), and an
    escaped name matches no file there. `test_yosys_reads_every_input_by_its_own_name` pins which
    is which.
    """
    return glob_escape(str(value))


def ys_path(value: Any) -> str:
    """Return a path as one `.ys` token, for the arguments that yosys unquotes itself.

    Yosys splits a script line at whitespace and keeps a double-quoted token whole, quotes
    included. Its frontends and backends, `tee -o`, `stat`, `dfflibmap` and `abc -liberty`,
    `abc -script` and `-constr`, and `techmap -map` then remove those quotes. So a path with
    whitespace gets quotes, and any other path is written as is. A path with a double quote
    cannot be written.
    """
    text = str(value)
    if '"' in text:
        raise FlowException(f"A yosys script cannot name the path {text!r}.")
    return f'"{text}"' if _NOT_A_YS_TOKEN.search(text) else text


PATH_ALIASES_DIR = "path_aliases"
"""The directory, in the run directory, of the whitespace-free aliases that
`YosysBase.verbatim_path` creates."""

_NOT_A_YS_TOKEN = re.compile(r'[\s"]+')
"""The characters that keep a path from being one verbatim `.ys` token: whitespace and a double
quote."""


#: The directory in the run directory that holds the temporary files of yosys. It is a relative
#: name without a space: yosys runs in the run directory, and yosys and ABC split a command line at
#: spaces.
YOSYS_TEMPORARY_DIRECTORY = "yosys_tmp"


class YosysBase(Flow):
    """The base of the flows that read a design with Yosys."""

    executables: ClassVar[tuple[str, ...]] = ("yosys", "yosys-config")

    # The generic synthesis flow accepts older yosys releases. `yosys_fpga` sets a higher minimum
    # for its pass flags.
    minimum_yosys: Optional[YosysRelease] = (0, 21)

    reads_sources = READ_SOURCE_TYPES

    class Settings(Flow.Settings):
        removed_settings: ClassVar[Dict[str, str]] = {
            **Flow.Settings.removed_settings,
            "script_format": NoReplacement(
                "Xeda selects the format of the script: a Tcl script only when a setting needs one"
            ),
        }
        log_file: Optional[Path] = Field(
            Path("yosys.log"),
            description="File yosys writes its full log to, relative to the run directory. "
            "Set to null to log only to the console.",
            json_schema_extra=WORKING,
        )
        plugins: List[str] = Field(
            [],
            description='Yosys plugins to load with `plugin -i`, e.g. "ghdl" for VHDL input or '
            '"slang" for SystemVerilog.',
        )
        flatten: Optional[bool] = Field(
            None,
            description="Flatten the design hierarchy. Unset leaves the choice to the flow: "
            "`yosys` flattens when it maps to a liberty library (`liberty` or a `platform`) and "
            "keeps the hierarchy otherwise, and `yosys_sim` flattens only through CXXRTL's own "
            "`cxxrtl.flatten`.",
        )
        read_verilog_flags: List[str] = Field(
            ["-noautowire", "-sv"],
            description="Flags passed to yosys' `read_verilog` for each Verilog source. They do "
            "not apply to the Verilog sources of a design that `slang` reads together with its "
            "SystemVerilog sources. When `yosys` maps to a liberty library, it refuses a flag that "
            "the script or a setting gives (`-defer`, `-D`, `-I`).",
        )
        read_systemverilog_flags: List[str] = Field(
            [],
            description="Flags passed to the SystemVerilog front-end selected by `systemverilog`, "
            "`uhdm` or `slang`. The built-in reader (`default`) takes `read_verilog_flags` "
            "instead. With `slang`, they also apply to the Verilog sources, which the same `read_slang` "
            "reads when the design has a SystemVerilog source. A flag that the script gives to "
            "`read_slang` itself (`--top`, `-D`, `-I`, `-G`, `--blackboxed-module`, "
            "`--extern-modules`, `--best-effort-hierarchy`) is refused.",
        )
        check_assert: bool = Field(
            True,
            description="Run yosys' `check -assert`. It fails the flow on structural problems "
            "such as combinational loops or multiply-driven wires.",
        )
        rtl_verilog: Optional[Path] = Field(
            None,
            description="Write the elaborated (pre-synthesis) design to this Verilog file. "
            "Useful for inspecting what yosys read.",
            json_schema_extra=deliverable("outputs/{design}_rtl.v"),
        )
        rtl_json: Optional[Path] = Field(
            None,
            description="Write the elaborated (pre-synthesis) design to this JSON file. Only the "
            "design's own modules are written, as for `rtl_verilog`: blackboxes and library cells "
            "are not.",
            json_schema_extra=deliverable("outputs/{design}_rtl.json"),
        )
        rtl_graph: Optional[Path] = Field(
            None,
            description="Render a `show` graph of the elaborated design to this file.",
            json_schema_extra=deliverable("outputs/{design}_rtl.dot"),
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
            "(`default`), Surelog/UHDM (`uhdm`), or the slang plugin (`slang`). With `slang`, "
            "one `read_slang` reads the Verilog sources of a design that has a SystemVerilog "
            "source too.",
        )
        use_slang_plugin: bool = Field(
            True,
            description='Load the slang plugin when `systemverilog` is "slang". Disable if slang '
            "is compiled into your yosys build.",
        )
        verilog_lib: List[Path] = Field(
            [],
            description="Verilog library files read with `-lib`: their modules are used only when "
            "instantiated, and are otherwise treated as black boxes.",
        )
        set_attribute: Dict[str, Any] = Field(
            {},
            description="Attributes to set, from a mapping or a JSON file, organized as "
            "attr_name -> (object_path -> attr_value).",
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
        top_is_vhdl: Optional[bool] = Field(
            None,
            description="Whether the top module is VHDL. Unset: it is VHDL if the last source in "
            "a hardware description language that the flow reads is VHDL.",
        )

        @field_validator("verilog_lib", mode="before")
        @classmethod
        def validate_verilog_lib(cls, value, info):
            if not isinstance(value, (list, tuple, set)):
                raise ValueError(
                    f"`verilog_lib` must be a path or a list of paths, "
                    f"got {type(value).__name__}: {value!r}"
                )
            resolved = []
            context = info.context if isinstance(info.context, dict) else {}
            design_root = context.get("design_root")
            for v in value:
                try:
                    if str(v).startswith("+/"):
                        resolved.append(Path(v))
                        continue
                    path = Path(v)
                    if not path.is_absolute() and design_root:
                        path = design_root / path
                    resolved.append(path.resolve(strict=True))
                except FileNotFoundError:
                    raise ValueError(f"`verilog_lib` file not found: {v}") from None
            return resolved

        @field_validator("set_attribute", "set_mod_attribute", mode="before")
        @classmethod
        def validate_set_attributes(cls, value):
            def format_attribute_value(v) -> Any:
                """Return the value in a canonical, unescaped form.

                A string value must reach yosys wrapped in double quotes. The `esc` template
                filter adds any script-specific escaping when it renders, because the `.ys` and
                `.tcl` templates differ.
                """
                if isinstance(v, str):
                    if v.startswith('\\"') and v.endswith('\\"'):
                        v = v[2:-2]  # accept a value that is already escaped for Tcl
                    if not (v.startswith('"') and v.endswith('"')):
                        v = f'"{v}"'
                return v

            if value:
                if isinstance(value, (str, Path)):
                    attr_file = Path(value)
                    if attr_file.suffix.endswith(".json"):
                        try:
                            log.debug("Parsing %s as JSON file", attr_file)
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

    @classmethod
    def script_format(cls, settings: "YosysBase.Settings") -> Literal["ys", "tcl"]:
        """Return the format of the script for `settings`: `tcl` when a setting needs a Tcl
        script (`tcl_reasons`), else `ys`, which every yosys build runs. It reads only the
        settings, so planning, the identity and the rendering agree."""
        return "tcl" if cls.tcl_reasons(settings) else "ys"

    @classmethod
    def tcl_reasons(cls, settings: "YosysBase.Settings") -> List[str]:
        """Return the names of the settings that need a Tcl script. A flow without such
        settings needs none."""
        return []

    @property
    def script_ext(self) -> str:
        """The extension of the script template to render (`.ys` or `.tcl`)."""
        assert isinstance(self.settings, self.Settings)
        return "." + type(self).script_format(self.settings)

    @property
    def script_flag(self) -> str:
        """The yosys option that runs the generated script."""
        assert isinstance(self.settings, self.Settings)
        return "-s" if type(self).script_format(self.settings) == "ys" else "-c"

    def add_template_helpers(self) -> None:
        """Add the filters and globals that every yosys template uses.

        `init()` calls this method. Code that renders a template without `init()`, such as a
        test, can call it to get all of them.
        """
        assert isinstance(self.settings, self.Settings)
        tcl = type(self).script_format(self.settings) == "tcl"
        # values are stored canonically (unescaped); escape them for the script language
        self.add_template_filter("esc", tcl_escape if tcl else ys_escape, replace_existing=True)
        # Every path a template emits goes through one of these filters: `read_path` for a file
        # that yosys reads by a name it expands as a pattern (`frontend_name`), `path` for any
        # other argument that yosys unquotes, and `verbatim_path` for one that yosys passes on as
        # is.
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

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Refuse what no yosys flow can honor, before anything runs.

        With the slang front end, `read_systemverilog_flags` must not repeat a flag that the
        script gives to `read_slang` itself. Every flow that reads SystemVerilog does it with the
        same command, so the rule is the same for all of them.
        """
        super().check_settings_supported(settings)
        problems = flag_problems(settings, SLANG_HATCHES)
        if problems:
            raise FlowSettingsError(
                [(key, message, None, "value_error") for key, message in problems], cls.Settings
            )

    @classmethod
    def check_design_supported(cls, design: Design, settings: Flow.Settings | None = None) -> None:
        """Refuse a string parameter that a yosys script cannot give to `read_slang -G`.

        With the slang front end, `-G NAME="text"` sets a string parameter. Xeda writes the text
        between double quotes without an escape, so a double quote inside it would end the
        string. The tokenizer of a yosys (`.ys`) script also ends
        the word at whitespace, and a Tcl script does not. Xeda selects the format from the
        settings alone, so the check refuses whitespace when the format is `.ys`. GHDL sets the
        generics of a VHDL top, and the check leaves such a design alone.
        """
        super().check_design_supported(design, settings)
        if not isinstance(settings, cls.Settings) or settings.systemverilog != "slang":
            return
        reads = cls.reads_sources or frozenset()
        read = [src for src in design.rtl.sources if src.type in reads]
        listed = [src for part in sorted(cls.design_parts) for src in getattr(design, part).sources]
        if SourceType.SystemVerilog not in {src.type for src in listed if src.type in reads}:
            return
        if cls.vhdl_top_of(settings, read):
            return
        ys = cls.script_format(settings) == "ys"
        for name, value in design.rtl.parameters.items():
            if isinstance(value, str) and re.search(r'"|\s' if ys else '"', value):
                raise FlowSettingsException(
                    f"The value of the parameter `{name}` has {'whitespace or ' if ys else ''}a "
                    "double quote, and the script cannot give it to `read_slang -G`. Remove "
                    f"{'them' if ys else 'it'} from the value, or set `systemverilog: \"default\"`."
                )

    def init(self):
        """Prepare the template helpers and the settings that every yosys flow shares."""
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        self.add_template_helpers()
        if ss.ghdl is None:
            ss.ghdl = GhdlSynth.Settings()
        if ss.keep_hierarchy:
            kh = "keep_hierarchy"
            if kh not in ss.set_mod_attribute:
                ss.set_mod_attribute[kh] = {}
            for mod in ss.keep_hierarchy:
                ss.set_mod_attribute[kh][mod] = 1
        ss.set_attribute = hierarchical_merge(self.design.rtl.attributes, ss.set_attribute)

        if ss.rtl_json:
            self.artifacts.rtl_json = ss.rtl_json
        if ss.rtl_verilog:
            self.artifacts.rtl_verilog = ss.rtl_verilog

    def prepare_output_parents(self) -> None:
        """Create the parent directories of the outputs when the flow runs, not in `init()`."""
        assert isinstance(self.settings, self.Settings)
        for name in ("rtl_json", "rtl_verilog"):
            path = getattr(self.settings, name)
            if path:
                self.run_directory.inside(path).parent.mkdir(parents=True, exist_ok=True)

    def top_is_vhdl(self) -> bool:
        """Return whether the top module is VHDL.

        The `top_is_vhdl` setting decides when it is set. Otherwise the last source in a hardware
        description language that the flow reads decides. Only such a source can hold the top
        module, so a constraint file, a memory image or a header listed after it changes nothing.

        The generics of a VHDL top module reach yosys through GHDL (`-g<name>=<value>`), and the
        top module that GHDL elaborates has no parameters left for `chparam`. The design's own
        parameters stay unchanged: every flow of a run shares the design, and its hash is already
        computed.
        """
        assert isinstance(self.settings, self.Settings)
        return self.vhdl_top_of(self.settings, self.sources_read(rtl=True, tb=False))

    @staticmethod
    def vhdl_top_of(settings: "YosysBase.Settings", rtl_sources: List[Any]) -> bool:
        """Return whether the top is VHDL, given the RTL sources that the flow reads (see
        `top_is_vhdl`)."""
        if settings.top_is_vhdl is not None:
            return settings.top_is_vhdl
        units = [src for src in rtl_sources if src.type in LANGUAGE_TYPES]
        return bool(units) and units[-1].type is SourceType.Vhdl

    def verbatim_path(self, path: str | os.PathLike[str]) -> str:
        """Return `path` as a `.ys` token for an argument that yosys takes verbatim.

        Yosys does not remove quotes from the arguments of `read_verilog -I<dir>`, `show -prefix`
        and the ghdl and slang plugins. It also splits a script line at whitespace. So no quoting
        can pass them a path with a space. Xeda names such a path through a whitespace-free
        symbolic link in the run directory (`path_aliases/`). A path that does not exist yet, such
        as an output, is named through an alias of its directory.
        """
        text = str(path)
        if not _NOT_A_YS_TOKEN.search(text):
            return text
        # Xeda renders the scripts and yosys runs in the run directory, so relative paths and
        # the alias are relative to it.
        target = self.run_file(text)
        if target.exists():
            return self._path_alias(target)
        if _NOT_A_YS_TOKEN.search(target.name):
            raise FlowException(
                f"A yosys script cannot name {text!r}: the command takes the path verbatim, and "
                "its file name contains whitespace. Choose a name without spaces."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        return f"{self._path_alias(target.parent)}/{target.name}"

    def _path_alias(self, target: Path) -> str:
        """Return the name of a whitespace-free symbolic link to the absolute path `target`.

        The name is relative to the run directory: `path_aliases/<digest of that path>/<its
        name>`. One target always gets the same alias and two targets never share one. The name
        keeps its extension (the ghdl plugin, for one, decides by it what a file is).
        """
        digest = hashlib.sha256(str(target).encode()).hexdigest()[:12]
        name = Path(PATH_ALIASES_DIR) / digest / _NOT_A_YS_TOKEN.sub("_", target.name)
        alias = self.run_file(name)
        alias.parent.mkdir(parents=True, exist_ok=True)
        if alias.is_symlink():
            if os.readlink(alias) == str(target):
                return name.as_posix()
            self.run_directory.remove(alias)
        try:
            alias.symlink_to(target, target_is_directory=target.is_dir())
        except OSError as e:
            raise FlowException(
                f"A yosys script cannot name {str(target)!r}, which contains whitespace, and "
                f"creating a whitespace-free link to it failed: {e}"
            ) from e
        return name.as_posix()

    def ghdl_arg(self, arg: str) -> str:
        """Return an argument of the ghdl plugin as a `.ys` token.

        The plugin takes all its arguments verbatim, so `verbatim_path` names a source file or
        the directory of a `-P<dir>` library path.
        """
        if arg.startswith("-P"):
            return "-P" + self.verbatim_path(arg[2:])
        if arg.startswith("-"):
            return arg
        return self.verbatim_path(arg)

    @cached_property
    def yosys(self):
        """The Yosys tool, with the output and debug options of the settings."""
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
            minimum_version=self.minimum_yosys,
            default_args=default_args,
        )

    def run_yosys(self, *args: Any) -> None:
        """Run yosys with `args`, and keep its temporary files and its state in the run directory.

        Yosys and the programs it starts make their temporary directories and files where the
        environment variable `TMPDIR` points, and `abc_new` leaves its directory there. Yosys also
        writes the history of its commands in `$XDG_STATE_HOME/yosys`, which is
        `~/.local/state/yosys` by default, and cuts the file there to its last 100 lines. The
        process gets both variables set to `YOSYS_TEMPORARY_DIRECTORY`, a new empty directory in
        the run directory, which is removed when yosys ends, however it ends. The environment of
        Xeda itself does not change, and no other tool gets the variables."""
        run_directory = self.run_directory
        run_directory.remove(YOSYS_TEMPORARY_DIRECTORY)
        run_directory.writable(YOSYS_TEMPORARY_DIRECTORY).mkdir()
        try:
            self.yosys.run(
                *args,
                env={
                    "TMPDIR": YOSYS_TEMPORARY_DIRECTORY,
                    "XDG_STATE_HOME": YOSYS_TEMPORARY_DIRECTORY,
                },
            )
        finally:
            run_directory.remove(YOSYS_TEMPORARY_DIRECTORY)

    def get_utilization(self) -> Optional[dict]:
        """Return the utilization report that this run wrote (`Flow.report_file`), if any."""
        report = self.report_file(self.artifacts.utilization_report)
        if report is None:
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


class YosysSynthesisBase(YosysBase):
    """The base of the flows that map a design to a netlist with yosys, `yosys` and `yosys_fpga`.

    It holds the netlist and its writers, the ABC mapping and the passes after synthesis.
    `yosys_sim` only reads the design with yosys, so it is a `YosysBase` and has none of these.
    """

    class Settings(YosysBase.Settings):
        splitnets: bool = Field(
            False, description="Run `splitnets`, splitting multi-bit nets into individual bits."
        )

        splitnets_driver: bool = Field(
            False, description="Pass `-driver` to `splitnets`, so nets are split by their driver."
        )

        splitnets_ports: bool = Field(
            False, description="Pass `-ports` to `splitnets`, so module ports are split too."
        )

        rmports: bool = Field(False, description="Remove unused or undriven ports.")

        synth_flags: List[str] = Field(
            [],
            description="Extra flags passed to yosys' `synth` command. When `yosys` maps to a "
            "liberty library, it refuses a flag that a setting or the script gives, and the "
            "error names the setting. With `arithmetic.wrap_operators`, the flags go to the "
            "coarse part of `synth` only, since wrapping replaces the fine part.",
        )

        noabc: bool = Field(
            False, description="Do not run the `abc` step. Also pass `-noabc` to `synth`."
        )

        abc_dff: bool = Field(True, description="Run abc and abc9 with `-dff`.")

        abc_flags: List[str] = Field(
            [],
            description="Extra flags passed to yosys' `abc`/`abc9`. When `yosys` maps to a "
            "liberty library, it refuses a flag that a setting gives, and the error names the "
            "setting.",
        )

        abc_constr: List[str] = Field(
            [],
            description='Lines written to abc\'s constraint file, e.g. "set_driving_cell ..." or '
            '"set_load ...".',
        )

        abc_script: Optional[str] = Field(
            None,
            description="Custom abc script, replacing the default mapping script: a script file, "
            'or the script itself written inline after a "+" ("+strash;map"). With '
            "`abc.mapper: abc_new` in `yosys`, the script works on a GIA and must end in a mapped "
            "one (`&nf`), and it replaces the scripts of wrapped operators. Advanced: a "
            "wrong script silently produces a poor or invalid netlist.",
        )

        post_synth_rename: List[str] = Field(
            [], description='Flags passed to yosys\' `rename` after synthesis, e.g. ["-hide"].'
        )

        netlist_verilog: Optional[Path] = Field(
            Path("netlist.v"),
            alias="netlist",
            description="Write the synthesized gate-level netlist to this Verilog file. Set to "
            "null to skip.",
            json_schema_extra=deliverable(),
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
            "switches above imply. When `yosys` maps to a liberty library, it refuses a flag "
            "that a `netlist_*` switch gives.",
        )

        netlist_src_attrs: bool = Field(
            False,
            description="Keep `src` attributes (source file and line) in the written netlists "
            "(JSON, Verilog and BLIF).",
        )

        netlist_unset_attributes: List[str] = Field(
            [], description="Attributes to strip from the design before writing any netlist."
        )

        netlist_json: Optional[Path] = Field(
            Path("netlist.json"),
            alias="json_netlist",
            description="Write the synthesized netlist to this JSON file. Consumers such as "
            "`nextpnr` need it.",
            json_schema_extra=deliverable(),
        )

        netlist_graph: Optional[Path] = Field(
            None,
            description="Render a `show` graph of the synthesized netlist to this file.",
            json_schema_extra=deliverable("outputs/{design}_netlist.dot"),
        )

        netlist_graph_flags: List[str] = Field(
            ["-stretch", "-enum", "-width", "-href", "-color maroon3 t:*dff"],
            description="Flags passed to yosys' `show` when rendering `netlist_graph`.",
        )

        write_blif: Optional[Path] = Field(
            None,
            description="Write the synthesized netlist to this BLIF file.",
            json_schema_extra=deliverable("outputs/{design}.blif"),
        )

        sta: bool = Field(
            False,
            description="Run a simple static timing analysis (implies `flatten`).",
        )

        post_synth_opt: Optional[bool] = Field(
            False,
            description="Run additional optimization steps after synthesis.",
        )

        ltp: bool = Field(False, description="Print the longest topological path in the design.")

        def write_verilog_flags(self) -> List[str]:
            """Return the `write_verilog` flags: `netlist_verilog_flags` plus the flags that the
            `netlist_*` switches imply.

            A switch left `None` adds nothing. The method runs when the script is written, so a
            switch set after construction takes effect (`Yosys.run` sets `netlist_expr` so).
            """
            flags = list(self.netlist_verilog_flags)
            switches = {
                "netlist_dec": self.netlist_dec,
                "netlist_hex": self.netlist_hex,
                "netlist_expr": self.netlist_expr,
                "netlist_attrs": self.netlist_attrs,
                "netlist_blackboxes": self.netlist_blackboxes,
                "netlist_simple_lhs": self.netlist_simple_lhs,
            }
            for field, flag, flag_when in WRITE_VERILOG_SWITCHES:
                switch = switches[field]
                if switch is flag_when:
                    flags.append(flag)
                elif switch is (not flag_when) and flag in flags:
                    flags.remove(flag)
            return unique(flags)

        def attributes_to_unset(self) -> List[str]:
            """Return `netlist_unset_attributes`, plus `src` unless `netlist_src_attrs` keeps it.

            The script removes these attributes before it writes any netlist. `netlist_attrs` has
            no effect on them: it drops attributes from the Verilog netlist only, and the JSON
            netlist always carries them.
            """
            attributes = list(self.netlist_unset_attributes)
            if not self.netlist_src_attrs:
                attributes.append("src")
            return unique(attributes)

        @field_validator("abc_script", mode="before")
        @classmethod
        def validate_abc_script(cls, value):
            """Replace the spaces of an inline script ("+...") with commas.

            Any other value names a script file.
            """
            if isinstance(value, str) and value.startswith("+"):
                return value.replace(" ", ",")
            if isinstance(value, str) and value:
                return value
            return value

    def _abc_script_file(self) -> Optional[Path]:
        """Return the ABC script file that `abc_script` names, or None.

        An inline script (one that starts with `+`) names no file.
        """
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        if ss.abc_script and not ss.abc_script.startswith("+"):
            return self.process_path(
                ss.abc_script, subs_vars=True, resolve_to=self.design.design_root
            )
        return None

    def locate_prepared_inputs(self) -> Optional[List[Path]]:
        """Return the ABC script file that `abc_script` names, as a list of at most one path."""
        return [file for file in (self._abc_script_file(),) if file]

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Refuse what no Yosys synthesis flow can honor, before anything runs.

        `sta` and `ltp` analyze the flattened design, so `flatten: false` conflicts with them. A
        flow that adds checks calls this method first.
        """
        super().check_settings_supported(settings)
        assert isinstance(settings, cls.Settings)
        if (settings.sta or settings.ltp) and settings.flatten is False:
            raise FlowSettingsException(
                "sta and ltp analyze the flattened design, so they cannot be combined with "
                "flatten=false."
            )

    def init(self):
        """Prepare the synthesis settings, and the shared settings through `YosysBase.init`."""
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        # A flow built directly is not planned, so check this rule here. The other rules of a
        # flow's hook stay with planning: tests that build a flow directly render a stop after
        # the RTL, for one.
        YosysSynthesisBase.check_settings_supported(ss)
        super().init()
        abc_script = self._abc_script_file()
        if abc_script:
            ss.abc_script = str(abc_script)
            self.implicit_inputs.append(abc_script)
        if ss.sta or ss.ltp:
            ss.flatten = True
        if ss.abc_dff:
            append_flag(ss.abc_flags, "-dff")
        if ss.netlist_verilog:
            self.artifacts.netlist_verilog = ss.netlist_verilog
        if ss.netlist_json:
            self.artifacts.netlist_json = ss.netlist_json

    def prepare_output_parents(self) -> None:
        """Create the parent directories of the outputs when the flow runs, not in `init()`."""
        super().prepare_output_parents()
        assert isinstance(self.settings, self.Settings)
        for name in ("netlist_verilog", "netlist_json"):
            path = getattr(self.settings, name)
            if path:
                self.run_directory.inside(path).parent.mkdir(parents=True, exist_ok=True)


def process_parameters(parameters: Dict[str, Any]) -> Dict[str, str]:
    out = dict()
    for k, v in parameters.items():
        if isinstance(v, bool):
            v = f"1'b{int(v)}"
        elif isinstance(v, str) and not re.match(r"\d+'b[01]+", v):
            # canonical (unescaped); the `esc` template filter escapes for the script language
            v = '"' + v + '"'
        out[k] = str(v)
    return out
