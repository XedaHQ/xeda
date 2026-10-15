"""The settings groups of the last flow: the detailed route, the antenna repair, the fill, the
extraction and the layout files.

A leaf whose description says "Unset: the value of the platform" is `None` in the model, and the
flow resolves it from the platform.
"""

from pathlib import Path
from typing import Annotated, ClassVar, Literal, Optional, Tuple, Union

from ...dataclass import Field, XedaBaseModel, tier
from ...flow.settings_group import Flag, SettingsGroup
from .groups import differing_leaves

__all__ = [
    "DetailedRouteGroup",
    "ExtractionGroup",
    "ExtractionScale",
    "FillGroup",
    "FinalAntennaGroup",
    "GdsGroup",
    "IrDropGroup",
    "NdrGroup",
    "NdrRule",
    "ReportsGroup",
    "TimingReport",
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
        Flag("verbose", "-verbose", "value"),
        Flag("end_iteration", "-droute_end_iter", "value"),
        Flag("reroute_seed", "-or_seed", "value"),
        Flag("reroute_swap_factor", "-or_k", "value"),
        Flag("drc_report_step", "-drc_report_iter_step", "value"),
        Flag("via_access_layer", "-via_access_layer", "value"),
        Flag("min_access_points", "-min_access_points", "value"),
        Flag("pin_access", "-no_pin_access", "skip"),
        Flag("via_generation", "-disable_via_gen", "skip"),
        Flag("repair_pdn_vias_layer", "-repair_pdn_vias", "value"),
        Flag("clean_patches", "-clean_patches", "switch"),
        Flag("save_guide_updates", "-save_guide_updates", "switch"),
    )
    #: The flags of `detailed_route` that no setting covers. The layers of the route are the ones
    #: of the database, the files of the reports are the ones of the flow, and the cloud workers
    #: need a service that Xeda does not start.
    EXTRA_FLAGS: ClassVar[frozenset[str]] = frozenset({"-db_process_node"})

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
    reroute_swap_factor: Optional[float] = Field(
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
        "(`-via_in_pin_bottom_layer`, `-via_in_pin_top_layer`). Unset: the flow gives the "
        "router no range. No platform gives one.",
        json_schema_extra=tier("advanced"),
    )
    via_access_layer: Optional[str] = Field(
        None,
        min_length=1,
        description="A pin on this layer or below is reached by vias only "
        "(`-via_access_layer`). Unset: the router decides.",
        json_schema_extra=tier("expert"),
    )
    min_access_points: Optional[int] = Field(
        None,
        ge=1,
        description="The least number of access points that the router finds for each pin of a "
        "standard cell or a macro (`-min_access_points`). Unset: the default of the router.",
        json_schema_extra=tier("expert"),
    )
    pin_access: bool = Field(
        True,
        description="The router analyzes the access to the pins itself. False skips that "
        "analysis (`-no_pin_access`): the router then works with the access points that an "
        "earlier `pin_access` left in the database, which `openroad_grt` runs with "
        "`pin_access.enabled`.",
        json_schema_extra=tier("expert"),
    )
    via_generation: bool = Field(
        True,
        description="The router makes vias for the pins and the wide wires besides the vias of "
        "the technology. False allows only the vias of the technology (`-disable_via_gen`).",
        json_schema_extra=tier("advanced"),
    )
    repair_pdn_vias_layer: Optional[str] = Field(
        None,
        min_length=1,
        description="The layer of the vias of the power grid that the router removes where they "
        "cause design rule violations (`-repair_pdn_vias`). A platform whose first two metal "
        "layers carry the power rails in parallel needs it. Unset: the router removes none.",
        json_schema_extra=tier("advanced"),
    )
    clean_patches: bool = Field(
        False,
        description="Remove the patch wires that the router does not need (`-clean_patches`).",
        json_schema_extra=tier("expert"),
    )
    save_guide_updates: bool = Field(
        False,
        description="Save the changes that the router makes to the route guides "
        "(`-save_guide_updates`).",
        json_schema_extra=tier("expert"),
    )
    verbose: int = Field(
        1,
        ge=0,
        description="The level of the messages of the router (`-verbose`). The default is the "
        "one of OpenROAD-flow-scripts.",
        json_schema_extra=tier("expert"),
    )
    default_vias: list[Annotated[str, Field(min_length=1)]] = Field(
        [],
        description="The vias that the router prefers, by name, one command each "
        "(`detailed_route_set_default_via`). The command has no entry in the help of OpenROAD. "
        "Empty: the router chooses.",
        json_schema_extra=tier("expert"),
    )
    unidirectional_layers: list[Annotated[str, Field(min_length=1)]] = Field(
        [],
        description="The layers where the router allows wires in the preferred direction only, "
        "one command each (`detailed_route_set_unidirectional_layer`). The command has no entry "
        "in the help of OpenROAD. Empty: the layers keep the direction rule of the technology.",
        json_schema_extra=tier("expert"),
    )
    require_routed: bool = Field(
        True,
        description="The flow fails when a net is unrouted after the route and the antenna "
        "repair (`design_is_routed`). OpenROAD-flow-scripts fails then. False keeps the "
        "partial route and goes on, so the later checks and the outputs describe it.",
        json_schema_extra=tier("advanced"),
    )
    snapshots: bool = Field(
        False,
        description="Save the database after each iteration that leaves violations "
        "(`set_debug_level DRT snapshot 1`) as `drt_iter<n>.odb` in the run directory, for "
        "each iteration `n` up to `end_iteration`. A database is large, so use it for a small "
        "design.",
        json_schema_extra=tier("expert"),
    )
    extra_args: list[str] = Field(
        [],
        description="More flags for `detailed_route`. Each flag must be `-db_process_node`, "
        "followed by its value. The router uses it to select the via rules of one process "
        "node, which the platform of the node gives.",
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


#: A width or spacing of a non-default rule: micrometers, or a factor of the value of the
#: technology, written `*2`.
NdrValue = Union[
    Annotated[float, Field(gt=0)],
    Annotated[str, Field(pattern=r"^\*[0-9]+(\.[0-9]+)?$")],
]


class NdrRule(XedaBaseModel):
    """A non-default routing rule: the widths and the spacings of the layers, and the vias."""

    width: dict[str, NdrValue] = Field(
        {},
        description="The width of the wires, by layer. A key is a layer name or a range "
        "`first:last` of routing layers. A value is a number in micrometers or a factor of the "
        "default width, written `*2`. A layer that no key names keeps its default width.",
    )
    spacing: dict[str, NdrValue] = Field(
        {},
        description="The spacing of the wires, by layer. A key is a layer name or a range "
        "`first:last` of routing layers. A value is a number in micrometers or a factor of the "
        "default spacing, written `*2`.",
    )
    vias: list[Annotated[str, Field(min_length=1)]] = Field(
        [],
        description="The vias that the nets of the rule may use, by name.",
    )


class NdrGroup(SettingsGroup):
    """The non-default routing rules for the detailed route, and the nets that follow them."""

    rules: dict[Annotated[str, Field(min_length=1)], NdrRule] = Field(
        {},
        description="The rules to create, by name (`create_ndr`). The database keeps them.",
        json_schema_extra=tier("advanced"),
    )
    assignments: dict[Annotated[str, Field(min_length=1)], Annotated[str, Field(min_length=1)]] = (
        Field(
            {},
            description="The rule of each net, by net name (`assign_ndr -net`). A rule is one "
            "of `rules` or a rule that the database already holds.",
            json_schema_extra=tier("advanced"),
        )
    )
    clock_rule: Optional[str] = Field(
        None,
        min_length=1,
        description="The rule for all clock nets (`assign_ndr -all_clocks`). A rule is one of "
        "`rules` or a rule that the database already holds. Unset: no rule for the clock nets.",
        json_schema_extra=tier("advanced"),
    )

    def problems(self, key: str) -> list[tuple[str, str]]:
        """Refuse a rule that changes nothing."""
        return [
            (
                f"{key}.rules.{name}",
                f"the rule `{name}` in `{key}.rules` sets no width, no spacing and no via: give "
                "one of them or delete the rule",
            )
            for name, rule in self.rules.items()
            if not (rule.width or rule.spacing or rule.vias)
        ]


class FinalAntennaGroup(SettingsGroup):
    """The repair of the antenna violations of the detailed route, with diodes or jumpers. The
    repair, the reroute and the check repeat until no violation is left or the iterations end."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (Flag("ratio_margin_percent", "-ratio_margin", "value"),)

    repair: bool = Field(
        True,
        description="Repair the antenna violations of the detailed route. OpenROAD-flow-scripts "
        "does it. The flow skips the repair, with a note in the log, when the platform says that "
        "the router cannot use its diode. The check of the antenna violations runs in every case.",
        json_schema_extra=tier("advanced"),
    )
    reroute_iterations: int = Field(
        5,
        ge=1,
        description="The most rounds of repair and reroute. The default is the one of "
        "OpenROAD-flow-scripts.",
        json_schema_extra=tier("advanced"),
    )
    method: Literal["both", "diode", "jumper"] = Field(
        "both",
        description="How `repair_antennas` fixes a violation. `diode` inserts diodes only "
        "(`-diode_only`), `jumper` moves wire to a higher layer only (`-jumper_only`), and "
        "`both` tries jumpers and then diodes. The default is the one of OpenROAD-flow-scripts, "
        "which names neither flag. The command reads the diffusion area of the diode to judge "
        "a violation, with every method, so a platform without a usable diode skips it.",
        json_schema_extra=tier("advanced"),
    )
    ratio_margin_percent: Optional[float] = Field(
        None,
        ge=0,
        lt=100,
        description="Repair a violation when the antenna ratio comes within this share of its "
        "limit, in percent (`-ratio_margin`). The margin costs area. Unset: the default of the "
        "tool, 0.",
        json_schema_extra=tier("advanced"),
    )
    diode_cell: Optional[str] = Field(
        None,
        min_length=1,
        description="The diode cell that `repair_antennas` inserts. Unset: the antenna cell of "
        "the platform. A cell named here is used even where the platform says that the router "
        "cannot use its diode.",
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

    FLAGS: ClassVar[Tuple[Flag, ...]] = (Flag("prefix", "-prefix", "value"),)

    cells: Optional[list[Annotated[str, Field(min_length=1)]]] = Field(
        None,
        description="The filler cells, as glob patterns of cell names (`filler_placement`). The "
        "flow places them after the last change of the route. An empty list places no filler. "
        "Unset: the value of the platform.",
        json_schema_extra=tier("common"),
    )
    decap_cells: list[Annotated[str, Field(min_length=1)]] = Field(
        [],
        description="The decoupling capacitor cells, as glob patterns of cell names. They fill "
        "the same gaps, and the flow gives them to the same `filler_placement` command, before "
        "`cells`. No platform gives any. Empty: no decap cell.",
        json_schema_extra=tier("advanced"),
    )
    prefix: Optional[str] = Field(
        None,
        min_length=1,
        description="The prefix of the names of the filler instances (`-prefix`). Unset: the "
        "default of the tool, `FILLER_`.",
        json_schema_extra=tier("expert"),
    )
    check: bool = Field(
        True,
        description="Check the placement after the filler cells are in (`check_placement`). The "
        "check stops the flow on an illegal placement. OpenROAD-flow-scripts does it.",
        json_schema_extra=tier("advanced"),
    )


class ExtractionScale(SettingsGroup):
    """The factors that scale the extracted parasitics (`adjust_rc`). A factor that is unset is 1."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("res", "-res_factor", "value"),
        Flag("cc", "-cc_factor", "value"),
        Flag("gnd", "-gndc_factor", "value"),
    )

    res: Optional[float] = Field(
        None,
        gt=0,
        description="The factor for the resistance of the wires (`-res_factor`). Unset: 1.",
        json_schema_extra=tier("expert"),
    )
    cc: Optional[float] = Field(
        None,
        gt=0,
        description="The factor for the coupling capacitance between wires (`-cc_factor`). "
        "Unset: 1.",
        json_schema_extra=tier("expert"),
    )
    gnd: Optional[float] = Field(
        None,
        gt=0,
        description="The factor for the capacitance of the wires to the ground "
        "(`-gndc_factor`). Unset: 1.",
        json_schema_extra=tier("expert"),
    )


