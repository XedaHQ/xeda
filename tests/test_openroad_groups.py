"""The settings groups of the OpenROAD flows.

The flows hold their options in groups, one per command or part of a command. These tests check
the shared groups (`RepairTiming`, `RepairDesign`, `Legalize`), the groups of each flow, the
words that a group prints, and the rules that a group or a flow refuses at planning. The flows
have no `run()` yet, so no test here runs one: planning and rendering need only the settings.
"""

from collections.abc import Sequence
from typing import Any, get_args

import pytest

from xeda.flow import FlowSettingsError
from xeda.flow.settings_group import SettingsGroup, render_flags
from xeda.flow_runner.settings_layers import compose_flow_settings
from xeda.flows.openroad import OpenroadCts, OpenroadGrt, OpenroadPlace
from xeda.flows.openroad import groups
from xeda.utils import tcl_word

from .openroad_utils import OPENROAD_FLOWS
from .test_documentation import undocumented_group_settings


def settings_of(flow, *items: Any):
    """Return the settings of `flow` as `xeda run` builds them from `-s` items.

    An item that is a table gives a value that text cannot give (a rectangle, a table of
    layers). It is a section of the design file: its keys may be dotted."""
    texts = [item for item in items if isinstance(item, str)]
    given: dict[str, Any] = {}
    for item in items:
        if not isinstance(item, str):
            given.update(item)
    return flow.Settings.from_input(compose_flow_settings(flow, [{flow.name: given}], texts))


def problems(flow, *items: Any) -> list[tuple[str, str]]:
    """Return the `(key, message)` pairs that planning refuses `items` with, or `[]`."""
    try:
        flow.check_settings_supported(settings_of(flow, *items))
    except FlowSettingsError as error:
        return [(key, message) for key, message, _, _ in error.errors]
    return []


def refused(flow, *items: Any) -> str:
    """Return every message that planning refuses `items` with, as one text."""
    found = problems(flow, *items)
    assert found, f"{flow.name} accepted {items}"
    return " ".join(" ".join(message for _, message in found).split())


def render(group: SettingsGroup) -> str:
    """Return what `render_flags` prints for `group`, with the OpenROAD list words."""
    return render_flags(group, tcl_word, groups.comma_word)


# ------------------------------------------------------------------------------------ shared groups


def test_every_group_of_the_flows_builds_with_no_argument_and_says_what_it_does():
    for flow in OPENROAD_FLOWS:
        assert undocumented_group_settings(flow) == []


def test_enabled_stays_the_first_field_of_the_groups_with_a_call_site_default():
    classes = [groups.RepairTiming, groups.RepairDesign]
    for flow in (OpenroadPlace, OpenroadCts, OpenroadGrt):
        for name, info in flow.Settings.model_fields.items():
            if isinstance(info.annotation, type) and issubclass(info.annotation, SettingsGroup):
                classes.append(info.annotation)
    for cls in classes:
        if "enabled" in cls.model_fields:
            assert next(iter(cls.model_fields)) == "enabled", cls.__qualname__


def test_repair_timing_with_its_defaults_prints_the_defaults_of_the_tool():
    assert render(groups.RepairTiming()) == '-repair_tns "100.0"'
    assert render(groups.RepairTiming().moves) == ""


@pytest.mark.parametrize("move", get_args(groups.SetupMove))
def test_every_move_of_the_sequence_prints_as_one_word(move):
    assert f'-sequence "{move}"' in render(groups.RepairTiming(sequence=[move]))


def test_the_sequence_prints_comma_separated_in_the_given_order():
    timing = groups.RepairTiming(sequence=["unbuffer", "sizeup", "swap", "vt_swap"])
    assert '-sequence "unbuffer,sizeup,swap,vt_swap"' in render(timing)


@pytest.mark.parametrize("phase", get_args(groups.RepairPhase))
def test_every_phase_prints_as_one_word(phase):
    assert f'-phases "{phase}"' in render(groups.RepairTiming(phases=[phase]))


def test_the_phases_print_comma_separated_in_the_given_order():
    assert '-phases "WNS,TNS"' in render(groups.RepairTiming(phases=["WNS", "TNS"]))


@pytest.mark.parametrize("fix", get_args(groups.RepairFix))
def test_every_fix_stands_for_the_checks_it_repairs(fix):
    expected = {"setup_and_hold": "", "setup": "-setup", "hold": "-hold"}[fix]
    assert groups.FIX_FLAGS[fix] == expected


@pytest.mark.parametrize(
    "move, flag",
    [
        ("pin_swap", "-skip_pin_swap"),
        ("gate_cloning", "-skip_gate_cloning"),
        ("buffer_removal", "-skip_buffer_removal"),
        ("vt_swap", "-skip_vt_swap"),
        ("crit_vt_swap", "-skip_crit_vt_swap"),
    ],
)
def test_a_move_that_is_off_prints_its_skip_flag(move, flag):
    moves = groups.RepairMoves(**{move: False})
    assert render(moves) == flag
    assert render(groups.RepairMoves()) == ""


def test_the_last_gasp_that_is_off_prints_its_skip_flag():
    assert "-skip_last_gasp" in render(groups.RepairTiming(last_gasp=False))
    assert "-skip_last_gasp" not in render(groups.RepairTiming())


def test_the_values_of_repair_timing_print_with_their_flags():
    timing = groups.RepairTiming(
        setup_margin=0.25, hold_margin=0.5, tns_end_percent=40, max_buffer_percent=30
    )
    words = render(timing)
    assert '-repair_tns "40.0"' in words and '-max_buffer_percent "30.0"' in words


def test_the_margins_are_in_nanoseconds_and_the_template_renders_them():
    """A margin is in nanoseconds on every platform, so a design file means the same on a ps
    platform and a ns platform. `render_flags` prints values as given, so the margins have no
    flag: the template converts them to the time unit of the Liberty files."""
    printed = {flag.field for flag in groups.RepairTiming.FLAGS}
    assert not printed & {"setup_margin", "hold_margin"}
    timing = groups.RepairTiming(setup_margin=0.25, hold_margin=0.5)
    assert "_margin" not in render(timing)
    for name in ("setup_margin", "hold_margin"):
        description = groups.RepairTiming.model_fields[name].description
        assert ", in nanoseconds." in description or " in nanoseconds." in description


