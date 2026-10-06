"""The resolver turns a launch into one immutable plan before anything runs: each declared
input's origin (a design source of an accepted type, else the default producer),
the outputs a consumer switches on, every node's effective settings -- a shared setting
agreeing along every edge: given in one place it reaches every connected flow, given
differently it is an error naming both places, and the command line wins for the whole run --
and every node's identity."""

import dataclasses
from pathlib import Path
from typing import ClassVar

import pytest

import xeda
from xeda import Design
from xeda.flow import Flow, FlowSettingsError, FlowSettingsException, flowrun_hash
from xeda.flow_runner.bindings import node_identity
from xeda.flow_runner.resolver import resolve
from xeda.flow_runner.settings_layers import compose_flow_settings, transitive_dependencies

from .io_flows import _Maker, _Place, _Reader, _Taker, _Wrapper

PART = "LFE5U-25F-6BG256C"
OTHER = "LFE5U-85F-6BG381C"


def _plan(tmp_path: Path, flow_cls, settings=None, sections=None, sources=(), **kwargs):
    root = tmp_path / "d"
    root.mkdir(exist_ok=True)
    for source in sources:
        (root / source["file"]).write_text("x\n")
    design = Design(name="d", design_root=root, rtl={"sources": list(sources), "top": "t"})
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
    plan = _plan(tmp_path, _Taker)
    assert plan.requested == "__taker"
    assert [node.name for node in plan.nodes] == ["__maker", "__taker"]
    (made,) = plan.node("__taker").inputs
    assert (made.origin, made.producer, made.output) == ("producer", "__maker", "made")
    assert made.describe() == "made <- __maker.made"
    assert plan.node("__taker").declared


def test_a_design_source_of_an_accepted_type_replaces_the_default_producer(tmp_path):
    plan = _plan(tmp_path, _Taker, sources=[{"file": "given.dat", "type": "Data"}])
    assert [node.name for node in plan.nodes] == ["__taker"]
    (made,) = plan.node("__taker").inputs
    assert made.origin == "source" and made.sources == (tmp_path / "d" / "given.dat",)


def test_two_sources_for_a_singular_input_are_an_error_naming_them(tmp_path):
    sources = [{"file": "a.dat", "type": "Data"}, {"file": "b.dat", "type": "Data"}]
    with pytest.raises(FlowSettingsException, match="takes one Data file") as raised:
        _plan(tmp_path, _Taker, sources=sources)
    assert "a.dat" in str(raised.value) and "b.dat" in str(raised.value)


def test_a_required_input_nothing_supplies_is_an_error_naming_it(tmp_path):
    with pytest.raises(FlowSettingsException, match=r"__reader needs its input `data`.*Data"):
        _plan(tmp_path, _Reader)


def test_an_output_a_consumer_needs_is_switched_on(tmp_path):
    plan = _plan(tmp_path, _Taker, sections={"__maker": {"write": False}})
    maker = plan.node("__maker")
    assert maker.settings.write is True and maker.switched_on == ("made",)


def test_an_undeclared_flow_is_one_node(tmp_path):
    plan = _plan(tmp_path, _Wrapper)
    (node,) = plan.nodes
    assert node.name == "__wrapper" and not node.declared and node.inputs == ()


def test_each_node_s_identity_is_what_the_launcher_hashes(tmp_path):
    plan = _plan(tmp_path, _Taker, sections={"__maker": {"text": "other\n"}})
    maker, taker = plan.nodes
    assert maker.origins == () and taker.origins == (("made", ((maker.flowrun_hash, "made"),)),)
    for node in plan.nodes:
        assert node.settings_hash == flowrun_hash(node.name, node.settings, "d")
        assert node.flowrun_hash == node_identity(node.settings_hash, node.origins)
        assert node.run_path == tmp_path / "run" / "d" / node.name


def test_the_plan_is_immutable(tmp_path):
    plan = _plan(tmp_path, _Taker)
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.requested = "other"  # type: ignore[misc]
    saved = plan.node("__maker").settings.model_dump()
    exposed = plan.node("__maker").settings
    exposed.text = "mutated"
    assert plan.node("__maker").settings.model_dump() == saved


def test_a_shared_setting_given_for_the_consumer_reaches_its_producer(tmp_path):
    plan = _plan(tmp_path, _Place, {"fpga": {"part": PART}, "clock_period": 10.0})
    for name in ("__synth", "__place"):
        settings = plan.node(name).settings
        assert settings.fpga.part == PART and settings.clocks["main_clock"].period == 10.0


