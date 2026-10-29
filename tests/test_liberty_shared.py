"""`liberty` is a shared setting: the producer and the consumer of a mapped netlist read the same
Liberty files.

`yosys` and the four OpenROAD flows have it. It agrees along a declared edge as an ordered list of
resolved paths, so a library given at one flow reaches the others. OpenROAD reads the files in the
restart header of every process, instead of the Liberty files of the platform, and refuses a file
with another time unit.
"""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from xeda.flow import Flow, FlowSettingsException
from xeda.flow_runner import DefaultRunner, resolver
from xeda.flow_runner.resolver import SHARED_SETTINGS
from xeda.flow_runner.trace_inputs import setting_read_paths
from xeda.flows import Yosys
from xeda.flows.openroad import Openroad, OpenroadCts, OpenroadGrt, OpenroadPlace
from xeda.utils import tcl_word

from .openroad_utils import (
    openroad_design,
    openroad_settings,
    probe_flows,
    register_openroad_flows,
    render_driver,
)
from .test_openroad_flows import plan, refusal
from .test_openroad_templates import header
from .tiny_platform import tiny_store
from .setting_texts import text_items
from .scoping import node_settings

FLOWS = ("yosys", "openroad_place", "openroad_cts", "openroad_grt", "openroad")


@pytest.fixture(autouse=True)
def openroad_flows():
    with register_openroad_flows():
        yield


def libraries(tmp_path: Path, *names: str) -> list[Path]:
    """Copies of the Liberty file of the `tiny` platform, under `tmp_path/libs`."""
    original = tiny_store(tmp_path, "origin") / "tiny" / "tiny.lib"
    folder = tmp_path / "libs"
    folder.mkdir(exist_ok=True)
    found = []
    for name in names:
        shutil.copy(original, folder / name)
        found.append(folder / name)
    return found


def liberty_of(planned) -> dict[str, list[Path]]:
    return {node.name: list(node.settings.liberty) for node in planned.nodes}


def test_liberty_is_a_shared_setting_that_every_platform_flow_has():
    assert "liberty" in SHARED_SETTINGS
    assert SHARED_SETTINGS.index("liberty") == SHARED_SETTINGS.index("platform_file") + 1
    for flow in (Yosys, OpenroadPlace, OpenroadCts, OpenroadGrt, Openroad):
        assert "liberty" in flow.Settings.model_fields, flow.name


def test_a_library_given_to_yosys_reaches_every_flow_of_the_chain(tmp_path):
    (lib,) = libraries(tmp_path, "a.lib")
    planned = plan(tmp_path, "openroad", typed={"flows.yosys.liberty": [str(lib)]}, rtl=True)
    assert liberty_of(planned) == {name: [lib.resolve()] for name in FLOWS}


def test_a_library_given_to_an_openroad_flow_reaches_yosys_and_the_others(tmp_path):
    (lib,) = libraries(tmp_path, "a.lib")
    planned = plan(tmp_path, "openroad", typed={"flows.openroad_cts.liberty": [str(lib)]}, rtl=True)
    assert liberty_of(planned) == {name: [lib.resolve()] for name in FLOWS}


def test_without_a_library_nothing_is_given(tmp_path):
    planned = plan(tmp_path, "openroad", rtl=True)
    assert liberty_of(planned) == {name: [] for name in FLOWS}


def test_two_different_libraries_disagree_and_the_message_names_both_nodes(tmp_path):
    a, b = libraries(tmp_path, "a.lib", "b.lib")
    message = refusal(
        tmp_path,
        "openroad",
        typed={"flows.yosys.liberty": [str(a)], "flows.openroad_place.liberty": [str(b)]},
        rtl=True,
    )
    assert "liberty" in message and "disagrees" in message
    assert "yosys" in message and "openroad_place" in message


