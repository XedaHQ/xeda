"""The placement flow.

`openroad_place` reads a gate-level netlist and the constraints, makes the floorplan, places the
tap cells and builds the power grid, places the pins and the cells, optimizes the design and
legalizes the placement. It owns the routing and optimization options that the database stores,
so no later flow sets them again.

`effective` is the one function that turns the settings and the platform into the values that
the templates read as `P`. It reads nothing else. A value that a stage needs and neither the
settings nor the platform give is an error that names where to give it, before OpenROAD starts.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Dict, List, Literal, Optional

from ...dataclass import Field, extras, tier
from ...design import SourceType
from ...flow import AsicSynthFlow, Flow, In, MetricSpec, Out, describe_results
from ...flow.asic import AsicPlatformSettings
from ...flow.flow import FlowSettingsError, FlowSettingsException
from ...platforms.model import CellPort, PlatformModel, Track
from ...units import Bounds, Time, ratio
from .base import EffectiveBase, OpenroadStage, base_values, platform_time_unit
from .groups import differing_leaves
from .metrics import MetricsError, stage_results
from .place_pdn import PdnGrid, connect_of, grid_of, stripe_of
from .place_groups import (
    ArithSwapGroup,
    DesignGroup,
    DetailedPlacementGroup,
    FloorplanGroup,
    GlobalPlacementGroup,
    IoGroup,
    PdnGroup,
    ResizeGroup,
    RoutingGroup,
    TapcellGroup,
    TieGroup,
)

__all__ = ["EffectivePlace", "OpenroadPlace", "effective"]

log = logging.getLogger(__name__)

#: The name of the file that the flow writes the constraints of the `clocks` setting to.
CLOCKS_SDC = "clocks.sdc"


@dataclass(frozen=True)
class EffectivePlace(EffectiveBase):
    """The values of the platform and the settings that the stages of `openroad_place` read as
    `P`. A value that the platform may leave out is `None` when no stage reads it."""

    #: The placement site of the core cells. `None` with a floorplan from a DEF file.
    site: Optional[str]
    #: The tracks of each routing layer. Empty: the tool makes the tracks from the LEF files.
    tracks: Dict[str, Track]
    #: The share of routing capacity to keep free, by layer or layer range.
    layer_adjustments: Dict[str, float]
    #: The routing layers for signal nets and for clock nets, as `min-max`, or `None`.
    signal_layers: Optional[str]
    clock_layers: Optional[str]
    #: The cell and port that drive a constant 0 and a constant 1. `None` without tie repair.
    tie_low: Optional[CellPort]
    tie_high: Optional[CellPort]
    #: What the tap cell stage does: `tap_endcap`, `cut_rows` or `none`.
    tapcell_mode: str
    tap_distance: Optional[float]
    tap_master: Optional[str]
    endcap_master: Optional[str]
    #: The power grid, with the kind of every global connection resolved. Empty without a grid.
    pdn: PdnGrid
    #: Whether the resizer swaps a cell only for a cell with the same footprint.
    match_cell_footprint: bool
    #: The target density of the placement with `density.mode: fixed`, else `None`.
    density_value: Optional[float]
    #: The layers of the pins on the horizontal and on the vertical edges. Empty when no stage
    #: places pins.
    io_horizontal: tuple[str, ...]
    io_vertical: tuple[str, ...]


def _platform_gap(model: PlatformModel, what: str, need: str, fix: str) -> FlowSettingsException:
    """Return the error for a value that the platform lacks. `need` says what needs the value
    and `fix` says how to give it."""
    return FlowSettingsException(
        f"the platform {model.name!r} gives no {what}, which {need}: {fix}. A residue file "
        "(`platform_file`) gives a platform value under `values`"
    )


def _platform_cannot_give(settings: AsicPlatformSettings, leaf: str) -> bool:
    """Return whether no layer of the platform can give the leaf `leaf` (a dotted key of the
    platform model). A platform that reads the configuration of its PDK (`upstream`) may give any
    leaf, and only the files tell. Without it, the residue files are all that give values. This
    reads the residue files only."""
    if settings.residue().upstream is not None:
        return False
    for residue in settings.residue_layers():
        if residue is None:
            continue
        value: object = residue.values
        for part in leaf.split("."):
            value = getattr(value, part)
        if value not in (None, [], {}):
            return False
    return True


def _layer_range(low: Optional[str], high: Optional[str]) -> Optional[str]:
    return f"{low}-{high}" if low and high else None


def _grid(settings: "OpenroadPlace.Settings", model: PlatformModel) -> PdnGrid:
    """Return the power grid: the grid of the platform with each leaf of the setting over it,
    with the kind of each global connection resolved and every stripe and connection in the one
    shape that the templates read. Raise if the grid cannot be built."""
    given = settings.pdn.grid
    grid = grid_of(model.pdn)
    if given is not None:
        # The settings of a launched flow are validated again, which marks every field of the
        # grid as given. So a leaf counts as given when it holds something.
        leaves = {name: getattr(given, name) for name in PdnGrid.model_fields}
        grid = grid.model_copy(
            update={name: value for name, value in leaves.items() if value not in (None, [])}
        )
    fix = "give `pdn.grid`, or set `pdn.mode: none` to build no grid"
    if grid.power_net is None or grid.ground_net is None:
        raise _platform_gap(model, "power_net and ground_net", "`pdn.mode: generated` needs", fix)
    if not grid.rails and not grid.straps:
        raise _platform_gap(model, "rails or straps", "`pdn.mode: generated` needs", fix)
    connections = []
    for connection in grid.global_connects:
        kind = connection.kind
        if kind is None:
            by_net: Dict[str, Literal["power", "ground"]] = {
                grid.power_net: "power",
                grid.ground_net: "ground",
            }
            kind = by_net.get(connection.net)
        if kind is None:
            raise FlowSettingsException(
                f"the global connection to the net {connection.net!r} has no `kind`, and the net "
                f"is neither the power net {grid.power_net!r} nor the ground net "
                f"{grid.ground_net!r}: give `kind: power` or `kind: ground` in `pdn.grid`"
            )
        connections.append(connection.model_copy(update={"kind": kind}))
    return grid.model_copy(
        update={
            "global_connects": connections,
            "straps": [stripe_of(strap) for strap in grid.straps],
            "connects": [connect_of(connect) for connect in grid.connects],
        }
    )


def _layer_adjustments(
    settings: "OpenroadPlace.Settings", model: PlatformModel, signal: Optional[str]
) -> Dict[str, float]:
    """Return the capacity to keep free by layer. The setting replaces the table of the platform.
    The one fraction of the platform covers the signal layers."""
    if settings.routing.layer_adjustments is not None:
        return dict(settings.routing.layer_adjustments)
    routing = model.routing
    if routing.layer_adjustments:
        return dict(routing.layer_adjustments)
    if routing.layer_adjustment is None:
        return {}
    if signal is None:
        raise _platform_gap(
            model,
            "min_layer and max_layer under `routing`",
            "the one `routing.layer_adjustment` of the platform covers",
            "give `routing.signal_layers` or `routing.layer_adjustments`",
        )
    return {signal: routing.layer_adjustment}


def places_pins(settings: "OpenroadPlace.Settings") -> bool:
    """Return whether the flow places the pins. `io.flow: concurrent` places them together with
    the cells (`global_placement -place_ios`). `io.flow: skip_io_pass` places them with
    `place_pins` after a placement without pins, except when a DEF file gives the pins."""
    flow = settings.io.flow
    return flow == "concurrent" or (flow == "skip_io_pass" and settings.floorplan.sizing != "def")


def _density(settings: "OpenroadPlace.Settings", model: PlatformModel) -> Optional[float]:
    """Return the target density for `density.mode: fixed`: the setting, else the platform."""
    if settings.global_placement.density.mode != "fixed":
        return None
    if settings.global_placement.density.value is not None:
        return settings.global_placement.density.value
    if model.place_density is None:
        raise _platform_gap(
            model,
            "`place_density`",
            "`global_placement.density.mode: fixed` needs",
            "add it, or give `global_placement.density.value`, or set "
            "`global_placement.density.mode` to `auto` or `uniform`",
        )
    return float(model.place_density)


def _io_layers(
    settings: "OpenroadPlace.Settings", model: PlatformModel
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the layers of the pins on the horizontal and on the vertical edges: the settings,
    else the platform. Both are empty when no stage places pins."""
    if not places_pins(settings):
        return (), ()
    found = []
    for edge, given, layer in (
        ("horizontal", settings.io.horizontal_layers, model.io_layers.horizontal),
        ("vertical", settings.io.vertical_layers, model.io_layers.vertical),
    ):
        if given is None and layer is None:
            raise _platform_gap(
                model,
                f"layer for the pins on the {edge} edges (`io_layers.{edge}`)",
                "the placement of the pins needs",
                f"add it, or give `io.{edge}_layers`, or set `io.flow: fixed`",
            )
        found.append(tuple(given) if given is not None else (str(layer),))
    return found[0], found[1]


