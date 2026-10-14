"""The placement flow.

`openroad_place` reads a gate-level netlist and the constraints, makes the floorplan, places the
tap cells and builds the power grid. It owns the routing and optimization options that the
database stores, so no later flow sets them again.

`effective` is the one function that turns the settings and the platform into the values that
the templates read as `P`. It reads nothing else. A value that a stage needs and neither the
settings nor the platform give is an error that names where to give it, before OpenROAD starts.
"""

from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Literal, Optional

from ...dataclass import Field, tier
from ...design import SourceType
from ...flow import AsicSynthFlow, Flow, In, Out
from ...flow.flow import FlowSettingsError, FlowSettingsException
from ...platforms.model import CellPort, PlatformModel, Pdn, Track
from .base import EffectiveBase, OpenroadStage, base_values
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
    pdn: Pdn
    #: Whether the resizer swaps a cell only for a cell with the same footprint.
    match_cell_footprint: bool


def _platform_gap(model: PlatformModel, what: str, need: str, fix: str) -> FlowSettingsException:
    """Return the error for a value that the platform lacks. `need` says what needs the value
    and `fix` says how to give it."""
    return FlowSettingsException(
        f"the platform {model.name!r} gives no {what}, which {need}: {fix}. A residue file "
        "(`platform_file`) gives a platform value under `values`"
    )


def _layer_range(low: Optional[str], high: Optional[str]) -> Optional[str]:
    return f"{low}-{high}" if low and high else None


