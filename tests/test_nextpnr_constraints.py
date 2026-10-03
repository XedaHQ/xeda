"""Typed pin and timing files retain order, provenance and one clock authority."""

import json
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow import FlowFatalError
from xeda.flow_runner import DefaultRunner
from xeda.flows import Nextpnr
from xeda.flows.nextpnr import NextpnrTool

from .test_nextpnr import write_nextpnr_config


def make_flow(tmp_path, monkeypatch, *, sources=(), settings=None, top="d"):
    design = Design(name="d", design_root=tmp_path, rtl={"sources": list(sources), "top": top})
    flow = Nextpnr(
        Nextpnr.Settings.from_input(
            settings or {"fpga": "LFE5U-25F-6BG381C"}, design_root=tmp_path
        ),
        design,
        tmp_path / "pnr",
    )
    netlist = tmp_path / "netlist.json"
    netlist.write_text(
        json.dumps(
            {
                "modules": {
                    top: {"ports": {"clk": {"bits": [2]}}, "netnames": {"alias": {"bits": [2]}}}
                }
            }
        )
    )
    flow.inputs.netlist = netlist
    accepted = Nextpnr.input_types(flow.settings, "constraints")
    flow.inputs.constraints = [s.path for s in design.rtl.sources if s.type in accepted]
    from xeda.design import SourceType

    flow.inputs.sdc = [s.path for s in design.rtl.sources if s.type is SourceType.Sdc]
    calls = []

    def run(self, *args, env=None):
        assert env == {"PYTHONDONTWRITEBYTECODE": "1"}
        calls.append(list(map(str, args)))
        write_nextpnr_config(flow, args)

    monkeypatch.setattr(NextpnrTool, "run", run)
    return flow, calls


@pytest.mark.parametrize(
    "part,kind,line",
    [
        ("LFE5U-25F-6BG381C", "lpf", 'LOCATE COMP "clk" SITE "P3";'),
        ("iCE40HX1K-TQ144", "pcf", "set_io clk 21"),
        ("LIFCL-40-9BG400C", "pdc", "ldc_set_location -site {A1} [get_ports {clk}]"),
    ],
)
def test_ordered_pin_sources_and_sdc_setting_reach_tool_once(
    tmp_path, monkeypatch, part, kind, line
):
    a = tmp_path / f"first [1].{kind}"
    b = tmp_path / "misleading.v"
    a.write_text(line)
    b.write_text("# second\n")
    timing = tmp_path / "timing.sdc"
    timing.write_text("# timing source")
    extra = tmp_path / "extra.sdc"
    extra.write_text("# setting")
    before = {p: (p.read_bytes(), p.stat()) for p in (a, b, timing, extra)}
    flow, calls = make_flow(
        tmp_path,
        monkeypatch,
        sources=[a.name, {"file": b.name, "type": kind}, timing.name],
        settings={"fpga": part, "sdc": extra.name},
    )
    flow.prepare_inputs()
    flow.run()
    args = calls[0]
    pins = [v.split("=", 1)[1] for v in args if v.startswith(f"--{kind}=")]
    sdcs = [v.split("=", 1)[1] for v in args if v.startswith("--sdc=")]
    assert len(pins) == len(sdcs) == 1
    assert Path(pins[0]).read_text() == line + "\n# second\n"
    assert Path(sdcs[0]).read_text() == "# timing source\n# setting\n"
    assert before == {p: (p.read_bytes(), p.stat()) for p in before}


@pytest.mark.parametrize(
    "command",
    [
        "create_clock -period 10 [get_ports {clk}]",
        'create_clock -name {clock name} \\\n -period 10 [get_ports "clk"]',
        "create_clock -period 10 [get_nets {alias}]",
        'FREQUENCY PORT "clk" 100 MHz;',
        'FREQUENCY NET "alias" 100 MHz;',
    ],
)
def test_file_clock_and_setting_duplicate_names_both_origins(tmp_path, monkeypatch, command):
    source = tmp_path / ("pins.lpf" if command.startswith("FREQUENCY") else "timing.sdc")
    source.write_text("# comment\n" + command + "\n")
    flow, calls = make_flow(
        tmp_path,
        monkeypatch,
        sources=[source.name],
        settings={"fpga": "LFE5U-25F-6BG381C", "clocks": {"main": {"port": "clk", "period": 10}}},
    )
    flow.prepare_inputs()
    with pytest.raises(FlowFatalError) as exc:
        flow.run()
    message = str(exc.value)
    assert f"{source}:2" in message and "main" in message and "10" in message
    assert calls == []


