"""`FlowLauncher.prepare_request` judges a request as `run()` does and launches nothing, and it is
the one entry point that takes the `flows` table of a host profile.

That table is the lowest layer of the settings of every flow: below the `flows` section of the
project file, the design file, the command line and the API.
"""

import pytest
import yaml

from xeda import Design
from xeda.flow import FlowSettingsException
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.default_runner import PreparedRequest
from xeda.flow_runner.dse import Dse
from xeda.flow_runner.settings_layers import HOST_PROFILE_ORIGIN, host_profile_origin
from xeda.design import DesignValidationError

from .test_dse_run import _DeclaredOptimizer
from .io_flows import _Maker, _Place, _Taker
from .test_checks_before_generation import REFUSALS, XILINX, Generated
from .test_checks_before_generation import _design as declared_design

HOST = "venona"
PART = "LFE5U-25F-6BG256C"
OTHER_PART = "LFE5U-85F-6BG381C"


@pytest.fixture(autouse=True)
def start_in_tmp_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def _design(root, **flow) -> Design:
    return Design(name="d", design_root=root, rtl={"sources": [], "top": "t"}, flows=flow)


def _runner(tmp_path) -> DefaultRunner:
    return DefaultRunner(tmp_path / "run", display_results=False)


def _project(tmp_path, flows) -> str:
    # not in the current directory, where a project file is found by itself
    path = tmp_path / "project" / "xedaproject.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(yaml.safe_dump({"flows": flows}))
    return str(path)


def _text(prepared: PreparedRequest) -> str:
    return prepared.plan.node("__maker").settings.text


def test_host_layer_is_the_lowest(tmp_path):
    """Each higher layer wins over the host: the project file, the design, the command line."""
    host = {"__maker": {"text": "host\n"}}
    project = _project(tmp_path, {"__maker": {"text": "project\n"}})
    runner = _runner(tmp_path)

    only_host = runner.prepare_request(_Taker, _design(tmp_path), host_flows=host, host=HOST)
    assert _text(only_host) == "host\n"
    assert only_host.sections["__maker"]["text"] == "host\n"

    with_project = runner.prepare_request(
        _Taker, _design(tmp_path), project, host_flows=host, host=HOST
    )
    assert _text(with_project) == "project\n"

    with_design = runner.prepare_request(
        _Taker,
        _design(tmp_path, __maker={"text": "design\n"}),
        project,
        host_flows=host,
        host=HOST,
    )
    assert _text(with_design) == "design\n"

    on_command_line = runner.prepare_request(
        _Taker,
        _design(tmp_path, __maker={"text": "design\n"}),
        project,
        ["flows.__maker.text=cli"],
        host_flows=host,
        host=HOST,
    )
    assert _text(on_command_line) == "cli"


def test_host_layer_merges_key_by_key_with_the_layers_above(tmp_path):
    host = {"__maker": {"text": "host\n", "nthreads": 8}}
    project = _project(tmp_path, {"__maker": {"text": "project\n"}})

    prepared = _runner(tmp_path).prepare_request(
        _Taker, _design(tmp_path), project, host_flows=host, host=HOST
    )

    assert prepared.plan.node("__maker").settings.nthreads == 8
    assert _text(prepared) == "project\n"


def test_host_layer_reaches_the_settings_of_a_producer_and_the_requested_flow(tmp_path):
    host = {"__synth": {"fpga": {"part": PART}}}

    prepared = _runner(tmp_path).prepare_request(
        _Place, _design(tmp_path), host_flows=host, host=HOST
    )

    assert [n.name for n in prepared.plan.nodes] == ["__synth", "__place"]
    assert prepared.plan.node("__place").settings.fpga.part == PART


def test_no_host_layer_changes_nothing(tmp_path):
    design = _design(tmp_path, __maker={"text": "design\n"})
    runner = _runner(tmp_path)

    plain = runner.prepare_request(_Taker, design)
    empty = runner.prepare_request(_Taker, design, host_flows={}, host=HOST)

    assert _text(plain) == _text(empty) == "design\n"
    assert [(n.name, n.flowrun_hash) for n in plain.plan.nodes] == [
        (n.name, n.flowrun_hash) for n in empty.plan.nodes
    ]


def test_prepared_request_holds_what_the_launch_would_use(tmp_path):
    design = _design(tmp_path)
    runner = _runner(tmp_path)

    prepared = runner.prepare_request(_Taker, design)

    assert isinstance(prepared, PreparedRequest)
    assert prepared.design.name == "d" and prepared.target is None
    assert [e.flow_class for e in prepared.flow_request.elements] == [_Taker]
    assert prepared.plan.requested == "__taker"
    assert set(prepared.sections) <= {"__maker", "__taker"}
    assert [n.flowrun_hash for n in prepared.plan.nodes] == [
        n.flowrun_hash for n in runner.plan(_Taker, design).nodes
    ]


