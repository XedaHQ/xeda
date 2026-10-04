"""Design targets: one design file, one target per board, selected with `--target`.

A target is an overlay on the design it is written in. Selecting one yields an ordinary
`Design`, equal to the same design written flat with the overlay applied by hand, so nothing
after the loader knows about targets but the documents that report the name.
"""

import json
import shutil
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from xeda import Design
from xeda.cli import cli
from xeda.design import DesignValidationError
from xeda.flow import FlowSettingsError
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.default_runner import semantic_hash
from xeda.flows import VivadoSynth
from xeda.xedaproject import XedaProject

from .tool_utils import use_fake_tools

RESOURCES = Path(__file__).parent / "resources" / "targets"
KNIGHT = RESOURCES / "knight.yaml"
SINGLE = RESOURCES / "single.yaml"


def design_hash(design: Design) -> str:
    return semantic_hash(dict(rtl_hash=design.rtl_hash, tb_hash=design.tb_hash))


def write_design(tmp_path: Path, data: dict, name: str = "d.yaml") -> Path:
    for source in ("knight.v", "por_sync.v", "knight_tb.v", "arty.xdc", "ulx3s.lpf"):
        if not (tmp_path / source).exists():
            shutil.copy(RESOURCES / source, tmp_path / source)
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


BASE = {"name": "d", "rtl": {"sources": ["knight.v"], "top": "knight"}}


def error_of(tmp_path: Path, data: dict, **kwargs) -> str:
    with pytest.raises(DesignValidationError) as raised:
        Design.from_file(write_design(tmp_path, data), **kwargs)
    return str(raised.value)


# ------------------------------------------------------------------------------ the oracle


@pytest.mark.parametrize("target", ["arty", "ulx3s"])
def test_a_selected_target_is_the_design_written_flat_by_hand(target):
    selected = Design.from_file(KNIGHT, target=target)
    flat = Design.from_file(RESOURCES / f"knight_{target}_flat.yaml")

    assert selected.target == target and flat.target is None
    assert selected.rtl_hash == flat.rtl_hash
    assert selected.tb_hash == flat.tb_hash
    assert design_hash(selected) == design_hash(flat)
    for name in Design.model_fields:
        if name != "target":
            assert getattr(selected, name) == getattr(flat, name), name
    assert selected.model_dump(exclude={"target"}) == flat.model_dump(exclude={"target"})
    assert selected.model_dump(mode="json", exclude={"target"}) == flat.model_dump(
        mode="json", exclude={"target"}
    )


def test_the_target_name_is_not_identity(tmp_path):
    """Two targets with the same overlay are the same design under two names."""
    overlay = {"defines": {"A": 1}}
    path = write_design(tmp_path, {**BASE, "targets": {"one": overlay, "two": dict(overlay)}})
    one, two = Design.from_file(path, target="one"), Design.from_file(path, target="two")
    assert (one.target, two.target) == ("one", "two")
    assert design_hash(one) == design_hash(two)


def test_sources_are_appended_and_other_lists_replace(tmp_path):
    data = {
        "name": "d",
        "authors": ["A <a@example.com>", "B <b@example.com>"],
        "rtl": {"sources": ["knight.v", "por_sync.v"], "top": "knight"},
        "tb": {"sources": ["knight_tb.v"], "top": "knight_tb"},
        "targets": {
            "t": {
                "authors": ["C <c@example.com>"],
                "sources": ["arty.xdc"],
                "tb": {"sources": ["ulx3s.lpf"]},
            }
        },
    }
    design = Design.from_file(write_design(tmp_path, data))
    assert [s.file.name for s in design.rtl.sources] == ["knight.v", "por_sync.v", "arty.xdc"]
    assert [s.file.name for s in design.tb.sources] == ["knight_tb.v", "ulx3s.lpf"]
    assert design.authors == ["C <c@example.com>"]
    assert design.rtl.top == "knight" and design.tb.top == ("knight_tb",)


