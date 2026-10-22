"""The platform and its PDK pin are shared along the edges of an ASIC chain.

The seven settings of `AsicPlatformSettings` agree along a declared edge like the other shared
settings. The pin of the PDK (provider and version) is filled in at planning from the residue of
the platform, so it is part of the identity of every node, while `pdk_root`, the place of the
store, is not. A flow that uses `AsicPlatformFlow` runs with `platform_file` alone.

The test flows below are out of the sweeps over the flows of the package (their names start
with `_`). Their store is a copy of the `tiny` fixture platform, and their residue is a user file
(`platform_file`) in the design.
"""

import json
import re
import shutil
import textwrap
from pathlib import Path
from typing import ClassVar, Optional

import pytest

from xeda.flow import MetricSpec
from xeda import Design
from xeda.design import SourceType
from xeda.dataclass import Field
from xeda.flow import Flow, FlowDependencyFailure, FlowSettingsError, FlowSettingsException, In
from xeda.flow.asic import AsicPlatformFlow, AsicPlatformSettings
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.trace_inputs import registered_input_files
from xeda.platforms import PdkNotInstalled

from .flow_registry import preserved_flow_registry
from .io_flows import _Maker, _Taker
from .settings_samples import flow_classes

TINY = Path(__file__).parent / "resources" / "platforms" / "tiny"
COMMIT = "6b305a712966be72d8908695c04398acdbe273ca"
OTHER_COMMIT = "1111111111111111111111111111111111111111"
ROLES = ["liberty", "tech_lef", "cell_lef"]


class _AsicMaker(AsicPlatformFlow, _Maker):
    """Makes `made.txt` for one platform, and registers the files of the platform it opens."""

    results_description = {}

    class Settings(AsicPlatformSettings, _Maker.Settings):
        pass

    def prepare_inputs(self) -> None:
        self.settings.locate_platform()
        self.implicit_inputs.extend(self.locate_prepared_inputs() or [])

    def locate_prepared_inputs(self) -> Optional[list[Path]]:
        return _opened(self.settings)


class _AsicTaker(AsicPlatformFlow, _Taker):
    """Reads what `_AsicMaker` made, on the same platform. It needs a platform."""

    results_description = {}
    required_settings = {
        "platform": "a bundled platform name, or a residue file in `platform_file`"
    }

    class Settings(AsicPlatformSettings, _Taker.Settings):
        pass

    class Inputs(_Taker.Inputs):
        made: Path = In(
            SourceType.Data,
            producer="__asic_maker",
            output="made",
            description="What it reads.",
        )

    def prepare_inputs(self) -> None:
        self.settings.locate_platform()
        self.implicit_inputs.extend(self.locate_prepared_inputs() or [])

    def locate_prepared_inputs(self) -> Optional[list[Path]]:
        return _opened(self.settings)


def _opened(settings: AsicPlatformSettings) -> list[Path]:
    return settings.platform_files(ROLES)


RESIDUE = """\
# tiny.yaml
name: tiny
providers:
  default: {default}
  directory: {{layout: orfs}}
  orfs: {{version: "{version}", directory: flow/platforms/tiny}}
upstream: {{adapter: orfs_config_mk, files: [config.mk]}}
corners: {{default: typical, aliases: {{tt: typical}}}}
files:
  liberty: {{typical: [lib/tiny.lib]}}
  tech_lef: lef/tiny.tlef
  cell_lef: [lef/tiny.lef]
"""


def _write_residue(root: Path, *, default: str = "directory", version: str = COMMIT) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "tiny.yaml"
    path.write_text(RESIDUE.format(default=default, version=version))
    return path


def _store(tmp_path: Path, name: str = "store") -> Path:
    """A store for the `directory` provider (layout `orfs`): `<store>/tiny` holds the files."""
    platform = tmp_path / name / "tiny"
    for source, target in (
        ("tiny.lib", "lib/tiny.lib"),
        ("tiny.tlef", "lef/tiny.tlef"),
        ("tiny.lef", "lef/tiny.lef"),
        ("config.mk", "config.mk"),
    ):
        (platform / target).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(TINY / source, platform / target)
    return platform.parent


