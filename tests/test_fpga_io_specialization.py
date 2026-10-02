"""Family selection precedes source displacement and final shared-setting agreement."""

from pathlib import Path
from typing import ClassVar

import pytest

from xeda import Design
from xeda.board import WithFpgaBoardSettings
from xeda.dataclass import Field
from xeda.design import SourceType
from xeda.flow import Flow, FlowSettingsError, FlowSettingsException, In, registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.flows import Nextpnr

from .test_declared_fpga_flows import tools as tools


@pytest.fixture(autouse=True)
def _restore_registry():
    before = registered_flows.copy()
    yield
    registered_flows.clear()
    registered_flows.update(before)


class _ConfigTaker(Flow):
    """Demand the selected configuration through a declared edge."""

    results_description: ClassVar[dict[str, str]] = {}
    required_settings = Nextpnr.required_settings

    class Settings(WithFpgaBoardSettings, Flow.Settings):
        nextpnr: Nextpnr.Settings = Field(
            default_factory=Nextpnr.Settings, description="Placement settings."
        )
        prjxray_db: Path | None = Field(None, description="Database override.")
        dependency_settings = {"nextpnr": ("fpga", "board", "custom_boards_file")}

    class Inputs(Flow.Inputs):
        config: Path = In(
            (SourceType.EcpConfig, SourceType.IceAsc, SourceType.Fasm),
            producer="nextpnr",
            output="config",
            description="The selected family configuration.",
        )

    @classmethod
    def input_types(cls, settings, name):
        return Nextpnr.output_types(settings, name)

    def run(self):
        self.results["read"] = self.inputs.config.read_text()


FAMILIES = [
    ("LFE5U-25F-6BG381C", SourceType.Lpf, SourceType.EcpConfig, "textcfg", "config.txt"),
    ("iCE40HX1K-TQ144", SourceType.Pcf, SourceType.IceAsc, "asc", "config.asc"),
    ("LIFCL-40-9BG400C", SourceType.Pdc, SourceType.Fasm, "fasm", "config.fasm"),
    ("xc7a35tcpg236-1", SourceType.Xdc, SourceType.Fasm, "fasm", "config.fasm"),
]


def _design(tmp_path, sources=()):
    root = tmp_path / "d"
    root.mkdir(exist_ok=True)
    entries = []
    for name, kind in sources:
        (root / name).write_text("{}\n" if kind == SourceType.JsonNetlist else "input\n")
        entries.append({"file": name, "type": kind.name})
    return Design(name="d", design_root=root, rtl={"sources": entries, "top": "top"})


@pytest.mark.parametrize("part,pins,config,setting,filename", FAMILIES)
def test_pure_family_hooks_keep_types_when_configuration_is_disabled(
    part, pins, config, setting, filename
):
    settings = Nextpnr.Settings(fpga=part, **{setting: None})
    before = settings.model_dump()
    assert Nextpnr.input_types(settings, "constraints") == (pins,)
    assert Nextpnr.input_types(settings, "sdc") == (SourceType.Sdc,)
    assert Nextpnr.output_types(settings, "config") == (config,)
    assert settings.model_dump() == before
    Nextpnr.enable_output(settings, "config")
    assert getattr(settings, setting) == Path(filename)


@pytest.mark.parametrize("part,pins,config,setting,filename", FAMILIES[:3])
def test_only_selected_pin_sources_and_all_sdc_sources_are_bound(
    tmp_path, part, pins, config, setting, filename
):
    design = _design(
        tmp_path,
        [
            ("foreign", SourceType.Xdc),
            ("second", pins),
            ("a.sdc", SourceType.Sdc),
            ("first", pins),
            ("b.sdc", SourceType.Sdc),
        ],
    )
    node = (
        DefaultRunner(tmp_path / "run")
        .plan(Nextpnr, design, flow_settings={"fpga": part})
        .node("nextpnr")
    )
    bindings = {entry.name: entry for entry in node.inputs}
    assert bindings["constraints"].sources == (
        design.root_path / "second",
        design.root_path / "first",
    )
    assert bindings["sdc"].sources == (design.root_path / "a.sdc", design.root_path / "b.sdc")


def test_foreign_pins_and_sdc_leave_pin_binding_empty(tmp_path):
    design = _design(tmp_path, [("foreign.xdc", SourceType.Xdc), ("a.sdc", SourceType.Sdc)])
    node = (
        DefaultRunner(tmp_path / "run")
        .plan(Nextpnr, design, flow_settings={"board": "ULX3S_85F"})
        .node("nextpnr")
    )
    assert next(entry for entry in node.inputs if entry.name == "constraints").origin == "none"