def test_mappings_merge_key_by_key_at_every_depth():
    design = Design.from_file(KNIGHT, target="ulx3s")
    assert design.rtl.parameters == {"WIDTH": 4}
    assert design.rtl.defines == {"CLK_HZ": 25000000}
    assert design.flow["nextpnr"] == {"seed": 3, "board": "ULX3S_85F"}
    assert design.flow["vivado_synth"] == {"clock": {"period": 10.0}}
    assert design.rtl.clock is not None and design.rtl.clock.port == "CLK"


def test_a_target_meets_the_design_s_other_spelling_of_a_key(tmp_path):
    """`flow`/`flows`, `generics`/`parameters`: one setting, however each side spells it."""
    data = {
        "name": "d",
        "rtl": {"sources": ["knight.v"], "top": "knight", "generics": {"WIDTH": 8, "DEPTH": 2}},
        "flow": {"nextpnr": {"seed": 3}},
        "targets": {
            "t": {"parameters": {"WIDTH": 4}, "flows": {"nextpnr": {"board": "ULX3S_85F"}}}
        },
    }
    design = Design.from_file(write_design(tmp_path, data))
    assert design.rtl.parameters == {"WIDTH": 4, "DEPTH": 2}
    assert design.flow["nextpnr"] == {"seed": 3, "board": "ULX3S_85F"}


def test_dotted_keys_in_a_target_are_expanded(tmp_path):
    data = {**BASE, "targets": {"t": {"clock.port": "CLK", "flows.nextpnr.seed": 5}}}
    design = Design.from_file(write_design(tmp_path, data))
    assert design.rtl.clock is not None and design.rtl.clock.port == "CLK"
    assert design.flow == {"nextpnr": {"seed": 5}}


def test_a_target_s_paths_resolve_against_the_design_root(tmp_path, monkeypatch):
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    (tmp_path / "d").mkdir()
    path = write_design(tmp_path / "d", {**BASE, "targets": {"t": {"sources": ["arty.xdc"]}}})
    design = Design.from_file(path)
    assert design.rtl.sources[-1].file == (tmp_path / "d" / "arty.xdc").resolve()


def test_design_overrides_win_over_the_target(tmp_path):
    data = {**BASE, "targets": {"t": {"top": "from_target", "defines": {"A": 1, "B": 1}}}}
    design = Design.from_file(
        write_design(tmp_path, data), overrides={"top": "from_override", "defines": {"A": 2}}
    )
    assert design.rtl.top == "from_override"
    assert design.rtl.defines == {"A": 2, "B": 1}


# ------------------------------------------------------------------------------ selection


def test_one_target_and_no_selection_uses_that_target():
    design = Design.from_file(SINGLE)
    assert design.target == "arty"
    assert [s.file.name for s in design.rtl.sources] == ["knight.v", "arty.xdc"]
    assert design.rtl.defines == {"CLK_HZ": 100000000}
    assert design.rtl.top == "knight"


def test_several_targets_and_no_selection_lists_them():
    with pytest.raises(DesignValidationError) as raised:
        Design.from_file(KNIGHT)
    message = str(raised.value)
    assert "arty, ulx3s" in message and "--target" in message
    assert str(KNIGHT) in message


def test_an_unknown_target_lists_them_with_a_close_match():
    with pytest.raises(DesignValidationError) as raised:
        Design.from_file(KNIGHT, target="ulx3")
    message = str(raised.value)
    assert "'ulx3'" in message and "arty, ulx3s" in message
    assert "did you mean 'ulx3s'" in message


def test_a_target_for_a_design_without_targets_is_an_error(tmp_path):
    message = error_of(tmp_path, BASE, target="arty")
    assert "'arty'" in message and "no `targets`" in message


def test_a_design_without_targets_is_what_it_was(tmp_path):
    """No `targets`: nothing is recorded, and the dump has the same content as before."""
    design = Design.from_file(write_design(tmp_path, BASE))
    assert design.target is None
    dump = design.model_dump(mode="json")
    assert dump.pop("target") is None
    assert "targets" not in dump
    # pinned on main (bfd08478) for this very design: the loader's result is unchanged
    assert design.rtl_hash == Design(design_root=tmp_path, **BASE).rtl_hash


