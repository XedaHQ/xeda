"""The resolver turns a launch into one immutable plan before anything runs: each declared
input's origin (a design source of an accepted type, else the default producer),
the outputs a consumer switches on, every node's effective settings -- a shared setting
agreeing along every edge: given in one place it reaches every connected flow, given
differently it is an error naming both places, and the command line wins for the whole run --
and every node's identity."""

import dataclasses
import re
from pathlib import Path
from typing import ClassVar

import pytest

from xeda.flow import MetricSpec
from xeda import Design
from xeda.flow import Flow, FlowSettingsError, FlowSettingsException, flowrun_hash
from xeda.flow_runner.bindings import node_identity
from xeda.flow_runner.configuration import configuration_hash
from xeda.flow_runner.resolver import resolve
from xeda.flow_runner.settings_layers import (
    COMMAND_LINE_ORIGIN,
    compose_flow_settings,
    transitive_dependencies,
)
from xeda.units import Time

from .flow_registry import preserved_flow_registry
from .io_flows import TChainUndeclared, TMaker, TPlace, TReader, TTaker
from .scoping import node_settings

PART = "LFE5U-25F-6BG256C"
OTHER = "LFE5U-85F-6BG381C"


def _plan(tmp_path: Path, flow_cls, settings=None, sections=None, sources=(), **kwargs):
    root = tmp_path / "d"
    root.mkdir(exist_ok=True)
    for source in sources:
        (root / source["file"]).write_text("x\n")
    design = Design(
        name="d", root=root, rtl={"sources": list(sources), "top": "t", "clocks": ["clk"]}
    )
    return resolve(
        flow_cls,
        design,
        settings or {},
        sections or {},
        runner_cwd=tmp_path,
        run_root=tmp_path / "run",
        hashed_run_dirs=False,
        run_path=lambda design_name, node, identity, target=None: (
            tmp_path / "run" / design_name / node
        ),
        **kwargs,
    )


def test_the_default_producer_is_planned_before_its_consumer(tmp_path):
    plan = _plan(tmp_path, TTaker)
    assert plan.requested == "t_taker"
    assert [node.name for node in plan.nodes] == ["t_maker", "t_taker"]
    (made,) = plan.node("t_taker").inputs
    assert (made.origin, made.producer, made.output) == ("producer", "t_maker", "made")
    assert made.describe() == "made <- t_maker.made"


def test_a_design_source_of_an_accepted_type_replaces_the_default_producer(tmp_path):
    plan = _plan(tmp_path, TTaker, sources=[{"file": "given.dat", "type": "Data"}])
    assert [node.name for node in plan.nodes] == ["t_taker"]
    (made,) = plan.node("t_taker").inputs
    assert made.origin == "source" and made.sources == (tmp_path / "d" / "given.dat",)


def test_two_sources_for_a_singular_input_are_an_error_naming_them(tmp_path):
    sources = [{"file": "a.dat", "type": "Data"}, {"file": "b.dat", "type": "Data"}]
    with pytest.raises(FlowSettingsException, match="takes one Data file") as raised:
        _plan(tmp_path, TTaker, sources=sources)
    assert "a.dat" in str(raised.value) and "b.dat" in str(raised.value)


def test_a_required_input_nothing_supplies_is_an_error_naming_it(tmp_path):
    with pytest.raises(FlowSettingsException, match=r"t_reader needs its input `data`.*Data"):
        _plan(tmp_path, TReader)


def test_an_output_a_consumer_needs_is_switched_on(tmp_path):
    plan = _plan(tmp_path, TTaker, sections={"t_maker": {"write": False}})
    maker = plan.node("t_maker")
    assert maker.settings.write is True and maker.switched_on == ("made",)


def test_a_flow_that_declares_nothing_is_one_node(tmp_path):
    plan = _plan(tmp_path, TChainUndeclared)
    (node,) = plan.nodes
    assert node.name == "t_chain_undeclared" and node.inputs == ()


def test_each_node_s_identity_is_what_the_launcher_hashes(tmp_path):
    plan = _plan(tmp_path, TTaker, sections={"t_maker": {"text": "other\n"}})
    maker, taker = plan.nodes
    assert maker.origins == () and taker.origins == (("made", ((maker.flowrun_hash, "made"),)),)
    for node in plan.nodes:
        assert node.settings_hash == flowrun_hash(node.name, node.settings, "d")
        assert node.flowrun_hash == node_identity(
            node.settings_hash, configuration_hash(node.configuration), node.origins
        )
        assert node.run_path == tmp_path / "run" / "d" / node.name


def test_the_plan_is_immutable(tmp_path):
    plan = _plan(tmp_path, TTaker)
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.requested = "other"  # type: ignore[misc]
    saved = plan.node("t_maker").settings.model_dump()
    exposed = plan.node("t_maker").settings
    exposed.text = "mutated"
    assert plan.node("t_maker").settings.model_dump() == saved


def test_a_shared_setting_given_for_the_consumer_reaches_its_producer(tmp_path):
    plan = _plan(
        tmp_path, TPlace, {"fpga": {"part": PART}, "clocks": {"main_clock": {"period": "10 ns"}}}
    )
    for name in ("t_synth", "t_place"):
        settings = plan.node(name).settings
        assert settings.fpga.part == PART and settings.clocks["main_clock"].period.to("ns") == 10.0