def test_the_same_files_in_another_order_disagree(tmp_path):
    a, b = libraries(tmp_path, "a.lib", "b.lib")
    message = refusal(
        tmp_path,
        "openroad",
        typed={
            "flows.yosys.liberty": [str(a), str(b)],
            "flows.openroad_place.liberty": [str(b), str(a)],
        },
        rtl=True,
    )
    assert "liberty" in message and "disagrees" in message


def test_a_relative_and_an_absolute_spelling_of_one_file_agree_and_give_one_identity(tmp_path):
    """The value that reaches the other flows is the list of resolved paths, so the identity of
    `yosys` does not depend on which flow spelled the path or how."""

    def planned(name: str, **given: list[str]):
        root = tmp_path / name
        folder = root / "design" / "lib"
        folder.mkdir(parents=True)
        shutil.copy(tiny_store(root, "origin") / "tiny" / "tiny.lib", folder / "t.lib")
        absolute = str((folder / "t.lib").resolve())
        typed = {
            key: [absolute if value == "ABS" else value for value in values]
            for key, values in given.items()
        }
        return plan(root, "openroad", typed=typed, rtl=True)

    both = planned(
        "a", **{"flows.yosys.liberty": ["lib/t.lib"], "flows.openroad_place.liberty": ["ABS"]}
    )
    relative_only = planned("b", **{"flows.yosys.liberty": ["lib/t.lib"]})
    absolute_only = planned("c", **{"flows.openroad_place.liberty": ["ABS"]})
    for result in (both, relative_only, absolute_only):
        for files in liberty_of(result).values():
            assert [path.name for path in files] == ["t.lib"] and files[0].is_absolute()
    assert (
        {n.name: n.settings_hash for n in both.nodes}
        == {n.name: n.settings_hash for n in relative_only.nodes}
        == {n.name: n.settings_hash for n in absolute_only.nodes}
    )


def test_a_library_changes_the_identity_of_every_node(tmp_path):
    (lib,) = libraries(tmp_path, "a.lib")
    base = plan(tmp_path / "a", "openroad", rtl=True)
    given = plan(tmp_path / "b", "openroad", typed={"flows.yosys.liberty": [str(lib)]}, rtl=True)
    before = {node.name: node.flowrun_hash for node in base.nodes}
    for node in given.nodes:
        assert node.flowrun_hash != before[node.name], node.name


def test_a_comma_separated_text_is_a_list_in_order(tmp_path):
    a, b = libraries(tmp_path, "a.lib", "b.lib")
    planned = plan(tmp_path, "openroad", typed={"flows.yosys.liberty": f"{a},{b}"}, rtl=True)
    assert liberty_of(planned) == {name: [a.resolve(), b.resolve()] for name in FLOWS}


def test_an_empty_list_says_nothing(tmp_path):
    (lib,) = libraries(tmp_path, "a.lib")
    planned = plan(
        tmp_path,
        "openroad",
        typed={"flows.yosys.liberty": [str(lib)], "flows.openroad_cts.liberty": []},
        rtl=True,
    )
    assert liberty_of(planned) == {name: [lib.resolve()] for name in FLOWS}


def test_a_library_of_the_command_line_replaces_one_of_the_design_file(tmp_path):
    """The precedence of every shared setting: a higher rank replaces a lower one, and the
    contributions of one rank must agree."""
    a, b = libraries(tmp_path, "a.lib", "b.lib")
    design = openroad_design(tmp_path / "design", rtl=True)
    text = design.read_text() + f"flows:\n  yosys:\n    liberty: [{json.dumps(str(a))}]\n"
    design.write_text(text)
    store = tiny_store(tmp_path)
    given = text_items(openroad_settings(store))
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    planned = runner.plan(
        "openroad",
        design,
        flow_settings=node_settings(
            "openroad", {**given, "flows.openroad_place.liberty": [str(b)]}
        ),
    )
    assert liberty_of(planned) == {name: [b.resolve()] for name in FLOWS}
    unchanged = runner.plan("openroad", design, flow_settings=node_settings("openroad", given))
    assert liberty_of(unchanged) == {name: [a.resolve()] for name in FLOWS}


