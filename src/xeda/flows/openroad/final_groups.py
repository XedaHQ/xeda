"""The settings groups of the last flow: the detailed route, the antenna repair, the fill, the
extraction and the layout files.

A leaf whose description says "Unset: the value of the platform" is `None` in the model, and the
flow resolves it from the platform.
"""

from pathlib import Path
from typing import Annotated, ClassVar, Literal, Optional, Tuple

from ...dataclass import Field, XedaBaseModel, tier
from ...flow.settings_group import Flag, SettingsGroup
from .groups import differing_leaves

__all__ = [
    "DetailedRouteGroup",
    "ExtractionGroup",
    "FillGroup",
    "FinalAntennaGroup",
    "GdsGroup",
    "IrDropGroup",
    "ViaInPin",
]


class ViaInPin(XedaBaseModel):
    """The range of layers where a via may sit inside a pin. `pin_access` and `detailed_route`
    must get the same range."""

    bottom: str = Field(
        min_length=1,
        description="The lowest layer where a via may sit inside a pin "
        "(`-via_in_pin_bottom_layer`).",
    )
    top: str = Field(
        min_length=1,
        description="The highest layer where a via may sit inside a pin "
        "(`-via_in_pin_top_layer`).",
    )


class DetailedRouteGroup(SettingsGroup):
    """The detailed router (`detailed_route`)."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("end_iteration", "-droute_end_iter", "value"),
        Flag("reroute_seed", "-or_seed", "value"),
        Flag("reroute_swap_factor", "-or_k", "value"),
        Flag("drc_report_step", "-drc_report_iter_step", "value"),
    )
    #: The flags of `detailed_route` that no setting covers. The layers of the route are the ones
    #: of the database, and the files of the reports are the ones of the flow.
    EXTRA_FLAGS: ClassVar[frozenset[str]] = frozenset(
        {"-via_access_layer", "-min_access_points", "-clean_patches", "-save_guide_updates"}
    )

    end_iteration: int = Field(
        64,
        ge=1,
        le=64,
        description="The most optimization iterations (`-droute_end_iter`). The tool clamps "
        "larger values to 64. The default is the one of OpenROAD-flow-scripts.",
        json_schema_extra=tier("common"),
    )
    reroute_seed: Optional[int] = Field(
        None,
        ge=0,
        description="The seed that shuffles the nets to reroute (`-or_seed`). A developer "
        "option of the router. Unset: no shuffling.",
        json_schema_extra=tier("expert"),
    )
    reroute_swap_factor: Optional[int] = Field(
        None,
        ge=0,
        description="The factor that sets how many swaps the shuffle makes (`-or_k`): the "
        "number of swaps is the factor times the number of nets to reroute. Needs "
        "`reroute_seed`.",
        json_schema_extra=tier("expert"),
    )
    drc_report_step: int = Field(
        5,
        ge=0,
        description="Write a DRC report every this many iterations (`-drc_report_iter_step`). 0 "
        "writes no report during the route. The default is the one of OpenROAD-flow-scripts.",
        json_schema_extra=tier("advanced"),
    )
    via_in_pin: Optional[ViaInPin] = Field(
        None,
        description="The range of layers where a via may sit inside a pin "
        "(`-via_in_pin_bottom_layer`, `-via_in_pin_top_layer`). Unset: the value of the "
        "platform.",
        json_schema_extra=tier("advanced"),
    )
    extra_args: list[str] = Field(
        [],
        description="More flags for `detailed_route`. Each flag must be one of "
        "`-via_access_layer`, `-min_access_points`, `-clean_patches` and "
        "`-save_guide_updates`, each followed by its value where it takes one.",
        json_schema_extra=tier("expert"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a swap factor without a seed."""
        if self.reroute_swap_factor is not None and self.reroute_seed is None:
            return [
                (
                    f"{key}.reroute_swap_factor",
                    f"`{key}.reroute_swap_factor` has no effect without `{key}.reroute_seed`: "
                    f"give the seed or delete `{key}.reroute_swap_factor`",
                )
            ]
        return []


class FinalAntennaGroup(SettingsGroup):
    """The repair of the antenna violations of the detailed route, with diodes. The repair, the
    reroute and the check repeat until no violation is left or the iterations end."""

    repair: bool = Field(
        True,
        description="Repair the antenna violations of the detailed route. OpenROAD-flow-scripts "
        "does it.",
        json_schema_extra=tier("advanced"),
    )
    reroute_iterations: int = Field(
        5,
        ge=1,
        description="The most rounds of repair and reroute. The default is the one of "
        "OpenROAD-flow-scripts.",
        json_schema_extra=tier("advanced"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse an option of the repair when the repair does not run."""
        if self.repair:
            return []
        return [
            (
                leaf,
                f"`{leaf}` has no effect with `{key}.repair: false`: set `{key}.repair: true` "
                f"or delete `{leaf}`",
            )
            for leaf in differing_leaves(self, key)
            if leaf != f"{key}.repair"
        ]


class FillGroup(SettingsGroup):
    """The filler cells that fill the gaps in the rows."""

    cells: Optional[list[Annotated[str, Field(min_length=1)]]] = Field(
        None,
        description="The filler cells, as glob patterns of cell names (`filler_placement`). The "
        "flow places them after the last change of the route. An empty list places no filler. "
        "Unset: the value of the platform.",
        json_schema_extra=tier("common"),
    )


class ExtractionGroup(SettingsGroup):
    """The extraction of the parasitics of the routed design."""

    method: Literal["auto", "rcx", "estimate"] = Field(
        "auto",
        description="`rcx` extracts the parasitics with OpenRCX, with the rules of the platform. "
        "`estimate` estimates them from the detailed route (`estimate_parasitics`). `auto` uses "
        "`rcx` when the platform has the rules, and `estimate` otherwise.",
        json_schema_extra=tier("common"),
    )


class IrDropGroup(SettingsGroup):
    """The analysis of the IR drop of the power grid."""

    enabled: bool = Field(
        True,
        description="Analyze the IR drop of each supply net that the platform gives a voltage. "
        "The analysis only reports, and it never fails the run. A platform without the "
        "voltages of its nets gets no analysis.",
        json_schema_extra=tier("advanced"),
    )


class GdsGroup(SettingsGroup):
    """The GDSII file of the routed design, which KLayout writes when `write_gds` is true."""

    seal_ring: Optional[Path] = Field(
        None,
        description="A GDSII file with a seal ring, which KLayout merges into the layout. A "
        "relative path is relative to the design root. Needs `write_gds`.",
        json_schema_extra=tier("advanced"),
    )