def test_a_shared_setting_given_only_for_the_producer_reaches_its_consumer(tmp_path):
    sections = {"t_synth": {"fpga": {"part": PART}}}
    plan = _plan(tmp_path, TPlace, compose_flow_settings(TPlace, [sections]), sections)
    assert plan.node("t_place").settings.fpga.part == PART  # and so its required `fpga` is met


def test_leaves_given_in_one_place_each_combine(tmp_path):
    sections = {"t_synth": {"fpga": {"family": "ecp5"}}}
    settings = compose_flow_settings(TPlace, [sections], {"fpga": {"part": PART}})
    plan = _plan(tmp_path, TPlace, settings, sections)
    assert plan.node("t_synth").settings.fpga.part == PART
    assert plan.node("t_synth").settings.fpga.family == "ecp5"


def test_conflicting_values_are_an_error_naming_both_places(tmp_path):
    """A project file's `[flows.nextpnr] fpga` must not silently win over a design file's
    `[flows.yosys_fpga] fpga`."""
    project = {"t_place": {"fpga": {"part": OTHER}}}
    design = {"t_synth": {"fpga": {"part": PART}}}
    settings = compose_flow_settings(TPlace, [project, design])
    with pytest.raises(FlowSettingsError) as raised:
        _plan(
            tmp_path,
            TPlace,
            settings,
            {**project, **design},
            origins=[("the project file", project), ("the design file", design)],
        )
    message = str(raised.value)
    for text in (
        PART,
        OTHER,
        "fpga.part",
        "`flows.t_place` in the project file",
        "`flows.t_synth` in the design file",
    ):
        assert text in message, text


def test_a_value_given_on_the_command_line_is_the_whole_run_s(tmp_path):
    design = {"t_synth": {"fpga": {"part": PART}}}
    command_line = {"t_place": {"fpga": {"part": OTHER}}}
    settings = compose_flow_settings(TPlace, [design], command_line["t_place"])
    plan = _plan(
        tmp_path,
        TPlace,
        settings,
        design,
        origins=[("the design file", design)],
        command_line=command_line,
    )
    assert plan.node("t_synth").settings.fpga.part == OTHER
    assert plan.node("t_place").settings.fpga.part == OTHER


def test_two_different_command_line_values_are_an_error(tmp_path):
    command_line = {"t_place": {"fpga": {"part": OTHER}}, "t_synth": {"fpga": {"part": PART}}}
    settings = compose_flow_settings(
        TPlace, [{"t_synth": command_line["t_synth"]}], command_line["t_place"]
    )
    with pytest.raises(FlowSettingsError, match=re.escape(COMMAND_LINE_ORIGIN)):
        _plan(
            tmp_path,
            TPlace,
            settings,
            {"t_synth": command_line["t_synth"]},
            origins=[(COMMAND_LINE_ORIGIN, {"t_synth": command_line["t_synth"]})],
            command_line=command_line,
        )


def test_a_missing_required_setting_names_the_requested_flow_first(tmp_path):
    with pytest.raises(FlowSettingsException, match=r"^t_place needs `fpga`"):
        _plan(tmp_path, TPlace)


def test_a_declared_producer_is_a_flow_the_command_line_may_address(tmp_path):
    design = Design(name="d", root=tmp_path, rtl={"sources": [], "top": "t"})
    assert set(transitive_dependencies(TTaker, design)) == {"t_maker"}
    assert set(transitive_dependencies(TPlace, design)) == {"t_synth"}
    assert TMaker.name in transitive_dependencies(TTaker, design)


@pytest.fixture(autouse=True)
def private_registry():
    """Locally defined contract probes do not leak into other registry sweeps."""
    with preserved_flow_registry():
        yield


def test_section_locations_and_origin_precedence_are_retained(tmp_path):
    project = {"t_place": {"fpga": {"part": OTHER}}}
    design = {"t_synth": {"fpga": {"part": PART}}}
    with pytest.raises(FlowSettingsError) as raised:
        _plan(
            tmp_path,
            TPlace,
            compose_flow_settings(TPlace, [project, design]),
            origins=[("project /p/project.toml", project), ("design /d/design.toml", design)],
        )
    message = str(raised.value)
    for text in ("/p/project.toml", "/d/design.toml", "t_place", "t_synth", PART, OTHER):
        assert text in message


@pytest.mark.parametrize(
    "cli_clock",
    [
        {"clocks": {"core": {"period": "4 ns"}}},
        {"clocks": {"core": {"freq": "250 MHz"}}},
    ],
)
def test_cli_clock_leaf_preserves_other_clocks_and_attributes(tmp_path, cli_clock):
    design = {
        "t_place": {
            "fpga": {"part": PART},
            "clocks": {
                "core": {"period": "10 ns", "port": "clk", "uncertainty": "100ps"},
                "aux": {"period": "20 ns", "port": "aux_clk"},
            },
        }
    }
    cli = {"t_place": cli_clock}
    given = compose_flow_settings(TPlace, [design], cli_clock)
    plan = _plan(tmp_path, TPlace, given, origins=[("design.toml", design)], command_line=cli)
    for node in plan.nodes:
        clocks = node.settings.clocks
        assert list(clocks) == ["core", "aux"]
        assert clocks["core"].period.to("ns") == 4
        assert clocks["core"].port == "clk" and clocks["core"].uncertainty.to(
            "ns"
        ) == pytest.approx(0.1)
        assert clocks["aux"].period.to("ns") == 20 and clocks["aux"].port == "aux_clk"