def test_an_empty_targets_table_is_no_targets(tmp_path):
    design = Design.from_file(write_design(tmp_path, {**BASE, "targets": {}}))
    assert design.target is None


def test_a_design_built_from_data_selects_its_only_target(tmp_path):
    write_design(tmp_path, BASE)
    design = Design(design_root=tmp_path, **BASE, targets={"t": {"defines": {"A": 1}}})
    assert design.target == "t" and design.rtl.defines == {"A": 1}
    with pytest.raises(DesignValidationError, match="a, b"):
        Design(design_root=tmp_path, **BASE, targets={"a": {}, "b": {}})
    chosen = Design(
        design_root=tmp_path, **Design.select_target({**BASE, "targets": {"a": {}, "b": {}}}, "b")
    )
    assert chosen.target == "b"


def test_a_recorded_target_survives_a_dump_and_reload():
    design = Design.from_file(KNIGHT, target="arty")
    reloaded = Design(**design.model_dump())
    assert reloaded.target == "arty"
    assert reloaded.model_dump() == design.model_dump()
    assert design_hash(reloaded) == design_hash(design)


# ------------------------------------------------------------------------------ strict input


@pytest.mark.parametrize("name", ["1st", "has space", "a/b", ""])
def test_a_target_name_is_a_name(tmp_path, name):
    message = error_of(tmp_path, {**BASE, "targets": {name: {}}})
    assert repr(name) in message and "not a target name" in message


@pytest.mark.parametrize("name", ["vivado_synth", "VivadoSynth", "nextpnr", "vivado-synth"])
def test_a_target_is_not_named_as_a_flow(tmp_path, name):
    message = error_of(tmp_path, {**BASE, "targets": {"ok": {}, name: {}}}, target="ok")
    assert repr(name) in message and "flow" in message
    assert f"targets.{name}" in message


def test_every_flow_name_and_alias_is_refused_as_a_target_name(tmp_path):
    from xeda.flow import registered_flows

    write_design(tmp_path, BASE)
    assert VivadoSynth.name in registered_flows
    for name in registered_flows:
        # a flow's name, or (a test's own `_Flow`) no name at all
        with pytest.raises(DesignValidationError, match="name of a flow|not a target name"):
            Design.select_target({**BASE, "targets": {name: {}}})


@pytest.mark.parametrize("key", ["name", "targets", "target", "design_root"])
def test_keys_that_are_the_design_s_alone_are_refused_in_a_target(tmp_path, key):
    message = error_of(tmp_path, {**BASE, "targets": {"t": {key: "x"}}})
    assert f"targets.t.{key}" in message and "not allowed in a target" in message


def test_an_unknown_key_in_a_target_names_it_and_a_close_match(tmp_path):
    data = {**BASE, "targets": {"t": {}, "u": {"sorces": ["arty.xdc"]}}}
    message = error_of(tmp_path, data, target="t")
    assert "targets.u.sorces" in message and "did you mean `sources`" in message


def test_an_unknown_nested_key_in_a_target_is_the_loader_s_error(tmp_path):
    message = error_of(tmp_path, {**BASE, "targets": {"t": {"rtl": {"tpo": "x"}}}})
    assert "rtl.tpo" in message and "Extra inputs are not permitted" in message


def test_a_target_is_a_table(tmp_path):
    assert "targets.t" in error_of(tmp_path, {**BASE, "targets": {"t": "arty.xdc"}})
    assert "targets" in error_of(tmp_path, {**BASE, "targets": ["t"]})


def test_there_is_no_singular_target_table(tmp_path):
    message = error_of(tmp_path, {**BASE, "target": {"t": {}}})
    assert "target:" in message and "`targets.<name>`" in message
    message = error_of(tmp_path, {**BASE, "target": "t", "targets": {"t": {}}})
    assert "`targets.<name>`" in message


# ------------------------------------------------------------------------------ projects