def test_a_shared_setting_given_only_for_the_producer_reaches_its_consumer(tmp_path):
    sections = {"__synth": {"fpga": {"part": PART}}}
    plan = _plan(tmp_path, _Place, compose_flow_settings(_Place, [sections]), sections)
    assert plan.node("__place").settings.fpga.part == PART  # and so its required `fpga` is met


def test_leaves_given_in_one_place_each_combine(tmp_path):
    sections = {"__synth": {"fpga": {"family": "ecp5"}}}
    settings = compose_flow_settings(_Place, [sections], {"fpga": {"part": PART}})
    plan = _plan(tmp_path, _Place, settings, sections)
    assert plan.node("__synth").settings.fpga.part == PART
    assert plan.node("__synth").settings.fpga.family == "ecp5"


def test_conflicting_values_are_an_error_naming_both_places(tmp_path):
    """A project file's `[flows.nextpnr] fpga` must not silently win over a design file's
    `[flows.yosys_fpga] fpga`."""
    project = {"__place": {"fpga": {"part": OTHER}}}
    design = {"__synth": {"fpga": {"part": PART}}}
    settings = compose_flow_settings(_Place, [project, design])
    with pytest.raises(FlowSettingsError) as raised:
        _plan(
            tmp_path,
            _Place,
            settings,
            {**project, **design},
            origins=[("the project file", project), ("the design file", design)],
        )
    message = str(raised.value)
    for text in (
        PART,
        OTHER,
        "fpga.part",
        "[flows.__place] in the project file",
        "[flows.__synth] in the design file",
    ):
        assert text in message, text


def test_a_value_given_on_the_command_line_is_the_whole_run_s(tmp_path):
    design = {"__synth": {"fpga": {"part": PART}}}
    command_line = {"__place": {"fpga": {"part": OTHER}}}
    settings = compose_flow_settings(_Place, [design], command_line["__place"])
    plan = _plan(
        tmp_path,
        _Place,
        settings,
        design,
        origins=[("the design file", design)],
        command_line=command_line,
    )
    assert plan.node("__synth").settings.fpga.part == OTHER
    assert plan.node("__place").settings.fpga.part == OTHER


def test_two_different_command_line_values_are_an_error(tmp_path):
    command_line = {"__place": {"fpga": {"part": OTHER}}, "__synth": {"fpga": {"part": PART}}}
    settings = compose_flow_settings(
        _Place, [{"__synth": command_line["__synth"]}], command_line["__place"]
    )
    with pytest.raises(FlowSettingsError, match="the command line"):
        _plan(
            tmp_path,
            _Place,
            settings,
            {"__synth": command_line["__synth"]},
            origins=[("the command line", {"__synth": command_line["__synth"]})],
            command_line=command_line,
        )


def test_a_missing_required_setting_names_the_requested_flow_first(tmp_path):
    with pytest.raises(FlowSettingsException, match=r"^__place needs `fpga`"):
        _plan(tmp_path, _Place)


def test_a_declared_producer_is_a_flow_the_command_line_may_address():
    assert set(transitive_dependencies(_Taker)) == {"__maker"}
    assert set(transitive_dependencies(_Place)) == {"__synth"}
    assert _Maker.name in transitive_dependencies(_Taker)


@pytest.fixture(autouse=True)
def private_registry():
    """Locally defined contract probes do not leak into other registry sweeps."""
    from xeda.flow import registered_flows

    saved = dict(registered_flows)
    yield
    registered_flows.clear()
    registered_flows.update(saved)


def test_section_locations_and_origin_precedence_are_retained(tmp_path):
    project = {"__place": {"fpga": {"part": OTHER}}}
    design = {"__synth": {"fpga": {"part": PART}}}
    with pytest.raises(FlowSettingsError) as raised:
        _plan(
            tmp_path,
            _Place,
            compose_flow_settings(_Place, [project, design]),
            origins=[("project /p/project.toml", project), ("design /d/design.toml", design)],
        )
    message = str(raised.value)
    for text in ("/p/project.toml", "/d/design.toml", "__place", "__synth", PART, OTHER):
        assert text in message


@pytest.mark.parametrize(
    "cli_clock",
    [{"clock_period": 4}, {"clock": {"freq": "250MHz"}}, {"clocks": {"core": {"freq": 250}}}],
)
def test_cli_clock_leaf_preserves_other_clocks_and_attributes(tmp_path, cli_clock):
    design = {
        "__place": {
            "fpga": PART,
            "clocks": {
                "core": {"period": 10, "port": "clk", "uncertainty": "100ps"},
                "aux": {"period": 20, "port": "aux_clk"},
            },
        }
    }
    cli = {"__place": cli_clock}
    given = compose_flow_settings(_Place, [design], cli_clock)
    plan = _plan(tmp_path, _Place, given, origins=[("design.toml", design)], command_line=cli)
    for node in plan.nodes:
        clocks = node.settings.clocks
        assert list(clocks) == ["core", "aux"]
        assert clocks["core"].period == 4
        assert clocks["core"].port == "clk" and clocks["core"].uncertainty == pytest.approx(0.1)
        assert clocks["aux"].period == 20 and clocks["aux"].port == "aux_clk"