def test_disjoint_cli_leaves_combine_across_nodes(tmp_path):
    design = {
        "t_place": {
            "fpga": {"part": PART},
            "clocks": {"main_clock": {"period": "10 ns", "port": "clk"}},
        }
    }
    cli = {
        "t_place": {"clocks": {"main_clock": {"freq": "250 MHz"}}},
        "t_synth": {"clocks": {"main_clock": {"uncertainty": "100ps"}}},
    }
    given = compose_flow_settings(TPlace, [design, cli])
    plan = _plan(tmp_path, TPlace, given, origins=[("design.toml", design)], command_line=cli)
    for node in plan.nodes:
        assert node.settings.main_clock.period.to("ns") == 4
        assert node.settings.main_clock.uncertainty.to("ns") == pytest.approx(0.1)
        assert node.settings.main_clock.port == "clk"


def test_conflicting_cli_clock_aliases_are_an_error(tmp_path):
    cli = {
        "t_place": {"fpga": {"part": PART}, "clocks": {"main_clock": {"period": "4 ns"}}},
        "t_synth": {"clocks": {"main_clock": {"freq": "200 MHz"}}},
    }
    with pytest.raises(FlowSettingsError, match=re.escape(COMMAND_LINE_ORIGIN)):
        _plan(tmp_path, TPlace, compose_flow_settings(TPlace, [cli]), command_line=cli)


def test_equivalent_frequency_and_period_agree(tmp_path):
    sections = {
        "t_place": {"fpga": {"part": PART}, "clocks": {"main_clock": {"period": "5ns"}}},
        "t_synth": {"clocks": {"main_clock": {"freq": "200MHz"}}},
    }
    plan = _plan(tmp_path, TPlace, compose_flow_settings(TPlace, [sections]), sections)
    assert all(node.settings.main_clock.period.to("ns") == 5 for node in plan.nodes)


@pytest.mark.parametrize(
    "place_period, synth_period", [("10 ns", "10 ns"), ("10 ns", "10000 ps"), ("0.01 us", "10 ns")]
)
def test_the_same_clock_period_in_any_unit_agrees(tmp_path, place_period, synth_period):
    sections = {
        "t_place": {"fpga": {"part": PART}, "clocks": {"main_clock": {"period": place_period}}},
        "t_synth": {"clocks": {"main_clock": {"period": synth_period}}},
    }
    plan = _plan(tmp_path, TPlace, compose_flow_settings(TPlace, [sections]), sections)
    assert all(node.settings.main_clock.period == Time("10 ns") for node in plan.nodes)


def test_different_clock_periods_disagree(tmp_path):
    sections = {
        "t_place": {"fpga": {"part": PART}, "clocks": {"main_clock": {"period": "10 ns"}}},
        "t_synth": {"clocks": {"main_clock": {"period": "5 ns"}}},
    }
    with pytest.raises(FlowSettingsError) as raised:
        _plan(tmp_path, TPlace, compose_flow_settings(TPlace, [sections]), sections)
    message = str(raised.value)
    assert "clocks.main_clock.period=10 ns at " in message
    assert "clocks.main_clock.period=5 ns at " in message
    assert "Time(" not in message


def test_a_clock_period_without_a_unit_is_refused_when_flows_agree(tmp_path):
    sections = {
        "t_place": {"fpga": {"part": PART}, "clocks": {"main_clock": {"period": "10 ns"}}},
        "t_synth": {"clocks": {"main_clock": {"period": "5"}}},
    }
    with pytest.raises(FlowSettingsError, match="needs a unit"):
        _plan(tmp_path, TPlace, compose_flow_settings(TPlace, [sections]), sections)


def test_api_overrides_have_their_own_highest_precedence(tmp_path):
    design = {"t_place": {"fpga": {"part": PART}}}
    cli = {"t_place": {"fpga": {"part": OTHER}}}
    api = {"t_synth": {"fpga": {"part": PART}}}
    plan = _plan(
        tmp_path,
        TPlace,
        compose_flow_settings(TPlace, [design, cli, api]),
        origins=[("design.toml", design)],
        command_line=cli,
        api_overrides=api,
    )
    assert all(node.settings.fpga.part == PART for node in plan.nodes)
    with pytest.raises(FlowSettingsError, match="the API") as raised:
        _plan(
            tmp_path,
            TPlace,
            api_overrides={
                "t_place": {"fpga": {"part": PART}},
                "t_synth": {"fpga": {"part": OTHER}},
            },
        )
    assert "command line" not in str(raised.value)


def test_mappings_and_settings_agree(tmp_path):
    values = {"fpga": {"part": PART}, "nthreads": 2}
    mapping = _plan(tmp_path, TPlace, values)
    model = TPlace.Settings.from_input(values, design_root=tmp_path / "d", runner_cwd=tmp_path)
    instance = _plan(tmp_path, TPlace, model)
    assert [n.settings for n in mapping.nodes] == [n.settings for n in instance.nodes]
    assert [n.flowrun_hash for n in mapping.nodes] == [n.flowrun_hash for n in instance.nodes]