def _grid(settings: "OpenroadPlace.Settings", model: PlatformModel) -> Pdn:
    """Return the power grid: the grid of the platform with each leaf of the setting over it,
    with the kind of each global connection resolved. Raise if the grid cannot be built."""
    given = settings.pdn.grid
    grid = model.pdn
    if given is not None:
        grid = grid.model_copy(
            update={name: getattr(given, name) for name in sorted(given.model_fields_set)}
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
    return grid.model_copy(update={"global_connects": connections})


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


def effective(settings: "OpenroadPlace.Settings", model: PlatformModel) -> EffectivePlace:
    """Return the values of the templates of `openroad_place` for `settings` and the platform
    `model`. The function reads only its arguments and changes neither.

    Each value comes from the setting that names it, else from the platform. Raise
    `FlowSettingsException` if a stage needs a value and neither gives it."""
    routing = model.routing
    sizing = settings.floorplan.sizing
    if sizing != "def" and model.site is None:
        raise _platform_gap(
            model,
            "`site`",
            f"`floorplan.sizing: {sizing}` needs to make the rows",
            "add it, or take the floorplan from a DEF file with `floorplan.sizing: def`",
        )
    if settings.tie.repair_fanout:
        for name in ("tie_low", "tie_high"):
            if getattr(model, name) is None:
                raise _platform_gap(
                    model,
                    f"`{name}` cell",
                    "`tie.repair_fanout` needs",
                    "add it, or set `tie.repair_fanout: false`",
                )
    signal = settings.routing.signal_layers or _layer_range(routing.min_layer, routing.max_layer)
    clock = settings.routing.clock_layers or _layer_range(
        routing.clock_min_layer, routing.max_layer
    )
    master = settings.tapcell.master or model.tap_cell
    mode = settings.tapcell.mode or ("tap_endcap" if master is not None else "cut_rows")
    given_distance = settings.tapcell.distance
    distance = given_distance if given_distance is not None else model.tapcell.distance
    if mode == "cut_rows" and settings.tapcell.mode is None and given_distance is not None:
        raise FlowSettingsException(
            f"`tapcell.distance` has no effect: the platform {model.name!r} has no tap cell, so "
            "`tapcell.mode` is `cut_rows`, which places no tap cells. Set `tapcell.mode: "
            "tap_endcap` and `tapcell.master`, or delete `tapcell.distance`"
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
    return EffectivePlace(
        **base_values(settings, model),
        site=model.site,
        tracks=dict(model.tracks),
        layer_adjustments=_layer_adjustments(settings, model, signal),
        signal_layers=signal,
        clock_layers=clock,
        tie_low=model.tie_low,
        tie_high=model.tie_high,
        tapcell_mode=mode,
        tap_distance=distance,
        tap_master=master,
        endcap_master=settings.tapcell.endcap_master or model.endcap_cell,
        pdn=_grid(settings, model) if settings.pdn.mode == "generated" else Pdn(),
        match_cell_footprint=bool(model.match_cell_footprint),
    )


class OpenroadPlace(OpenroadStage, AsicSynthFlow):
    """Read a gate-level netlist, then floorplan, place and optimize it with OpenROAD.

    One OpenROAD process loads the Liberty and LEF files of the platform, the netlist and the
    constraints. It makes the floorplan, places the tap cells and builds the power grid. It writes
    the database `place.odb` and the constraints `place.sdc`. The constraints are the SDC files of
    the design, then the clocks that the `clocks` setting gives, then `sdc_files`.

    The routing layers, the capacity of each layer and the cells that the resizer must not touch
    stay in the database, so the flows after this one keep them. These settings belong to this
    flow."""

    results_description = {}
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
            json_schema_extra=tier("advanced"),
        )
        output_delay_frac: float = Field(
            0.20,
            ge=0,
            lt=1,
            description="The output delay of the generated constraints, as a fraction of the "
            "clock period, from 0 up to but not including 1. Used only when `output_delay` is "
            "unset.",
            json_schema_extra=tier("advanced"),
        )
        input_delay: Optional[float] = Field(
            None,
            ge=0,
            description="The input delay of the generated constraints, in picoseconds. The flow "
            "converts it to the time unit of the platform. It takes precedence over "
            "`input_delay_frac`.",
            json_schema_extra=tier("advanced"),
        )
        output_delay: Optional[float] = Field(
            None,
            ge=0,
            description="The output delay of the generated constraints, in picoseconds. The flow "
            "converts it to the time unit of the platform. It takes precedence over "
            "`output_delay_frac`.",
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
        """Refuse what the groups cannot judge alone: the swap of arithmetic modules needs the
        hierarchy of the design. This reads the settings only."""
        super().check_settings_supported(settings)
        if isinstance(settings, cls.Settings) and settings.arith_swap.enabled:
            if not settings.hierarchical:
                raise FlowSettingsError(
                    [
                        (
                            "arith_swap.enabled",
                            "`arith_swap.enabled` swaps the arithmetic modules of a hierarchical "
                            "netlist, and `hierarchical` is false: set `hierarchical: true` or "
                            "delete `arith_swap.enabled`",
                            None,
                            "value_error",
                        )
                    ],
                    cls.Settings,
                )

    # --- the files ----------------------------------------------------------------------------

    def floorplan_def(self) -> Optional[Path]:
        """Return the DEF file of `floorplan.def_file` as a path, or `None`. A relative path is
        relative to the design root."""
        settings = self.settings
        assert isinstance(settings, self.Settings)
        given = settings.floorplan.def_file
        return None if given is None else self.normalize_path_to_design_root(given)

    def prepare_inputs(self) -> None:
        """Find the files of the platform, and check the DEF file of the floorplan."""
        super().prepare_inputs()
        path = self.floorplan_def()
        if path is not None and not path.is_file():
            raise FlowSettingsException(
                f"`floorplan.def_file` names {path}, which is not a file: give the DEF file, or "
                "set `floorplan.sizing: utilization` or `floorplan.sizing: area`"
            )

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
        database."""
        odb, sdc = self.run_path / "place.odb", self.run_path / "place.sdc"
        self.outputs.odb, self.outputs.sdc = odb, sdc  # type: ignore[attr-defined]
        self.bind_template_values()
        self.copy_from_template(CLOCKS_SDC, trim_blocks=True, lstrip_blocks=True)
        self.run_openroad("openroad_place", "metrics.json", [odb, sdc, self.failed_odb])

    @abstractmethod
    def _placement_stages_missing(self) -> None:
        """Keep the flow incomplete. The driver ends after the power grid, so the flow does not
        place the cells yet, and Xeda does not register an incomplete flow. The change that adds
        the placement stages removes this method."""