def test_no_time_value_of_a_group_is_printed_by_render_flags():
    """A time leaf is in nanoseconds. Only the template can convert it, so no group prints one."""
    from xeda.flow.settings_group import SettingsGroup

    seen = set()

    def walk(group_class):
        if group_class in seen:
            return
        seen.add(group_class)
        printed = {flag.field for flag in group_class.FLAGS}
        for name, info in group_class.model_fields.items():
            text = (info.description or "").lower()
            if "nanosecond" in text:
                assert name not in printed, f"{group_class.__qualname__}.{name}"
            inner = info.annotation
            if isinstance(inner, type) and issubclass(inner, SettingsGroup):
                walk(inner)

    for flow in OPENROAD_FLOWS:
        for info in flow.Settings.model_fields.values():
            if isinstance(info.annotation, type) and issubclass(info.annotation, SettingsGroup):
                walk(info.annotation)


def test_a_move_or_phase_that_the_tool_does_not_know_is_refused_before_any_process():
    with pytest.raises(Exception, match="sequence"):
        groups.RepairTiming(sequence=["swap_pins"])
    with pytest.raises(Exception, match="phases"):
        groups.RepairTiming(phases=["wns"])
    with pytest.raises(Exception, match="pin_swap|extra"):
        groups.RepairMoves(clone=False)


@pytest.mark.parametrize(
    "name, value",
    [
        ("setup_margin", -0.1),
        ("hold_margin", -1),
        ("tns_end_percent", -1),
        ("tns_end_percent", 100.5),
        ("max_buffer_percent", 101),
    ],
)
def test_repair_timing_refuses_a_value_out_of_range(name, value):
    with pytest.raises(Exception, match=name):
        groups.RepairTiming(**{name: value})


def test_repair_design_prints_its_margins_and_its_wire_length():
    assert render(groups.RepairDesign()) == '-slew_margin "0.0" -cap_margin "0.0"'
    design = groups.RepairDesign(slew_margin_percent=20, cap_margin_percent=10, max_wire_length=300)
    assert render(design) == ('-max_wire_length "300.0" -slew_margin "20.0" -cap_margin "10.0"')


@pytest.mark.parametrize("name, value", [("slew_margin_percent", 100), ("cap_margin_percent", -1)])
def test_repair_design_refuses_a_margin_out_of_range(name, value):
    with pytest.raises(Exception, match=name):
        groups.RepairDesign(**{name: value})


def test_legalize_refuses_a_negative_padding():
    assert groups.Legalize().cell_padding == 0
    with pytest.raises(Exception, match="cell_padding"):
        groups.Legalize(cell_padding=-1)


def refusal_cases() -> Sequence[tuple[str, tuple[str, ...], str]]:
    """Return `(name, items, text of the message)` for `openroad_cts`, which holds one
    `RepairTiming` as `repair`."""
    return [
        (
            "a move that its switch turns off",
            ("repair.sequence=swap,sizeup", "repair.moves.pin_swap=false"),
            "`repair.sequence` names `swap`, and `repair.moves.pin_swap` is false",
        ),
        (
            "unbuffer and the removal of buffers",
            ("repair.sequence=unbuffer", "repair.moves.buffer_removal=false"),
            "`repair.moves.buffer_removal` is false",
        ),
        (
            "clone and gate cloning",
            ("repair.sequence=clone", "repair.moves.gate_cloning=false"),
            "`repair.moves.gate_cloning` is false",
        ),
        (
            "vt_swap and the VT swap",
            ("repair.sequence=vt_swap", "repair.moves.vt_swap=false"),
            "`repair.moves.vt_swap` is false",
        ),
        (
            "a sequence that no phase reads",
            ("repair.sequence=sizeup", "repair.phases=GLOBAL_SIZING"),
            "`repair.sequence` has no effect with the phases",
        ),
        (
            "a sequence with only phases that choose their own moves",
            ("repair.sequence=sizeup", "repair.phases=LAST_GASP,REROUTE"),
            "`repair.sequence` has no effect with the phases",
        ),
        (
            "the reroute move before there is a route",
            ("repair.sequence=sizeup,reroute",),
            "`reroute` needs the parasitics of a global route",
        ),
        (
            "the reroute phase before there is a route",
            ("repair.phases=REROUTE",),
            "`REROUTE` needs the parasitics of a global route",
        ),
        (
            "a setup setting with fix: hold",
            ("repair.fix=hold", "repair.tns_end_percent=50"),
            "`repair.tns_end_percent` repairs setup, and `repair.fix` is `hold`",
        ),
        (
            "a sequence with fix: hold",
            ("repair.fix=hold", "repair.sequence=sizeup"),
            "`repair.sequence` repairs setup, and `repair.fix` is `hold`",
        ),
        (
            "phases with fix: hold",
            ("repair.fix=hold", "repair.phases=WNS"),
            "`repair.phases` repairs setup, and `repair.fix` is `hold`",
        ),
        (
            "a move with fix: hold",
            ("repair.fix=hold", "repair.moves.pin_swap=false"),
            "`repair.moves.pin_swap` repairs setup, and `repair.fix` is `hold`",
        ),
        (
            "the last gasp with fix: hold",
            ("repair.fix=hold", "repair.last_gasp=false"),
            "`repair.last_gasp` repairs setup, and `repair.fix` is `hold`",
        ),
        (
            "a hold setting with fix: setup",
            ("repair.fix=setup", "repair.hold_margin=0.1"),
            "`repair.hold_margin` repairs hold, and `repair.fix` is `setup`",
        ),
        (
            "the buffer limit of the hold repair with fix: setup",
            ("repair.fix=setup", "repair.max_buffer_percent=30"),
            "`repair.max_buffer_percent` repairs hold, and `repair.fix` is `setup`",
        ),
    ]


@pytest.mark.parametrize(
    "items, text", [case[1:] for case in refusal_cases()], ids=[c[0] for c in refusal_cases()]
)
def test_repair_timing_refuses_a_combination_that_cannot_work(items, text):
    assert text in refused(OpenroadCts, *items)


def test_the_setup_margin_stays_allowed_with_fix_hold():
    assert problems(OpenroadCts, "repair.fix=hold", "repair.setup_margin=0.1") == []


def test_the_reroute_move_and_phase_are_allowed_after_global_routing():
    for item in ("repair.timing.sequence=sizeup,reroute", "repair.timing.phases=REROUTE"):
        assert problems(OpenroadGrt, item) == []


def test_repair_timing_reports_every_problem_at_once():
    found = problems(
        OpenroadCts,
        "repair.fix=hold",
        "repair.tns_end_percent=50",
        "repair.sequence=reroute",
    )
    assert len(found) == 3
    assert {key for key, _ in found} == {
        "repair.tns_end_percent",
        "repair.sequence",
    }