def project_file(tmp_path: Path) -> Path:
    design = yaml.safe_load(KNIGHT.read_text())
    write_design(tmp_path, design, "unused.yaml")
    path = tmp_path / "xedaproject.yaml"
    path.write_text(yaml.safe_dump({"designs": [design, {**BASE, "name": "plain"}]}))
    return path


def test_a_project_design_takes_a_target_the_same_way(tmp_path):
    project = XedaProject.from_file(project_file(tmp_path))
    design = project.get_design("knight", target="ulx3s")
    assert design is not None and design.target == "ulx3s"
    flat = Design.from_file(RESOURCES / "knight_ulx3s_flat.yaml")
    assert design.rtl_hash == flat.rtl_hash and design.flow == flat.flow
    with pytest.raises(DesignValidationError, match="arty, ulx3s"):
        project.get_design("knight")
    with pytest.raises(DesignValidationError, match="no `targets`"):
        project.get_design("plain", target="arty")
    plain = project.get_design("plain")
    assert plain is not None and plain.target is None


# ------------------------------------------------------------------------------ the launcher and CLI


def invoke(*args: str):
    result = CliRunner().invoke(cli, [*args, "--json"], catch_exceptions=False)
    return result, json.loads(result.stdout)


def test_the_launcher_plans_the_selected_target(tmp_path):
    launcher = DefaultRunner(tmp_path / "xeda_run")
    plan = launcher.plan("vivado_synth", design=KNIGHT, target="arty")
    flat = launcher.plan("vivado_synth", design=RESOURCES / "knight_arty_flat.yaml")
    assert plan.context.target == "arty" and flat.context.target is None
    assert plan.context.design_hash == flat.context.design_hash
    assert [n.flowrun_hash for n in plan.nodes] == [n.flowrun_hash for n in flat.nodes]
    assert plan.node("vivado_synth").settings == flat.node("vivado_synth").settings


def test_a_built_design_takes_no_target(tmp_path):
    design = Design.from_file(KNIGHT, target="arty")
    with pytest.raises(ValueError, match="already built"):
        DefaultRunner(tmp_path / "xeda_run").plan("vivado_synth", design=design, target="ulx3s")