@pytest.mark.parametrize("part,pins,config,setting,filename", FAMILIES[:3])
def test_demand_enables_configuration_and_executes_exactly_the_plan(
    tmp_path, monkeypatch, tools, part, pins, config, setting, filename
):
    monkeypatch.chdir(tmp_path)
    design = _design(tmp_path)
    values = {"fpga": part, "nextpnr": {setting: None}}
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    plan = runner.plan(_ConfigTaker, design, flow_settings=values)
    assert plan.node("nextpnr").switched_on == ("config",)
    assert getattr(plan.node("nextpnr").settings, setting) == Path(filename)
    first = runner.run_flow(_ConfigTaker, design, values)
    assert first.succeeded and first.results["read"] == "config\n"
    assert [flow.name for flow in runner.launched] == [node.name for node in plan.nodes]
    again = runner.run_flow(_ConfigTaker, design, values)
    assert again.reused and len(tools) == 1
    assert values["nextpnr"][setting] is None


def test_out_of_context_demand_is_rejected_before_tools(tmp_path):
    with pytest.raises(FlowSettingsException, match="out.of.context"):
        DefaultRunner(tmp_path / "run").plan(
            _ConfigTaker,
            _design(tmp_path),
            flow_settings={"fpga": FAMILIES[0][0], "nextpnr": {"out_of_context": True}},
        )
    assert not (tmp_path / "run").exists()


def test_mixed_configurations_select_only_the_agreed_family(tmp_path):
    design = _design(
        tmp_path, [("given.config", SourceType.EcpConfig), ("foreign.fasm", SourceType.Fasm)]
    )
    plan = DefaultRunner(tmp_path / "run").plan(
        _ConfigTaker,
        design,
        flow_settings={"fpga": FAMILIES[0][0]},
    )
    assert [node.name for node in plan.nodes] == [_ConfigTaker.name]
    assert plan.nodes[0].inputs[0].sources == (design.root_path / "given.config",)


def test_matching_source_displaces_conflicting_producer_settings(tmp_path):
    design = _design(tmp_path, [("given.config", SourceType.EcpConfig)])
    design.flow["nextpnr"] = {"fpga": FAMILIES[1][0]}
    design.flow[_ConfigTaker.name] = {"fpga": FAMILIES[0][0]}
    plan = DefaultRunner(tmp_path / "run").plan(_ConfigTaker, design)
    assert [node.name for node in plan.nodes] == [_ConfigTaker.name]
    assert plan.nodes[0].settings.fpga.part == FAMILIES[0][0]


def test_matching_source_displaces_a_conflicting_producer_board(tmp_path):
    design = _design(tmp_path, [("given.config", SourceType.EcpConfig)])
    design.flow[_ConfigTaker.name] = {"fpga": FAMILIES[0][0]}
    design.flow["nextpnr"] = {"board": "ARTY_A7_35T"}
    plan = DefaultRunner(tmp_path / "run").plan(_ConfigTaker, design)
    assert [node.name for node in plan.nodes] == [_ConfigTaker.name]
    assert plan.nodes[0].settings.board is None


def test_foreign_fasm_cannot_displace_ecp5_production(tmp_path):
    plan = DefaultRunner(tmp_path / "run").plan(
        _ConfigTaker,
        _design(tmp_path, [("foreign.fasm", SourceType.Fasm)]),
        flow_settings={"fpga": FAMILIES[0][0]},
    )
    assert [node.name for node in plan.nodes] == ["yosys_fpga", "nextpnr", _ConfigTaker.name]
    assert plan.nodes[-1].inputs[0].origin == "producer"


def test_displaced_target_does_not_survive_in_the_final_plan(tmp_path):
    design = _design(tmp_path, [("given.config", SourceType.EcpConfig)])
    design.flow["nextpnr"] = {"fpga": FAMILIES[0][0]}
    with pytest.raises(FlowSettingsException, match=r"needs `fpga`"):
        DefaultRunner(tmp_path / "run").plan(_ConfigTaker, design)


def test_active_conflicting_targets_still_fail(tmp_path):
    design = _design(tmp_path, [("foreign.fasm", SourceType.Fasm)])
    design.flow["nextpnr"] = {"fpga": FAMILIES[1][0]}
    design.flow[_ConfigTaker.name] = {"fpga": FAMILIES[0][0]}
    with pytest.raises(FlowSettingsError, match="disagrees"):
        DefaultRunner(tmp_path / "run").plan(_ConfigTaker, design)


@pytest.mark.parametrize("owner", [_ConfigTaker.name, "nextpnr"])
def test_database_override_and_context_aliases_agree(tmp_path, owner):
    design = _design(tmp_path)
    design.flow[owner] = {"prjxray_db": "$DESIGN_ROOT/db"}
    design.flow["nextpnr" if owner == _ConfigTaker.name else _ConfigTaker.name] = {
        "prjxray_db": "db"
    }
    plan = DefaultRunner(tmp_path / "run").plan(
        _ConfigTaker, design, flow_settings={"fpga": FAMILIES[0][0]}
    )
    for name in (_ConfigTaker.name, "nextpnr"):
        assert plan.node(name).settings.prjxray_db.resolve() == design.root_path / "db"