def test_two_groups_with_a_problem_each_give_two_problems_in_one_error():
    found = problems(
        OpenroadCts,
        "repair.phases=REROUTE",
        "buffers.cells=BUF_X1",
    )
    assert {key for key, _ in found} == {"repair.phases", "buffers.cells"}


def test_a_leaf_that_changed_in_a_disabled_repair_is_refused_naming_the_leaf():
    message = refused(OpenroadCts, "repair.enabled=false", "repair.setup_margin=0.1")
    assert "`repair.setup_margin` has no effect while `repair.enabled` is false" in message


# ------------------------------------------------------------------------- the groups of each flow


def test_the_call_sites_of_repair_timing_have_their_own_defaults():
    place = OpenroadPlace.Settings()
    cleanup = place.floorplan.netlist_cleanup
    assert (cleanup.mode, cleanup.repair.enabled, cleanup.repair.fix) == (
        "repair_timing",
        True,
        "setup",
    )
    assert cleanup.repair.sequence == ["unbuffer", "sizeup", "swap", "vt_swap"]
    assert cleanup.repair.last_gasp is False
    assert render(cleanup.repair) == (
        '-sequence "unbuffer,sizeup,swap,vt_swap" -repair_tns "100.0" -skip_last_gasp'
    )
    assert place.resize.repair_timing.enabled is False
    assert place.resize.repair_design.enabled is True
    cts = OpenroadCts.Settings().repair
    assert (cts.enabled, cts.fix, cts.last_gasp) == (True, "setup_and_hold", True)
    grt = OpenroadGrt.Settings().repair
    assert (grt.strategy, grt.design.enabled, grt.timing.enabled) == ("incremental", True, True)
    assert grt.timing.fix == "setup_and_hold"


def test_a_leaf_changed_in_a_repair_that_is_off_is_refused_naming_the_leaf():
    message = refused(OpenroadPlace, "resize.repair_timing.setup_margin=0.1")
    assert (
        "`resize.repair_timing.setup_margin` has no effect while "
        "`resize.repair_timing.enabled` is false" in message
    )
    assert (
        problems(
            OpenroadPlace,
            "resize.repair_timing.enabled=true",
            "resize.repair_timing.setup_margin=0.1",
        )
        == []
    )


def test_the_cleanup_of_the_netlist_has_one_switch_for_one_choice():
    message = refused(OpenroadPlace, "floorplan.netlist_cleanup.repair.enabled=false")
    assert "set `floorplan.netlist_cleanup.mode: none`" in message
    assert problems(OpenroadPlace, "floorplan.netlist_cleanup.mode=none") == []


@pytest.mark.parametrize("mode", ["none", "remove_buffers"])
def test_a_leaf_of_the_cleanup_repair_has_no_effect_without_the_repair_mode(mode):
    found = problems(
        OpenroadPlace,
        f"floorplan.netlist_cleanup.mode={mode}",
        "floorplan.netlist_cleanup.repair.setup_margin=0.1",
        "floorplan.netlist_cleanup.repair.moves.gate_cloning=false",
    )
    assert [key for key, _ in found] == [
        "floorplan.netlist_cleanup.repair.setup_margin",
        "floorplan.netlist_cleanup.repair.moves.gate_cloning",
    ]
    assert f"`floorplan.netlist_cleanup.mode: {mode}`" in found[0][1]


def test_a_disabled_cleanup_repair_reports_each_leaf_once():
    found = problems(
        OpenroadPlace,
        "floorplan.netlist_cleanup.mode=none",
        "floorplan.netlist_cleanup.repair.enabled=false",
        "floorplan.netlist_cleanup.repair.setup_margin=0.1",
    )
    assert sorted(key for key, _ in found) == [
        "floorplan.netlist_cleanup.repair.enabled",
        "floorplan.netlist_cleanup.repair.setup_margin",
    ]


FLOORPLAN_REFUSALS = [
    (
        ({"floorplan.die_area": [0, 0, 100, 100]},),
        "`floorplan.die_area` is a leaf of `floorplan.sizing: area`",
    ),
    (("floorplan.def_file=a.def",), "`floorplan.def_file` is a leaf of `floorplan.sizing: def`"),
    (
        ("floorplan.sizing=area", "floorplan.core_utilization_percent=40"),
        "`floorplan.core_utilization_percent` is a leaf of `floorplan.sizing: utilization`",
    ),
    (
        ("floorplan.sizing=area", {"floorplan.die_area": [0, 0, 100, 100]}),
        "needs `floorplan.die_area` and `floorplan.core_area`, and `floorplan.core_area` is not set",
    ),
    (
        ("floorplan.sizing=area", "floorplan.core_margin=3", {"floorplan.die_area": [0, 0, 9, 9]}),
        "`floorplan.core_margin` is a leaf of `floorplan.sizing: utilization`",
    ),
    (
        ("floorplan.sizing=def",),
        "`floorplan.sizing: def` takes the floorplan from `floorplan.def_file`, and that is not set",
    ),
    (
        ("floorplan.sizing=def", "floorplan.def_file=a.def", "floorplan.aspect_ratio=2"),
        "`floorplan.aspect_ratio` is a leaf of `floorplan.sizing: utilization`",
    ),
    (
        (
            "floorplan.sizing=area",
            {"floorplan.die_area": [10, 0, 0, 100]},
            {"floorplan.core_area": [1, 1, 2, 2]},
        ),
        "`floorplan.die_area` must list the lower-left corner and then the upper-right corner",
    ),
    (
        (
            "floorplan.sizing=area",
            {"floorplan.die_area": [0, 0, 100, 100]},
            {"floorplan.core_area": [1, 1, 101, 99]},
        ),
        "`floorplan.core_area` must lie inside `floorplan.die_area`",
    ),
]


@pytest.mark.parametrize("items, text", FLOORPLAN_REFUSALS)
def test_the_floorplan_refuses_a_sizing_that_contradicts_its_leaves(items, text):
    assert text in refused(OpenroadPlace, *items)


def test_the_floorplan_accepts_each_way_of_sizing():
    assert problems(OpenroadPlace) == []
    assert (
        problems(
            OpenroadPlace,
            "floorplan.sizing=area",
            {"floorplan.die_area": [0, 0, 100, 100]},
            {"floorplan.core_area": [2, 2, 98, 98]},
        )
        == []
    )
    assert problems(OpenroadPlace, "floorplan.sizing=def", "floorplan.def_file=a.def") == []