def test_a_dry_run_reports_the_target(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result, document = invoke("run", "vivado_synth", str(KNIGHT), "--target", "arty", "--dry-run")
    assert result.exit_code == 0, document
    assert document["target"] == "arty" and document["plan"]["target"] == "arty"

    result, document = invoke(
        "run", "vivado_synth", str(SINGLE.parent / "knight_arty_flat.yaml"), "--dry-run"
    )
    assert result.exit_code == 0, document
    assert document["target"] is None and document["plan"]["target"] is None

    text = CliRunner().invoke(
        cli, ["run", "vivado_synth", str(KNIGHT), "--target", "arty", "--dry-run"]
    )
    assert text.exit_code == 0 and "target arty" in text.output


def test_a_run_reports_the_target(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    use_fake_tools(monkeypatch)
    result, document = invoke("run", "vivado_synth", str(KNIGHT), "--target", "arty")
    assert result.exit_code == 0, document
    assert document["success"] is True and document["target"] == "arty"
    settings = json.loads(Path(document["settings_json"]).read_text())
    assert settings["design"]["target"] == "arty"

    flat = RESOURCES / "knight_arty_flat.yaml"
    result, document = invoke("run", "vivado_synth", str(flat), "--run-root", "flat")
    assert result.exit_code == 0, document
    assert document["target"] is None


def test_a_failed_selection_is_a_document_that_names_the_targets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result, document = invoke("run", "vivado_synth", str(KNIGHT))
    assert result.exit_code == 1 and document["success"] is False
    assert document["target"] is None
    assert document["error"]["type"] == "DesignValidationError"
    assert "arty, ulx3s" in document["error"]["message"]

    result, document = invoke("run", "vivado_synth", str(KNIGHT), "--target", "nexys")
    assert result.exit_code == 1 and document["target"] == "nexys"
    assert "arty, ulx3s" in document["error"]["message"]
    assert not (tmp_path / "xeda_run").exists()


def test_dse_takes_a_target(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result, document = invoke(
        "dse",
        "vivado_synth",
        "--design",
        str(KNIGHT),
        "--target",
        "nexys",
        "--init-freq-low",
        "100",
        "--init-freq-high",
        "200",
    )
    assert result.exit_code == 1 and document["target"] == "nexys"
    assert "arty, ulx3s" in document["error"]["message"]


# ------------------------------------------------------------------------------ the layer order


def layered_project(tmp_path: Path) -> Path:
    """A project and two copies of one design: `plain` has no targets, `layered` has `ulx3s`.

    Every layer writes `seed`: the project 1, the design 3, the target 5. The project alone writes
    `placer`; the design alone writes `rtl.parameters.DEPTH` and `rtl.defines.B`; the target
    overrides `board`, `rtl.parameters.WIDTH` and `rtl.defines.A`, which the design wrote too.
    """
    design = {
        "name": "plain",
        "rtl": {
            "sources": ["knight.v"],
            "top": "knight",
            "parameters": {"WIDTH": 8, "DEPTH": 2},
            "defines": {"A": 1, "B": 2},
        },
        "flows": {"nextpnr": {"seed": 3, "board": "ARTY_A7_100T"}},
    }
    target = {
        "rtl": {"parameters": {"WIDTH": 4}, "defines": {"A": 9}},
        "flows": {"nextpnr": {"seed": 5, "board": "ULX3S_85F"}},
    }
    write_design(tmp_path, design, "unused.yaml")
    path = tmp_path / "xedaproject.yaml"
    project = {
        "flows": {"nextpnr": {"seed": 1, "placer": "heap"}},
        "designs": [design, {**design, "name": "layered", "targets": {"ulx3s": target}}],
    }
    path.write_text(yaml.safe_dump(project))
    return path


def planned_nextpnr(tmp_path: Path, design: str, target: str | None = None, **layers):
    plan = DefaultRunner(tmp_path / "xeda_run").plan(
        "nextpnr",
        design,
        xedaproject=str(layered_project(tmp_path)),
        target=target,
        **layers,
    )
    return plan.node("nextpnr").settings


def test_a_target_overrides_the_design_and_only_where_it_writes(tmp_path):
    """defaults < project < design < target < command line < API, key by key."""
    project = XedaProject.from_file(layered_project(tmp_path))
    plain, target = project.get_design("plain"), project.get_design("layered", "ulx3s")
    assert plain is not None and target is not None
    # Design-level keys: the target's value wins; what it does not write stays the design's.
    assert plain.rtl.parameters == {"WIDTH": 8, "DEPTH": 2} and plain.rtl.defines == {
        "A": 1,
        "B": 2,
    }
    assert target.rtl.parameters == {"WIDTH": 4, "DEPTH": 2}
    assert target.rtl.defines == {"A": 9, "B": 2}

    # Flow settings, all the way down: the project's `placer` survives every layer above it.
    base = planned_nextpnr(tmp_path, "plain")
    assert (base.seed, base.placer, base.board) == (3, "heap", "ARTY_A7_100T")  # design > project
    chosen = planned_nextpnr(tmp_path, "layered", "ulx3s")
    assert (chosen.seed, chosen.placer, chosen.board) == (5, "heap", "ULX3S_85F")  # target > design
    cli = planned_nextpnr(tmp_path, "layered", "ulx3s", flow_settings=["seed=9"])
    assert (cli.seed, cli.placer, cli.board) == (9, "heap", "ULX3S_85F")  # command line > target
    qualified = planned_nextpnr(
        tmp_path, "layered", "ulx3s", flow_settings=["flows.nextpnr.seed=8"]
    )
    assert qualified.seed == 8
    api = planned_nextpnr(tmp_path, "layered", "ulx3s", flow_overrides={"seed": 11})
    assert api.seed == 11  # API > target
    both = planned_nextpnr(
        tmp_path, "layered", "ulx3s", flow_settings=["seed=9"], flow_overrides={"seed": 11}
    )
    assert both.seed == 11  # API > command line > target
    board = planned_nextpnr(tmp_path, "layered", "ulx3s", flow_settings=["board=ARTY_A7_35T"])
    assert board.board == "ARTY_A7_35T"  # a command-line `board` beats the target's too


def test_a_target_disagreeing_with_its_design_on_a_shared_leaf_is_not_an_error(tmp_path):
    """The agreement rule is between two nodes of one graph; a target is the design's own author.

    The design says `board: ARTY_A7_100T` for `nextpnr` and the target `ULX3S_85F`: the target
    wins, with no error naming both. (Two *nodes* disagreeing, yosys against nextpnr, is the
    resolver's error; that is another mechanism, tested in `test_resolver`.)
    """
    settings = planned_nextpnr(tmp_path, "layered", "ulx3s")
    assert settings.board == "ULX3S_85F"
    assert settings.fpga.part == "LFE5U-85F-6BG381C"


def test_two_nodes_still_must_agree_when_a_target_supplies_one_side(tmp_path):
    """A target overrides its design, but `yosys_fpga` and `nextpnr` still agree on `fpga`."""
    data = {
        "name": "d",
        "rtl": {"sources": ["knight.v"], "top": "knight"},
        "flows": {"nextpnr": {"board": "ARTY_A7_100T"}},
        "targets": {"t": {"flows": {"yosys_fpga": {"fpga": "LFE5U-85F-6BG381C"}}}},
    }
    path = write_design(tmp_path, data)
    with pytest.raises(FlowSettingsError, match="disagrees with yosys_fpga"):
        DefaultRunner(tmp_path / "xeda_run").plan("nextpnr", path, target="t")


@pytest.mark.parametrize("leaf", ["board", "fpga", "custom_boards_file"])
def test_a_shared_leaf_at_a_target_s_top_level_is_refused_and_says_where_it_goes(tmp_path, leaf):
    """Not implemented yet: a shared leaf is a setting of each flow, under `flows.<flow>`."""
    message = error_of(tmp_path, {**BASE, "targets": {"t": {leaf: "x"}}})
    assert f"targets.t.{leaf}" in message
    assert f"flows.<flow>.{leaf}" in message and "not supported at a target's top level" in message


# ------------------------------------------------------------------------------ what a target cannot do yet


def test_a_design_clock_carries_no_frequency(tmp_path):
    """Investigation (a): `rtl.clock` names a port; a constraint is a flow's `clock` setting."""
    message = error_of(
        tmp_path, {**BASE, "targets": {"t": {"clock": {"port": "CLK", "freq": "100MHz"}}}}
    )
    assert "freq" in message and "Extra inputs are not permitted" in message


def test_a_target_constrains_a_flow_s_clock_through_its_flow_section(tmp_path):
    """What works today: the target's `flows.<flow>.clock`, matched to the design's clock port."""
    data = {
        "name": "d",
        "rtl": {"sources": ["knight.v"], "top": "knight", "clock": {"port": "CLK"}},
        "flows": {"vivado_synth": {"fpga": {"part": "xc7a100tcsg324-1"}}},
        "targets": {
            "fast": {"flows": {"vivado_synth": {"clock": {"freq": "200MHz"}}}},
            "slow": {"flows": {"vivado_synth": {"clock": {"freq": "25MHz"}}}},
        },
    }
    path = write_design(tmp_path, data)
    launcher = DefaultRunner(tmp_path / "xeda_run")
    periods = {}
    for target in ("fast", "slow"):
        design = Design.from_file(path, target=target)
        settings = VivadoSynth.Settings(**design.flow["vivado_synth"])
        flow = VivadoSynth(settings, design, tmp_path / target)
        clock = flow.settings.clocks["main_clock"]
        periods[target] = clock.period
        assert clock.port == "CLK"
        assert launcher.plan("vivado_synth", design=path, target=target).context.target == target
    assert periods == {"fast": 5.0, "slow": 40.0}