def test_an_environment_variable_in_a_path_means_the_same_in_every_flow(tmp_path, monkeypatch):
    """`$VAR` expands when the settings are read, so `yosys`, the OpenROAD flows and the
    comparison of shared settings see one path. `~` is not expanded by any of them."""
    (lib,) = libraries(tmp_path, "a.lib")
    monkeypatch.setenv("XEDA_TEST_LIBS", str(lib.parent))
    planned = plan(
        tmp_path,
        "openroad",
        typed={
            "flows.yosys.liberty": ["$XEDA_TEST_LIBS/a.lib"],
            "flows.openroad_place.liberty": [str(lib)],
        },
        rtl=True,
    )
    assert liberty_of(planned) == {name: [lib.resolve()] for name in FLOWS}
    for cls in (Yosys, OpenroadPlace):
        settings = cls.Settings.from_input(
            {"liberty": ["~/a.lib", "${XEDA_TEST_LIBS}/a.lib"]},
            design_root=tmp_path / "design",
            runner_cwd=tmp_path,
        )
        assert settings.liberty_files() == [tmp_path / "design" / "~" / "a.lib", lib.resolve()]


# ------------------------------------------------------------------------------ OpenROAD reads it


def test_openroad_reads_the_given_libraries_in_the_restart_header(tmp_path):
    (lib,) = libraries(tmp_path, "a.lib")
    with probe_flows() as probes:
        flow = probes.flow(tmp_path, settings={"liberty": [str(lib)]})
        assert flow.effective().liberty == (lib,)
        lines = header(render_driver(flow, "openroad_probe"))
        assert [line for line in lines if line.startswith("read_liberty")] == [
            f"read_liberty {tcl_word(lib)}"
        ]


def test_the_platform_files_are_read_without_the_setting(tmp_path):
    with probe_flows() as probes:
        flow = probes.flow(tmp_path)
        assert flow.effective().liberty == tuple(flow.platform_model.files["liberty"])


def test_the_given_libraries_do_not_change_the_platform_model(tmp_path):
    (lib,) = libraries(tmp_path, "a.lib")
    with probe_flows() as probes:
        plain = probes.flow(tmp_path / "x")
        given = probes.flow(tmp_path / "y", settings={"liberty": [str(lib)]})
        assert given.platform_model.model_dump(
            exclude={"files"}
        ) == plain.platform_model.model_dump(exclude={"files"})
        assert given.platform_model.files["liberty"] != [lib]


@pytest.mark.parametrize("spelling", ["1.0ns", "1 ns", "1000ps", "1ns ", " 1ns", "1\tns"])
def test_a_time_unit_that_opensta_reads_as_another_unit_is_refused(tmp_path, spelling):
    """OpenROAD reads `1000ps` as 1 ps, and `1.0ns` and `1 ns` as one second (STA-1150, STA-1153),
    although they look like 1 ns. The check refuses them whatever the value looks like."""
    (lib,) = libraries(tmp_path, "odd.lib")
    lib.write_text(lib.read_text().replace('time_unit : "1ns"', f'time_unit : "{spelling}"'))
    with probe_flows() as probes:
        with pytest.raises(FlowSettingsException, match="no unit that OpenSTA reads"):
            probes.flow(tmp_path, settings={"liberty": [str(lib)]})


def test_a_unit_in_capital_letters_is_the_unit_opensta_reads(tmp_path):
    """OpenSTA lowercases the unit before it reads it, so `1Ns` is one nanosecond."""
    (lib,) = libraries(tmp_path, "caps.lib")
    lib.write_text(lib.read_text().replace('time_unit : "1ns"', 'time_unit : "1Ns"'))
    with probe_flows() as probes:
        flow = probes.flow(tmp_path, settings={"liberty": [str(lib)]})
        assert flow.effective().liberty == (lib,)