@pytest.mark.parametrize(
    "item",
    [
        "floorplan.core_utilization_percent=0",
        "floorplan.core_utilization_percent=101",
        "floorplan.aspect_ratio=0",
        "floorplan.core_margin=-1",
        "floorplan.sizing=file",
    ],
)
def test_the_floorplan_refuses_a_value_out_of_range(item):
    with pytest.raises(Exception, match=item.split("=")[0].split(".")[1]):
        settings_of(OpenroadPlace, item)


def test_the_defaults_of_the_floorplan_are_the_stage_defaults():
    floorplan = OpenroadPlace.Settings().floorplan
    assert (floorplan.sizing, floorplan.core_utilization_percent) == ("utilization", 50.0)
    assert (floorplan.aspect_ratio, floorplan.core_margin) == (1.0, 1.0)
    assert (floorplan.die_area, floorplan.core_area, floorplan.def_file) == (None, None, None)


PLACE_REFUSALS = [
    ("tie.separation=2", "tie.repair_fanout=false", "`tie.separation` has no effect with"),
    (
        "tapcell.mode=none",
        "tapcell.distance=10",
        "`tapcell.distance` has no effect with `tapcell.mode: none`",
    ),
    ("tapcell.mode=none", "tapcell.endcap_master=TAP", "`tapcell.endcap_master` has no effect"),
    ("tapcell.mode=cut_rows", "tapcell.master=TAP", "`tapcell.master` has no effect with"),
    ("pdn.mode=none", "pdn.grid.power_net=VDD", "`pdn.grid` has no effect with `pdn.mode: none`"),
    (
        "io.min_distance_unit=tracks",
        "io.corner_avoidance=1",
        "`io.min_distance_unit: tracks` is the unit of `io.min_distance`, and that is not set",
    ),
    (
        "global_placement.density.value=0.5",
        "global_placement.density.mode=uniform",
        "`global_placement.density.value` has no effect with `global_placement.density.mode: uniform`",
    ),
    (
        "global_placement.density.headroom=0.2",
        "global_placement.density.mode=fixed",
        "`global_placement.density.headroom` has no effect with",
    ),
    (
        "global_placement.density.mode=auto",
        "global_placement.cell_padding=1",
        "`global_placement.density.mode: auto` needs `global_placement.density.headroom`",
    ),
    (
        "global_placement.nesterov.min_phi_coef=1.2",
        "global_placement.nesterov.max_phi_coef=1.0",
        "`global_placement.nesterov.min_phi_coef` is 1.2, above",
    ),
]


@pytest.mark.parametrize("first, second, text", PLACE_REFUSALS)
def test_the_groups_of_the_placement_refuse_what_cannot_work(first, second, text):
    assert text in refused(OpenroadPlace, first, second)


def test_an_empty_list_of_pin_layers_is_refused_with_the_way_out():
    message = refused(OpenroadPlace, {"io.horizontal_layers": []}, "io.vertical_layers=")
    assert "`io.horizontal_layers` is empty: name at least one layer, or delete it" in message
    assert "`io.vertical_layers` is empty" in message
    assert problems(OpenroadPlace, "io.horizontal_layers=metal5,metal7") == []


def test_the_groups_of_the_placement_accept_their_combinations():
    for items in (
        ("global_placement.density.mode=auto", "global_placement.density.headroom=0.2"),
        ("global_placement.density.value=0.5",),
        (
            "global_placement.nesterov.min_phi_coef=0.95",
            "global_placement.nesterov.max_phi_coef=1.05",
        ),
        ("io.min_distance=4", "io.min_distance_unit=tracks"),
        ("tapcell.mode=tap_endcap", "tapcell.distance=120", "tapcell.master=TAP"),
        ("pdn.grid.power_net=VDD",),
        ("tapcell.mode=cut_rows", "tapcell.endcap_master=TAP"),
    ):
        assert problems(OpenroadPlace, *items) == [], items


@pytest.mark.parametrize(
    "item",
    [
        "routing.signal_layers=metal2",
        "routing.signal_layers=metal2-metal5-metal7",
        "routing.clock_layers=a b-c",
        {"routing.layer_adjustments": {"metal2": 1.5}},
        {"routing.layer_adjustments": {"metal2": -0.1}},
        "global_placement.density.value=0",
        "global_placement.density.value=1.5",
        "global_placement.density.headroom=1",
        "global_placement.cell_padding=-1",
        {"detailed_placement.improve.max_displacement": [5]},
        {"detailed_placement.improve.max_displacement": [5, -1]},
        "detailed_placement.check=warn",
        {"floorplan.die_area": [0, 0, 1]},
        "tapcell.mode=welltap",
        "input_delay_frac=1",
        "output_delay_frac=-0.1",
        "input_delay=-1",
    ],
)
def test_the_placement_settings_refuse_a_value_out_of_range(item):
    with pytest.raises(Exception):
        settings_of(OpenroadPlace, item)


def test_the_routing_layers_take_a_range_and_a_table_of_adjustments():
    routing = settings_of(
        OpenroadPlace,
        "routing.signal_layers=metal2-metal10",
        {"routing.layer_adjustments": {"metal2-metal3": 0.5, "metal4": 0.25}},
    ).routing
    assert routing.signal_layers == "metal2-metal10"
    assert routing.layer_adjustments == {"metal2-metal3": 0.5, "metal4": 0.25}
    assert (routing.clock_layers, OpenroadPlace.Settings().routing.layer_adjustments) == (
        None,
        None,
    )


def test_the_platform_grid_type_is_the_one_of_the_platform_model():
    from xeda.platforms.model import Pdn

    grid = settings_of(
        OpenroadPlace,
        "pdn.grid.power_net=VDD",
        {"pdn.grid.straps": [{"layer": "m4", "width": 1, "pitch": 9, "offset": 0}]},
    ).pdn.grid
    assert isinstance(grid, Pdn) and grid.power_net == "VDD" and grid.straps[0].layer == "m4"


def test_the_flags_of_the_global_placement_groups():
    place = OpenroadPlace.Settings().global_placement
    assert render(place.timing_driven) == "-timing_driven"
    assert render(place.routability_driven) == "-routability_driven"
    assert render(place.initial_place) == "-force_center_initial_place"
    assert render(place) == ""
    assert render(place.nesterov) == ""
    settings = settings_of(
        OpenroadPlace,
        "global_placement.timing_driven.enabled=false",
        "global_placement.initial_place.from_center=false",
        "global_placement.nesterov.min_phi_coef=0.9",
        "global_placement.nesterov.max_phi_coef=1.1",
        "global_placement.random_seed=7",
    ).global_placement
    assert render(settings.timing_driven) == ""
    assert render(settings.initial_place) == ""
    assert render(settings.nesterov) == '-min_phi_coef "0.9" -max_phi_coef "1.1"'
    assert render(settings) == '-random_seed "7"'