def test_database_conflict_and_command_line_precedence(tmp_path):
    design = _design(tmp_path)
    design.flow[_ConfigTaker.name] = {"fpga": FAMILIES[0][0], "prjxray_db": "a"}
    design.flow["nextpnr"] = {"prjxray_db": "b"}
    runner = DefaultRunner(tmp_path / "run")
    with pytest.raises(FlowSettingsError, match="prjxray_db.*disagrees"):
        runner.plan(_ConfigTaker, design)
    plan = runner.plan(
        _ConfigTaker, design, flow_settings=["flows.nextpnr.prjxray_db=$DESIGN_ROOT/c"]
    )
    assert (
        plan.node("nextpnr").settings.prjxray_db
        == plan.node(_ConfigTaker.name).settings.prjxray_db
        == design.root_path / "c"
    )


@pytest.mark.parametrize("owner", [_ConfigTaker.name, "nextpnr"])
def test_one_database_override_reaches_only_endpoints_with_the_setting(tmp_path, owner):
    design = _design(tmp_path)
    design.flow[owner] = {"prjxray_db": "db"}
    plan = DefaultRunner(tmp_path / "run").plan(
        _ConfigTaker, design, flow_settings={"fpga": FAMILIES[0][0]}
    )
    assert plan.node(_ConfigTaker.name).settings.prjxray_db == design.root_path / "db"
    assert plan.node("nextpnr").settings.prjxray_db == design.root_path / "db"
    assert not hasattr(plan.node("yosys_fpga").settings, "prjxray_db")


@pytest.mark.parametrize("owner", [_ConfigTaker.name, "nextpnr"])
def test_board_target_propagates_before_family_selection(tmp_path, owner):
    design = _design(tmp_path)
    design.flow[owner] = {"board": "ULX3S_85F"}
    plan = DefaultRunner(tmp_path / "run").plan(_ConfigTaker, design)
    assert all(node.settings.fpga.family == "ecp5" for node in plan.nodes)
    assert plan.node(_ConfigTaker.name).inputs[0].origin == "producer"
    assert plan.node("nextpnr").settings.board == "ULX3S_85F"


def test_displaced_board_target_cannot_satisfy_the_consumer(tmp_path):
    design = _design(tmp_path, [("given.config", SourceType.EcpConfig)])
    design.flow["nextpnr"] = {"board": "ULX3S_85F"}
    with pytest.raises(FlowSettingsException, match=r"needs `fpga`"):
        DefaultRunner(tmp_path / "run").plan(_ConfigTaker, design)


def test_displaced_producer_still_gets_syntax_validation(tmp_path):
    design = _design(tmp_path, [("given.config", SourceType.EcpConfig)])
    design.flow["nextpnr"] = {"unknown_setting": True}
    with pytest.raises(FlowSettingsError, match="unknown_setting"):
        DefaultRunner(tmp_path / "run").plan(
            _ConfigTaker, design, flow_settings={"fpga": FAMILIES[0][0]}
        )


def test_two_matching_configurations_fail_with_selected_type_and_paths(tmp_path):
    design = _design(
        tmp_path,
        [("a", SourceType.EcpConfig), ("b", SourceType.EcpConfig), ("foreign", SourceType.Fasm)],
    )
    with pytest.raises(FlowSettingsException, match="takes one EcpConfig file") as raised:
        DefaultRunner(tmp_path / "run").plan(
            _ConfigTaker, design, flow_settings={"fpga": FAMILIES[0][0]}
        )
    assert str(design.root_path / "a") in str(raised.value)
    assert str(design.root_path / "b") in str(raised.value)
    assert "foreign" not in str(raised.value)


def test_selected_producer_output_can_feed_a_narrow_static_input(tmp_path):
    class _EcpTaker(_ConfigTaker):
        """A consumer whose static vocabulary is ECP5 only."""

        results_description = {}

        class Inputs(Flow.Inputs):
            config: Path = In(
                SourceType.EcpConfig,
                producer="nextpnr",
                output="config",
                description="ECP5 configuration.",
            )

        @classmethod
        def input_types(cls, settings, name):
            return Flow.input_types.__func__(cls, settings, name)

    plan = DefaultRunner(tmp_path / "run").plan(
        _EcpTaker, _design(tmp_path), flow_settings={"fpga": FAMILIES[0][0]}
    )
    assert plan.nodes[-1].inputs[0].origin == "producer"
    with pytest.raises(FlowSettingsException, match="no compatible output"):
        DefaultRunner(tmp_path / "other").plan(
            _EcpTaker, _design(tmp_path), flow_settings={"fpga": FAMILIES[1][0]}
        )


def test_dry_run_exposes_selected_types_and_catalog_keeps_the_vocabulary_union(tmp_path):
    from xeda.introspect import flow_info, plan_info

    plan = DefaultRunner(tmp_path / "run").plan(
        Nextpnr, _design(tmp_path), flow_settings={"fpga": FAMILIES[0][0]}
    )
    node = plan_info(plan)["nodes"][-1]
    assert node["input_types"]["constraints"] == ["Lpf"]
    assert node["input_types"]["sdc"] == ["Sdc"]
    assert node["output_types"]["config"] == ["EcpConfig"]
    constraints = next(
        entry for entry in flow_info(Nextpnr)["inputs"] if entry["name"] == "constraints"
    )
    assert constraints["types"] == ["Lpf", "Pcf", "Pdc", "Xdc"]
    assert not (tmp_path / "run").exists()