def test_disjoint_cli_leaves_combine_across_nodes(tmp_path):
    design = {"__place": {"fpga": PART, "clock": {"period": 10, "port": "clk"}}}
    cli = {
        "__place": {"clock": {"freq": 250}},
        "__synth": {"clocks": {"main_clock": {"uncertainty": "100ps"}}},
    }
    given = compose_flow_settings(_Place, [design, cli])
    plan = _plan(tmp_path, _Place, given, origins=[("design.toml", design)], command_line=cli)
    for node in plan.nodes:
        assert node.settings.clock.period == 4
        assert node.settings.clock.uncertainty == pytest.approx(0.1)
        assert node.settings.clock.port == "clk"


def test_conflicting_cli_clock_aliases_are_an_error(tmp_path):
    cli = {"__place": {"fpga": PART, "clock_period": 4}, "__synth": {"clock": {"freq": 200}}}
    with pytest.raises(FlowSettingsError, match="the command line"):
        _plan(tmp_path, _Place, compose_flow_settings(_Place, [cli]), command_line=cli)


def test_equivalent_frequency_and_period_agree(tmp_path):
    sections = {
        "__place": {"fpga": PART, "clock_period": "5ns"},
        "__synth": {"clock": {"freq": "200MHz"}},
    }
    plan = _plan(tmp_path, _Place, compose_flow_settings(_Place, [sections]), sections)
    assert all(node.settings.clock.period == 5 for node in plan.nodes)


def test_api_overrides_have_their_own_highest_precedence(tmp_path):
    design = {"__place": {"fpga": PART}}
    cli = {"__place": {"fpga": OTHER}}
    api = {"__synth": {"fpga": PART}}
    plan = _plan(
        tmp_path,
        _Place,
        compose_flow_settings(_Place, [design, cli, api]),
        origins=[("design.toml", design)],
        command_line=cli,
        api_overrides=api,
    )
    assert all(node.settings.fpga.part == PART for node in plan.nodes)
    with pytest.raises(FlowSettingsError, match="the API") as raised:
        _plan(
            tmp_path, _Place, api_overrides={"__place": {"fpga": PART}, "__synth": {"fpga": OTHER}}
        )
    assert "command line" not in str(raised.value)


def test_mappings_and_settings_agree_on_aliases(tmp_path):
    values = {"fpga": PART, "ncpus": 2}
    mapping = _plan(tmp_path, _Place, values)
    model = _Place.Settings.from_input(values, design_root=tmp_path / "d", runner_cwd=tmp_path)
    instance = _plan(tmp_path, _Place, model)
    assert [n.settings for n in mapping.nodes] == [n.settings for n in instance.nodes]
    assert [n.flowrun_hash for n in mapping.nodes] == [n.flowrun_hash for n in instance.nodes]


def test_deep_plan_protection_and_resolution_purity(tmp_path):
    from xeda import Design

    root = tmp_path / "d"
    root.mkdir()
    design = Design(name="d", design_root=root, rtl={"sources": [], "top": "t"})
    sections = {"__place": {"fpga": PART}, "__synth": {"lib_paths": [("work", "lib")]}}
    given = _Place.Settings.from_input(
        compose_flow_settings(_Place, [sections]), design_root=root, runner_cwd=tmp_path
    )
    initial = given.model_dump()
    design_state = design.model_dump()
    plan = resolve(
        _Place,
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
    exposed = plan.node("__synth").settings
    exposed.lib_paths.append(("other", Path("other")))
    exposed.fpga.part = OTHER
    given.fpga.part = OTHER
    sections["__place"]["fpga"] = OTHER
    assert [(n.settings.model_dump(), n.flowrun_hash, n.run_path) for n in plan.nodes] == saved
    assert design.model_dump() == design_state
    assert initial["fpga"]["part"] == PART
    assert plan.context.design_root == root and plan.context.runner_cwd == tmp_path
    assert plan.context.hashed_run_dirs and plan.context.debug
    with pytest.raises(TypeError):
        plan.context.input_settings["fpga"]["part"] = "bad"
    assert not (tmp_path / "run").exists()
    assert all(node.settings.debug for node in plan.nodes)


def _board_place():
    from xeda.board import WithFpgaBoardSettings
    from xeda.design import SourceType
    from xeda.flow import FpgaSynthFlow, In

    class _BoardPlace(FpgaSynthFlow):
        """A board-aware consumer of the same test synthesis producer."""

        results_description: ClassVar[dict[str, str]] = {}

        class Settings(WithFpgaBoardSettings):
            """A board-aware implementation's settings."""

        class Inputs(Flow.Inputs):
            netlist: Path = In(
                SourceType.JsonNetlist, producer="__synth", description="The netlist."
            )

        def run(self):
            pass

    return _BoardPlace


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
            tmp_path, cls, {"board": "MY_BOARD", "custom_boards_file": "boards.toml", "fpga": OTHER}
        )