def test_prepare_request_runs_no_flow_and_refuses_what_run_refuses(tmp_path):
    runner = _runner(tmp_path)
    prepared = runner.prepare_request("__maker+__taker", _design(tmp_path))
    assert [n.name for n in prepared.plan.nodes] == ["__maker", "__taker"]
    assert len(prepared.flow_request.elements) == 2
    assert runner.launched == []
    assert not (tmp_path / "run").exists()

    # a request that `run` refuses on the plan, `prepare_request` refuses with the same error
    refused = _runner(tmp_path / "refused")
    flow, settings = "vivado_synth", {"fpga": {"part": "xc7a12tcsg325-1"}}
    (tmp_path / "a.sc").write_text("")
    design = Design(name="d", design_root=tmp_path, rtl={"sources": ["a.sc"], "top": "t"})
    with pytest.raises(FlowSettingsException) as from_run:
        refused.run(flow, design, flow_settings=settings)
    with pytest.raises(FlowSettingsException) as from_prepare:
        refused.prepare_request(flow, design, flow_settings=settings)
    assert str(from_prepare.value) == str(from_run.value)


@pytest.mark.parametrize("case", list(REFUSALS))
def test_prepare_request_refuses_a_declared_design_before_its_generator_runs(tmp_path, case):
    writes, extra, flow, settings, refusal = REFUSALS[case]
    generated = declared_design(tmp_path, writes, extra)
    with pytest.raises(FlowSettingsException, match=refusal):
        _runner(tmp_path).prepare_request(flow, str(generated.file), flow_settings=settings)
    assert generated.runs == 0
    assert not (generated.root / "gen").exists()
    assert not (tmp_path / "run").exists(), "nothing was set up"


def test_prepare_request_planning_refuses_a_generator(tmp_path):
    """As `plan()` does: with `planning`, a design that needs a generator is refused, and the
    generator does not run. Without it, the generator runs."""
    generated = Generated(tmp_path, ["gen/top.v"])
    runner = _runner(tmp_path)

    with pytest.raises(DesignValidationError, match="Cannot plan a design that needs a generator"):
        runner.prepare_request(
            "vivado_synth", str(generated.file), flow_settings=dict(XILINX), planning=True
        )
    assert generated.runs == 0

    prepared = runner.prepare_request(
        "vivado_synth", str(generated.file), flow_settings=dict(XILINX)
    )
    assert prepared.plan.requested == "vivado_synth"
    assert generated.runs == 1


def test_prepare_request_refuses_outputs_to_for_a_programmer(tmp_path):
    runner = DefaultRunner(tmp_path / "run", display_results=False, outputs_to=tmp_path / "out")

    with pytest.raises(Exception, match="writes no outputs"):
        runner.prepare_request("__chain_simple_producer+__chain_action", _design(tmp_path))


def test_message_names_the_host_profile_layer(tmp_path):
    """Two flows of the host profile that disagree on a shared leaf: the message names the
    layer, and the host."""
    host = {"__place": {"fpga": {"part": OTHER_PART}}, "__synth": {"fpga": {"part": PART}}}
    with pytest.raises(FlowSettingsException) as raised:
        _runner(tmp_path).prepare_request(_Place, _design(tmp_path), host_flows=host, host=HOST)
    assert f"in {host_profile_origin(HOST)}" in str(raised.value), raised.value
    assert f"in the host profile of {HOST}" in str(raised.value)


def test_message_names_the_host_profile_without_a_host_name(tmp_path):
    host = {"__place": {"fpga": {"part": OTHER_PART}}, "__synth": {"fpga": {"part": PART}}}
    with pytest.raises(FlowSettingsException) as raised:
        _runner(tmp_path).prepare_request(_Place, _design(tmp_path), host_flows=host)
    assert f"in {HOST_PROFILE_ORIGIN}" in str(raised.value), raised.value
    assert "host profile of" not in str(raised.value)


def test_a_malformed_host_table_names_the_host_profile(tmp_path):
    with pytest.raises(FlowSettingsException) as raised:
        _runner(tmp_path).prepare_request(
            _Maker, _design(tmp_path), host_flows={"__maker": 5}, host=HOST  # type: ignore[dict-item]
        )
    assert host_profile_origin(HOST) in str(raised.value), raised.value


def test_a_host_value_a_higher_layer_replaces_is_still_judged(tmp_path):
    """The host layer is a real layer: a value no higher layer replaces is validated."""
    with pytest.raises(FlowSettingsException) as raised:
        _runner(tmp_path).prepare_request(
            _Maker, _design(tmp_path), host_flows={"__maker": {"no_such_setting": 1}}, host=HOST
        )
    assert "no_such_setting" in str(raised.value)


def test_the_label_of_the_host_profile():
    assert host_profile_origin(HOST) == "the host profile of venona"
    assert host_profile_origin(None) == HOST_PROFILE_ORIGIN


def test_a_launcher_that_takes_no_chain_refuses_before_it_loads_anything(tmp_path):
    runner = Dse(_DeclaredOptimizer, run_root=tmp_path / "run", variations={}, max_workers=1)
    with pytest.raises(FlowSettingsException, match="local"):
        runner.prepare_request("__maker+__taker", tmp_path / "missing.yaml")