def _design(root: Path, **sections) -> Design:
    root.mkdir(parents=True, exist_ok=True)
    return Design(name="d", design_root=root, rtl={"sources": [], "top": "t"}, flow=sections)


def _runner(tmp_path: Path) -> DefaultRunner:
    return DefaultRunner(tmp_path / "run", display_results=False)


def _plan(tmp_path: Path, root: Path, settings=(), flow=_AsicTaker, **sections):
    return _runner(tmp_path).plan(flow, _design(root, **sections), flow_settings=list(settings))


def _hashes(plan) -> dict[str, str]:
    return {node.name: node.flowrun_hash for node in plan.nodes}


# ---------------------------------------------------------------------------------------------
# Agreement
# ---------------------------------------------------------------------------------------------


def test_a_location_given_on_one_node_reaches_every_connected_node(tmp_path):
    store = _store(tmp_path)
    root = tmp_path / "d"
    _write_residue(root)
    plan = _plan(tmp_path, root, [f"pdk_root={store}", "platform_file=tiny.yaml"])
    assert [node.name for node in plan.nodes] == ["__asic_maker", "__asic_taker"]
    for node in plan.nodes:
        assert node.settings.pdk_root == store.resolve(), node.name
        assert node.settings.platform_file == (root / "tiny.yaml").resolve(), node.name


def test_a_section_of_a_producer_reaches_the_consumer(tmp_path):
    store = _store(tmp_path)
    root = tmp_path / "d"
    _write_residue(root)
    plan = _plan(
        tmp_path,
        root,
        __asic_maker={"pdk_root": str(store), "platform_file": "tiny.yaml", "corner": "tt"},
    )
    for node in plan.nodes:
        assert node.settings.pdk_root == store.resolve(), node.name
        assert node.settings.corner == "tt", node.name


def test_two_stores_fail_and_name_both_locations(tmp_path):
    first, second = _store(tmp_path, "first"), _store(tmp_path, "second")
    root = tmp_path / "d"
    _write_residue(root)
    with pytest.raises(FlowSettingsError) as raised:
        _plan(
            tmp_path,
            root,
            __asic_maker={"pdk_root": str(first), "platform_file": "tiny.yaml"},
            __asic_taker={"pdk_root": str(second)},
        )
    message = str(raised.value)
    assert "pdk_root" in message and str(first.resolve()) in message
    assert str(second.resolve()) in message


def test_two_platform_files_fail_and_name_both_locations(tmp_path):
    root = tmp_path / "d"
    _write_residue(root)
    (root / "other").mkdir()
    shutil.copy(root / "tiny.yaml", root / "other" / "tiny.yaml")
    with pytest.raises(FlowSettingsError) as raised:
        _plan(
            tmp_path,
            root,
            __asic_maker={"platform_file": "tiny.yaml"},
            __asic_taker={"platform_file": "other/tiny.yaml"},
        )
    message = str(raised.value)
    assert "platform_file" in message
    assert str(root / "tiny.yaml") in message and str(root / "other" / "tiny.yaml") in message


def test_a_relative_location_is_read_against_the_design_root(tmp_path):
    store = _store(tmp_path / "d" / "pdks")
    root = tmp_path / "d"
    _write_residue(root)
    plan = _plan(
        tmp_path, root, ["pdk_root=pdks/store", "platform_file=tiny.yaml"], __asic_maker={}
    )
    for node in plan.nodes:
        assert node.settings.pdk_root == store.resolve(), node.name