def _board_edge():
    from xeda.board import WithFpgaBoardSettings
    from xeda.design import SourceType
    from xeda.flow import In, Out

    class _BoardMaker(Flow):
        """Board-aware producer for shared database contracts."""

        Settings = WithFpgaBoardSettings
        results_description: ClassVar[dict[str, str]] = {}

        class Outputs(Flow.Outputs):
            made: Path = Out(SourceType.Data, description="The produced file.")

        def run(self):
            pass

    class _BoardTaker(Flow):
        """Board-aware consumer for shared database contracts."""

        Settings = WithFpgaBoardSettings
        results_description: ClassVar[dict[str, str]] = {}

        class Inputs(Flow.Inputs):
            made: Path = In(SourceType.Data, producer="__board_maker", description="The file.")

        def run(self):
            pass

    return _BoardTaker, _BoardMaker


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
            {"board": "PRIVAT", "fpga": PART},
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
    plan = _plan(tmp_path, _Place, {"fpga": PART}, sections={"__synth": {}}, sources=sources)
    assert [n.name for n in plan.nodes] == ["__place"]
    assert "unused" in caplog.text
    with pytest.raises(FlowSettingsError, match="unknown_setting"):
        _plan(
            tmp_path,
            _Place,
            {"fpga": PART},
            sections={"__synth": {"unknown_setting": 1}},
            sources=sources,
        )


def test_optional_inputs_and_source_order(tmp_path):
    from xeda.design import SourceType
    from xeda.flow import In

    class _Optional(Flow):
        """Optional files and an ordered required collection."""

        results_description: ClassVar[dict[str, str]] = {}

        class Inputs(Flow.Inputs):
            absent: Path | None = In(SourceType.JsonNetlist, description="Optional netlist.")
            empty: list[Path] = In(
                SourceType.Bitstream, optional=True, description="Optional bitstreams."
            )
            data: list[Path] = In(SourceType.Data, description="Ordered data.")

        def run(self):
            pass

    sources = [{"file": "b.dat", "type": "Data"}, {"file": "a.dat", "type": "Data"}]
    plan = _plan(tmp_path, _Optional, sources=sources)
    absent, empty, data = plan.node(_Optional.name).inputs
    assert absent.origin == empty.origin == "none"
    assert data.sources == (tmp_path / "d/b.dat", tmp_path / "d/a.dat")
    with pytest.raises(FlowSettingsException, match="needs its input `data`"):
        _plan(tmp_path, _Optional)


@pytest.mark.parametrize(
    "producer, output, types, many, message",
    [
        ("no_such_producer", None, ("Data",), False, "unknown producer"),
        ("__maker", "missing", ("Data",), False, "compatible output"),
        (None, "made", ("Data", "JsonNetlist"), False, "compatible output"),
        (None, "made", ("Data",), True, "produces many"),
    ],
)
def test_invalid_producer_output_contracts_fail_at_plan_time(
    tmp_path, producer, output, types, many, message
):
    from xeda.design import SourceType
    from xeda.flow import In, Out

    class _UnsafeMaker(Flow):
        """A producer used to check cardinality and full type coverage."""

        results_description: ClassVar[dict[str, str]] = {}

        class Outputs(Flow.Outputs):
            made: list[Path] if many else Path = Out(
                tuple(SourceType[t] for t in types), description="Produced files."
            )

        def run(self):
            pass

    class _UnsafeTaker(Flow):
        """Consumes Data only."""

        results_description: ClassVar[dict[str, str]] = {}

        class Inputs(Flow.Inputs):
            data: Path = In(
                SourceType.Data,
                producer=producer or _UnsafeMaker.name,
                output=output,
                description="Consumed data.",
            )

        def run(self):
            pass

    with pytest.raises(FlowSettingsException, match=message):
        _plan(tmp_path, _UnsafeTaker)