def test_the_flags_of_the_pin_placement_and_the_tie_cells():
    io = settings_of(OpenroadPlace, "io.min_distance=4", "io.corner_avoidance=2.5").io
    assert render(io) == '-corner_avoidance "2.5"'  # the flow prints `min_distance` itself
    assert render(OpenroadPlace.Settings().io) == ""
    assert render(OpenroadPlace.Settings().tie) == '-separation "0.0"'


def test_the_maximum_displacement_of_the_improvement_prints_as_a_tcl_list():
    improve = OpenroadPlace.Settings().detailed_placement.improve
    assert render_flags(improve, tcl_word) == '-max_displacement [list "5" "1"]'


def test_the_defaults_of_the_placement_are_the_stage_defaults():
    place = OpenroadPlace.Settings()
    assert (place.input_delay_frac, place.output_delay_frac) == (0.2, 0.2)
    assert (place.input_delay, place.output_delay, place.sdc_files) == (None, None, [])
    assert (place.design.eliminate_dead_logic, place.design.dont_touch) == (True, [])
    assert (place.tie.repair_fanout, place.tie.separation) == (True, 0.0)
    assert place.pdn.mode == "generated" and place.pdn.grid is None
    assert (place.io.flow, place.io.min_distance_unit) == ("skip_io_pass", "um")
    density = place.global_placement.density
    assert (density.mode, density.value, density.headroom) == ("fixed", None, None)
    assert place.global_placement.cell_padding == 0
    assert place.global_placement.timing_driven.enabled is True
    assert place.global_placement.routability_driven.enabled is True
    assert place.resize.remove_buffers is True and place.resize.buffer_ports.enabled is True
    assert place.arith_swap.enabled is False
    detail = place.detailed_placement
    assert (detail.cell_padding, detail.mirroring, detail.check) == (0, True, "fail")
    assert (detail.improve.enabled, detail.improve.max_displacement) == (True, [5, 1])
    assert place.tapcell.mode is None and place.routing.signal_layers is None


def test_the_defaults_of_the_clock_tree_and_the_global_route_are_the_stage_defaults():
    cts = OpenroadCts.Settings()
    assert cts.repair_clock_inverters is True
    assert (cts.buffers.select, cts.buffers.cells, cts.buffers.root) == ("inferred", [], None)
    assert (cts.sink_clustering.mode, cts.tree.style) == ("auto", "lut")
    assert cts.repair_clock_nets.when == "in_cts" and cts.legalize.cell_padding == 0
    grt = OpenroadGrt.Settings()
    assert grt.pin_access.enabled is True
    assert (grt.congestion.iterations, grt.congestion.allow) == (30, False)
    assert (grt.router.seed, grt.legalize.cell_padding) == (None, 0)
    assert (grt.repair.wns_polish, grt.repair.recover_power_percent) == (None, None)
    assert (grt.antenna.repair, grt.antenna.iterations, grt.antenna.diode_cell) == (True, 5, None)


def test_the_defaults_of_the_last_flow_are_the_stage_defaults():
    from xeda.flows.openroad import Openroad

    last = Openroad.Settings()
    assert (last.write_gds, last.save_images) == (False, False)
    assert (last.fail_timing, last.fail_drc, last.fail_antenna) == (True, True, True)
    route = last.detailed_route
    assert (route.end_iteration, route.drc_report_step) == (64, 5)
    assert (route.reroute_seed, route.reroute_swap_factor, route.via_in_pin) == (None, None, None)
    assert (last.antenna.repair, last.antenna.reroute_iterations) == (True, 5)
    assert (last.fill.cells, last.extraction.method, last.ir_drop.enabled) == (None, "auto", True)
    text = " ".join(type(last.ir_drop).model_fields["enabled"].description.split())
    assert "voltage" in text and "skips the analysis" in text and "log says so" in text
    assert last.gds.seal_ring is None


CTS_REFUSALS = [
    (("buffers.cells=BUF_X1",), "`buffers.cells` has no effect with `buffers.select: inferred`"),
    (("buffers.select=list",), "`buffers.select: list` takes the buffers from `buffers.cells`"),
    (("buffers.library=lib",), "`buffers.library` has no effect with `buffers.select: inferred`"),
    (
        ("buffers.select=library",),
        "`buffers.select: library` takes the buffers from `buffers.library`",
    ),
    (
        ("buffers.select=list", "buffers.cells=BUF_X1", "buffers.library=lib"),
        "`buffers.library` has no effect with `buffers.select: list`",
    ),
    (
        ("sink_clustering.size=10",),
        "`sink_clustering.size` has no effect with `sink_clustering.mode: auto`",
    ),
    (
        ("sink_clustering.mode=off", "sink_clustering.max_diameter=50"),
        "`sink_clustering.max_diameter` has no effect with `sink_clustering.mode: off`",
    ),
    (
        ("sink_clustering.mode=explicit",),
        "`sink_clustering.mode: explicit` needs `sink_clustering.size`",
    ),
    (("tree.buffer_distance=100",), "`tree.buffer_distance` has no effect with `tree.style: lut`"),
    (("tree.style=simple_segments",), "`tree.style: simple_segments` needs `tree.buffer_distance`"),
]


@pytest.mark.parametrize("items, text", CTS_REFUSALS)
def test_the_groups_of_the_clock_tree_refuse_what_cannot_work(items, text):
    assert text in refused(OpenroadCts, *items)


def test_the_clock_tree_groups_render_the_flags_of_their_choices():
    cts = settings_of(
        OpenroadCts,
        "buffers.select=list",
        "buffers.cells=CLKBUF_X1,CLKBUF_X2",
        "buffers.root=CLKBUF_X2",
        "sink_clustering.mode=explicit",
        "sink_clustering.size=20",
        "sink_clustering.max_diameter=60",
        "tree.style=simple_segments",
        "tree.buffer_distance=100",
    )
    assert problems(OpenroadCts, "buffers.select=list", "buffers.cells=CLKBUF_X1") == []
    assert (
        render_flags(cts.buffers, tcl_word) == '-root_buf "CLKBUF_X2"'
    )  # the flow prints the rest
    assert render(cts.sink_clustering) == (
        '-sink_clustering_size "20" -sink_clustering_max_diameter "60.0"'
    )
    assert render(cts.tree) == ""  # the flow prints the distance when the style asks for it
    assert render(OpenroadCts.Settings().buffers) == ""