def test_the_default_store_is_the_home_directory_of_ciel(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "d"
    _write_residue(root)
    settings = AsicPlatformSettings.from_input(
        {"platform_file": "tiny.yaml"}, design_root=root, runner_cwd=tmp_path
    )
    assert settings.pdk_root is None
    with pytest.raises(PdkNotInstalled, match=re.escape(str(tmp_path / "home" / ".ciel"))):
        settings.locate_platform()


def test_the_install_command_of_a_user_file_names_the_file(tmp_path):
    """A platform from a user file is installed with `--platform-file`, not by a bundled name."""
    user = _write_residue(tmp_path, default="orfs")
    store = tmp_path / "empty"
    with pytest.raises(PdkNotInstalled) as raised:
        _settings(tmp_path, platform_file="tiny.yaml", pdk_root=str(store)).locate_platform()
    install = [line for line in str(raised.value).splitlines() if "install:" in line]
    assert len(install) == 1
    assert f"--platform-file {user.resolve()}" in install[0]
    assert "xeda pdk install tiny" not in install[0]


def test_a_flow_that_declares_a_platform_declares_the_pin_of_its_pdk():
    """The pin is part of the identity of every node of a group. A node whose settings declare
    `platform` and not the pin would leave it out, so each such flow inherits
    `AsicPlatformSettings`."""
    declaring = [name for cls, name in flow_classes() if "platform" in cls.Settings.model_fields]
    assert "yosys" in declaring
    for cls, name in flow_classes():
        if "platform" in cls.Settings.model_fields:
            assert issubclass(cls.Settings, AsicPlatformSettings), name
            assert issubclass(cls, AsicPlatformFlow), name


def test_a_flow_that_declares_a_platform_without_the_pin_is_refused_when_planned(tmp_path):
    with preserved_flow_registry():

        class _PlatformWithoutPin(Flow):
            """Declares `platform` and no pin."""

            results_description: ClassVar[dict[str, MetricSpec]] = {}

            class Settings(Flow.Settings):
                platform: Optional[str] = Field(None, description="A platform.")

            def run(self) -> None:
                pass

        with pytest.raises(TypeError, match="_platform_without_pin.*AsicPlatformSettings"):
            _runner(tmp_path).plan(
                _PlatformWithoutPin,
                _design(tmp_path / "d"),
                flow_settings=["platform=nangate45"],
            )


def test_a_variable_in_the_location_is_expanded(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "d"
    _write_residue(root)
    settings = AsicPlatformSettings.from_input(
        {"platform_file": "tiny.yaml", "pdk_root": "$HOME/store"},
        design_root=root,
        runner_cwd=tmp_path,
    )
    assert settings.pdk_root == store
    assert settings.locate_platform().directory == store / "tiny"


# ---------------------------------------------------------------------------------------------
# The pin
# ---------------------------------------------------------------------------------------------


def test_an_unset_pin_is_filled_in_from_the_residue_in_every_node(tmp_path):
    root = tmp_path / "d"
    _write_residue(root, default="orfs", version=OTHER_COMMIT)
    plan = _plan(tmp_path, root, ["platform_file=tiny.yaml"])
    for node in plan.nodes:
        assert node.settings.pdk_provider == "orfs", node.name
        assert node.settings.pdk_version == OTHER_COMMIT, node.name


def test_the_pin_of_a_directory_is_unpinned_unless_given(tmp_path):
    root = tmp_path / "d"
    _write_residue(root)
    plan = _plan(tmp_path, root, ["platform_file=tiny.yaml"])
    for node in plan.nodes:
        assert (node.settings.pdk_provider, node.settings.pdk_version) == ("directory", "unpinned")
    plan = _plan(tmp_path, root, ["platform_file=tiny.yaml", "pdk_version=abc"])
    for node in plan.nodes:
        assert (node.settings.pdk_provider, node.settings.pdk_version) == ("directory", "abc")


def test_a_given_provider_takes_its_own_pin(tmp_path):
    root = tmp_path / "d"
    _write_residue(root, default="directory")
    plan = _plan(tmp_path, root, ["platform_file=tiny.yaml", "pdk_provider=orfs"])
    for node in plan.nodes:
        assert (node.settings.pdk_provider, node.settings.pdk_version) == ("orfs", COMMIT)


def test_a_bundled_platform_gives_its_pin(tmp_path):
    from xeda.platforms import load_residue

    entry = load_residue("nangate45").providers.orfs
    assert entry is not None
    plan = _plan(tmp_path, tmp_path / "d", ["platform=nangate45"])
    for node in plan.nodes:
        assert (node.settings.pdk_provider, node.settings.pdk_version) == ("orfs", entry.version)


def test_the_provider_that_is_given_takes_its_own_pin_from_its_own_entry(tmp_path):
    from xeda.platforms import load_residue

    entry = load_residue("sky130hd").providers.lambdapdk
    assert entry is not None
    plan = _plan(tmp_path, tmp_path / "d", ["platform=sky130hd", "pdk_provider=lambdapdk"])
    for node in plan.nodes:
        assert (node.settings.pdk_provider, node.settings.pdk_version) == (
            "lambdapdk",
            entry.version,
        ), node.name


def test_a_corner_of_ciel_only_is_refused_at_planning_when_lambdapdk_is_selected(tmp_path):
    chosen = ["platform=sky130hd", "corner=ss_100C_1v60"]
    _plan(tmp_path, tmp_path / "d", chosen)  # ciel has this corner
    with pytest.raises(FlowSettingsException, match="ss_n40C_1v40.*provider .lambdapdk."):
        _plan(tmp_path, tmp_path / "d", [*chosen, "pdk_provider=lambdapdk"])


def test_nothing_is_filled_in_without_a_platform(tmp_path):
    plan = _plan(tmp_path, tmp_path / "d", flow=_AsicMaker)
    (node,) = plan.nodes
    assert node.settings.pdk_provider is None and node.settings.pdk_version is None


def test_the_pin_is_part_of_the_identity_of_every_node(tmp_path):
    hashes = {}
    for name, version in (("a", COMMIT), ("b", OTHER_COMMIT), ("c", COMMIT)):
        root = tmp_path / name / "d"
        _write_residue(root, default="orfs", version=version)
        hashes[name] = _hashes(_plan(tmp_path / name, root, ["platform_file=tiny.yaml"]))
    assert hashes["a"].keys() == hashes["b"].keys()
    for node in hashes["a"]:
        assert hashes["a"][node] != hashes["b"][node], node
        assert hashes["a"][node] == hashes["c"][node], node


def test_a_moved_store_is_the_same_run(tmp_path):
    first, second = _store(tmp_path, "first"), _store(tmp_path, "second")
    root = tmp_path / "d"
    _write_residue(root)
    plans = [
        _plan(tmp_path, root, ["platform_file=tiny.yaml", f"pdk_root={s}"])
        for s in (
            first,
            second,
        )
    ]
    assert _hashes(plans[0]) == _hashes(plans[1])
    assert plans[0].node("__asic_maker").settings.pdk_root != (
        plans[1].node("__asic_maker").settings.pdk_root
    )


# ---------------------------------------------------------------------------------------------
# A platform file alone
# ---------------------------------------------------------------------------------------------


def test_a_platform_file_alone_is_enough(tmp_path):
    root = tmp_path / "d"
    _write_residue(root)
    plan = _plan(tmp_path, root, ["platform_file=tiny.yaml"])
    assert all(node.settings.platform is None for node in plan.nodes)
    states = _runner(tmp_path).plan_with_freshness(
        _AsicTaker, _design(root), flow_settings=["platform_file=tiny.yaml"]
    )[1]
    assert set(states) == {"__asic_maker", "__asic_taker"}


def test_a_consumer_without_a_platform_and_a_platform_file_is_refused(tmp_path):
    with pytest.raises(FlowSettingsException, match=r"__asic_taker needs `platform`"):
        _plan(tmp_path, tmp_path / "d")


def test_a_platform_alone_is_enough_for_the_requirement(tmp_path):
    plan = _plan(tmp_path, tmp_path / "d", ["platform=nangate45"])
    assert plan.node("__asic_taker").settings.platform == "nangate45"


def test_the_requirement_loses_the_platform_when_a_file_is_given(tmp_path):
    root = tmp_path / "d"
    plain = _AsicTaker.Settings.from_input({}, design_root=root, runner_cwd=tmp_path)
    given = _AsicTaker.Settings.from_input(
        {"platform_file": "tiny.yaml"}, design_root=root, runner_cwd=tmp_path
    )
    assert set(_AsicTaker.required_settings_for(plain)) == {"platform"}
    assert _AsicTaker.required_settings_for(given) == {}
    assert _AsicMaker.required_settings_for(given) == {}


def test_a_launch_with_a_platform_file_alone_registers_the_files_it_locates(tmp_path):
    store = _store(tmp_path)
    root = tmp_path / "d"
    _write_residue(root)
    design = _design(root)
    flow_settings = {"platform_file": "tiny.yaml", "pdk_root": store}

    def launch():
        flow = _runner(tmp_path).run_flow(_AsicTaker, design, flow_settings)
        assert flow is not None and flow.succeeded
        return flow

    flow = launch()
    directory = store / "tiny"
    expected = [
        directory / "lib/tiny.lib",
        directory / "lef/tiny.tlef",
        directory / "lef/tiny.lef",
        directory / "config.mk",
        root / "tiny.yaml",
    ]
    assert [path.resolve() for path in flow.implicit_inputs] == [p.resolve() for p in expected]
    assert flow.locate_prepared_inputs() == flow.implicit_inputs
    assert set(registered_input_files(flow)) == {p.resolve() for p in expected}
    assert launch().reused
    (directory / "lib/tiny.lib").write_text("/* changed */\n" + (TINY / "tiny.lib").read_text())
    again = launch()
    assert not again.reused and again.stale_reason.startswith("input changed")


def test_a_dry_run_says_that_a_missing_store_cannot_be_judged(tmp_path):
    root = tmp_path / "d"
    _write_residue(root)
    states = _runner(tmp_path).plan_with_freshness(
        _AsicTaker,
        _design(root),
        flow_settings=["platform_file=tiny.yaml", f"pdk_root={tmp_path / 'empty'}"],
    )[1]
    state = states["__asic_maker"]
    assert state.state == "unknown" and "needs the PDK files" in state.reason, state


def test_a_launch_with_a_missing_store_stops_at_the_first_node(tmp_path):
    root = tmp_path / "d"
    _write_residue(root)
    with pytest.raises(FlowDependencyFailure, match="needs the PDK files"):
        _runner(tmp_path).run_flow(
            _AsicTaker,
            _design(root),
            {"platform_file": "tiny.yaml", "pdk_root": tmp_path / "empty"},
        )
    maker = json.loads((tmp_path / "run" / "d" / "__asic_maker" / "results.json").read_text())
    assert maker["success"] is False and maker["error"]["code"] == "reported_failure"
    taker = tmp_path / "run" / "d" / "__asic_taker"
    assert json.loads((taker / "results.json").read_text())["success"] is False
    assert not (taker / "trace.json").exists()


# ---------------------------------------------------------------------------------------------
# Refusals at planning
# ---------------------------------------------------------------------------------------------


def test_a_platform_and_a_file_that_name_different_platforms_are_refused(tmp_path):
    root = tmp_path / "d"
    _write_residue(root)
    with pytest.raises(FlowSettingsException) as raised:
        _plan(tmp_path, root, ["platform=nangate45", "platform_file=tiny.yaml"])
    message = str(raised.value)
    assert "nangate45" in message and "tiny" in message and "tiny.yaml" in message


def test_a_platform_and_a_file_that_name_one_platform_agree(tmp_path):
    root = tmp_path / "d"
    _write_residue(root)
    plan = _plan(tmp_path, root, ["platform=tiny", "platform_file=tiny.yaml"])
    assert plan.node("__asic_taker").settings.platform == "tiny"


@pytest.mark.parametrize(
    "setting, message",
    [
        ("platform=nangate45/config.toml", r"takes a bundled name, not the path of a config\.toml"),
        ("platform=nosuch", r"no platform 'nosuch'.*nangate45.*platform_file"),
        ("corner=zz", r"unknown corner 'zz'.*typical.*tt"),
        ("pdk_provider=ciel", r"not served by provider 'ciel'.*it serves: orfs, directory"),
        ("pdk_provider=nosuch", r"pdk_provider"),
        ("pdk_version=main", r"not a full commit hash"),
    ],
)
def test_a_setting_the_platform_cannot_use_is_refused_at_planning(tmp_path, setting, message):
    root = tmp_path / "d"
    _write_residue(root)
    settings = ["platform_file=tiny.yaml", setting]
    if setting.startswith("platform="):
        settings = [setting]
    elif setting == "pdk_version=main":
        settings.append("pdk_provider=orfs")
    with pytest.raises(FlowSettingsException, match=message):
        _plan(tmp_path, root, settings)
    with pytest.raises(FlowSettingsException):
        _runner(tmp_path).run_flow(
            _AsicTaker, _design(root), dict(item.split("=", 1) for item in settings)
        )
    assert not (tmp_path / "run").exists()


def test_a_corner_takes_one_name(tmp_path):
    with pytest.raises(FlowSettingsException) as raised:
        _AsicMaker.Settings.from_input(
            {"platform": "nangate45", "corner": ["tt", "ss"]},
            design_root=tmp_path,
            runner_cwd=tmp_path,
        )
    assert "`corner` takes one corner name: multi-corner analysis is not supported" in str(
        raised.value
    )


def test_a_list_of_corners_is_refused_at_planning(tmp_path):
    with pytest.raises(FlowSettingsException, match="takes one corner name"):
        _plan(tmp_path, tmp_path / "d", ["platform=nangate45"], __asic_taker={"corner": ["a", "b"]})


@pytest.mark.parametrize("problem", ["missing", "not a table", "invalid"])
def test_a_platform_file_that_cannot_be_read_is_refused_naming_it(tmp_path, problem):
    root = tmp_path / "d"
    root.mkdir()
    if problem == "not a table":
        (root / "tiny.yaml").write_text("- a\n- b\n")
    elif problem == "invalid":
        (root / "tiny.yaml").write_text("name: tiny\nnonsense: 1\n")
    with pytest.raises(FlowSettingsException) as raised:
        _plan(tmp_path, root, ["platform_file=tiny.yaml"])
    assert str(root / "tiny.yaml") in str(raised.value)


# ---------------------------------------------------------------------------------------------
# The settings
# ---------------------------------------------------------------------------------------------


def _settings(tmp_path: Path, **given) -> AsicPlatformSettings:
    return AsicPlatformSettings.from_input(given, design_root=tmp_path, runner_cwd=tmp_path)


def test_a_bundled_name_gives_its_residue(tmp_path):
    residue = _settings(tmp_path, platform="nangate45").residue()
    assert residue.name == "nangate45" and residue.providers.default == "orfs"


def test_a_complete_user_file_is_the_residue(tmp_path):
    _write_residue(tmp_path)
    residue = _settings(tmp_path, platform_file="tiny.yaml").residue()
    assert residue.name == "tiny" and residue.providers.default == "directory"


def test_a_user_file_that_extends_a_bundled_platform_changes_only_what_it_gives(tmp_path):
    (tmp_path / "mine.yaml").write_text(
        "name: nangate45\nextends: nangate45\nproviders: {default: directory}\n"
    )
    residue = _settings(tmp_path, platform_file="mine.yaml").residue()
    assert residue.providers.default == "directory"
    assert residue.providers.orfs is not None and "typical" in residue.files.liberty


def test_the_files_of_a_platform_come_in_a_fixed_order_without_repeats(tmp_path):
    store = _store(tmp_path)
    user = _write_residue(tmp_path)
    settings = _settings(tmp_path, platform_file="tiny.yaml", pdk_root=str(store))
    directory = store / "tiny"
    config = directory / "config.mk"
    lib, tlef, lef = (
        directory / "lib/tiny.lib",
        directory / "lef/tiny.tlef",
        directory / "lef/tiny.lef",
    )
    assert settings.platform_files(["liberty"]) == [lib, tlef, lef, config, user]
    assert settings.platform_files(["cell_lef", "liberty"]) == [lef, lib, tlef, config, user]
    assert settings.platform_files([*ROLES, "liberty"]) == [lib, tlef, lef, config, user]
    assert settings.platform_files([]) == [lib, tlef, lef, config, user]
    assert settings.platform_files(["gds"]) == [lib, tlef, lef, config, user]


def test_a_bundled_platform_has_no_user_file_among_its_files(tmp_path):
    (tmp_path / "mine.yaml").write_text("name: nangate45\nextends: nangate45\n")
    store = tmp_path / "store"
    with pytest.raises(PdkNotInstalled) as raised:
        _settings(tmp_path, platform="nangate45", pdk_root=str(store)).platform_files(["liberty"])
    assert str(store) in str(raised.value) and "xeda pdk install nangate45" in str(raised.value)


def test_locating_the_platform_reads_no_file_but_the_residue(tmp_path):
    store = _store(tmp_path)
    _write_residue(tmp_path)
    located = _settings(tmp_path, platform_file="tiny.yaml", pdk_root=str(store)).locate_platform()
    assert (located.provider, located.version) == ("directory", "unpinned")
    assert located.corner == "typical" and located.directory == store / "tiny"
    assert located.upstream_files == [store / "tiny" / "config.mk"]


def test_a_list_of_cells_that_the_configuration_names_is_an_input_of_the_run(tmp_path):
    """The upstream files include the list that `DRC_EXCLUDE_CELL_LIST` names. The platform finds
    it with `stat`, and `platform_files` (what the run records and what a dry run checks) has it."""
    store = _store(tmp_path)
    directory = store / "tiny"
    (directory / "config.tcl").write_text(
        'set ::env(DRC_EXCLUDE_CELL_LIST) "$::env(PDK_ROOT)/$::env(PDK)/cells.list"\n'
    )
    (directory / "cells.list").write_text("# excluded\nBAD_X1\n")
    (tmp_path / "mine.yaml").write_text(
        "name: tiny\nproviders: {default: directory, directory: {layout: orfs}}\n"
        "upstream: {adapter: open_pdks_config, files: [config.tcl, cells.list]}\n"
        "corners: {default: typical}\n"
        "files: {liberty: {typical: [lib/tiny.lib]}, tech_lef: lef/tiny.tlef,"
        " cell_lef: [lef/tiny.lef]}\n"
    )
    settings = _settings(tmp_path, platform_file="mine.yaml", pdk_root=str(store))
    located = settings.locate_platform()
    assert located.upstream_files == [directory / "config.tcl", directory / "cells.list"]
    assert settings.platform_files([])[-3:-1] == located.upstream_files
    (directory / "cells.list").unlink()
    with pytest.raises(PdkNotInstalled, match="cells.list"):
        settings.locate_platform()


def test_locating_the_platform_uses_the_corner_and_the_pin(tmp_path):
    store = _store(tmp_path)
    _write_residue(tmp_path, default="orfs")
    settings = _settings(
        tmp_path,
        platform_file="tiny.yaml",
        pdk_root=str(store),
        pdk_provider="directory",
        pdk_version="v1",
        corner="tt",
    )
    located = settings.locate_platform()
    assert (located.provider, located.version, located.corner) == ("directory", "v1", "typical")


def test_the_location_is_a_locator_and_the_other_settings_are_not():
    from xeda.dataclass import is_locator

    names = list(AsicPlatformSettings.model_fields)
    for name in (
        "platform",
        "platform_file",
        "corner",
        "dont_use_cells",
        "pdk_provider",
        "pdk_version",
        "pdk_root",
    ):
        assert name in names
        assert is_locator(AsicPlatformSettings, name) == (name == "pdk_root")


def test_nothing_in_the_package_compares_a_platform_as_a_model():
    root = Path(__file__).parent.parent
    needles = ("_platform_key", "INDIVISIBLE_SETTINGS")
    for folder in ("src", "tests"):
        for path in (root / folder).rglob("*.py"):
            if path == Path(__file__):
                continue
            text = path.read_text(encoding="utf-8")
            assert not any(needle in text for needle in needles), path


def test_the_mixin_leaves_the_registry_to_the_flows():
    from xeda.flow import registered_flows

    assert "AsicPlatformFlow" not in registered_flows
    assert "asic_platform_flow" not in registered_flows
    assert textwrap.dedent(AsicPlatformFlow.__doc__ or "").strip()
