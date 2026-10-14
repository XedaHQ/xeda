"""The base of the OpenROAD flows.

Each flow runs OpenROAD once or twice. A process starts from a netlist (`openroad_place`) or from
the database (`.odb`) of the flow before it, and writes its own database and its SDC file. The
database stores the design and the routing and optimization options that the placement flow set
(`openroad_place` owns them, and no later flow sets them again). It does not store the
Liberty files, the SDC constraints, the wire parasitics or the cells that must not be used. So
every process starts with the same restart header (`templates/restart.tcl.j2`) that applies them.

`OpenroadStage` holds what the four flows share: the settings `hierarchical` and `wire_rc`, the
platform files, the values that the templates read (`effective_base`), the way a driver script
runs (`run_openroad`) and the metrics file that OpenROAD writes (`read_metrics`).
"""

import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, ClassVar, Dict, List, Optional, Sequence

from ...dataclass import (
    Field,
    ListLiteralText,
    XedaBaseModel,
    comma_separated_items,
    field_validator,
)
from ...design import Design
from ...flow import Flow
from ...flow.asic import AsicPlatformFlow, AsicPlatformSettings
from ...flow.flow import FlowSettingsException
from ...flow.settings_group import render_flags
from ...platforms.model import PlatformModel, WireRc
from ...proc_utils import ProcessTimeout
from ...tool import NonZeroExitCode, Tool
from ...units import convert_unit
from ...utils import tcl_word, unique
from .groups import FIX_FLAGS, comma_word
from .metrics import MetricsError, load_metrics

__all__ = [
    "EffectiveBase",
    "OpenroadStage",
    "effective_base",
    "base_values",
    "failure_lines",
    "WireRcSettings",
    "merge_wire_rc",
    "platform_time_unit",
    "space_word",
    "tcl_number",
    "tcl_numbers",
    "to_lib_time",
    "to_nanometers",
    "to_seconds",
]

log = logging.getLogger(__name__)

#: The roles of the platform files that every OpenROAD process opens: the Liberty files of the
#: corner and, to read a netlist or a database, the LEF files.
PLATFORM_ROLES = ("liberty", "tech_lef", "cell_lef")