@pytest.mark.parametrize(
    "item",
    [
        "buffers.select=footprint",
        "sink_clustering.mode=on",
        "sink_clustering.size=0",
        "sink_clustering.max_diameter=0",
        "tree.style=h_tree",
        "tree.buffer_distance=0",
        "repair_clock_nets.when=before",
        "legalize.cell_padding=-1",
    ],
)
def test_the_clock_tree_settings_refuse_a_value_out_of_range(item):
    with pytest.raises(Exception):
        settings_of(OpenroadCts, item)


GRT_REFUSALS = [
    (
        ("repair.strategy=none", "repair.wns_polish=true"),
        "`repair.wns_polish` has no effect with `repair.strategy: none`",
    ),
    (
        ("repair.strategy=none", "repair.recover_power_percent=10"),
        "`repair.recover_power_percent` has no effect with `repair.strategy: none`",
    ),
    (
        ("repair.strategy=none", "repair.design.cap_margin_percent=5"),
        "`repair.design.cap_margin_percent` has no effect with `repair.strategy: none`",
    ),
    (
        ("repair.strategy=none", "repair.timing.enabled=false"),
        "`repair.timing.enabled` has no effect with `repair.strategy: none`",
    ),
    (
        ("repair.wns_polish=true", "repair.recover_power_percent=10"),
        "`repair.recover_power_percent` has no effect with `repair.wns_polish: true`",
    ),
    (
        ("antenna.repair=false", "antenna.iterations=3"),
        "`antenna.iterations` has no effect with `antenna.repair: false`",
    ),
    (
        ("antenna.repair=false", "antenna.diode_cell=DIODE"),
        "`antenna.diode_cell` has no effect with `antenna.repair: false`",
    ),
]


@pytest.mark.parametrize("items, text", GRT_REFUSALS)
def test_the_groups_of_the_global_route_refuse_what_cannot_work(items, text):
    assert text in refused(OpenroadGrt, *items)


def test_the_global_route_groups_render_their_flags():
    grt = settings_of(
        OpenroadGrt,
        "congestion.iterations=50",
        "congestion.allow=true",
        "antenna.iterations=3",
        "repair.timing.setup_margin=0.1",
    )
    assert render(grt.congestion) == '-congestion_iterations "50" -allow_congestion'
    assert render(OpenroadGrt.Settings().congestion) == '-congestion_iterations "30"'
    assert render(grt.antenna) == '-iterations "3"'
    assert render(grt.repair.timing) == '-repair_tns "100.0"'
    assert grt.repair.timing.setup_margin == 0.1
    assert problems(OpenroadGrt, "repair.wns_polish=false", "repair.recover_power_percent=10") == []


@pytest.mark.parametrize(
    "item",
    [
        "congestion.iterations=0",
        "router.seed=0",
        "repair.strategy=full",
        "repair.recover_power_percent=0",
        "repair.recover_power_percent=101",
        "antenna.iterations=0",
    ],
)
def test_the_global_route_settings_refuse_a_value_out_of_range(item):
    with pytest.raises(Exception):
        settings_of(OpenroadGrt, item)


def test_the_last_flow_refuses_what_cannot_work():
    from xeda.flows.openroad import Openroad

    assert "`detailed_route.reroute_swap_factor` has no effect without" in refused(
        Openroad, "detailed_route.reroute_swap_factor=2"
    )
    assert (
        problems(Openroad, "detailed_route.reroute_seed=3", "detailed_route.reroute_swap_factor=2")
        == []
    )
    message = refused(Openroad, "antenna.repair=false", "antenna.reroute_iterations=2")
    assert "`antenna.reroute_iterations` has no effect with `antenna.repair: false`" in message


@pytest.mark.parametrize(
    "item",
    [
        "detailed_route.end_iteration=0",
        "detailed_route.end_iteration=65",
        "detailed_route.drc_report_step=-1",
        "detailed_route.reroute_seed=-1",
        {"detailed_route.via_in_pin": {"bottom": "m1"}},
        "extraction.method=spef",
        "antenna.reroute_iterations=0",
        'fill.cells=[""]',
        "fail_drc=maybe",
    ],
)
def test_the_last_flow_refuses_a_value_out_of_range(item):
    from xeda.flows.openroad import Openroad

    with pytest.raises(Exception):
        settings_of(Openroad, item)


def test_the_last_flow_prints_the_flags_of_the_detailed_route():
    from xeda.flows.openroad import Openroad

    route = settings_of(
        Openroad,
        "detailed_route.end_iteration=32",
        "detailed_route.reroute_seed=3",
        "detailed_route.reroute_swap_factor=2",
    ).detailed_route
    assert render(route) == (
        '-droute_end_iter "32" -or_seed "3" -or_k "2" -drc_report_iter_step "5"'
    )
    fill = settings_of(Openroad, "fill.cells=FILL_*").fill
    assert fill.cells == ["FILL_*"]
    assert settings_of(Openroad, "fill.cells=").fill.cells == []
    via = settings_of(
        Openroad, {"detailed_route.via_in_pin": {"bottom": "m1", "top": "m5"}}
    ).detailed_route
    assert (via.via_in_pin.bottom, via.via_in_pin.top) == ("m1", "m5")


# ---------------------------------------------------------------------------- the escape hatches

#: The hatch of each command: the flow, the key of the group, and the flags that it accepts.
HATCHES = {
    "detailed_placement": (
        OpenroadPlace,
        "detailed_placement",
        {
            "-use_diamond_legalizer",
            "-site_search_window",
            "-row_search_window",
            "-drc_penalty",
            "-disable_window_extension",
        },
    ),
    "global_route": (
        OpenroadGrt,
        "router",
        {
            "-use_cugr",
            "-resistance_aware",
            "-res_aware_nets_percentage",
            "-snapshot_batched_width",
        },
    ),
    "pin_access": (OpenroadGrt, "pin_access", {"-via_access_layer", "-min_access_points"}),
    "detailed_route": (
        None,
        "detailed_route",
        {"-via_access_layer", "-min_access_points", "-clean_patches", "-save_guide_updates"},
    ),
}