def test_aliases_resolve_to_canonical_producers(tmp_path):
    from xeda.design import SourceType
    from xeda.flow import In

    class _AliasTaker(Flow):
        """Consumes a producer addressed by its class name alias."""

        results_description: ClassVar[dict[str, str]] = {}

        class Inputs(Flow.Inputs):
            data: Path = In(SourceType.Data, producer="_Maker", description="The produced data.")

        def run(self):
            pass

    assert _Maker.name in transitive_dependencies(_AliasTaker)
    plan = _plan(tmp_path, _AliasTaker, sections={"_Maker": {"text": "alias"}})
    assert plan.node(_Maker.name).settings.text == "alias"
    assert plan.node(_AliasTaker.name).inputs[0].producer == _Maker.name


def test_a_cycle_is_reported_by_one_finite_walk(tmp_path):
    from xeda.design import SourceType
    from xeda.flow import In, Out

    class _CycleA(Flow):
        """First node of a declaration cycle."""

        results_description: ClassVar[dict[str, str]] = {}

        class Inputs(Flow.Inputs):
            data: Path = In(SourceType.Data, producer="__cycle_b", description="B's data.")

        class Outputs(Flow.Outputs):
            data: Path = Out(SourceType.Data, description="A's data.")

        def run(self):
            pass

    class _CycleB(Flow):
        """Second node of a declaration cycle."""

        results_description: ClassVar[dict[str, str]] = {}

        class Inputs(Flow.Inputs):
            data: Path = In(SourceType.Data, producer="__cycle_a", description="A's data.")

        class Outputs(Flow.Outputs):
            data: Path = Out(SourceType.Data, description="B's data.")

        def run(self):
            pass

    assert list(transitive_dependencies(_CycleA)) == ["__cycle_b"]
    with pytest.raises(FlowSettingsException, match="__cycle_a -> __cycle_b -> __cycle_a"):
        _plan(tmp_path, _CycleA)


def test_two_inputs_share_one_producer_and_union_enables(tmp_path):
    from xeda.dataclass import Field
    from xeda.design import SourceType
    from xeda.flow import In, Out

    class _TwoOutputs(Flow):
        """Two optional outputs controlled by independent flags."""

        results_description: ClassVar[dict[str, str]] = {}

        class Settings(Flow.Settings):
            first: bool = Field(False, description="Enable the first output.")
            second: bool = Field(False, description="Enable the second output.")

        class Outputs(Flow.Outputs):
            a: Path | None = Out(SourceType.Data, enabled_by="first", description="First output.")
            b: Path | None = Out(SourceType.Data, enabled_by="second", description="Second output.")

        def run(self):
            pass

    class _TwoInputs(Flow):
        """Two inputs from one configured producer."""

        results_description: ClassVar[dict[str, str]] = {}

        class Inputs(Flow.Inputs):
            a: Path = In(
                SourceType.Data, producer=_TwoOutputs.name, output="a", description="First input."
            )
            b: Path = In(
                SourceType.Data, producer=_TwoOutputs.name, output="b", description="Second input."
            )

        def run(self):
            pass

    plan = _plan(tmp_path, _TwoInputs)
    assert [n.name for n in plan.nodes] == [_TwoOutputs.name, _TwoInputs.name]
    made = plan.node(_TwoOutputs.name)
    assert made.settings.first and made.settings.second
    assert made.switched_on == ("a", "b")


def test_no_flow_constructor_run_or_tool_probe_is_used(tmp_path, monkeypatch):
    from xeda.tool import Tool

    def forbidden(*args, **kwargs):
        raise AssertionError("execution during planning")

    monkeypatch.setattr(Flow, "__init__", forbidden)
    monkeypatch.setattr(_Maker, "run", forbidden)
    monkeypatch.setattr(Tool, "probe_stdout", forbidden)
    plan = _plan(tmp_path, _Taker)
    assert len(plan.nodes) == 2 and not (tmp_path / "run").exists()


def test_validated_clock_models_keep_file_provenance(tmp_path):
    project = {"__place": {"fpga": PART, "clock": {"period": "4ns"}}}
    design = {"__synth": {"clock": {"freq": "200MHz"}}}
    given = _Place.Settings.from_input(compose_flow_settings(_Place, [project, design]))
    with pytest.raises(FlowSettingsError) as raised:
        _plan(tmp_path, _Place, given, origins=[("project.toml", project), ("design.toml", design)])
    message = str(raised.value)
    assert "project.toml" in message and "design.toml" in message
    assert "the API" not in message