def test_deep_plan_protection_and_resolution_purity(tmp_path):
    from xeda import Design

    root = tmp_path / "d"
    root.mkdir()
    design = Design(name="d", root=root, rtl={"sources": [], "top": "t"})
    sections = {"t_place": {"fpga": {"part": PART}}, "t_synth": {"lib_paths": [("work", "lib")]}}
    given = TPlace.Settings.from_input(
        compose_flow_settings(TPlace, [sections]), design_root=root, runner_cwd=tmp_path
    )
    initial = given.model_dump()
    design_state = design.model_dump()
    plan = resolve(
        TPlace,
        design,
        given,
        sections,
        runner_cwd=tmp_path,
        run_root=tmp_path / "run",
        hashed_run_dirs=True,
        run_path=lambda d, n, h, target=None: tmp_path / "run" / d / f"{n}_{h}",
        origins=[("design.toml", sections)],
        debug=True,
    )
    saved = [(n.settings.model_dump(), n.flowrun_hash, n.run_path) for n in plan.nodes]
    exposed = plan.node("t_synth").settings
    exposed.lib_paths.append(("other", Path("other")))
    exposed.fpga.part = OTHER
    given.fpga.part = OTHER
    sections["t_place"]["fpga"] = OTHER
    assert [(n.settings.model_dump(), n.flowrun_hash, n.run_path) for n in plan.nodes] == saved
    assert design.model_dump() == design_state
    assert initial["fpga"]["part"] == PART
    assert plan.context.design_root == root and plan.context.runner_cwd == tmp_path
    assert plan.context.hashed_run_dirs and plan.context.debug
    with pytest.raises(TypeError):
        plan.context.input_settings["fpga"]["part"] = "bad"
    assert not (tmp_path / "run").exists()
    assert all(node.settings.verbosity == "debug" for node in plan.nodes)


def _board_place():
    from xeda.board import WithFpgaBoardSettings
    from xeda.design import SourceType
    from xeda.flow import FpgaSynthFlow, In

    class TBoardPlace(FpgaSynthFlow):
        """A board-aware consumer of the same test synthesis producer."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Settings(WithFpgaBoardSettings):
            """A board-aware implementation's settings."""

        class Inputs(Flow.Inputs):
            netlist: Path = In(
                SourceType.JsonNetlist, producer="t_synth", description="The netlist."
            )

        def run(self):
            pass

    return TBoardPlace


def test_board_expansion_reaches_a_producer_without_board_settings(tmp_path):
    cls = _board_place()
    root = tmp_path / "d"
    root.mkdir()
    (root / "boards.toml").write_text(f'[MY_BOARD]\nfpga.part = "{PART}"\n')
    plan = _plan(tmp_path, cls, {"board": "MY_BOARD", "custom_boards_file": "boards.toml"})
    assert all(n.settings.fpga.part == PART for n in plan.nodes)
    assert plan.node(cls.name).settings.custom_boards_file == root / "boards.toml"


@pytest.mark.parametrize(
    "values, message",
    [
        ({"board": "no_such_board"}, "Unknown board"),
        ({"fpga": {"part": True}}, "part"),
        ({"fpga": {"part": PART, "pins": "bad"}}, "pins"),
    ],
)
def test_invalid_boards_and_fpga_values_are_useful_errors(tmp_path, values, message):
    with pytest.raises(FlowSettingsError, match=message):
        _plan(tmp_path, _board_place(), values)


def test_board_part_mismatch_is_rejected_before_launch(tmp_path):
    cls = _board_place()
    root = tmp_path / "d"
    root.mkdir()
    (root / "boards.toml").write_text(f'[MY_BOARD]\nfpga.part = "{PART}"\n')
    with pytest.raises(FlowSettingsError, match=r"board.*fpga\.part.*disagree"):
        _plan(
            tmp_path,
            cls,
            {"board": "MY_BOARD", "custom_boards_file": "boards.toml", "fpga": {"part": OTHER}},
        )


def _board_edge():
    from xeda.board import WithFpgaBoardSettings
    from xeda.design import SourceType
    from xeda.flow import In, Out

    class TBoardMaker(Flow):
        """Board-aware producer for shared database contracts."""

        Settings = WithFpgaBoardSettings
        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Outputs(Flow.Outputs):
            made: Path = Out(SourceType.Data, description="The produced file.")

        def run(self):
            pass

    class TBoardTaker(Flow):
        """Board-aware consumer for shared database contracts."""

        Settings = WithFpgaBoardSettings
        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Inputs(Flow.Inputs):
            made: Path = In(SourceType.Data, producer="t_board_maker", description="The file.")

        def run(self):
            pass

    return TBoardTaker, TBoardMaker


