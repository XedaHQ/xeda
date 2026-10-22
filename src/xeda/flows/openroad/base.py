"""The base of the OpenROAD flows.

`openroad_place`, `openroad_cts` and `openroad_grt` run one OpenROAD process each; `openroad` runs
two, and a third with `save_images`. A process starts from a netlist (`openroad_place`) or from a
database (`.odb`): the one of the flow before it, or the one that an earlier process of `openroad`
wrote. The database stores the design and the routing and optimization options that the placement
flow set (`openroad_place` owns them, and no later flow sets them again). It does not store the
Liberty files, the SDC constraints, the wire parasitics or the cells that must not be used. So every
process starts with the same restart header (`templates/restart.tcl.j2`) that applies them.

`OpenroadStage` holds what the four flows share: the settings `hierarchical` and `wire_rc`, the
platform files, the values that the templates read (`effective_base`), the way a driver script
runs (`run_openroad`) and the metrics file that OpenROAD writes (`read_metrics`).
"""

import logging
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, ClassVar, Dict, List, Optional, Sequence, TypeVar

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
from ...platforms.derive import liberty_mentions, read_liberty_summary
from ...platforms.model import PlatformModel, WireRc
from ...proc_utils import ProcessTimeout
from ...tool import NonZeroExitCode, Tool
from ...units import Capacitance, Length, Power, Quantity, Resistance, Time, Voltage, library_units
from ...utils import tcl_word, unique
from .groups import FIX_FLAGS, comma_word
from .metrics import MetricsError, load_metrics

__all__ = [
    "EffectiveBase",
    "OpenroadStage",
    "effective_base",
    "base_values",
    "fraction_of",
    "failure_lines",
    "WireRcSettings",
    "merge_wire_rc",
    "platform_time_unit",
    "space_word",
    "tcl_number",
    "tcl_numbers",
    "to_farads",
    "to_femtofarads",
    "to_lib_time",
    "to_micrometers",
    "to_nanometers",
    "to_ohms",
    "to_seconds",
    "to_volts",
    "to_watts",
]

log = logging.getLogger(__name__)

Q = TypeVar("Q", bound=Quantity)

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


#: The time unit of a Liberty file: a scale and one of the units of a time (`1ns`, `10ps`).
_LIBERTY_TIME_UNIT = re.compile(
    r"\s*(?P<scale>[0-9]+(?:\.[0-9]*)?)?\s*(?P<unit>fs|ps|ns|us|ms|s)\s*"
)


def _quantity(value: Any, kind: type[Q]) -> Q:
    """Return `value` if it is a quantity of the type `kind`. A plain number has no unit, so a
    script never gets one from a setting: this is a bug of the caller."""
    if not isinstance(value, kind):
        raise TypeError(
            f"expected a {kind.kind} with a unit, such as `{kind.example}`, for a Tcl script, "
            f"got {value!r}"
        )
    return value


#: The time unit of a Liberty file as OpenSTA reads it: 1, 10 or 100 and a unit, without spaces.
#: OpenSTA warns about another multiplier and keeps the unit alone, and it reads a unit with a
#: space or a fraction as one second.
_OPENSTA_TIME_UNIT = re.compile(r"(?P<scale>1|10|100)(?P<unit>fs|ps|ns|us|ms|s)", re.IGNORECASE)


def liberty_time_unit_seconds(unit: str) -> float:
    """Return the time unit of a Liberty file (`1ns`, `10ps`) in seconds, as OpenSTA reads it.

    Raise `ValueError` for a spelling that OpenSTA reads as another unit, such as `1000ps`
    (OpenSTA reads `1ps`), `1.0ns` or `1 ns` (OpenSTA reads one second)."""
    found = _OPENSTA_TIME_UNIT.fullmatch(unit)
    if found is None:
        raise ValueError(
            f"the time unit {unit!r} is no unit that OpenSTA reads: write 1, 10 or 100 and fs, "
            "ps, ns, us, ms or s, without spaces (`1ns`, `10ps`)"
        )
    return int(found["scale"]) * Time.of(1, found["unit"].lower()).to("s")


_CAPACITANCE_UNIT = re.compile(r"(?P<scale>[0-9.]+)(?P<unit>[a-z]*)", re.IGNORECASE)
_FARADS = {"ff": 1e-15, "pf": 1e-12, "nf": 1e-9, "uf": 1e-6, "mf": 1e-3, "f": 1.0}