def test_inconsistent_clock_pair_is_not_erased_by_agreement(tmp_path):
    with pytest.raises(FlowSettingsError, match="disagree"):
        _plan(tmp_path, _Place, {"fpga": PART, "clock": {"period": 4, "freq": 200}})


def test_displaced_producer_without_nested_settings_still_validates_syntax(tmp_path):
    with pytest.raises(FlowSettingsError, match="unknown_setting"):
        _plan(
            tmp_path,
            _Taker,
            sources=[{"file": "given.dat", "type": "Data"}],
            sections={"__maker": {"unknown_setting": True}},
        )


def test_command_line_sections_keep_the_p1_run_flow_check(tmp_path):
    with pytest.raises(FlowSettingsError, match="no flow of this run"):
        _plan(tmp_path, _Taker, command_line={"__reader": {}})


def test_pure_support_hook_runs_only_on_effective_settings(tmp_path, monkeypatch):
    seen = []

    def supported(cls, settings):
        seen.append(settings.fpga.part)

    monkeypatch.setattr(_Place, "check_settings_supported", classmethod(supported), raising=False)
    _plan(tmp_path, _Place, sections={"__synth": {"fpga": PART}})
    assert seen == [PART]


def test_working_path_escape_is_rejected_and_deliveries_keep_hash_rules(tmp_path):
    from xeda.dataclass import WORKING, Field, deliverable
    from xeda.design import SourceType
    from xeda.flow import Out

    class _LocatedOutput(Flow):
        """A declared output with existing working/delivery roles."""

        results_description: ClassVar[dict[str, str]] = {}

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
        _plan(tmp_path, _LocatedOutput, {"scratch": "../escape"})
    first = _plan(tmp_path, _LocatedOutput, {"filename": tmp_path / "first.dat"})
    second = _plan(tmp_path, _LocatedOutput, {"filename": tmp_path / "second.dat"})
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
        _plan(tmp_path, _Place, {"fpga": PART, **values})


def test_clock_model_defaults_are_not_new_explicit_contributions(tmp_path):
    values = {"fpga": PART, "clock_period": 5}
    mapping = _plan(tmp_path, _Place, values)
    model = _plan(tmp_path, _Place, _Place.Settings.from_input(values))
    assert [n.flowrun_hash for n in mapping.nodes] == [n.flowrun_hash for n in model.nodes]
    assert [n.settings for n in mapping.nodes] == [n.settings for n in model.nodes]


def test_synthesized_clock_name_does_not_override_an_explicit_name(tmp_path):
    sections = {
        "__place": {"fpga": PART, "clock_period": 5},
        "__synth": {"clocks": {"main_clock": {"period": 5, "name": "design_clock"}}},
    }
    given = compose_flow_settings(_Place, [sections])
    plan = _plan(tmp_path, _Place, given, origins=[("design.toml", sections)])
    assert all(n.settings.clock.name == "design_clock" for n in plan.nodes)


@pytest.mark.parametrize("model_input", [False, True])
def test_direct_entry_points_share_context_and_alias_validation(tmp_path, monkeypatch, model_input):
    from xeda import DefaultRunner

    monkeypatch.chdir(tmp_path)
    root = tmp_path / "d"
    root.mkdir()
    design = Design(name="d", design_root=root, rtl={"sources": [], "top": "t"})
    values = {"ncpus": 2, "lib_paths": [("work", "$DESIGN_ROOT/lib")], "text": "entry point\n"}
    given = (
        _Maker.Settings.from_input(values, design_root=root, runner_cwd=tmp_path)
        if model_input
        else values
    )
    plan = _plan(tmp_path, _Maker, given)
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    flows = [
        runner.run(_Maker, design=design, flow_settings=values),
        runner.run_flow(_Maker, design, given),
        runner.launch_flow(_Maker, design, given),
    ]
    for flow in flows:
        assert flow is not None and flow.results.success
        assert flow.results.flow_hash == plan.node(_Maker.name).flowrun_hash
        assert flow.settings.nthreads == 2
        assert flow.settings.lib_paths == plan.node(_Maker.name).settings.lib_paths
    assert values["lib_paths"] == [("work", "$DESIGN_ROOT/lib")]


def test_cli_clock_rename_preserves_file_leaves(tmp_path):
    design = {
        "__place": {
            "fpga": PART,
            "clocks": {"core": {"period": 10, "port": "clk", "uncertainty": "100ps"}},
        }
    }
    cli = {"__place": {"clock": {"name": "renamed", "period": 4}}}
    given = compose_flow_settings(_Place, [design], cli["__place"])
    plan = _plan(tmp_path, _Place, given, origins=[("design.toml", design)], command_line=cli)
    for node in plan.nodes:
        assert node.settings.clocks["renamed"].port == "clk"
        assert node.settings.clocks["renamed"].uncertainty == pytest.approx(0.1)