def _device_relay():
    """A producer with a device, a relay that declares none, and a consumer that needs one: no
    edge of the graph shares `fpga` between the producer and the consumer."""
    from xeda.design import SourceType
    from xeda.flow import FpgaSynthFlow, In, Out

    class TDeviceMaker(FpgaSynthFlow):
        """Makes the file the relay passes on; its device is its own."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Outputs(FpgaSynthFlow.Outputs):
            made: Path = Out(SourceType.JsonNetlist, description="The produced file.")

        def run(self):
            pass

    class TDeviceRelay(Flow):
        """Passes the file on; it has no device."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Inputs(Flow.Inputs):
            made: Path = In(
                SourceType.JsonNetlist, producer="t_device_maker", description="The file."
            )

        class Outputs(Flow.Outputs):
            passed: Path = Out(SourceType.Data, description="The file, passed on.")

        def run(self):
            pass

    class TDeviceTaker(FpgaSynthFlow):
        """Reads the relay's file, and needs a device."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Inputs(FpgaSynthFlow.Inputs):
            passed: Path = In(SourceType.Data, producer="t_device_relay", description="The file.")

        def run(self):
            pass

    return TDeviceTaker, TDeviceMaker


def test_a_required_setting_given_where_no_edge_carries_it_is_named(tmp_path):
    taker, maker = _device_relay()
    with pytest.raises(FlowSettingsException, match=f"{taker.name} needs `fpga`") as raised:
        _plan(tmp_path, taker, {}, {maker.name: {"fpga": {"part": PART}}})
    assert (
        f"The `fpga` in `flows.{maker.name}` in the supplied flow sections does not reach "
        f"{taker.name}: no edge of this run carries `fpga` from {maker.name} to it"
        in str(raised.value)
    )


@pytest.mark.parametrize("placement", ["consumer", "producer", "split", "reverse_split"])
def test_custom_board_and_database_agree_before_lookup(tmp_path, placement):
    taker, maker = _board_edge()
    root = tmp_path / "d"
    root.mkdir()
    database = root / "boards.toml"
    database.write_text(f'[PRIVATE]\nfpga.part = "{PART}"\n')
    board = {"board": "PRIVATE"}
    custom = {"custom_boards_file": "boards.toml"}
    values = {
        "consumer": ({**board, **custom}, {}),
        "producer": ({}, {**board, **custom}),
        "split": (board, custom),
        "reverse_split": (custom, board),
    }
    consumer, producer = values[placement]
    plan = _plan(tmp_path, taker, consumer, {maker.name: producer})
    for node in plan.nodes:
        assert node.settings.board == "PRIVATE"
        assert node.settings.custom_boards_file == database
        assert node.settings.fpga.part == PART
        assert (
            node.settings.model_dump()
            == node.flow_class.Settings.from_input(node.settings.model_dump()).model_dump()
        )


@pytest.mark.parametrize("leaf", ["board", "custom_boards_file"])
def test_board_database_file_conflicts_and_cli_agreement(tmp_path, leaf):
    taker, maker = _board_edge()
    root = tmp_path / "d"
    root.mkdir()
    for filename in ("a.toml", "b.toml"):
        (root / filename).write_text(
            f'[PRIVATE]\nfpga.part = "{PART}"\n[OTHER]\nfpga.part = "{PART}"\n'
        )
    common = {"board": "PRIVATE", "custom_boards_file": "a.toml"}
    alternative = "OTHER" if leaf == "board" else "b.toml"
    files = {taker.name: common, maker.name: {**common, leaf: alternative}}
    with pytest.raises(FlowSettingsError) as raised:
        _plan(tmp_path, taker, origins=[("project.toml", files)])
    message = str(raised.value)
    assert leaf in message and "project.toml" in message
    assert taker.name in message and maker.name in message
    plan = _plan(
        tmp_path,
        taker,
        origins=[("project.toml", files)],
        command_line={taker.name: {leaf: alternative}},
    )
    for node in plan.nodes:
        assert getattr(node.settings, leaf) == (
            alternative if leaf == "board" else root / alternative
        )


def test_unknown_board_with_explicit_fpga_fails_after_database_agreement(tmp_path):
    taker, maker = _board_edge()
    root = tmp_path / "d"
    root.mkdir()
    (root / "boards.toml").write_text(f'[PRIVATE]\nfpga.part = "{PART}"\n')
    with pytest.raises(FlowSettingsError) as raised:
        _plan(
            tmp_path,
            taker,
            {"board": "PRIVAT", "fpga": {"part": PART}},
            {maker.name: {"custom_boards_file": "boards.toml"}},
        )
    message = str(raised.value)
    assert (
        "Unknown board" in message and "PRIVATE" in message and str(root / "boards.toml") in message
    )


def test_displaced_producer_skips_launch_checks_but_checks_syntax(tmp_path, caplog):
    import logging

    sources = [{"file": "net.json", "type": "JsonNetlist"}]
    caplog.set_level(logging.INFO)
    sections = {"t_synth": {"verbosity": "quiet"}}
    plan = _plan(tmp_path, TPlace, {"fpga": {"part": PART}}, sections=sections, sources=sources)
    assert [n.name for n in plan.nodes] == ["t_place"]
    assert "t_synth is not part of this run: its settings are unused" in caplog.text
    with pytest.raises(FlowSettingsError, match="unknown_setting"):
        _plan(
            tmp_path,
            TPlace,
            {"fpga": {"part": PART}},
            sections={"t_synth": {"unknown_setting": 1}},
            sources=sources,
        )


def test_optional_inputs_and_source_order(tmp_path):
    from xeda.design import SourceType
    from xeda.flow import In

    class TOptional(Flow):
        """Optional files and an ordered required collection."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Inputs(Flow.Inputs):
            absent: Path | None = In(SourceType.JsonNetlist, description="Optional netlist.")
            empty: list[Path] = In(
                SourceType.Bitstream, optional=True, description="Optional bitstreams."
            )
            data: list[Path] = In(SourceType.Data, description="Ordered data.")

        def run(self):
            pass

    sources = [{"file": "b.dat", "type": "Data"}, {"file": "a.dat", "type": "Data"}]
    plan = _plan(tmp_path, TOptional, sources=sources)
    absent, empty, data = plan.node(TOptional.name).inputs
    assert absent.origin == empty.origin == "none"
    assert data.sources == (tmp_path / "d/b.dat", tmp_path / "d/a.dat")
    with pytest.raises(FlowSettingsException, match="needs its input `data`"):
        _plan(tmp_path, TOptional)


