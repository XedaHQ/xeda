"""Input short forms normalize before serialization, merging, and run identity."""

import json
from copy import deepcopy

import pytest
from click.testing import CliRunner

from xeda.cli import cli
from xeda.design import Design, DesignValidationError
from xeda.flow import FlowSettingsError, flowrun_hash
from xeda.flow_runner.settings_layers import merge_layers
from xeda.flows import VivadoSynth
from xeda.introspect import settings_info

pytestmark = pytest.mark.python_compat


@pytest.mark.parametrize(
    "clock", ["CLK", {"port": "CLK"}, {"port": "CLK", "name": "sys"}, None, ""]
)
def test_design_clock_short_form_has_canonical_state_and_identity(tmp_path, clock):
    clocks = [] if clock in (None, "") else [clock]
    short = Design(name="d", root=tmp_path, rtl={"sources": [], "clock": clock})
    canonical = Design(name="d", root=tmp_path, rtl={"sources": [], "clocks": clocks})
    assert short.model_dump() == canonical.model_dump()
    assert short.rtl_hash == canonical.rtl_hash
    assert "clock" not in short.model_dump()["rtl"]


@pytest.mark.parametrize(
    "clock", [{"period": "5 ns"}, {"freq": "200 MHz"}, {"name": "sys", "period": "5 ns"}]
)
@pytest.mark.parametrize("part", ["xc7a35tcpg236-1", {"part": "xc7a35tcpg236-1"}])
def test_flow_short_forms_have_canonical_settings_and_identity(tmp_path, clock, part):
    name = clock.get("name", "main_clock")
    short = VivadoSynth.Settings.from_input({"fpga": part, "clock": clock}, design_root=tmp_path)
    canonical = VivadoSynth.Settings.from_input(
        {"fpga": {"part": "xc7a35tcpg236-1"}, "clocks": {name: {"name": name, **clock}}},
        design_root=tmp_path,
    )
    assert short.model_dump() == canonical.model_dump()
    assert flowrun_hash("vivado_synth", short, "d") == flowrun_hash("vivado_synth", canonical, "d")
    document = json.loads(short.model_dump_json())
    assert "clock" not in document and isinstance(document["fpga"], dict)
    reloaded = VivadoSynth.Settings.from_input(document, design_root=tmp_path)
    assert flowrun_hash("vivado_synth", reloaded, "d") == flowrun_hash("vivado_synth", short, "d")


@pytest.mark.parametrize("layered", [False, True])
def test_design_refuses_both_clock_forms_in_one_layer(tmp_path, layered):
    layer = {"rtl": {"clock": "CLK", "clocks": ["CLK"]}}
    if layered:
        layer = Design._merge_target({"rtl": {"clocks": ["base"]}}, layer, append=False)
    with pytest.raises(DesignValidationError, match="Specify only one"):
        Design(name="d", root=tmp_path, **layer)


@pytest.mark.parametrize("layered", [False, True])
def test_flow_refuses_both_clock_forms_in_one_layer(layered):
    layer = {"clock": {"period": "5 ns"}, "clocks": {"main_clock": {"period": "10 ns"}}}
    if layered:
        layer = merge_layers(
            {"clocks": {"main_clock": {"period": "20 ns"}}},
            layer,
            settings_cls=VivadoSynth.Settings,
        )
    with pytest.raises(FlowSettingsError, match="not both"):
        VivadoSynth.Settings.from_input({"fpga": {"part": "xc7a35t"}, **layer})


def test_command_line_text_of_the_single_clock_is_read_by_the_clock_model():
    merged = merge_layers(
        ["clock.period=10ns", "clock.duty_cycle=0.4"], settings_cls=VivadoSynth.Settings
    )
    clock = merged["clocks"]["main_clock"]
    assert clock["duty_cycle"] == 0.4 and str(clock["period"]) == "10ns"