def test_cli_shorthand_targets_named_clock_supplied_by_producer(tmp_path):
    design = {
        "__place": {"fpga": PART},
        "__synth": {"clocks": {"core": {"period": 10, "port": "clk"}}},
    }
    cli = {"__synth": {"clock_period": 4}}
    plan = _plan(tmp_path, _Place, origins=[("design.toml", design)], command_line=cli)
    for node in plan.nodes:
        assert list(node.settings.clocks) == ["core"]
        assert node.settings.clocks["core"].period == 4
        assert node.settings.clocks["core"].port == "clk"


def test_displaced_producer_cannot_satisfy_consumer_required_setting(tmp_path):
    with pytest.raises(FlowSettingsException, match="__place needs `fpga`"):
        _plan(
            tmp_path,
            _Place,
            sections={"__synth": {"fpga": PART}},
            sources=[{"file": "net.json", "type": "JsonNetlist"}],
        )


@pytest.mark.parametrize("bad_clock", [5, "5ns"])
def test_bad_individual_clock_is_a_settings_error(tmp_path, bad_clock):
    with pytest.raises(FlowSettingsError):
        _plan(tmp_path, _Place, {"fpga": PART, "clocks": {"core": bad_clock}})


def test_api_can_clear_a_nullable_clock_leaf_across_the_edge(tmp_path):
    design = {
        "__place": {"fpga": PART},
        "__synth": {"clocks": {"main_clock": {"period": 10, "port": "clk", "uncertainty": 0.1}}},
    }
    api = {"__place": {"clocks": {"main_clock": {"port": None, "uncertainty": None}}}}
    plan = _plan(tmp_path, _Place, origins=[("design.toml", design)], api_overrides=api)
    for node in plan.nodes:
        assert node.settings.clock.port is None
        assert node.settings.clock.uncertainty is None


# ------------------------------------------------------------ the ASIC shared leaves


def _asic_taker():
    """A declared consumer of `yosys`'s netlist that shares its ASIC configuration -- `platform`,
    `corner` and `dont_use_cells` with yosys's own types -- as `openroad` will once it declares
    its input. Today it is the only declared edge on which the three can agree."""
    from typing import List, Optional, Union

    from xeda.dataclass import Field, field_validator
    from xeda.design import SourceType
    from xeda.flow import In
    from xeda.flows import Yosys
    from xeda.platforms import AsicsPlatform

    fields = Yosys.Settings.model_fields

    class _AsicTaker(Flow):
        """Reads yosys's gate-level netlist for one platform, as a place and route would."""

        results_description: ClassVar[dict[str, str]] = {}

        class Settings(Flow.Settings):
            platform: Optional[AsicsPlatform] = Field(
                None, description=fields["platform"].description
            )
            corner: Optional[Union[str, List]] = Field(
                None, description=fields["corner"].description
            )
            dont_use_cells: List[str] = Field([], description="Cells not to use.")

            @field_validator("platform", mode="before")
            @classmethod
            def _platform(cls, value):
                return AsicsPlatform.from_setting(value)

        class Inputs(Flow.Inputs):
            netlist: Path = In(
                SourceType.VerilogNetlist,
                producer="yosys",
                output="netlist",
                description="The gate-level netlist.",
            )

        def run(self):
            pass

    return _AsicTaker


def _asic_plan(tmp_path, taker, **kwargs):
    root = tmp_path / "d"
    root.mkdir(parents=True, exist_ok=True)
    (root / "t.v").write_text("module t; endmodule\n")
    design = Design(name="d", design_root=root, rtl={"sources": ["t.v"], "top": "t"})
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


# Where the installed xeda keeps its bundled platforms: a non-editable install (tox) is not the
# checkout's `src/xeda`, and `-s platform=nangate45` resolves inside the one that is imported.
BUNDLED_NANGATE45 = Path(xeda.__file__).parent / "platforms/nangate45/config.toml"


def test_every_bundled_platform_is_named_as_its_directory():
    """`-s platform=<name>` and the path to that platform's `config.toml` are one model only if
    the file names the platform as `from_resource` does: a `config.toml` without a `name`
    validated to `name = None` by its path, and the merged library to `None_merged`."""
    from xeda.platforms import AsicsPlatform
    from xeda.platforms.platform import bundled_platform_names

    for name in bundled_platform_names():
        config = BUNDLED_NANGATE45.parent.parent / name / "config.toml"
        assert AsicsPlatform.from_setting(str(config)).name == name