def effective(settings: "OpenroadPlace.Settings", model: PlatformModel) -> EffectivePlace:
    """Return the values of the templates of `openroad_place` for `settings` and the platform
    `model`. The function reads only its arguments and changes neither.

    Each value comes from the setting that names it, else from the platform. Raise
    `FlowSettingsException` if a stage needs a value and neither gives it."""
    routing = model.routing
    sizing = settings.floorplan.sizing
    site = settings.floorplan.site or model.site
    if sizing != "def" and site is None:
        raise _platform_gap(
            model,
            "`site`",
            f"`floorplan.sizing: {sizing}` needs to make the rows",
            "add it, or give `floorplan.site`, or take the floorplan from a DEF file with "
            "`floorplan.sizing: def`",
        )
    if settings.floorplan.tracks == "platform" and not model.tracks:
        raise _platform_gap(
            model,
            "`tracks`",
            "`floorplan.tracks: platform` needs",
            "add them, or set `floorplan.tracks: from_tech` to take the pitch of each layer from "
            "the LEF files",
        )
    for leaf in ("repair_fanout", "insert_cells"):
        if not getattr(settings.tie, leaf):
            continue
        for name in ("tie_low", "tie_high"):
            if getattr(model, name) is None:
                raise _platform_gap(
                    model,
                    f"`{name}` cell",
                    f"`tie.{leaf}` needs",
                    f"add it, or set `tie.{leaf}: false`",
                )
    signal = settings.routing.signal_layers or _layer_range(routing.min_layer, routing.max_layer)
    clock = settings.routing.clock_layers or _layer_range(
        routing.clock_min_layer, routing.max_layer
    )
    master = settings.tapcell.master or model.tap_cell
    mode = settings.tapcell.mode or ("tap_endcap" if master is not None else "cut_rows")
    given_distance = settings.tapcell.distance
    distance = given_distance.to("um") if given_distance is not None else model.tapcell.distance
    if mode == "cut_rows" and settings.tapcell.mode is None:
        for leaf in (
            *(["tapcell.distance"] if given_distance is not None else []),
            *differing_leaves(settings.tapcell.nwell_masters, "tapcell.nwell_masters"),
            *differing_leaves(settings.tapcell.corner_masters, "tapcell.corner_masters"),
        ):
            raise FlowSettingsException(
                f"`{leaf}` has no effect: the platform {model.name!r} has no tap cell, so "
                "`tapcell.mode` is `cut_rows`, which places no tap cells. Set `tapcell.mode: "
                f"tap_endcap` and `tapcell.master`, or delete `{leaf}`"
            )
    if mode == "tap_endcap":
        if master is None:
            raise _platform_gap(
                model,
                "`tap_cell`",
                "`tapcell.mode: tap_endcap` needs",
                "add it, or give `tapcell.master`",
            )
        if distance is None:
            raise _platform_gap(
                model,
                "`tapcell.distance`",
                "`tapcell.mode: tap_endcap` needs",
                "add it, or give the setting `tapcell.distance`",
            )
    io_horizontal, io_vertical = _io_layers(settings, model)
    return EffectivePlace(
        **base_values(settings, model),
        site=site,
        tracks={} if settings.floorplan.tracks == "from_tech" else dict(model.tracks),
        layer_adjustments=_layer_adjustments(settings, model, signal),
        signal_layers=signal,
        clock_layers=clock,
        tie_low=model.tie_low,
        tie_high=model.tie_high,
        tapcell_mode=mode,
        tap_distance=distance,
        tap_master=master,
        endcap_master=settings.tapcell.endcap_master or model.endcap_cell,
        pdn=_grid(settings, model) if settings.pdn.mode == "generated" else PdnGrid(),
        match_cell_footprint=bool(model.match_cell_footprint),
        density_value=_density(settings, model),
        io_horizontal=io_horizontal,
        io_vertical=io_vertical,
    )