def test_duplicate_clocks_across_typed_sdc_and_setting(tmp_path, monkeypatch):
    for name, selector in (("first.sdc", "get_ports {clk}"), ("last.sdc", "get_nets {alias}")):
        (tmp_path / name).write_text(f"create_clock -period 10 [{selector}]\n")
    flow, _ = make_flow(
        tmp_path,
        monkeypatch,
        sources=["first.sdc"],
        settings={"fpga": "LFE5U-25F-6BG381C", "sdc": "last.sdc"},
    )
    flow.prepare_inputs()
    with pytest.raises(FlowFatalError) as exc:
        flow.run()
    assert "first.sdc:1" in str(exc.value) and "last.sdc:1" in str(exc.value)


def test_file_only_clock_suppresses_frequency_and_comments_do_not_duplicate(tmp_path, monkeypatch):
    (tmp_path / "timing.sdc").write_text(
        "# create_clock -period 1 [get_ports clk]\ncreate_clock -period 10 [get_ports clk]\n"
    )
    flow, calls = make_flow(tmp_path, monkeypatch, sources=["timing.sdc"])
    flow.prepare_inputs()
    flow.run()
    assert not any(v.startswith("--freq") for v in calls[0])


def test_local_board_fallback_is_fresh_immediately_then_stale_on_edit(tmp_path, monkeypatch):
    from .tool_utils import use_fake_fpga_tools

    use_fake_fpga_tools(monkeypatch, tmp_path)
    pins = tmp_path / "board.lpf"
    pins.write_text('LOCATE COMP "clk" SITE "P3";\n')
    boards = tmp_path / "boards.toml"
    boards.write_text('[CUSTOM]\nfpga.part="LFE5U-25F-6BG381C"\nlpf="board.lpf"\n')
    netlist = tmp_path / "d.json"
    netlist.write_text('{"modules":{"d":{"ports":{},"cells":{},"netnames":{}}}}')
    design = Design(
        name="d",
        design_root=tmp_path,
        rtl={"sources": [{"file": netlist.name, "type": "JsonNetlist"}], "top": "d"},
    )
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    settings = {"board": "CUSTOM", "custom_boards_file": boards}
    first = runner.launch_flow(Nextpnr, design, settings)
    assert first.succeeded and pins in first.implicit_inputs
    assert runner.launch_flow(Nextpnr, design, settings).reused
    pins.write_text('LOCATE COMP "clk" SITE "P4";\n')
    changed = runner.launch_flow(Nextpnr, design, settings)
    assert changed.succeeded and not changed.reused
    assert runner.launch_flow(Nextpnr, design, settings).reused


def test_xilinx_clocks_are_literal_and_merged_line_errors_keep_origins(tmp_path, monkeypatch):
    from xeda.flows.nextpnr_constraints import merge_constraints

    (tmp_path / "one.xdc").write_text("# pin file without final newline")
    (tmp_path / "two.xdc").write_text("# second\n")
    flow, _ = make_flow(
        tmp_path,
        monkeypatch,
        sources=["one.xdc", "two.xdc"],
        settings={
            "fpga": "xc7a35tcsg324-1",
            "clocks": {
                "main": {"port": "clk[0]", "period": 10},
                "aux": {"port": "clk with space[1]", "period": 20},
            },
        },
    )
    # This oracle covers literal constraint rendering, independent of cache/tool setup.
    monkeypatch.setattr(Nextpnr, "_prepare_chipdb", lambda self: None)
    flow.prepare_inputs()
    pins, sdc, freq = flow._merged_constraints()
    assert sdc is None and freq is None
    text = pins.read_text()
    assert text.count("create_clock") == 2
    assert "[get_ports {clk[0]}]" in text
    assert "[get_ports {{clk with space[1]}}]" in text
    merged = flow._pin_constraints
    assert "one.xdc:1" in merged.diagnostic("error on line 1")
    assert "two.xdc:1" in merged.diagnostic("error on line 2")
    assert "clock setting clocks.main" in merged.diagnostic("duplicate clock at line 3")
    assert merge_constraints([tmp_path / "one.xdc", tmp_path / "two.xdc"]).lines == merged.lines[:2]