def test_two_spellings_of_one_platform_agree_on_one_yosys(tmp_path):
    """A bundled name on one node and the path to that bundled `config.toml` on the
    other are one platform: one plan, one yosys identity, one run."""
    from xeda.platforms import AsicsPlatform

    taker = _asic_taker()
    by_name = {"platform": "nangate45"}
    by_path = {"platform": str(BUNDLED_NANGATE45)}
    mixed = _asic_plan(
        tmp_path / "mixed",
        taker,
        origins=[("project.yaml", {taker.name: by_name, "yosys": by_path})],
    )
    named = _asic_plan(
        tmp_path / "named",
        taker,
        origins=[("project.yaml", {taker.name: by_name, "yosys": by_name})],
    )
    assert [node.name for node in mixed.nodes] == ["yosys", taker.name]
    expected = AsicsPlatform.from_resource("nangate45").model_dump()
    for node in mixed.nodes:
        assert node.settings.platform.model_dump() == expected
    assert mixed.node("yosys").flowrun_hash == named.node("yosys").flowrun_hash


def test_two_platforms_that_share_a_name_and_a_root_conflict(tmp_path):
    """Two `config.toml` files in one directory with one `name` are two platforms;
    the error names both nodes and both origins."""
    root = tmp_path / "pdk"
    root.mkdir(parents=True)
    for config, load in (("a.toml", 1.0), ("b.toml", 2.0)):
        (root / config).write_text(
            f'name = "pdk"\nsc_lef = "x.lef"\nabc_load_in_ff = {load}\nlib_files = ["x.lib"]\n'
        )
    taker = _asic_taker()
    files = {
        taker.name: {"platform": str(root / "a.toml")},
        "yosys": {"platform": str(root / "b.toml")},
    }
    with pytest.raises(FlowSettingsError) as raised:
        _asic_plan(tmp_path, taker, origins=[("project.yaml", files)])
    message = str(raised.value)
    assert "platform" in message and "project.yaml" in message
    assert taker.name in message and "yosys" in message


def test_a_mapping_platform_is_propagated_whole_and_valid(tmp_path):
    """The propagated value is a valid `platform` input and each node's model is
    complete; a mapping is one value, never merged key by key with another node's."""
    from xeda.platforms import AsicsPlatform

    taker = _asic_taker()
    bundled = AsicsPlatform.from_resource("nangate45")
    mapping = bundled.model_dump(exclude={"voltage_expressions_"})
    plan = _asic_plan(
        tmp_path, taker, origins=[("project.yaml", {taker.name: {"platform": mapping}})]
    )
    for node in plan.nodes:
        dumped = node.settings.model_dump()["platform"]
        assert (
            dumped
            == node.flow_class.Settings.from_input({"platform": dumped}).model_dump()["platform"]
        )
        assert node.settings.platform.model_dump() == bundled.model_dump()
    other = {**mapping, "abc_load_in_ff": 9.0}
    files = {taker.name: {"platform": mapping}, "yosys": {"platform": other}}
    with pytest.raises(FlowSettingsError, match="platform"):
        _asic_plan(tmp_path / "conflict", taker, origins=[("project.yaml", files)])


@pytest.mark.parametrize("leaf, value", [("corner", "SS"), ("dont_use_cells", "AND2_X2")])
def test_a_corner_or_dont_use_list_given_once_reaches_both(tmp_path, leaf, value):
    """`-s corner=SS` and `-s dont_use_cells=X`, given once, reach both flows."""
    taker = _asic_taker()
    platform = "asap7" if leaf == "corner" else "nangate45"
    plan = _asic_plan(
        tmp_path,
        taker,
        origins=[("project.yaml", {taker.name: {"platform": platform}})],
        command_line={taker.name: {leaf: value}},
    )
    for node in plan.nodes:
        given = getattr(node.settings, leaf)
        assert given == (value if leaf == "corner" else [value]), node.name
    assert plan.node("yosys").settings.platform.default_corner == (
        "SS" if leaf == "corner" else "tt"
    )


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


def test_one_corner_spelled_as_a_list_agrees_and_two_corners_conflict(tmp_path):
    taker = _asic_taker()
    files = {taker.name: {"platform": "asap7", "corner": "SS"}, "yosys": {"corner": ["SS"]}}
    plan = _asic_plan(tmp_path, taker, origins=[("project.yaml", files)])
    assert plan.node("yosys").settings.platform.default_corner == "SS"
    files["yosys"]["corner"] = "FF"
    with pytest.raises(FlowSettingsError, match="corner"):
        _asic_plan(tmp_path / "conflict", taker, origins=[("project.yaml", files)])