@pytest.mark.parametrize(
    "producer, output, types, many, message",
    [
        ("no_such_producer", None, ("Data",), False, "unknown producer"),
        ("t_maker", "missing", ("Data",), False, "compatible output"),
        (None, "made", ("Data", "JsonNetlist"), False, "compatible output"),
        (None, "made", ("Data",), True, "produces many"),
    ],
)
def test_invalid_producer_output_contracts_fail_at_plan_time(
    tmp_path, producer, output, types, many, message
):
    from xeda.design import SourceType
    from xeda.flow import In, Out

    class TUnsafeMaker(Flow):
        """A producer whose outputs are checked for cardinality and full type coverage."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Outputs(Flow.Outputs):
            made: list[Path] if many else Path = Out(
                tuple(SourceType[t] for t in types), description="Produced files."
            )

        def run(self):
            pass

    class TUnsafeTaker(Flow):
        """Consumes Data only."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Inputs(Flow.Inputs):
            data: Path = In(
                SourceType.Data,
                producer=producer or TUnsafeMaker.name,
                output=output,
                description="Consumed data.",
            )

        def run(self):
            pass

    with pytest.raises(FlowSettingsException, match=message):
        _plan(tmp_path, TUnsafeTaker)


def test_a_cycle_is_reported_by_one_finite_walk(tmp_path):
    from xeda.design import SourceType
    from xeda.flow import In, Out

    class TCycleA(Flow):
        """First node of a declaration cycle."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Inputs(Flow.Inputs):
            data: Path = In(SourceType.Data, producer="t_cycle_b", description="B's data.")

        class Outputs(Flow.Outputs):
            data: Path = Out(SourceType.Data, description="A's data.")

        def run(self):
            pass

    class TCycleB(Flow):
        """Second node of a declaration cycle."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Inputs(Flow.Inputs):
            data: Path = In(SourceType.Data, producer="t_cycle_a", description="A's data.")

        class Outputs(Flow.Outputs):
            data: Path = Out(SourceType.Data, description="B's data.")

        def run(self):
            pass

    design = Design(name="d", root=tmp_path, rtl={"sources": [], "top": "t"})
    assert list(transitive_dependencies(TCycleA, design)) == ["t_cycle_b"]
    with pytest.raises(FlowSettingsException, match="t_cycle_a -> t_cycle_b -> t_cycle_a"):
        _plan(tmp_path, TCycleA)


def test_two_inputs_share_one_producer_and_union_enables(tmp_path):
    from xeda.dataclass import Field
    from xeda.design import SourceType
    from xeda.flow import In, Out

    class TTwoOutputs(Flow):
        """Two optional outputs controlled by independent flags."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Settings(Flow.Settings):
            first: bool = Field(False, description="Enable the first output.")
            second: bool = Field(False, description="Enable the second output.")

        class Outputs(Flow.Outputs):
            a: Path | None = Out(SourceType.Data, enabled_by="first", description="First output.")
            b: Path | None = Out(SourceType.Data, enabled_by="second", description="Second output.")

        def run(self):
            pass

    class TTwoInputs(Flow):
        """Two inputs from one configured producer."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Inputs(Flow.Inputs):
            a: Path = In(
                SourceType.Data, producer=TTwoOutputs.name, output="a", description="First input."
            )
            b: Path = In(
                SourceType.Data, producer=TTwoOutputs.name, output="b", description="Second input."
            )

        def run(self):
            pass

    plan = _plan(tmp_path, TTwoInputs)
    assert [n.name for n in plan.nodes] == [TTwoOutputs.name, TTwoInputs.name]
    made = plan.node(TTwoOutputs.name)
    assert made.settings.first and made.settings.second
    assert made.switched_on == ("a", "b")


def test_no_flow_constructor_run_or_tool_probe_is_used(tmp_path, monkeypatch):
    from xeda.tool import Tool

    def forbidden(*args, **kwargs):
        raise AssertionError("execution during planning")

    monkeypatch.setattr(Flow, "__init__", forbidden)
    monkeypatch.setattr(TMaker, "run", forbidden)
    monkeypatch.setattr(Tool, "probe_stdout", forbidden)
    plan = _plan(tmp_path, TTaker)
    assert len(plan.nodes) == 2 and not (tmp_path / "run").exists()


def test_validated_clock_models_keep_file_provenance(tmp_path):
    project = {"t_place": {"fpga": {"part": PART}, "clocks": {"main_clock": {"period": "4ns"}}}}
    design = {"t_synth": {"clocks": {"main_clock": {"freq": "200MHz"}}}}
    given = TPlace.Settings.from_input(compose_flow_settings(TPlace, [project, design]))
    with pytest.raises(FlowSettingsError) as raised:
        _plan(tmp_path, TPlace, given, origins=[("project.toml", project), ("design.toml", design)])
    message = str(raised.value)
    assert "project.toml" in message and "design.toml" in message
    assert "the API" not in message