def tcl_number(value: Any) -> str:
    """Return the number `value` as a Tcl word. Anything that is no finite number is a bug of the
    caller: a script never gets text for a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TypeError(f"expected a finite number for a Tcl script, got {value!r}")
    return repr(value)


def tcl_numbers(values: Any) -> str:
    """Return the numbers `values` as one Tcl list word: `[list 0.0 1.5]`. Each item goes through
    `tcl_number`, so text is never written as a number."""
    return "[list " + " ".join(tcl_number(value) for value in values) + "]"


def failure_lines(log_text: str, *, last_errors: int = 5) -> list[str]:
    """Return the lines of the log of an OpenROAD process that say why it failed: the last line
    of the Tcl error (`Error: ...`), then the last `last_errors` lines of the form `[ERROR ...]`,
    in the order of the log. A log with neither gives an empty list."""
    lines = log_text.splitlines()
    tcl_errors = [line for line in lines if line.startswith("Error:")]
    tool_errors = [line for line in lines if line.startswith("[ERROR")]
    return [*tcl_errors[-1:], *tool_errors[-last_errors:]]


def space_word(items: Sequence[Any]) -> str:
    """Return the items as one Tcl word with spaces between them: `"5 1"`. A command such as
    `improve_placement -max_displacement` reads its list from one word. Pass it as the `list_word`
    of `render_flags`."""
    return tcl_word(" ".join(str(item) for item in items))


def platform_time_unit(model: PlatformModel) -> str:
    """Return the time unit of the Liberty files of the platform, written as `1ns`. A platform
    that names none has the default of the format."""
    return model.time_unit or "1ns"


def to_lib_time(nanoseconds: Any, unit: str) -> str:
    """Return a time given in nanoseconds as a Tcl number of the time `unit` of the Liberty files
    (`1ns`, `1ps`, `10ps`). A setting for a time is in nanoseconds on every platform, and a command
    of OpenROAD takes the unit of the Liberty files.

    The result has nine decimals at most, so the noise of the conversion does not show: `0.25 ns`
    is `250.0` in `1ps`."""
    if (
        isinstance(nanoseconds, bool)
        or not isinstance(nanoseconds, (int, float))
        or not math.isfinite(nanoseconds)
    ):
        raise TypeError(
            f"expected a finite number of nanoseconds for a Tcl script, got {nanoseconds!r}"
        )
    converted = convert_unit(nanoseconds, to_unit=unit, from_unit="nanosecond")
    return tcl_number(round(converted, 9))


def to_seconds(nanoseconds: Any) -> str:
    """Return a time given in nanoseconds as a Tcl number of seconds. A setting for a time is in
    nanoseconds on every platform. A few commands of OpenROAD take seconds and not the unit of
    the Liberty files (`set_global_sizing_config -setup_slack_margin`).

    The result has twelve significant digits, so the noise of the conversion does not show:
    `0.25 ns` is `2.5e-10`."""
    if (
        isinstance(nanoseconds, bool)
        or not isinstance(nanoseconds, (int, float))
        or not math.isfinite(nanoseconds)
    ):
        raise TypeError(
            f"expected a finite number of nanoseconds for a Tcl script, got {nanoseconds!r}"
        )
    converted = convert_unit(nanoseconds, to_unit="second", from_unit="nanosecond")
    return tcl_number(float(f"{converted:.12g}"))


def to_nanometers(micrometers: Any) -> str:
    """Return a length given in micrometers as a Tcl number of nanometers. A setting for a
    distance is in micrometers, and a few commands of OpenROAD take nanometers
    (`global_placement -perturb_dist`).

    The result has nine decimals at most, so the noise of the conversion does not show."""
    if (
        isinstance(micrometers, bool)
        or not isinstance(micrometers, (int, float))
        or not math.isfinite(micrometers)
    ):
        raise TypeError(
            f"expected a finite number of micrometers for a Tcl script, got {micrometers!r}"
        )
    converted = convert_unit(micrometers, to_unit="nanometer", from_unit="micrometer")
    return tcl_number(round(converted, 9))


#: A number of the setting `wire_rc`. `nan` and `inf` are no resistance or capacitance.
Finite = Annotated[float, Field(allow_inf_nan=False)]


class WireRcSettings(XedaBaseModel):
    """The setting `wire_rc`: the wire resistance and capacitance that replace the values of the
    platform. It has the fields of the platform's `WireRc`, but it reads its numbers as every
    number setting does, so the command line can give them (`-s wire_rc.vias.v1=0.5`). The
    platform model keeps its strict numbers for the typed files of a platform."""

    signal_layer: Optional[str] = Field(
        None, description="Layer whose resistance and capacitance estimate a signal net."
    )
    clock_layer: Optional[str] = Field(
        None, description="Layer whose resistance and capacitance estimate a clock net."
    )
    layers: Dict[str, tuple[Finite, Finite]] = Field(
        default_factory=dict,
        description="Resistance and capacitance per micrometer of each layer, in the units of "
        "the Liberty file. On the command line, give a pair as `resistance,capacitance`.",
    )
    vias: Dict[str, Finite] = Field(
        default_factory=dict, description="Resistance of each via, by via name."
    )

    @field_validator("layers", mode="before")
    @classmethod
    def _pair_as_text(cls, value: Any) -> Any:
        """Read a pair given as text by the rule that every list given as text uses."""
        if not isinstance(value, dict):
            return value
        pairs = {}
        for layer, pair in value.items():
            if isinstance(pair, str):
                try:
                    pair = comma_separated_items(f"layers.{layer}", pair)
                except ListLiteralText as error:
                    raise error.validation_error() from None
            pairs[layer] = pair
        return pairs


def merge_wire_rc(platform: WireRc, given: Optional[WireRcSettings]) -> WireRc:
    """Return the wire RC of the platform with the leaves of `given` over it.

    Each leaf that `given` sets replaces the leaf of the platform. The tables `layers` and `vias`
    merge key by key. Without `given`, the result is the RC of the platform."""
    if given is None:
        return platform
    return platform.model_copy(
        update={
            "signal_layer": (
                platform.signal_layer if given.signal_layer is None else given.signal_layer
            ),
            "clock_layer": platform.clock_layer if given.clock_layer is None else given.clock_layer,
            "layers": {**platform.layers, **given.layers},
            "vias": {**platform.vias, **given.vias},
        }
    )


@dataclass(frozen=True)
class EffectiveBase:
    """The values of the platform and the settings that the restart header of every OpenROAD
    process reads. The values of each flow (`P` in its templates) inherit this class."""

    #: The Liberty files of the corner.
    liberty: tuple[Path, ...]
    #: The technology LEF file, or `None` if the platform has none.
    tech_lef: Optional[Path]
    #: The LEF files of the cells.
    cell_lefs: tuple[Path, ...]
    #: The wire RC of the platform, with the setting `wire_rc` over it.
    wire_rc: WireRc
    #: The cells that must not be used: those of the platform, then those of the setting
    #: `dont_use_cells`, without repeats.
    dont_use: tuple[str, ...]
    #: The time unit of the Liberty files, written as `1ns`.
    time_unit: str


def base_values(settings: "OpenroadStage.Settings", model: PlatformModel) -> Dict[str, Any]:
    """Return the fields of `EffectiveBase` for `settings` and the platform `model`, by name.

    A flow builds its own `Effective<Flow>` with these and its own fields. The function reads
    only its arguments."""
    tech = model.files.get("tech_lef", [])
    return dict(
        liberty=tuple(model.files.get("liberty", [])),
        tech_lef=tech[0] if tech else None,
        cell_lefs=tuple(model.files.get("cell_lef", [])),
        wire_rc=merge_wire_rc(model.wire_rc, settings.wire_rc),
        dont_use=tuple(unique([*model.dont_use_cells, *settings.dont_use_cells])),
        time_unit=platform_time_unit(model),
    )


def effective_base(settings: "OpenroadStage.Settings", model: PlatformModel) -> EffectiveBase:
    """Return the values of `EffectiveBase` for `settings` and the platform `model`."""
    return EffectiveBase(**base_values(settings, model))


class OpenroadStage(AsicPlatformFlow, Flow):
    """The base class of the OpenROAD flows. Each stage works on a platform and reads only its
    declared inputs."""

    reads_sources = None
    design_parts = frozenset({"rtl"})
    required_settings = {
        "platform": "the ASIC platform: give a bundled platform's name with `-s platform=<name>` "
        "(see `xeda list-platforms`), or a residue file with `platform_file`; in the design "
        "file, give it as `flows.{flow}.platform`",
    }

    #: The roles of the platform files that the flow opens. `prepare_inputs()` registers these
    #: files and `locate_prepared_inputs()` returns them, in this order.
    platform_roles: ClassVar[tuple[str, ...]] = PLATFORM_ROLES
    #: The names of the metrics files that the flow's processes write, in the run directory.
    metrics_files: ClassVar[tuple[str, ...]] = ()
    #: The report files that the flow's scripts write, by key: a fixed name under `reports/`.
    reports: ClassVar[Dict[str, str]] = {}

    class Settings(AsicPlatformSettings):
        """The settings that every OpenROAD flow shares."""

        hierarchical: bool = Field(
            False,
            description="Keep the hierarchy of the design: OpenROAD links the netlist with "
            "`link_design -hier` and reads each database with `read_db -hier`. The database does "
            "not tell how it was read, so every OpenROAD flow of a chain must agree on this "
            "setting. It is separate from the hierarchy that `yosys` keeps.",
        )
        wire_rc: Optional[WireRcSettings] = Field(
            None,
            description="Resistance and capacitance of the wires, which replace the values of "
            "the platform leaf by leaf: the layers and vias that you list replace the ones of "
            "the platform with the same name, and the others stay. The default is the wire RC "
            "of the platform. The database stores no wire RC, so every OpenROAD flow of a chain "
            "must agree on this setting.",
        )

    # --- planning -----------------------------------------------------------------------------

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Refuse a platform that has no LEF files, since OpenROAD cannot read a design without
        them. This reads the residue files only."""
        super().check_settings_supported(settings)
        if not isinstance(settings, AsicPlatformSettings):
            return
        if settings.platform is None and settings.platform_file is None:
            return  # the missing platform is reported with the other required settings
        files = settings.residue().files
        missing = [role for role in ("tech_lef", "cell_lef") if not getattr(files, role)]
        if missing:
            raise FlowSettingsException(
                f"the platform {settings.residue().name!r} gives no {' and no '.join(missing)}, "
                f"which {cls.name} reads: add them to `files` in its residue file"
            )

    @classmethod
    def check_design_supported(
        cls, design: Design, settings: Optional[Flow.Settings] = None
    ) -> None:
        """Refuse a top module whose name would put a file of OpenROAD outside the run directory.

        OpenRCX writes `<top>.totCap` in the working directory. A name with a `/` (an escaped
        Verilog name can have one) would name a file in another directory."""
        super().check_design_supported(design, settings)
        top = design.rtl.top
        if top is not None and ("/" in top or os.sep in top):
            raise FlowSettingsException(
                f"the top module {top!r} has a `/` in its name, so OpenRCX would write "
                f"{top}.totCap outside the run directory: rename the top module"
            )

    # --- the files of the platform ------------------------------------------------------------

    def prepare_inputs(self) -> None:
        """Find the files of the platform and build the platform model."""
        super().prepare_inputs()
        self.prepare_platform(self.platform_roles)

    def locate_prepared_inputs(self) -> Optional[List[Path]]:
        """Return the files that `prepare_inputs()` registers, in the same order."""
        located = super().locate_prepared_inputs()
        if located is None:
            return None
        return [*located, *self.locate_platform_inputs(self.platform_roles)]

    # --- the values of the templates ----------------------------------------------------------

    def effective(self) -> EffectiveBase:
        """Return the values that the templates read as `P`. A flow that needs more than the
        restart header reads overrides this method. It runs after `prepare_inputs()`."""
        model = self.platform_model
        assert model is not None, "prepare_inputs() builds the platform model"
        settings = self.settings
        assert isinstance(settings, self.Settings)
        return effective_base(settings, model)

    @property
    def failed_odb(self) -> str:
        """The name of the database that a failed process writes, in the run directory."""
        return f"{self.name}-failed.odb"

    def add_template_helpers(self) -> None:
        """Add the helpers that the templates of every OpenROAD flow use: `render_flags` (it
        quotes for Tcl, and its second argument is the `list_word` for a list flag, such as
        `comma_word`), `comma_word`, `space_word`, `fix_flags`, the filters `tcl_number`,
        `tcl_numbers`, `lib_time`, `seconds` and `nanometers`, `reports` and `failed_odb`.
        `init()` calls it.
        Code that renders a template without `init()`, such as a test, can call it."""
        self.add_template_filter("tcl_number", tcl_number, replace_existing=True)
        self.add_template_filter("tcl_numbers", tcl_numbers, replace_existing=True)
        self.add_template_filter("lib_time", self._lib_time, replace_existing=True)
        self.add_template_filter("seconds", to_seconds, replace_existing=True)
        self.add_template_filter("nanometers", to_nanometers, replace_existing=True)
        # one line for each command in a script, with no blank lines from the control statements
        self.jinja_env.trim_blocks = True
        self.jinja_env.lstrip_blocks = True
        # the last line of an included file ends, so the next command starts on its own line
        self.jinja_env.keep_trailing_newline = True
        self.jinja_env.globals.update(
            render_flags=lambda group, list_word=None: render_flags(group, tcl_word, list_word),
            comma_word=comma_word,
            space_word=space_word,
            fix_flags=FIX_FLAGS,
            reports=dict(self.reports),
            failed_odb=self.failed_odb,
        )

    def _lib_time(self, nanoseconds: Any) -> str:
        """The template filter `lib_time`: a time in nanoseconds as a number of the time unit of
        the Liberty files of the platform (`to_lib_time`). It reads the platform model, so a
        template can use it after `prepare_inputs()`."""
        model = self.platform_model
        assert model is not None, "prepare_inputs() builds the platform model"
        return to_lib_time(nanoseconds, platform_time_unit(model))

    def bind_template_values(self) -> None:
        """Add the values that exist only after `prepare_inputs()`: `P` (the values of the
        platform and the settings), `inputs`, `outputs` and `dont_use`. `run_openroad()` calls it
        before it renders a driver."""
        values = self.effective()
        self.jinja_env.globals.update(
            P=values,
            inputs=self.inputs,
            outputs=self.outputs,
            dont_use=list(values.dont_use),
        )

    def init(self) -> None:
        """Add the template helpers. This reads no input and writes no file."""
        super().init()
        self.add_template_helpers()

    # --- running OpenROAD ---------------------------------------------------------------------

    def openroad(self) -> Tool:
        """Return the `openroad` tool of this flow."""
        tool = getattr(self, "_openroad_tool", None)
        if tool is None:
            tool = self._openroad_tool = Tool("openroad", self, version_flag="-version")
        return tool

    def remove_failed_odb(self) -> None:
        """Remove the database that a failed process of an earlier run left."""
        self.run_directory.remove(self.failed_odb)

    def _relative_to_run(self, path: str | os.PathLike) -> Path:
        """Return `path` relative to the run directory. Raise `RunDirectoryError` if it leads
        outside."""
        given = Path(os.path.abspath(Path(self.run_path) / path))
        for root in (Path(os.path.abspath(self.run_path)), self.run_directory.path):
            if given.is_relative_to(root):
                return given.relative_to(root)
        self.run_directory.inside(path)  # raises the error that names what leads outside
        raise AssertionError(f"{path} is outside the run directory")

    def _guard_write(self, path: str | os.PathLike) -> Path:
        """Make ready the file that OpenROAD will write at `path`, and return its place.

        A symbolic link that a tool left at the name of the file, or at the name of a directory
        above it, is removed as itself. A missing directory is made. So the write makes a
        regular file in the run directory and never goes through a link."""
        relative = self._relative_to_run(path)
        for parent in reversed(relative.parents):
            if parent != Path("."):
                self.run_directory.writable(parent).mkdir(exist_ok=True)
        return self.run_directory.writable(relative)

    def run_openroad(
        self,
        driver: str,
        metrics: str,
        writes: Sequence[str | os.PathLike] = (),
        *,
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        """Render the script `drivers/<driver>.tcl.j2` and run OpenROAD on it, once.

        `metrics` is the file that OpenROAD writes its metrics to, and `writes` lists the other
        files that the script writes (databases, SDC files, reports, the failed database). Xeda
        makes each of them ready before OpenROAD starts (`_guard_write`), since the tool follows
        a link that it finds at one of those names. The script is `<driver>.tcl` and the log is
        `<driver>.log`, both in the run directory. The log goes through `utils.live_log`, so
        OpenROAD's own `-log` is not used."""
        self.remove_failed_odb()
        self.bind_template_values()
        for path in (metrics, *writes):
            self._guard_write(path)
        script = self.copy_from_template(
            f"drivers/{driver}.tcl.j2",
            script_filename=f"{driver}.tcl",
            trim_blocks=True,
            lstrip_blocks=True,
        )
        log_file = self.run_directory.writable(f"{driver}.log")
        threads = self.settings.nthreads or "max"
        try:
            self.openroad().run(
                "-no_init",
                "-no_splash",
                "-threads",
                str(threads),
                "-exit",
                "-metrics",
                metrics,
                script,
                env=env,
                tee=log_file,
                merge_stderr=True,
            )
        except NonZeroExitCode as error:
            if isinstance(error, ProcessTimeout):
                raise
            raise NonZeroExitCode(
                error.command_args,
                error.exit_code,
                self._failure_message(driver, log_file),
            ) from None

    def _failure_message(self, driver: str, log_file: Path) -> str:
        """Return what a failed process of `driver` adds to the message of the failure: the flow,
        the script and the error lines of this run's log (`failure_lines`). OpenROAD reports the
        error of a script only in the log."""
        message = f"{self.name}: OpenROAD stopped while it ran {driver}.tcl"
        report = self.report_file(log_file)
        found = failure_lines(report.read_text(errors="replace")) if report is not None else []
        if not found:
            return f"{message}; see {driver}.log"
        return f"{message}; see {driver}.log: " + " | ".join(found)

    def read_metrics(self, name: str) -> Optional[Dict[str, Any]]:
        """Return the metrics file `name` of the run directory, or `None`.

        The file must exist, be written by this run (`report_file`) and hold a JSON object."""
        path = self.report_file(Path(self.run_path) / name)
        if path is None:
            return None
        try:
            return load_metrics(path)
        except MetricsError as error:
            log.warning("%s", error)
            return None