@pytest.mark.parametrize("selector", ["[get_ports {clk aux}]", '[get_ports "clk aux"]'])
def test_literal_selector_lists_diagnose_overlap(tmp_path, monkeypatch, selector):
    (tmp_path / "timing.sdc").write_text(
        f"create_clock -period 10 {selector}\ncreate_clock -period 20 [get_ports aux]\n"
    )
    flow, _ = make_flow(tmp_path, monkeypatch, sources=["timing.sdc"])
    flow.prepare_inputs()
    with pytest.raises(FlowFatalError, match="Duplicate clock.*timing.sdc:1.*timing.sdc:2"):
        flow.run()


@pytest.mark.parametrize(
    "command",
    [
        'FREQUENCY NET "unknown" 25 MHz;',
        "create_clock -period 40 [get_nets {unknown}]",
        "create_clock -period 40 [get_ports {*} ]",
    ],
)
def test_unresolved_or_ambiguous_clock_selectors_are_honest(tmp_path, monkeypatch, command):
    (tmp_path / "timing.sdc").write_text(command + "\n")
    flow, calls = make_flow(
        tmp_path,
        monkeypatch,
        sources=["timing.sdc"],
        settings={"fpga": "LFE5U-25F-6BG381C", "clocks": {"main": {"port": "clk", "period": 40}}},
    )
    flow.prepare_inputs()
    with pytest.raises(FlowFatalError) as exc:
        flow.run()
    assert "timing.sdc:1" in str(exc.value) and "literal" in str(exc.value)
    assert calls == []


def test_cross_pin_sdc_clock_duplicates_keep_each_path(tmp_path, monkeypatch):
    (tmp_path / "pins.lpf").write_text('FREQUENCY PORT "clk" 25 MHz;\n')
    (tmp_path / "timing.v").write_text("create_clock -period 40 [get_nets {alias}]\n")
    flow, _ = make_flow(
        tmp_path, monkeypatch, sources=["pins.lpf", {"file": "timing.v", "type": "Sdc"}]
    )
    flow.prepare_inputs()
    with pytest.raises(FlowFatalError, match="pins.lpf:1.*timing.v:1"):
        flow.run()


def test_settings_clock_ignores_comment_clocks_and_port_only_design_warns(
    tmp_path, monkeypatch, caplog
):
    (tmp_path / "pins.lpf").write_text('# FREQUENCY PORT "clk" 25 MHz;\n')
    flow, calls = make_flow(
        tmp_path,
        monkeypatch,
        sources=["pins.lpf"],
        settings={"fpga": "LFE5U-25F-6BG381C", "clocks": {"main": {"port": "clk", "period": 40}}},
    )
    flow.prepare_inputs()
    flow.run()
    assert "--freq=25.0" in calls[0]
    untimed, calls = make_flow(tmp_path, monkeypatch)
    from xeda.design import Clock

    untimed.design.rtl.clocks = [Clock(port="clk")]
    untimed.prepare_inputs()
    untimed.run()
    assert "12 MHz" in caplog.text
    assert not any(v.startswith("--freq") for v in calls[0])


def test_typed_sources_displace_board_fallback_and_foreign_sources_do_not(tmp_path, monkeypatch):
    (tmp_path / "pins.lpf").write_text("# chosen\n")
    (tmp_path / "foreign.xdc").write_text("# foreign\n")
    (tmp_path / "timing.sdc").write_text("# timing\n")
    settings = {"board": "ULX3S_85F"}
    chosen, _ = make_flow(tmp_path, monkeypatch, sources=["pins.lpf"], settings=settings)
    chosen.prepare_inputs()
    assert chosen._pin_inputs == [tmp_path / "pins.lpf"] and chosen.implicit_inputs == []
    foreign, _ = make_flow(
        tmp_path, monkeypatch, sources=["foreign.xdc", "timing.sdc"], settings=settings
    )
    foreign.prepare_inputs()
    assert len(foreign._pin_inputs) == 1
    assert foreign._pin_inputs[0].name == "board.lpf"
    assert foreign.implicit_inputs == foreign._pin_inputs


def test_xilinx_missing_pins_names_typed_source_remedy(tmp_path, monkeypatch):
    flow, _ = make_flow(tmp_path, monkeypatch, settings={"fpga": "xc7a35tcsg324-1"})
    with pytest.raises(FlowFatalError, match="typed Xdc.*rtl.sources"):
        flow.prepare_inputs()