def test_the_units_of_ten_and_a_hundred_are_compared_by_value(tmp_path):
    (lib,) = libraries(tmp_path, "ps.lib")
    lib.write_text(lib.read_text().replace('time_unit : "1ns"', 'time_unit : "100ps"'))
    with probe_flows() as probes:
        with pytest.raises(FlowSettingsException, match="100ps.*1ns"):
            probes.flow(tmp_path, settings={"liberty": [str(lib)]})


def test_a_platform_unit_in_another_spelling_is_compared_by_value(tmp_path):
    """The platform model can state its unit as `1 ns`; the stages scale by its value, so a
    Liberty file with the unit `1ns` is the same unit."""
    from xeda.flows.openroad.base import model_time_unit_seconds

    assert model_time_unit_seconds("1 ns") == model_time_unit_seconds("0.001us")
    assert model_time_unit_seconds("1 ns") == pytest.approx(1e-9)
    with pytest.raises(ValueError, match="cannot read the time unit"):
        model_time_unit_seconds("fast")


def test_a_library_with_another_time_unit_is_refused(tmp_path):
    (lib,) = libraries(tmp_path, "ps.lib")
    lib.write_text(lib.read_text().replace('time_unit : "1ns"', 'time_unit : "1ps"'))
    with probe_flows() as probes:
        with pytest.raises(FlowSettingsException) as error:
            probes.flow(tmp_path, settings={"liberty": [str(lib)]})
    message = " ".join(str(error.value).split())
    assert "liberty" in message and "1ps" in message and "1ns" in message and str(lib) in message


def test_a_time_unit_that_only_an_included_file_states_is_checked(tmp_path):
    """The reader follows `include_file(...)` as OpenSTA does, so the unit of an included file is
    the unit of the library, and a missing included file is an error that names it."""
    (lib,) = libraries(tmp_path, "inc.lib")
    unit = '  time_unit : "1ns";\n'
    assert unit in lib.read_text()
    units = lib.parent / "units.lib"
    units.write_text('time_unit : "1ps";\n')
    lib.write_text(lib.read_text().replace(unit, f"  include_file({units});\n"))
    with probe_flows() as probes:
        with pytest.raises(FlowSettingsException, match="1ps"):
            probes.flow(tmp_path, settings={"liberty": [str(lib)]})
        (lib.parent / "units.lib").write_text('time_unit : "1ns";\n')
        flow = probes.flow(tmp_path / "ok", settings={"liberty": [str(lib)]})
        assert flow.effective().liberty == (lib,)
        (lib.parent / "units.lib").unlink()
        with pytest.raises(FlowSettingsException, match=r"units\.lib, which is not a file"):
            probes.flow(tmp_path / "gone", settings={"liberty": [str(lib)]})


def test_the_files_that_a_library_includes_are_prepared_inputs_and_located_for_a_dry_run(
    tmp_path,
):
    (lib,) = libraries(tmp_path, "inc.lib")
    units = lib.parent / "units.lib"
    unit = '  time_unit : "1ns";\n'
    units.write_text(unit)
    lib.write_text(lib.read_text().replace(unit, f"  include_file({units});\n"))
    with probe_flows() as probes:
        flow = probes.flow(tmp_path, settings={"liberty": [str(lib)]})
        assert units in flow.implicit_inputs
        assert flow.locate_prepared_inputs() == list(flow.implicit_inputs)


def test_a_library_with_another_capacitance_unit_is_refused(tmp_path):
    """OpenROAD reads the wire capacitance of the platform in the capacitance unit of the first
    Liberty file, so a file with another unit would scale it."""
    (lib,) = libraries(tmp_path, "pf.lib")
    assert "capacitive_load_unit (1,ff)" in lib.read_text()
    lib.write_text(
        lib.read_text().replace("capacitive_load_unit (1,ff)", "capacitive_load_unit (1,pf)")
    )
    with probe_flows() as probes:
        with pytest.raises(FlowSettingsException, match="capacitance unit 1pf.*1ff"):
            probes.flow(tmp_path, settings={"liberty": [str(lib)]})