def _capacitance_in_farads(unit: str) -> float:
    """Return a capacitance unit as the platform model writes it (`1ff`, `0.1pf`) in farads."""
    found = _CAPACITANCE_UNIT.fullmatch(unit.strip())
    if found is None or found["unit"].lower() not in _FARADS:
        raise ValueError(f"cannot read the capacitance unit {unit!r}")
    return float(found["scale"]) * _FARADS[found["unit"].lower()]


def model_time_unit_seconds(unit: str) -> float:
    """Return the time unit of the platform model (`1ns`, `1 ns`, `0.001us`) in seconds. The model
    takes the unit from a residue file or from the Liberty files of the platform, and the stages
    scale times by its value (`to_lib_time`), so this reads the value and not the spelling that
    OpenSTA requires of a Liberty file (`liberty_time_unit_seconds`)."""
    found = _LIBERTY_TIME_UNIT.fullmatch(unit)
    scale = float(found["scale"]) if found is not None and found["scale"] else 1.0
    if found is None or scale <= 0:
        raise ValueError(f"cannot read the time unit {unit!r} of the platform")
    return scale * Time.of(1, found["unit"]).to("s")


def to_lib_time(time: Any, unit: str) -> str:
    """Return the time `time` as a Tcl number of the time `unit` of the Liberty files (`1ns`,
    `1ps`, `10ps`). A setting for a time has a unit of its own (`5 ns`, `250 ps`), and a command
    of OpenROAD takes the unit of the Liberty files.

    `0.25 ns` is `250.0` in `1ps`. The result has nine decimals at most."""
    time = _quantity(time, Time)
    found = _LIBERTY_TIME_UNIT.fullmatch(unit)
    scale = float(found["scale"]) if found is not None and found["scale"] else 1.0
    if found is None or scale <= 0:
        raise ValueError(f"cannot read the time unit {unit!r} of the Liberty files")
    return tcl_number(round(time.to(found["unit"]) / scale, 9))


def fraction_of(time: Time, fraction: float) -> Time:
    """Return the part `fraction` of the time `time`: the input delay of 0.2 of a clock period."""
    return Time.of(fraction * time.to("ns"), "ns")


def to_seconds(time: Any) -> str:
    """Return a time as a Tcl number of seconds. A few commands of OpenROAD take seconds and not
    the unit of the Liberty files (`set_global_sizing_config -setup_slack_margin`).

    `0.25 ns` is `2.5e-10`."""
    return tcl_number(_quantity(time, Time).to("s"))


def to_farads(capacitance: Any) -> str:
    """Return a capacitance as a Tcl number of farads. A few commands of OpenROAD take farads and
    not the unit of the Liberty files (`configure_cts_characterization -max_cap`).

    `0.05 pF` is `5e-14`. The result has twelve significant digits, so the noise of the division
    does not show."""
    femtofarads = _quantity(capacitance, Capacitance).to("fF")
    return tcl_number(float(f"{femtofarads / 1e15:.12g}"))


def to_femtofarads(capacitance: Any) -> str:
    """Return a capacitance as a Tcl number of femtofarads. A few commands of OpenRCX take
    femtofarads (`extract_parasitics -coupling_threshold`).

    `0.0001 pF` is `0.1`."""
    return tcl_number(_quantity(capacitance, Capacitance).to("fF"))


def to_micrometers(length: Any) -> str:
    """Return a length as a Tcl number of micrometers, which most commands of OpenROAD take. A
    list or a tuple of lengths gives a Tcl list: `[list 0.0 1.5]`.

    `500 nm` is `0.5`."""
    if isinstance(length, (list, tuple)):
        return "[list " + " ".join(to_micrometers(item) for item in length) + "]"
    return tcl_number(_quantity(length, Length).to("um"))


def to_nanometers(length: Any) -> str:
    """Return a length as a Tcl number of nanometers. A few commands of OpenROAD take nanometers
    (`global_placement -perturb_dist`).

    `0.0005 um` is `0.5`."""
    return tcl_number(_quantity(length, Length).to("nm"))


def to_ohms(resistance: Any) -> str:
    """Return a resistance as a Tcl number of ohms (`extract_parasitics -max_res`).

    `2 kohm` is `2000.0`."""
    return tcl_number(_quantity(resistance, Resistance).to("ohm"))


def to_volts(voltage: Any) -> str:
    """Return a voltage as a Tcl number of volts (`set_pdnsim_net_voltage -voltage`).

    `900 mV` is `0.9`."""
    return tcl_number(_quantity(voltage, Voltage).to("V"))