def test_archive_board_preparation_reuses_and_replaces_content_identity(tmp_path, monkeypatch):
    import zipfile
    import xeda.flows.nextpnr as nextpnr
    from .tool_utils import use_fake_fpga_tools

    use_fake_fpga_tools(monkeypatch, tmp_path / "tools")
    archive = tmp_path / "boards.zip"

    def pack(content):
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("boards/ulx3s/board.lpf", content)

    pack("# archive one\n")
    monkeypatch.setattr(nextpnr, "files", lambda package: zipfile.Path(archive))
    netlist = tmp_path / "d.json"
    netlist.write_text('{"modules":{"d":{"ports":{},"cells":{},"netnames":{}}}}')
    design = Design(
        name="d",
        design_root=tmp_path,
        rtl={"sources": [{"file": netlist.name, "type": "JsonNetlist"}], "top": "d"},
    )
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    first = runner.launch_flow(Nextpnr, design, {"board": "ULX3S_85F"})
    assert first.succeeded
    prepared = first.implicit_inputs[0]
    assert prepared.is_relative_to(runner.run_root / ".cache/board-files")
    before = prepared.stat().st_mtime_ns
    assert runner.launch_flow(Nextpnr, design, {"board": "ULX3S_85F"}).reused
    assert prepared.stat().st_mtime_ns == before
    pack("# archive two\n")
    changed = runner.launch_flow(Nextpnr, design, {"board": "ULX3S_85F"})
    assert changed.succeeded and not changed.reused
    assert changed.implicit_inputs[0] != prepared
    assert prepared.read_text() == "# archive one\n"
    assert runner.launch_flow(Nextpnr, design, {"board": "ULX3S_85F"}).reused


def test_url_constraints_fetch_only_during_run_into_owned_scratch(tmp_path, monkeypatch):
    import io
    import xeda.flows.nextpnr as nextpnr

    (tmp_path / "boards.toml").write_text(
        '[REMOTE]\nfpga.part="LFE5U-25F-6BG381C"\nlpf="https://example.invalid/pins.lpf"\n'
    )
    fetched = []
    monkeypatch.setattr(
        nextpnr, "urlopen", lambda url: fetched.append(url) or io.BytesIO(b"# remote\n")
    )
    flow, calls = make_flow(
        tmp_path, monkeypatch, settings={"board": "REMOTE", "custom_boards_file": "boards.toml"}
    )
    flow.prepare_inputs()
    assert fetched == [] and flow.implicit_inputs == [] and not flow.run_path.exists()
    assert flow.always_runs() == "its constraints are fetched from a URL"
    flow.run_path.mkdir()
    flow.run()
    assert fetched == ["https://example.invalid/pins.lpf"]
    assert (flow.run_path / "board-download.constraints").read_text() == "# remote\n"
    assert f"--lpf={flow.run_path / 'constraints.lpf'}" in calls[0]


def test_prepared_board_file_is_reserved_against_delivery(tmp_path, monkeypatch):
    from xeda.deliver import DeliveryError
    from .tool_utils import use_fake_fpga_tools

    use_fake_fpga_tools(monkeypatch, tmp_path / "tools")
    pins = tmp_path / "board.lpf"
    pins.write_text("# board input\n")
    boards = tmp_path / "boards.toml"
    boards.write_text('[CUSTOM]\nfpga.part="LFE5U-25F-6BG381C"\nlpf="board.lpf"\n')
    netlist = tmp_path / "d.json"
    netlist.write_text('{"modules":{"d":{"ports":{},"cells":{},"netnames":{}}}}')
    design = Design(
        name="d",
        design_root=tmp_path,
        rtl={"sources": [{"file": netlist.name, "type": "JsonNetlist"}], "top": "d"},
    )
    runner = DefaultRunner(tmp_path / "run", display_results=False, overwrite_outputs=True)
    with pytest.raises(DeliveryError, match="input"):
        runner.launch_flow(
            Nextpnr, design, {"board": "CUSTOM", "custom_boards_file": boards, "textcfg": pins}
        )
    assert pins.read_text() == "# board input\n"