#: The flags that `help <command>` of the installed OpenROAD lists for each command that has a
#: hatch. The hatch, the typed flags and the flags of the sub-groups come from this list.
INSTALLED_HELP = {
    "place_pins": {
        "-hor_layers",
        "-ver_layers",
        "-random_seed",
        "-random",
        "-corner_avoidance",
        "-min_distance",
        "-min_distance_in_tracks",
        "-exclude",
        "-group_pins",
        "-annealing",
        "-write_pin_placement",
    },
    "cluster_flops": {
        "-tray_weight",
        "-timing_weight",
        "-max_split_size",
        "-num_paths",
        "-clock_power_weight",
    },
    "set_simulated_annealing": {"-temperature", "-max_iterations", "-perturb_per_iter", "-alpha"},
    "set_pin_length": {"-hor_length", "-ver_length"},
    "set_pin_length_extension": {"-hor_extension", "-ver_extension"},
    "set_pin_thick_multiplier": {"-hor_multiplier", "-ver_multiplier"},
    "global_placement": {
        "-skip_initial_place",
        "-force_center_initial_place",
        "-skip_nesterov_place",
        "-timing_driven",
        "-timing_driven_repair_timing",
        "-routability_driven",
        "-virtual_cts",
        "-incremental",
        "-skip_io",
        "-place_ios",
        "-bin_grid_count",
        "-density",
        "-init_density_penalty",
        "-init_wirelength_coef",
        "-min_phi_coef",
        "-max_phi_coef",
        "-reference_hpwl",
        "-overflow",
        "-initial_place_max_iter",
        "-initial_place_max_fanout",
        "-routability_use_grt",
        "-routability_target_rc_metric",
        "-routability_check_overflow",
        "-routability_snapshot_overflow",
        "-routability_max_density",
        "-routability_inflation_ratio_coef",
        "-routability_max_inflation_ratio",
        "-routability_min_congestion_for_inflation",
        "-routability_max_inflation_total",
        "-routability_net_weight_max",
        "-routability_congested_nets_percentage",
        "-routability_rc_coefficients",
        "-keep_resize_below_overflow",
        "-timing_driven_net_reweight_overflow",
        "-timing_driven_net_weight_max",
        "-timing_driven_nets_percentage",
        "-virtual_cts_max_skew_fraction",
        "-timing_driven_repair_tns_end_percent",
        "-pad_left",
        "-pad_right",
        "-disable_revert_if_diverge",
        "-disable_pin_density_adjust",
        "-random_seed",
        "-perturb_dist",
        "-enable_routing_congestion",
    },
    "detailed_placement": {
        "-max_displacement",
        "-disallow_one_site_gaps",
        "-incremental",
        "-report_file_name",
        "-use_diamond_legalizer",
        "-site_search_window",
        "-row_search_window",
        "-drc_penalty",
        "-disable_window_extension",
        "-quiet",
    },
    "improve_placement": {
        "-random_seed",
        "-max_displacement",
        "-global_swap_args",
        "-enable_extra_dpl",
        "-disallow_one_site_gaps",
    },
    "global_route": {
        "-guide_file",
        "-congestion_iterations",
        "-congestion_report_file",
        "-congestion_report_iter_step",
        "-grid_origin",
        "-critical_nets_percentage",
        "-res_aware_nets_percentage",
        "-skip_large_fanout_nets",
        "-allow_congestion",
        "-snapshot_batched_width",
        "-verbose",
        "-start_incremental",
        "-end_incremental",
        "-use_cugr",
        "-resistance_aware",
        "-infinite_cap",
    },
    "pin_access": {
        "-db_process_node",
        "-bottom_routing_layer",
        "-top_routing_layer",
        "-via_access_layer",
        "-via_in_pin_bottom_layer",
        "-via_in_pin_top_layer",
        "-min_access_points",
        "-verbose",
        "-distributed",
        "-remote_host",
        "-remote_port",
        "-shared_volume",
        "-cloud_size",
    },
    "detailed_route": {
        "-output_maze",
        "-output_drc",
        "-output_guide_coverage",
        "-drc_report_iter_step",
        "-db_process_node",
        "-disable_via_gen",
        "-droute_end_iter",
        "-via_in_pin_bottom_layer",
        "-via_in_pin_top_layer",
        "-via_access_layer",
        "-or_seed",
        "-or_k",
        "-bottom_routing_layer",
        "-top_routing_layer",
        "-verbose",
        "-distributed",
        "-remote_host",
        "-remote_port",
        "-shared_volume",
        "-cloud_size",
        "-clean_patches",
        "-no_pin_access",
        "-min_access_points",
        "-save_guide_updates",
        "-repair_pdn_vias",
        "-single_step_dr",
    },
    "clock_tree_synthesis": {
        "-wire_unit",
        "-buf_list",
        "-root_buf",
        "-clk_nets",
        "-tree_buf",
        "-distance_between_buffers",
        "-branching_point_buffers_distance",
        "-clustering_exponent",
        "-clustering_unbalance_ratio",
        "-sink_clustering_size",
        "-sink_clustering_max_diameter",
        "-macro_clustering_size",
        "-macro_clustering_max_diameter",
        "-sink_clustering_enable",
        "-balance_levels",
        "-sink_clustering_levels",
        "-num_static_layers",
        "-sink_clustering_buffer",
        "-obstruction_aware",
        "-no_obstruction_aware",
        "-apply_ndr",
        "-sink_buffer_max_cap_derate",
        "-dont_use_dummy_load",
        "-delay_buffer_derate",
        "-library",
        "-repair_clock_nets",
        "-no_insertion_delay",
    },
    "repair_timing": {
        "-setup",
        "-hold",
        "-recover_power",
        "-setup_margin",
        "-hold_margin",
        "-slack_margin",
        "-libraries",
        "-allow_setup_violations",
        "-sequence",
        "-phases",
        "-skip_pin_swap",
        "-skip_gate_cloning",
        "-skip_size_down",
        "-skip_buffering",
        "-skip_buffer_removal",
        "-skip_buffer_to_inverters",
        "-skip_last_gasp",
        "-skip_vt_swap",
        "-skip_crit_vt_swap",
        "-skip_crpr",
        "-repair_tns",
        "-max_passes",
        "-max_iterations",
        "-max_buffer_percent",
        "-max_utilization",
        "-match_cell_footprint",
        "-max_repairs_per_pass",
        "-verbose",
    },
    "repair_design": {
        "-max_wire_length",
        "-max_utilization",
        "-slew_margin",
        "-cap_margin",
        "-buffer_gain",
        "-pre_placement",
        "-match_cell_footprint",
        "-reroute",
        "-verbose",
    },
    "repair_antennas": {
        "-iterations",
        "-ratio_margin",
        "-jumper_only",
        "-diode_only",
        "-allow_congestion",
    },
    "repair_tie_fanout": {"-separation", "-max_fanout", "-verbose"},
}