def to_watts(power: Any) -> str:
    """Return a power as a Tcl number of watts (`set_pdnsim_inst_power -power`).

    `2 mW` is `0.002`."""
    return tcl_number(_quantity(power, Power).to("W"))


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
        description="Resistance and capacitance per micrometer of each layer, in the units of the "
        "library: the units of the Liberty file. On the command line, give a pair as "
        "`resistance,capacitance`.",
        json_schema_extra=library_units(),
    )
    vias: Dict[str, Finite] = Field(
        default_factory=dict,
        description="Resistance of each via, by via name, in the units of the library: the "
        "resistance unit of the Liberty file.",
        json_schema_extra=library_units(),
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

    #: The Liberty files: those of `liberty`, else those of the corner of the platform.
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
        liberty=tuple(settings.liberty_files() or model.files.get("liberty", [])),
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

    executables: ClassVar[tuple[str, ...]] = ("openroad",)

    reads_sources = None
    design_parts = frozenset({"rtl"})
    required_settings = {
        "platform": "the ASIC platform: give a bundled platform's name with `-s platform=<name>` "
        "(see `xeda list-platforms`), or a residue file with `platform_file`; in the design "
        "file, give it as `flows.{flow}.platform`",
    }

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

    @property
    def platform_roles(self) -> tuple[str, ...]:
        """The roles of the platform files that the flow opens. `prepare_inputs()` registers
        these files and `locate_prepared_inputs()` returns them, in this order. A flow that opens
        more files for some settings overrides the property. It reads only the settings and the
        residue file of the platform, so a dry run, which builds no platform model, gets the same
        roles as a run."""
        return PLATFORM_ROLES

    def prepare_inputs(self) -> None:
        """Find the files of the platform and build the platform model."""
        super().prepare_inputs()
        model = self.prepare_platform(self.platform_roles)
        self._check_liberty_units(model)

    def _check_liberty_units(self, model: PlatformModel) -> None:
        """Refuse a file of `liberty` whose time or capacitance unit is not the platform's.

        The stages scale times by the time unit of the platform model, which the platform's own
        Liberty files give, and OpenROAD reads the wire capacitance of the platform in the
        capacitance unit of the first Liberty file. A replacing file with another time unit, or a first
        file with another capacitance unit, would shift every time or every wire capacitance
        silently. The files are path settings, so the trace
        records their content."""
        settings = self.settings
        assert isinstance(settings, self.Settings)
        wanted = platform_time_unit(model)
        for path in settings.liberty_files():
            if not path.is_file():
                raise FlowSettingsException(f"`liberty` names {path}, which is not a file")
            try:
                summary = read_liberty_summary(path)
                for attribute, value in (
                    ("time_unit", summary.time_unit),
                    ("capacitive_load_unit", summary.capacitive_load_unit),
                ):
                    if value is None and liberty_mentions(path, attribute):
                        raise ValueError(
                            f"the reader cannot read its `{attribute}`: write `{attribute}` as "
                            "a complete statement that ends in a semicolon"
                        )
                found = summary.time_unit or "1ns"
            except ValueError as error:
                raise FlowSettingsException(
                    f"`liberty` file {path} cannot be read: {error}"
                ) from None
            try:
                same = math.isclose(
                    liberty_time_unit_seconds(found), model_time_unit_seconds(wanted)
                )
            except ValueError as error:
                raise FlowSettingsException(f"`liberty` file {path}: {error}") from None
            if not same:
                raise FlowSettingsException(
                    f"`liberty` file {path} has the time unit {found}, and the platform "
                    f"{model.name!r} has {wanted}: the flow scales all times by the unit of the "
                    "platform. Give Liberty files with the time unit of the platform"
                )
            if path == settings.liberty_files()[0]:
                self._check_liberty_capacitance_unit(model, path, summary.capacitive_load_unit)

    @staticmethod
    def _check_liberty_capacitance_unit(
        model: PlatformModel, path: Path, found: Optional[str]
    ) -> None:
        """Refuse the first file of `liberty` if its capacitance unit is not the platform's.

        OpenROAD reads the capacitance of the wire RC of the platform in the capacitance unit of
        the first Liberty file, so another unit would scale every wire capacitance. A later
        file only scales its own cells, so its unit is free."""
        # a Liberty file that states no capacitance unit has the default of OpenSTA, 1 pF
        found, wanted = found or "1pf", model.capacitance_unit or "1pf"
        try:
            same = math.isclose(_capacitance_in_farads(found), _capacitance_in_farads(wanted))
        except ValueError as error:
            raise FlowSettingsException(f"`liberty` file {path}: {error}") from None
        if not same:
            raise FlowSettingsException(
                f"`liberty` file {path} has the capacitance unit {found}, and the platform "
                f"{model.name!r} has {wanted}: OpenROAD reads the wire capacitance of the "
                "platform in the unit of the first Liberty file. Give Liberty files with the "
                "capacitance unit of the platform"
            )

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
        """Add the helpers that the templates of every OpenROAD flow use. The globals are
        `render_flags` (it quotes for Tcl, and its second argument is the `list_word` for a list
        flag, such as `comma_word`), `comma_word`, `space_word`, `fraction_of`, `fix_flags`,
        `reports` and `failed_odb`. The filters are `tcl_number` and `tcl_numbers` for plain
        numbers, and one filter for each kind of quantity: `lib_time` and `seconds` for a time,
        `farads` and `femtofarads` for a capacitance, `micrometers` and `nanometers` for a
        length, `ohms` for a resistance, `volts` for a voltage and `watts` for a power.
        `init()` calls it.
        Code that renders a template without `init()`, such as a test, can call it."""
        self.add_template_filter("tcl_number", tcl_number, replace_existing=True)
        self.add_template_filter("tcl_numbers", tcl_numbers, replace_existing=True)
        self.add_template_filter("lib_time", self._lib_time, replace_existing=True)
        self.add_template_filter("seconds", to_seconds, replace_existing=True)
        self.add_template_filter("farads", to_farads, replace_existing=True)
        self.add_template_filter("femtofarads", to_femtofarads, replace_existing=True)
        self.add_template_filter("micrometers", to_micrometers, replace_existing=True)
        self.add_template_filter("nanometers", to_nanometers, replace_existing=True)
        self.add_template_filter("ohms", to_ohms, replace_existing=True)
        self.add_template_filter("volts", to_volts, replace_existing=True)
        self.add_template_filter("watts", to_watts, replace_existing=True)
        # one line for each command in a script, with no blank lines from the control statements
        self.jinja_env.trim_blocks = True
        self.jinja_env.lstrip_blocks = True
        # the last line of an included file ends, so the next command starts on its own line
        self.jinja_env.keep_trailing_newline = True
        self.jinja_env.globals.update(
            render_flags=lambda group, list_word=None: render_flags(group, tcl_word, list_word),
            comma_word=comma_word,
            space_word=space_word,
            fraction_of=fraction_of,
            fix_flags=dict(FIX_FLAGS),  # a copy: a flow shares no mutable object
            reports=dict(self.reports),
            failed_odb=self.failed_odb,
        )

    def _lib_time(self, time: Any) -> str:
        """The template filter `lib_time`: a time as a number of the time unit of the Liberty
        files of the platform (`to_lib_time`). It reads the platform model, so a template can use
        it after `prepare_inputs()`."""
        model = self.platform_model
        assert model is not None, "prepare_inputs() builds the platform model"
        return to_lib_time(time, platform_time_unit(model))

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

    def _numbered_reports(self, writes: Sequence[str | os.PathLike]) -> List[Path]:
        """Return the numbered copies of the reports in `writes` that an earlier run left.

        Every few iterations, the global router writes `<name>-<n>.rpt` beside a report
        `<name>.rpt`, and the detailed router writes `<name>.rpt-<n>.rpt`. The names depend on
        the iterations that the process reaches, so they cannot be listed before it starts. Only
        an entry that exists can be a link, so the entries that exist are the ones to make
        ready. Call it after the listed writes are ready: then no directory above is a link."""
        found: List[Path] = []
        for path in writes:
            relative = self._relative_to_run(path)
            directory = Path(self.run_path) / relative.parent
            if relative.suffix != ".rpt" or directory.is_symlink() or not directory.is_dir():
                continue
            stems = "|".join(re.escape(name) for name in (relative.stem, relative.name))
            numbered = re.compile(f"(?:{stems})-[0-9]+\\.rpt")
            with os.scandir(directory) as entries:
                found += [relative.parent / e.name for e in entries if numbered.fullmatch(e.name)]
        return found

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
        a link that it finds at one of those names. The numbered copies of a report that the
        routers write are made ready too (`_numbered_reports`). The script is `<driver>.tcl` and the log is
        `<driver>.log`, both in the run directory. The log goes through `utils.live_log`, so
        OpenROAD's own `-log` is not used."""
        self.remove_failed_odb()
        self.bind_template_values()
        for path in (metrics, *writes):
            self._guard_write(path)
        for path in self._numbered_reports(writes):
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
        report = self.report_file(log_file, optional=True)
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