def test_tool_constraint_lines_are_translated_using_current_run_log(tmp_path, monkeypatch):
    from xeda.tool import NonZeroExitCode

    (tmp_path / "first.sdc").write_text("# first\n")
    (tmp_path / "second.sdc").write_text("# second\n")
    flow, _ = make_flow(tmp_path, monkeypatch, sources=["first.sdc", "second.sdc"])
    flow.prepare_inputs()
    flow.run_path.mkdir()

    def fail(self, *args, env=None):
        (flow.run_path / "nextpnr.log").write_text(
            "ERROR: constraints.sdc line 2 has an invalid constraint\n"
        )
        raise NonZeroExitCode(args, 1)

    monkeypatch.setattr(NextpnrTool, "run", fail)
    with pytest.raises(FlowFatalError, match="second.sdc:1"):
        flow.run()


def test_hook_failure_lines_are_not_attributed_to_pin_constraints(tmp_path, monkeypatch):
    from xeda.tool import NonZeroExitCode

    (tmp_path / "pins.lpf").write_text("# first\n# second\n")
    flow, _ = make_flow(tmp_path, monkeypatch, sources=["pins.lpf"])
    flow.prepare_inputs()
    flow.run_path.mkdir()

    def fail(self, *args, env=None):
        (flow.run_path / "nextpnr.log").write_text('File "pre_route.py", line 2, in hook\n')
        raise NonZeroExitCode(args, 1)

    monkeypatch.setattr(NextpnrTool, "run", fail)
    with pytest.raises(NonZeroExitCode):
        flow.run()


def test_ordered_sdc_sources_alone_keep_explicit_types_and_line_map(tmp_path, monkeypatch):
    (tmp_path / "one.sdc").write_text("# one\n# two")
    (tmp_path / "two.pcf").write_text("create_clock -period 40 [get_ports clk]\n")
    flow, calls = make_flow(
        tmp_path, monkeypatch, sources=["one.sdc", {"file": "two.pcf", "type": "Sdc"}]
    )
    flow.prepare_inputs()
    flow.run()
    args = calls[0]
    assert [arg for arg in args if arg.startswith("--sdc=")] == [
        f"--sdc={flow.run_path / 'constraints.sdc'}"
    ]
    assert (
        flow.run_path / "constraints.sdc"
    ).read_text() == "# one\n# two\ncreate_clock -period 40 [get_ports clk]\n"
    assert "two.pcf:1" in flow._sdc_constraints.diagnostic("ERROR line 3")
    assert not any(arg.startswith("--pcf=") or arg.startswith("--freq=") for arg in args)


def test_sdc_setting_alone_remains_tracked_and_applied(tmp_path, monkeypatch):
    (tmp_path / "timing.sdc").write_text("create_clock -period 40 [get_ports clk]\n")
    flow, calls = make_flow(
        tmp_path, monkeypatch, settings={"fpga": "LFE5U-25F-6BG381C", "sdc": "timing.sdc"}
    )
    flow.prepare_inputs()
    flow.run()
    assert f"--sdc={flow.run_path / 'constraints.sdc'}" in calls[0]
    assert not any(arg.startswith("--freq=") for arg in calls[0])


@pytest.mark.parametrize("changed", ["source", "setting"])
def test_timing_file_edit_invalidates_the_next_launch(tmp_path, monkeypatch, changed):
    from .tool_utils import use_fake_fpga_tools

    use_fake_fpga_tools(monkeypatch, tmp_path / "tools")
    netlist = tmp_path / "d.json"
    netlist.write_text('{"modules":{"d":{"ports":{},"cells":{},"netnames":{}}}}')
    source = tmp_path / "source.sdc"
    setting = tmp_path / "setting.sdc"
    source.write_text("# source\n")
    setting.write_text("# setting\n")
    design = Design(
        name="d",
        design_root=tmp_path,
        rtl={"sources": [{"file": netlist.name, "type": "JsonNetlist"}, source.name], "top": "d"},
    )
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    settings = {"fpga": "LFE5U-25F-6BG381C", "sdc": setting}
    assert runner.launch_flow(Nextpnr, design, settings).succeeded
    assert runner.launch_flow(Nextpnr, design, settings).reused
    (source if changed == "source" else setting).write_text("# edited\n")
    flow = runner.launch_flow(Nextpnr, design, settings)
    assert flow.succeeded and not flow.reused
    assert runner.launch_flow(Nextpnr, design, settings).reused