def test_inconsistent_clock_pair_is_not_erased_by_agreement(tmp_path):
    with pytest.raises(FlowSettingsError, match="disagree"):
        _plan(
            tmp_path,
            TPlace,
            {
                "fpga": {"part": PART},
                "clocks": {"main_clock": {"period": "4 ns", "freq": "200 MHz"}},
            },
        )


def test_displaced_producer_without_nested_settings_still_validates_syntax(tmp_path):
    with pytest.raises(FlowSettingsError, match="unknown_setting"):
        _plan(
            tmp_path,
            TTaker,
            sources=[{"file": "given.dat", "type": "Data"}],
            sections={"t_maker": {"unknown_setting": True}},
        )


def test_command_line_sections_keep_the_p1_run_flow_check(tmp_path):
    with pytest.raises(FlowSettingsError, match="names no node of this run"):
        _plan(tmp_path, TTaker, command_line={"t_reader": {}})


def test_pure_support_hook_runs_only_on_effective_settings(tmp_path, monkeypatch):
    seen = []

    def supported(cls, settings):
        seen.append(settings.fpga.part)

    monkeypatch.setattr(TPlace, "check_settings_supported", classmethod(supported), raising=False)
    _plan(tmp_path, TPlace, sections={"t_synth": {"fpga": {"part": PART}}})
    assert seen == [PART]


def test_working_path_escape_is_rejected_and_deliveries_keep_hash_rules(tmp_path):
    from xeda.dataclass import WORKING, Field, deliverable
    from xeda.design import SourceType
    from xeda.flow import Out

    class TLocatedOutput(Flow):
        """A declared output with existing working/delivery roles."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Settings(Flow.Settings):
            scratch: Path = Field(
                Path("scratch"), description="Working directory.", json_schema_extra=WORKING
            )
            filename: Path = Field(
                Path("made.dat"),
                description="Delivered data.",
                json_schema_extra=deliverable(conventional="made.dat"),
            )

        class Outputs(Flow.Outputs):
            data: Path = Out(SourceType.Data, description="Produced data.")

        def run(self):
            pass

    with pytest.raises(FlowSettingsError, match="scratch"):
        _plan(tmp_path, TLocatedOutput, {"scratch": "../escape"})
    first = _plan(tmp_path, TLocatedOutput, {"filename": tmp_path / "first.dat"})
    second = _plan(tmp_path, TLocatedOutput, {"filename": tmp_path / "second.dat"})
    assert first.nodes[0].flowrun_hash == second.nodes[0].flowrun_hash
    assert first.context.input_settings["filename"] != second.context.input_settings["filename"]


@pytest.mark.parametrize(
    "values",
    [
        {"synth": "bad"},
        {"synth": False},
        {"clocks": "bad"},
        {"clocks": [1]},
        {"fpga": True},
        {"fpga": {"unknown": 1}},
    ],
)
def test_malformed_shared_and_nested_inputs_never_disappear_or_escape(tmp_path, values):
    with pytest.raises(FlowSettingsError):
        _plan(tmp_path, TPlace, {"fpga": {"part": PART}, **values})


def test_clock_model_defaults_are_not_new_explicit_contributions(tmp_path):
    values = {"fpga": {"part": PART}, "clocks": {"main_clock": {"period": "5 ns"}}}
    mapping = _plan(tmp_path, TPlace, values)
    model = _plan(tmp_path, TPlace, TPlace.Settings.from_input(values))
    assert [n.flowrun_hash for n in mapping.nodes] == [n.flowrun_hash for n in model.nodes]
    assert [n.settings for n in mapping.nodes] == [n.settings for n in model.nodes]


def test_synthesized_clock_name_does_not_override_an_explicit_name(tmp_path):
    sections = {
        "t_place": {"fpga": {"part": PART}, "clocks": {"main_clock": {"period": "5 ns"}}},
        "t_synth": {"clocks": {"main_clock": {"period": "5 ns", "name": "design_clock"}}},
    }
    given = compose_flow_settings(TPlace, [sections])
    plan = _plan(tmp_path, TPlace, given, origins=[("design.toml", sections)])
    assert all(n.settings.main_clock.name == "design_clock" for n in plan.nodes)


@pytest.mark.parametrize("model_input", [False, True])
def test_direct_entry_points_share_context_and_validation(tmp_path, monkeypatch, model_input):
    from xeda import DefaultRunner

    monkeypatch.chdir(tmp_path)
    root = tmp_path / "d"
    root.mkdir()
    # the same design as the plan's (`_plan`): its configuration is part of the identity
    design = Design(name="d", root=root, rtl={"sources": [], "top": "t", "clocks": ["clk"]})
    values = {"nthreads": 2, "lib_paths": [("work", "$DESIGN_ROOT/lib")], "text": "entry point\n"}
    given = (
        TMaker.Settings.from_input(values, design_root=root, runner_cwd=tmp_path)
        if model_input
        else values
    )
    plan = _plan(tmp_path, TMaker, given)
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    flows = [
        runner.run(TMaker, design=design, flow_settings=node_settings(TMaker, values)),
        runner.run_flow(TMaker, design, given),
        runner.launch_flow(TMaker, design, given),
    ]
    for flow in flows:
        assert flow is not None and flow.results.success
        assert flow.results.flow_hash == plan.node(TMaker.name).flowrun_hash
        assert flow.settings.nthreads == 2
        assert flow.settings.lib_paths == plan.node(TMaker.name).settings.lib_paths
    assert values["lib_paths"] == [("work", "$DESIGN_ROOT/lib")]


def test_displaced_producer_cannot_satisfy_consumer_required_setting(tmp_path):
    with pytest.raises(FlowSettingsException, match="t_place needs `fpga`"):
        _plan(
            tmp_path,
            TPlace,
            sections={"t_synth": {"fpga": {"part": PART}}},
            sources=[{"file": "net.json", "type": "JsonNetlist"}],
        )


@pytest.mark.parametrize("bad_clock", [5, "5ns"])
def test_bad_individual_clock_is_a_settings_error(tmp_path, bad_clock):
    with pytest.raises(FlowSettingsError):
        _plan(tmp_path, TPlace, {"fpga": {"part": PART}, "clocks": {"core": bad_clock}})


def test_api_can_clear_a_nullable_clock_leaf_across_the_edge(tmp_path):
    design = {
        "t_place": {"fpga": {"part": PART}},
        "t_synth": {
            "clocks": {"main_clock": {"period": "10 ns", "port": "clk", "uncertainty": "0.1 ns"}}
        },
    }
    api = {"t_place": {"clocks": {"main_clock": {"port": None, "uncertainty": None}}}}
    plan = _plan(tmp_path, TPlace, origins=[("design.toml", design)], api_overrides=api)
    for node in plan.nodes:
        assert node.settings.main_clock.port is None
        assert node.settings.main_clock.uncertainty is None


# ------------------------------------------------------------ the ASIC shared leaves


def _asic_taker():
    """A declared consumer of `yosys`'s netlist that shares its ASIC configuration (`platform`,
    `corner`, `dont_use_cells` and the PDK pin, through `AsicPlatformSettings`). No flow in the
    package takes such a netlist, so the test declares one."""
    from xeda.design import SourceType
    from xeda.flow import In
    from xeda.flow.asic import AsicPlatformFlow, AsicPlatformSettings

    class TAsicTaker(AsicPlatformFlow, Flow):
        """Reads yosys's gate-level netlist for one platform, as a place and route would."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Settings(AsicPlatformSettings):
            pass

        class Inputs(Flow.Inputs):
            netlist: Path = In(
                SourceType.VerilogNetlist,
                producer="yosys",
                output="netlist",
                description="The gate-level netlist.",
            )

        def run(self):
            pass

    return TAsicTaker