class OpenroadPlace(OpenroadStage, AsicSynthFlow):
    """Read a gate-level netlist, then floorplan, place and optimize it with OpenROAD.

    One OpenROAD process loads the Liberty and LEF files of the platform, the netlist and the
    constraints. It makes the floorplan, places the tap cells and builds the power grid, places
    the pins and the cells globally, optimizes the design, and legalizes the placement. It writes
    the database `place.odb` and the constraints `place.sdc`. The constraints are the SDC files of
    the design, then the clocks that the `clocks` setting gives, then `sdc_files`.

    The routing layers, the capacity of each layer and the cells that the resizer must not touch
    stay in the database, so the flows after this one keep them. These settings belong to this
    flow."""

    results_description = describe_results(
        "wns",
        "tns",
        "whs",
        "worst_slack",
        "fmax",
        "utilization",
        "power",
        area=MetricSpec(
            "Area of the standard cells after the placement, in square micrometers.", "number"
        ),
        hold_tns=MetricSpec(
            "Total negative hold slack after the placement, from the parasitics that OpenROAD "
            "estimates from the placement.",
            "number",
            "ns",
        ),
        platform=MetricSpec(
            "The platform that the design is placed on: `name`, the PDK `provider` and "
            "`version`, the `corner`, and `sources`, the layer (derived, upstream, residue or "
            "user) that gave each value of the platform model.",
            "object",
        ),
    )
    design_parts = frozenset({"rtl"})
    design_source_types = frozenset({SourceType.Sdc, SourceType.VerilogNetlist})

    class Settings(OpenroadStage.Settings, AsicSynthFlow.Settings):
        """The settings of the placement flow."""

        input_delay_frac: float = Field(
            0.20,
            ge=0,
            lt=1,
            description="The input delay of the generated constraints, as a fraction of the clock "
            "period, from 0 up to but not including 1. Used only when `input_delay` is unset.",
            json_schema_extra=extras(tier("advanced"), ratio()),
        )
        output_delay_frac: float = Field(
            0.20,
            ge=0,
            lt=1,
            description="The output delay of the generated constraints, as a fraction of the "
            "clock period, from 0 up to but not including 1. Used only when `output_delay` is "
            "unset.",
            json_schema_extra=extras(tier("advanced"), ratio()),
        )
        input_delay: Optional[Annotated[Time, Bounds(ge="0 ns")]] = Field(
            None,
            description="The input delay of the generated constraints, a time with a unit, such "
            "as `100 ps`. The flow converts it to the time unit of the platform. It takes "
            "precedence over `input_delay_frac`.",
            json_schema_extra=tier("advanced"),
        )
        output_delay: Optional[Annotated[Time, Bounds(ge="0 ns")]] = Field(
            None,
            description="The output delay of the generated constraints, a time with a unit, such "
            "as `100 ps`. The flow converts it to the time unit of the platform. It takes "
            "precedence over `output_delay_frac`.",
            json_schema_extra=tier("advanced"),
        )
        sdc_files: list[Path] = Field(
            [],
            description="More SDC files. The flow reads them after the constraints that it "
            "generates from `clocks`. A relative path is relative to the design root.",
            json_schema_extra=tier("advanced"),
        )
        design: DesignGroup = Field(
            default_factory=DesignGroup,
            description="The loading of the netlist.",
        )
        floorplan: FloorplanGroup = Field(
            default_factory=FloorplanGroup,
            description="The size and the shape of the floorplan, and the cleanup of the netlist "
            "before the placement.",
        )
        routing: RoutingGroup = Field(
            default_factory=RoutingGroup,
            description="The routing layers and the capacity of each layer. The database stores "
            "them, so the later flows keep them.",
        )
        tie: TieGroup = Field(
            default_factory=TieGroup,
            description="The tie cells that drive the constant nets.",
        )
        tapcell: TapcellGroup = Field(
            default_factory=TapcellGroup,
            description="The well tap cells and the end cap cells.",
        )
        pdn: PdnGroup = Field(
            default_factory=PdnGroup,
            description="The power grid over the standard cells.",
        )
        io: IoGroup = Field(
            default_factory=IoGroup,
            description="The placement of the pins of the design.",
        )
        global_placement: GlobalPlacementGroup = Field(
            default_factory=GlobalPlacementGroup,
            description="The global placement of the cells.",
        )
        resize: ResizeGroup = Field(
            default_factory=ResizeGroup,
            description="The optimization of the design after the global placement.",
        )
        arith_swap: ArithSwapGroup = Field(
            default_factory=ArithSwapGroup,
            description="The swap of the arithmetic modules for faster or smaller variants.",
        )
        detailed_placement: DetailedPlacementGroup = Field(
            default_factory=DetailedPlacementGroup,
            description="The legalization and the optimization of the placement.",
        )

    class Inputs(OpenroadStage.Inputs):
        netlist: Path = In(
            SourceType.VerilogNetlist,
            producer="yosys",
            output="netlist",
            description="The gate-level Verilog netlist of the design, mapped to the cells of "
            "the platform.",
        )
        sdc: list[Path] = In(
            SourceType.Sdc,
            optional=True,
            description="The SDC files that constrain the design. They follow the clocks "
            "that the flow writes from the `clocks` setting.",
        )

    class Outputs(OpenroadStage.Outputs):
        odb: Path = Out(
            SourceType.OpenroadDbPlaced,
            description="The OpenROAD database after placement.",
        )
        sdc: Path = Out(
            SourceType.OpenroadSdcPlaced,
            description="The SDC file of the placed design.",
        )

    # --- planning -----------------------------------------------------------------------------

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Refuse what the groups cannot judge alone. The swap of arithmetic modules needs the
        hierarchy of the design. The buffers to remove need the timing-driven placement, and the
        limit of the utilization needs a command of the resize stage that reads it. A floorplan from a DEF file keeps the pins of the file, so a
        setting for the placement of the pins has no effect. The annealing of the pin placer
        needs `place_pins`, which the concurrent placement of the pins does not run. Pins that
        stay need a DEF file or pins placed by hand. A platform that gives no density or
        no pin layer, and cannot get one, needs the setting. This reads the settings and the
        residue files only."""
        super().check_settings_supported(settings)
        if not isinstance(settings, cls.Settings):
            return
        problems: List[tuple[Optional[str], Optional[str], Optional[str], Optional[str]]] = []

        def refuse(key: str, message: str) -> None:
            problems.append((key, message, None, "value_error"))

        if settings.arith_swap.enabled and not settings.hierarchical:
            refuse(
                "arith_swap.enabled",
                "`arith_swap.enabled` swaps the arithmetic modules of a hierarchical netlist, "
                "and `hierarchical` is false: set `hierarchical: true` or delete "
                "`arith_swap.enabled`",
            )
        resize = settings.resize
        if resize.remove_buffers_instances and not settings.global_placement.timing_driven.enabled:
            refuse(
                "resize.remove_buffers_instances",
                "`resize.remove_buffers_instances` names buffers to remove before the "
                "timing-driven placement, and `global_placement.timing_driven.enabled` is "
                "false, so the flow removes none: set "
                "`global_placement.timing_driven.enabled: true` or delete "
                "`resize.remove_buffers_instances`",
            )
        if resize.max_utilization_percent is not None and not (
            resize.buffer_ports.enabled
            or resize.repair_design.enabled
            or resize.repair_timing.enabled
        ):
            refuse(
                "resize.max_utilization_percent",
                "`resize.max_utilization_percent` has no effect: it limits `buffer_ports`, "
                "`repair_design` and `repair_timing`, and the resize stage runs none of them. "
                "Enable one of them or delete `resize.max_utilization_percent`",
            )
        if settings.floorplan.sizing == "def" and settings.io.flow == "skip_io_pass":
            for leaf in differing_leaves(settings.io, "io"):
                if leaf == "io.fixed_pins":
                    continue  # the pins by hand replace pins of the file
                if leaf == "io.algorithm" or leaf.startswith("io.annealing."):
                    refuse(
                        leaf,
                        f"`{leaf}` has no effect with `floorplan.sizing: def`: the flow keeps the "
                        "pins of the DEF file and runs no `place_pins`, and only `place_pins` "
                        f"anneals the pins: delete `{leaf}`",
                    )
                    continue
                refuse(
                    leaf,
                    f"`{leaf}` has no effect with `floorplan.sizing: def`, which keeps the pins "
                    f"of the DEF file: set `io.flow: concurrent` to place the pins again, or "
                    f"delete `{leaf}`",
                )
        if settings.io.flow == "concurrent" and settings.io.algorithm == "annealing":
            advice = (
                "with `floorplan.sizing: def` no flow runs `place_pins`: delete `io.algorithm`"
                if settings.floorplan.sizing == "def"
                else "set `io.flow: skip_io_pass`, or delete `io.algorithm`"
            )
            refuse(
                "io.algorithm",
                "`io.algorithm: annealing` is an algorithm of `place_pins`, and `io.flow: "
                f"concurrent` places the pins with `global_placement -place_ios` instead; {advice}",
            )
        if (
            settings.io.flow == "fixed"
            and settings.floorplan.sizing != "def"
            and not settings.io.fixed_pins
        ):
            refuse(
                "io.flow",
                "`io.flow: fixed` keeps the pins that the floorplan placed, and "
                f"`floorplan.sizing: {settings.floorplan.sizing}` places none: nothing places "
                "the pins of a netlist without a DEF floorplan, and the global placement stops "
                "on the unplaced pins (GPL-0326). Set `floorplan.sizing: def` with a DEF file "
                "that places the pins, give `io.fixed_pins`, or set `io.flow: skip_io_pass`",
            )
        if settings.platform is not None or settings.platform_file is not None:
            density = settings.global_placement.density
            if (
                density.mode == "fixed"
                and density.value is None
                and _platform_cannot_give(settings, "place_density")
            ):
                refuse(
                    "global_placement.density.value",
                    f"the platform {settings.residue().name!r} gives no `place_density`, which "
                    "`global_placement.density.mode: fixed` needs: give "
                    "`global_placement.density.value`, add `place_density` to `values` in the "
                    "residue file (`platform_file`), or set `global_placement.density.mode` to "
                    "`auto` or `uniform`",
                )
            if places_pins(settings):
                for edge, given in (
                    ("horizontal", settings.io.horizontal_layers),
                    ("vertical", settings.io.vertical_layers),
                ):
                    if given is None and _platform_cannot_give(settings, f"io_layers.{edge}"):
                        refuse(
                            f"io.{edge}_layers",
                            f"the platform {settings.residue().name!r} gives no layer for the "
                            f"pins on the {edge} edges (`io_layers.{edge}`), which the placement "
                            f"of the pins needs: give `io.{edge}_layers`, add `io_layers` to "
                            "`values` in the residue file (`platform_file`), or set `io.flow: "
                            "fixed`",
                        )
        if problems:
            raise FlowSettingsError(problems, cls.Settings)

    # --- the files ----------------------------------------------------------------------------

    def floorplan_def(self) -> Optional[Path]:
        """Return the DEF file of `floorplan.def_file` as a path, or `None`. A relative path is
        relative to the design root."""
        settings = self.settings
        assert isinstance(settings, self.Settings)
        given = settings.floorplan.def_file
        return None if given is None else self.normalize_path_to_design_root(given)

    def prepare_inputs(self) -> None:
        """Find the files of the platform, and check the DEF file of the floorplan.

        Then compute the values of the templates. A value that a stage needs and the platform
        lacks stops the run here, before OpenROAD starts."""
        super().prepare_inputs()
        path = self.floorplan_def()
        if path is not None and not path.is_file():
            raise FlowSettingsException(
                f"`floorplan.def_file` names {path}, which is not a file: give the DEF file, or "
                "set `floorplan.sizing: utilization` or `floorplan.sizing: area`"
            )
        self.effective()

    def sdc_list(self) -> List[Path]:
        """Return the SDC files that the process reads, in order: those of the design, the file
        that the flow writes from `clocks`, and `sdc_files`."""
        settings = self.settings
        assert isinstance(settings, self.Settings)
        given = [self.normalize_path_to_design_root(path) for path in settings.sdc_files]
        return [*(self.inputs.sdc or []), Path(CLOCKS_SDC), *given]  # type: ignore[attr-defined]

    # --- the values of the templates ----------------------------------------------------------

    def effective(self) -> EffectivePlace:
        """Return the values that the templates read as `P`. It runs after `prepare_inputs()`."""
        model = self.platform_model
        assert model is not None, "prepare_inputs() builds the platform model"
        settings = self.settings
        assert isinstance(settings, self.Settings)
        return effective(settings, model)

    def bind_template_values(self) -> None:
        """Add `sdc_files` and `floorplan_def` to the values of the base."""
        super().bind_template_values()
        self.jinja_env.globals.update(sdc_files=self.sdc_list(), floorplan_def=self.floorplan_def())

    # --- running OpenROAD ---------------------------------------------------------------------

    def run(self) -> None:
        """Write the constraints of the clocks, then run the process that builds the placement
        database. Warn when the padding of the legalization is above the padding of the global
        placement."""
        odb, sdc = self.run_path / "place.odb", self.run_path / "place.sdc"
        self.outputs.odb, self.outputs.sdc = odb, sdc  # type: ignore[attr-defined]
        settings = self.settings
        assert isinstance(settings, self.Settings)
        detail = settings.detailed_placement.cell_padding
        if detail > settings.global_placement.cell_padding:
            log.warning(
                "detailed_placement.cell_padding (%d) is above global_placement.cell_padding "
                "(%d): the legalization may find no legal place for a cell that the global "
                "placement packed closer",
                detail,
                settings.global_placement.cell_padding,
            )
        self.bind_template_values()
        self.copy_from_template(CLOCKS_SDC, trim_blocks=True, lstrip_blocks=True)
        self.run_openroad("openroad_place", "metrics.json", [odb, sdc, self.failed_odb])

    # --- the results --------------------------------------------------------------------------

    def parse_reports(self) -> bool:
        """Read the metrics of the last stage, the legalization. The slacks are estimates from the
        placement, before any clock tree or route. Return False if the metrics file is missing or
        holds a value that is no number."""
        model = self.platform_model
        assert model is not None, "prepare_inputs() builds the platform model"
        platform = self.platform_result()
        if platform is not None:
            self.results["platform"] = platform
        metrics = self.read_metrics("metrics.json")
        if metrics is None:
            log.error(
                "OpenROAD wrote no usable metrics file: %s",
                Path(self.run_path) / "metrics.json",
            )
            return False
        try:
            found = stage_results(
                metrics,
                "detailedplace",
                time_unit=platform_time_unit(model),
                file=Path(self.run_path) / "metrics.json",
            )
        except MetricsError as error:
            log.error("%s", error)
            return False
        self.results.update(found)
        return True