class ExtractionGroup(SettingsGroup):
    """The extraction of the parasitics of the routed design. Every setting but `method` is an
    option of OpenRCX, so it has no effect when the parasitics are an estimate."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("corner_index", "-corner", "value"),
        Flag("corner_count", "-corner_cnt", "value"),
        Flag("max_res", "-max_res", "value"),
        Flag("cc_model", "-cc_model", "value"),
        Flag("context_depth", "-context_depth", "value"),
        Flag("merge_via_res", "-no_merge_via_res", "skip"),
        Flag("skip_over_cell", "-skip_over_cell", "switch"),
        Flag("model_version", "-version", "value"),
    )

    method: Literal["auto", "rcx", "estimate"] = Field(
        "auto",
        description="`rcx` extracts the parasitics with OpenRCX, with the rules of the platform. "
        "`estimate` estimates them from the detailed route (`estimate_parasitics`). `auto` uses "
        "`rcx` when the platform has the rules, and `estimate` otherwise.",
        json_schema_extra=tier("common"),
    )
    rules_file: Optional[Path] = Field(
        None,
        description="The rules file of OpenRCX (`set_extraction_rules_file`). A relative path is "
        "relative to the design root. Unset: the rules file of the platform.",
        json_schema_extra=tier("advanced"),
    )
    model_index: int = Field(
        0,
        ge=0,
        description="The index of the process corner in the rules file "
        "(`define_process_corner -ext_model_index`). The default is the one of "
        "OpenROAD-flow-scripts.",
        json_schema_extra=tier("expert"),
    )
    corner_index: Optional[int] = Field(
        None,
        ge=0,
        description="The corner to extract (`-corner`). Unset: the default of the tool, all.",
        json_schema_extra=tier("expert"),
    )
    corner_count: Optional[int] = Field(
        None,
        ge=1,
        description="The number of corners to extract (`-corner_cnt`). Unset: the default of "
        "the tool, 1.",
        json_schema_extra=tier("expert"),
    )
    max_res: Optional[float] = Field(
        None,
        ge=0,
        description="The resistors in series merge up to this resistance, in ohms "
        "(`-max_res`). Unset: the default of the tool, 50.",
        json_schema_extra=tier("expert"),
    )
    coupling_threshold: Optional[float] = Field(
        None,
        ge=0,
        description="A coupling capacitance below this value counts as a capacitance to the "
        "ground, in picofarads (`-coupling_threshold`, which takes femtofarads). Unset: the "
        "default of the tool, 0.0001 pF.",
        json_schema_extra=tier("advanced"),
    )
    cc_model: Optional[int] = Field(
        None,
        ge=0,
        description="The number of tracks on each side of a wire that count for its lateral "
        "coupling (`-cc_model`). Unset: the default of the tool, 10.",
        json_schema_extra=tier("expert"),
    )
    context_depth: Optional[int] = Field(
        None,
        ge=0,
        description="The number of layers above and below a wire that count for its capacitance "
        "(`-context_depth`). Unset: the default of the tool, 5.",
        json_schema_extra=tier("expert"),
    )
    lef_values: Literal["none", "res", "rc"] = Field(
        "none",
        description="Take the resistance (`res`, `-lef_res`) or the resistance and the "
        "capacitance (`rc`, `-lef_rc`) of the wires from the LEF files and not from the rules "
        "file. `none` takes both from the rules file. OpenROAD v2.0-26132-g6b305a712 crashes "
        "with `rc` (signal 11 in `dbVia::getBottomLayer`, called from `extMain::getShapeRC`) "
        "on a routed nangate45 design; no upstream issue is known.",
        json_schema_extra=tier("expert"),
    )
    merge_via_res: bool = Field(
        True,
        description="Merge the resistance of a via into the wire next to it. False keeps the "
        "resistance of the vias apart (`-no_merge_via_res`).",
        json_schema_extra=tier("expert"),
    )
    skip_over_cell: bool = Field(
        False,
        description="Ignore the shapes inside the cells when OpenRCX looks for the neighbors of "
        "a wire (`-skip_over_cell`).",
        json_schema_extra=tier("expert"),
    )
    model_version: Optional[float] = Field(
        None,
        gt=0,
        description="The version of the extraction model (`-version`): 1.0 or 2.0. Unset: the "
        "default of the tool, 1.0.",
        json_schema_extra=tier("expert"),
    )
    scale: ExtractionScale = Field(
        default_factory=ExtractionScale,
        description="The factors that scale the extracted parasitics.",
    )


class TimingReport(SettingsGroup):
    """The report of the timing paths (`report_checks`), which the flow writes to
    `reports/timing.rpt`."""

    FLAGS: ClassVar[Tuple[Flag, ...]] = (
        Flag("paths", "-group_path_count", "value"),
        Flag("format", "-format", "value"),
        Flag("fields", "-fields", "list"),
        Flag("unconstrained", "-unconstrained", "switch"),
    )

    enabled: bool = Field(
        True,
        description="Write the report of the timing paths. OpenROAD-flow-scripts does it.",
        json_schema_extra=tier("advanced"),
    )
    paths: int = Field(
        1,
        ge=1,
        description="The number of paths to report in each path group (`-group_path_count`). "
        "The default is the one of the tool.",
        json_schema_extra=tier("advanced"),
    )
    format: Literal[
        "full",
        "full_clock",
        "full_clock_expanded",
        "short",
        "end",
        "slack_only",
        "summary",
        "json",
    ] = Field(
        "full_clock_expanded",
        description="How the report shows a path (`-format`). The default is the one of "
        "OpenROAD-flow-scripts. With `json` the report ignores `fields`.",
        json_schema_extra=tier("advanced"),
    )
    fields: list[
        Literal["capacitance", "slew", "fanout", "input_pins", "net", "src_attr", "variation"]
    ] = Field(
        ["slew", "capacitance", "input_pins", "net", "fanout"],
        description="The columns that the report adds to a path (`-fields`). The default is the "
        "one of OpenROAD-flow-scripts.",
        json_schema_extra=tier("expert"),
    )
    unconstrained: bool = Field(
        True,
        description="Report the paths without a constraint too (`-unconstrained`). "
        "OpenROAD-flow-scripts does it.",
        json_schema_extra=tier("advanced"),
    )


class ReportsGroup(SettingsGroup):
    """The text reports of the final process, which the flow writes under `reports/`. They never
    fail a run. A change of a report setting runs the flow again."""

    timing: TimingReport = Field(
        default_factory=TimingReport,
        description="The report of the timing paths, `reports/timing.rpt`.",
    )
    clock_skew: bool = Field(
        True,
        description="Report the clock skew (`report_clock_skew`) in `reports/clock_skew.rpt` and "
        "put the skew of the setup and hold checks in the results. OpenROAD-flow-scripts can "
        "switch it off since it can take long on a large design.",
        json_schema_extra=tier("advanced"),
    )
    power: bool = Field(
        True,
        description="Write the power report (`report_power`) to `reports/power.rpt`. The total "
        "power in the results does not depend on it.",
        json_schema_extra=tier("common"),
    )
    check_setup: bool = Field(
        False,
        description="Write the sanity checks of the constraints (`check_setup -verbose`) to "
        "`reports/check_setup.rpt`.",
        json_schema_extra=tier("advanced"),
    )


class IrDropGroup(SettingsGroup):
    """The analysis of the IR drop of the power grid."""

    enabled: bool = Field(
        True,
        description="Analyze the IR drop of the power grid, for each supply net that the "
        "platform gives a voltage. The analysis only reports, and it never fails the run. On a "
        "platform that gives no voltage for any supply net, the flow skips the analysis, and the "
        "log says so.",
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