#: The command of each group inside `io`, which has the flags of its own command.
HELP_OF_IO_GROUP = {
    "annealing": "set_simulated_annealing",
    "pin_length": "set_pin_length",
    "pin_extension": "set_pin_length_extension",
    "pin_thickness": "set_pin_thick_multiplier",
}


def hatch_group(name: str):
    from xeda.flows.openroad import Openroad

    flow, key, _ = HATCHES[name]
    return (flow or Openroad).Settings.model_fields[key].annotation


@pytest.mark.parametrize("command", sorted(HATCHES))
def test_a_hatch_accepts_exactly_the_listed_flags(command):
    group = hatch_group(command)
    assert group.EXTRA_FLAGS == frozenset(HATCHES[command][2])
    assert group.EXTRA_FLAGS <= INSTALLED_HELP[command]
    assert not group.EXTRA_FLAGS & {flag.flag for flag in group.FLAGS}


@pytest.mark.parametrize("command", sorted(HATCHES))
def test_an_allowed_flag_with_its_value_passes_and_prints_after_the_typed_flags(command):
    from xeda.flows.openroad import Openroad

    flow, key, flags = HATCHES[command]
    flow = flow or Openroad
    flag = sorted(flags)[0]
    assert problems(flow, f"{key}.extra_args={flag},3") == []
    group = getattr(settings_of(flow, f"{key}.extra_args={flag},3"), key)
    assert render(group).endswith(f'"{flag}" "3"')


@pytest.mark.parametrize("command", sorted(HATCHES))
def test_an_unknown_flag_in_a_hatch_is_refused_with_the_allowed_flags(command):
    from xeda.flows.openroad import Openroad

    flow, key, flags = HATCHES[command]
    message = refused(flow or Openroad, f"{key}.extra_args=-bogus")
    assert f"`-bogus` in `{key}.extra_args` is not allowed" in message
    assert ", ".join(sorted(flags)) in message


def test_a_flag_that_a_typed_leaf_prints_is_refused_naming_the_setting():
    from xeda.flows.openroad import Openroad

    message = refused(Openroad, "detailed_route.extra_args=-droute_end_iter,3")
    assert (
        "`-droute_end_iter` in `detailed_route.extra_args` is the setting "
        "`detailed_route.end_iteration`: set that instead"
    ) in message


def test_the_flag_of_a_leaf_in_a_sub_group_is_refused_as_not_allowed():
    message = refused(OpenroadPlace, "detailed_placement.extra_args=-max_displacement,3")
    assert "`-max_displacement` in `detailed_placement.extra_args` is not allowed" in message


def test_a_hatch_must_start_with_a_flag():
    message = refused(OpenroadPlace, "detailed_placement.extra_args=5,-use_diamond_legalizer")
    assert "`detailed_placement.extra_args` must start with a flag, not `5`" in message


def test_the_clock_tree_has_no_hatch():
    assert "extra_args" not in OpenroadCts.Settings.model_fields
    for group in OpenroadCts.Settings().__dict__.values():
        assert not hasattr(group, "extra_args")


def test_a_hatch_of_a_disabled_group_is_refused():
    found = problems(
        OpenroadGrt, "pin_access.enabled=false", "pin_access.extra_args=-via_access_layer,m2"
    )
    assert [key for key, _ in found] == ["pin_access.extra_args"]


@pytest.mark.parametrize(
    "command, flags",
    [
        (
            "repair_timing",
            [flag.flag for flag in groups.RepairTiming.FLAGS]
            + [flag.flag for flag in groups.RepairMoves.FLAGS],
        ),
        ("repair_design", [flag.flag for flag in groups.RepairDesign.FLAGS]),
        (
            "place_pins",
            [flag.flag for flag in OpenroadPlace.Settings.model_fields["io"].annotation.FLAGS],
        ),
        (
            "global_placement",
            [
                flag.flag
                for flag in OpenroadPlace.Settings.model_fields["global_placement"].annotation.FLAGS
            ],
        ),
        (
            "clock_tree_synthesis",
            [
                flag.flag
                for group in (
                    OpenroadCts.Settings.model_fields[name].annotation
                    for name in ("buffers", "sink_clustering", "tree")
                )
                for flag in group.FLAGS
            ],
        ),
        (
            "global_route",
            [
                flag.flag
                for flag in OpenroadGrt.Settings.model_fields["congestion"].annotation.FLAGS
            ],
        ),
        (
            "repair_antennas",
            [flag.flag for flag in OpenroadGrt.Settings.model_fields["antenna"].annotation.FLAGS],
        ),
        (
            "repair_tie_fanout",
            [flag.flag for flag in OpenroadPlace.Settings.model_fields["tie"].annotation.FLAGS],
        ),
    ],
)
def test_every_flag_that_a_group_prints_is_in_the_help_of_the_installed_tool(command, flags):
    assert flags
    assert set(flags) <= INSTALLED_HELP[command]


def test_every_other_flag_that_a_group_prints_is_in_the_help_of_the_installed_tool():
    from xeda.flows.openroad import Openroad

    place = OpenroadPlace.Settings.model_fields
    placement = place["global_placement"].annotation.model_fields
    for name in ("timing_driven", "routability_driven", "initial_place", "nesterov"):
        flags = {f.flag for f in placement[name].annotation.FLAGS}
        assert flags and flags <= INSTALLED_HELP["global_placement"], name
    flop = placement["flop_clustering"].annotation
    assert {f.flag for f in flop.FLAGS} <= INSTALLED_HELP["cluster_flops"]
    for name in ("annealing", "pin_length", "pin_extension", "pin_thickness"):
        group = place["io"].annotation.model_fields[name].annotation
        assert {f.flag for f in group.FLAGS} <= INSTALLED_HELP[HELP_OF_IO_GROUP[name]], name
    assert {
        f.flag
        for f in place["detailed_placement"].annotation.model_fields["improve"].annotation.FLAGS
    } <= INSTALLED_HELP["improve_placement"]
    route = Openroad.Settings.model_fields["detailed_route"].annotation
    assert {f.flag for f in route.FLAGS} <= INSTALLED_HELP["detailed_route"]