@pytest.mark.parametrize("where", ["target", "updates"])
def test_design_clock_overlay_refines_first_clock_and_keeps_other_clocks(tmp_path, where):
    document = {
        "name": "d",
        "rtl": {"sources": [], "clocks": [{"port": "CLK", "name": "sys"}, {"port": "AUX"}]},
    }
    overlay = {"rtl": {"clock": {"port": "NEW"}}}
    if where == "target":
        document["targets"] = {"board": overlay}
    file = tmp_path / "d.json"
    file.write_text(json.dumps(document))
    design = Design.load(file, **({"target": "board"} if where == "target" else {}))
    if where == "updates":
        design = design.with_updates(overlay)
    assert list(design.rtl.clocks) == [
        {"port": "NEW", "name": "sys"},
        {"port": "AUX", "name": "AUX"},
    ]


@pytest.mark.parametrize("name", [None, "sys", "renamed"])
def test_higher_flow_short_forms_merge_as_tables(name):
    low = {
        "fpga": {"part": "xc7a100t", "speed": "2"},
        "clocks": {"sys": {"period": "10 ns", "port": "CLK"}},
    }
    clock = {"freq": "200 MHz", **({"name": name} if name else {})}
    high = {"fpga": "xc7a35t", "clock": clock}
    original = deepcopy((low, high))
    merged = merge_layers(low, high, settings_cls=VivadoSynth.Settings)
    settings = VivadoSynth.Settings.from_input(merged)
    assert settings.fpga.part == "xc7a35t" and settings.fpga.speed == "2"
    assert list(settings.clocks) == [name or "sys"]
    assert settings.main_clock.period.to("ns") == 5
    assert settings.main_clock.port == "CLK"
    assert (low, high) == original


def test_list_settings_advertises_input_only_clock():
    fields = {field["name"]: field for field in settings_info(VivadoSynth)["fields"]}
    assert fields["clock"]["json_schema"]["x-xeda-input-only"] is True
    assert "clock" not in VivadoSynth.Settings.model_fields


def test_cli_short_forms_have_the_same_hash_and_run_directory(tmp_path):
    (tmp_path / "top.v").write_text("module top(input CLK); endmodule\n")
    file = tmp_path / "d.yaml"
    plans = []
    for clock, settings in (
        ("clock: CLK", ["fpga=xc7a35tcpg236-1", "clock.period=5ns"]),
        (
            "clocks: [{port: CLK}]",
            [
                "fpga.part=xc7a35tcpg236-1",
                "clocks.main_clock.name=main_clock",
                "clocks.main_clock.period=5ns",
            ],
        ),
    ):
        file.write_text(f"name: d\nrtl:\n  sources: [top.v]\n  top: top\n  {clock}\n")
        result = CliRunner().invoke(
            cli,
            [
                "run",
                "vivado_synth",
                str(file),
                "--run-root",
                str(tmp_path / "runs"),
                "--hashed-run-dirs",
                "--dry-run",
                "--json",
                "-s",
                *settings,
            ],
        )
        assert result.exit_code == 0, result.output
        plans.append(json.loads(result.output)["plan"]["nodes"])
    assert [(n["settings_hash"], n["flowrun_hash"], n["run_path"]) for n in plans[0]] == [
        (n["settings_hash"], n["flowrun_hash"], n["run_path"]) for n in plans[1]
    ]


@pytest.mark.parametrize("clock", [None, ""])
def test_higher_empty_design_clock_clears_the_canonical_list(tmp_path, clock):
    merged = Design._merge_target(
        {"rtl": {"sources": [], "clocks": ["CLK", "AUX"]}},
        {"rtl": {"clock": clock}},
        append=False,
    )
    assert Design(name="d", root=tmp_path, **merged).rtl.clocks == ()


def test_dotted_clock_inputs_in_a_design_file_normalize_to_canonical_state(tmp_path):
    file = tmp_path / "d.yaml"
    file.write_text(
        "name: d\nrtl.sources: []\nrtl.clock.port: CLK\n"
        "flows.vivado_synth.fpga: xc7a35tcpg236-1\n"
        "flows.vivado_synth.clock.period: 5 ns\n"
    )
    design = Design.load(file)
    assert list(design.rtl.clocks) == [{"port": "CLK", "name": "CLK"}]
    settings = VivadoSynth.Settings.from_input(design.flows["vivado_synth"])
    assert settings.fpga.part == "xc7a35tcpg236-1"
    assert settings.main_clock.period.to("ns") == 5