def _asic_plan(tmp_path, taker, **kwargs):
    root = tmp_path / "d"
    root.mkdir(parents=True, exist_ok=True)
    (root / "t.v").write_text("module t; endmodule\n")
    design = Design(name="d", root=root, rtl={"sources": ["t.v"], "top": "t"})
    return resolve(
        taker,
        design,
        kwargs.pop("settings", {}),
        kwargs.pop("sections", {}),
        runner_cwd=tmp_path,
        run_root=tmp_path / "run",
        hashed_run_dirs=False,
        run_path=lambda design_name, node, identity, target=None: tmp_path
        / "run"
        / design_name
        / node,
        **kwargs,
    )


#: A platform file with two corners, for the tests that give a corner.
TWO_CORNERS = """\
name: two
providers:
  default: directory
  directory: {layout: orfs}
corners: {default: typical}
files:
  liberty: {typical: [tiny.lib], SS: [tiny.lib]}
  tech_lef: tiny.tlef
  cell_lef: [tiny.lef]
"""


@pytest.mark.parametrize("leaf, value", [("corner", "SS"), ("dont_use_cells", "AND2_X2")])
def test_a_corner_or_dont_use_list_given_once_reaches_both(tmp_path, leaf, value):
    """`-s corner=SS` and `-s dont_use_cells=X`, given once, reach both flows."""
    taker = _asic_taker()
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "two.yaml").write_text(TWO_CORNERS)
    plan = _asic_plan(
        tmp_path,
        taker,
        origins=[("project.yaml", {taker.name: {"platform_file": "two.yaml"}})],
        command_line={taker.name: {leaf: value}},
    )
    for node in plan.nodes:
        given = getattr(node.settings, leaf)
        assert given == (value if leaf == "corner" else [value]), node.name
        assert node.settings.platform_file == tmp_path / "d" / "two.yaml", node.name


def test_a_dont_use_list_as_text_agrees_with_the_same_list(tmp_path):
    taker = _asic_taker()
    files = {
        taker.name: {"platform": "nangate45", "dont_use_cells": "A,B"},
        "yosys": {"dont_use_cells": ["A", "B"]},
    }
    plan = _asic_plan(tmp_path, taker, origins=[("project.yaml", files)])
    for node in plan.nodes:
        assert node.settings.dont_use_cells == ["A", "B"]
    files["yosys"]["dont_use_cells"] = ["A", "C"]
    with pytest.raises(FlowSettingsError, match="dont_use_cells"):
        _asic_plan(tmp_path / "conflict", taker, origins=[("project.yaml", files)])