def test_a_library_without_a_capacitance_unit_has_the_default_of_opensta(tmp_path):
    """OpenSTA reads 1 pF when the file states no unit, so the check compares that with the
    platform's 1 fF."""
    (lib,) = libraries(tmp_path, "none.lib")
    lib.write_text(lib.read_text().replace("  capacitive_load_unit (1,ff);\n", ""))
    with probe_flows() as probes:
        with pytest.raises(FlowSettingsException, match="capacitance unit 1pf.*1ff"):
            probes.flow(tmp_path, settings={"liberty": [str(lib)]})


def test_a_time_unit_without_a_semicolon_is_refused_and_not_taken_for_the_default(tmp_path):
    """OpenSTA reads a simple attribute that has no closing semicolon. The reader of Xeda does
    not, so the check must not take the missing value for the default of 1 ns."""
    (lib,) = libraries(tmp_path, "bare.lib")
    lib.write_text(
        'library (t) {\n  capacitive_load_unit (1,ff);\n  time_unit : "1ps"\n'
        "  cell (X) { area : 1; }\n}\n"
    )
    with probe_flows() as probes:
        with pytest.raises(FlowSettingsException, match="cannot read its `time_unit`"):
            probes.flow(tmp_path, settings={"liberty": [str(lib)]})


def test_only_the_first_library_sets_the_capacitance_unit_of_the_wire_rc(tmp_path):
    """A macro library with its own capacitance unit after the standard cells is read as it is."""
    first, macro = libraries(tmp_path, "cells.lib", "macro.lib")
    macro.write_text(
        macro.read_text().replace("capacitive_load_unit (1,ff)", "capacitive_load_unit (1,pf)")
    )
    with probe_flows() as probes:
        flow = probes.flow(tmp_path, settings={"liberty": [str(first), str(macro)]})
        assert flow.effective().liberty == (first, macro)
        with pytest.raises(FlowSettingsException, match="capacitance unit 1pf"):
            probes.flow(tmp_path / "swapped", settings={"liberty": [str(macro), str(first)]})


def test_a_capacitance_unit_of_the_same_value_is_accepted(tmp_path):
    (lib,) = libraries(tmp_path, "same.lib")
    lib.write_text(
        lib.read_text().replace("capacitive_load_unit (1,ff)", "capacitive_load_unit (0.001,pf)")
    )
    with probe_flows() as probes:
        flow = probes.flow(tmp_path, settings={"liberty": [str(lib)]})
        assert flow.effective().liberty == (lib,)


def test_a_library_that_is_no_file_is_refused(tmp_path):
    with probe_flows() as probes:
        with pytest.raises(FlowSettingsException, match="liberty.*is not a file"):
            probes.flow(tmp_path, settings={"liberty": [str(tmp_path / "missing.lib")]})


def test_the_given_libraries_are_inputs_of_the_run(tmp_path):
    """They are path settings, so the trace reads them (`test_openroad_flows` runs the chain and
    shows that a changed file makes it stale)."""
    (lib,) = libraries(tmp_path, "a.lib")
    with probe_flows() as probes:
        flow = probes.flow(tmp_path, settings={"liberty": [str(lib)]})
        assert lib in [Path(p) for p in setting_read_paths(flow.settings, flow.run_path)]


# ------------------------------------------------------------------------------ the guard


def test_a_flow_with_a_platform_and_no_liberty_is_a_programming_error():
    """The settings of a flow that declares `platform` must inherit `AsicPlatformSettings`, so
    that every node of a group holds the pin and the Liberty files in its identity."""

    class NoLiberty(Flow.Settings):
        platform: str | None = None
        pdk_provider: str | None = None
        pdk_version: str | None = None

    flow = SimpleNamespace(name="partial", Settings=NoLiberty)
    request = resolver._Request(flow, resolver._Located(), "test")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="liberty.*AsicPlatformSettings"):
        resolver._pin_pdks([request], {})
