"""`Project`: the view of a project file, and the one way to select a design from it.

A project holds designs, the `flows` table they share, a run root and a default target. A design
that the project selects carries the `flows` table as its project layer. A design file given on
its own never gets that layer.
"""

import json
import pickle
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

import xeda.cli_utils
from xeda import errors
from xeda.cli import cli
from xeda.flow_runner import DefaultRunner
from xeda.frozen import FrozenDict
from xeda.project import PROJECT_KEYS, REMOVED_PROJECT_KEYS, Project
from xeda.types import UNSET, Unset
from xeda.xedaproject import PROJECT_FILE_NAMES, find_default_xedaproject, resolve_project_file

from .project_files import PROJECT_FILE

RESOURCES = Path(__file__).parent / "resources" / "targets"
PART = ("-s", "fpga.part=xc7a100tcsg324-1")


def rtl(top: str = "knight") -> dict:
    return {"sources": ["knight.v"], "top": top}


def write_project(directory: Path, project: dict, name: str = PROJECT_FILE) -> Path:
    """Write `project` and the one source file that its designs use; return the project file."""
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "knight.v"
    if not source.exists():
        source.write_text((RESOURCES / "knight.v").read_text())
    path = directory / name
    path.write_text(yaml.safe_dump(project, sort_keys=False))
    return path


def design_file(directory: Path, name: str, data: dict | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "knight.v"
    if not source.exists():
        source.write_text((RESOURCES / "knight.v").read_text())
    path = directory / name
    path.write_text(yaml.safe_dump(data or {"name": path.stem, "rtl": rtl()}, sort_keys=False))
    return path


def invalid_project(tmp_path: Path, text: str) -> errors.ProjectError:
    path = tmp_path / PROJECT_FILE
    path.write_text(text)
    with pytest.raises(errors.ProjectError) as raised:
        Project.load(path)
    return raised.value


# ------------------------------------------------------------------------------ the fields


def test_a_project_has_the_fields_of_its_file(tmp_path):
    path = write_project(
        tmp_path / "hw",
        {
            "designs": [{"name": "fifo", "rtl": rtl()}, {"name": "alu", "rtl": rtl()}],
            "flows": {"vivado_synth": {"fpga.part": "xc7a100tcsg324-1"}},
            "default_target": "arty",
            "run_root": "build",
        },
    )

    project = Project.load(path)

    assert project.path == path and project.path.is_absolute()
    assert project.root == path.parent.resolve()
    assert project.designs == ("fifo", "alu")
    assert project.run_root == project.root / "build"
    assert project.default_target == "arty"
    assert isinstance(project.flows, FrozenDict)
    assert project.flows == {"vivado_synth": {"fpga": {"part": "xc7a100tcsg324-1"}}}


def test_the_run_root_is_relative_to_the_project_file_and_defaults_beside_it(tmp_path):
    path = write_project(tmp_path / "a", {"designs": [{"name": "d", "rtl": rtl()}]})
    assert Project.load(path).run_root == path.parent.resolve() / "xeda_run"
    path = write_project(tmp_path / "b", {"run_root": "../out/runs"})
    assert Project.load(path).run_root == (tmp_path / "out" / "runs").resolve()
    path = write_project(tmp_path / "c", {"run_root": str(tmp_path / "elsewhere")})
    assert Project.load(path).run_root == tmp_path / "elsewhere"


def test_a_project_without_designs_or_default_target_loads(tmp_path):
    project = Project.load(write_project(tmp_path, {"flows": {}}))
    assert project.designs == () and project.default_target is None and project.flows == {}


def test_a_design_entry_can_name_a_file(tmp_path):
    design_file(tmp_path / "hw", "fifo.yaml")
    design_file(tmp_path / "hw", "named.yaml", {"name": "other", "rtl": rtl()})
    path = write_project(
        tmp_path, {"designs": [{"file": "hw/fifo.yaml"}, {"file": "hw/named.yaml"}]}
    )

    project = Project.load(path)

    assert project.designs == ("fifo", "other")
    fifo = project.design("fifo")
    assert fifo.name == "fifo" and fifo.root == (tmp_path / "hw").resolve()
    assert project.design("other").name == "other"


def test_a_project_has_no_constructor_and_cannot_change(tmp_path):
    with pytest.raises(TypeError):
        Project()  # type: ignore[call-arg]
    project = Project.load(write_project(tmp_path, {"flows": {}}))
    with pytest.raises(AttributeError):
        project.designs = ("x",)  # type: ignore[misc]


def test_the_keys_of_a_project_are_these():
    assert PROJECT_KEYS == ("designs", "flows", "run_root", "default_target")


# ------------------------------------------------------------------------------ design()


def test_a_project_design_carries_the_project_layer_with_its_origin(tmp_path):
    path = write_project(
        tmp_path,
        {
            "designs": [{"name": "fifo", "rtl": rtl()}],
            "flows": {"vivado_synth": {"fpga": {"part": "xc7a100tcsg324-1"}}},
        },
    )
    project = Project.load(path)

    design = project.design("fifo")

    request = DefaultRunner(tmp_path / "run")._request("vivado_synth", design)
    layers = dict(request.origins)
    assert layers[str(path)] == {"vivado_synth": {"fpga": {"part": "xc7a100tcsg324-1"}}}
    assert request.settings["fpga"]["part"] == "xc7a100tcsg324-1"
    # the layer stays when the design is updated or another target is selected
    updated = design.with_updates({"description": "x"}).with_target(None)
    request = DefaultRunner(tmp_path / "run")._request("vivado_synth", updated)
    assert str(path) in dict(request.origins)


def test_the_only_design_of_a_project_is_selected_without_a_name(tmp_path):
    project = Project.load(write_project(tmp_path, {"designs": [{"name": "fifo", "rtl": rtl()}]}))
    assert project.design().name == "fifo"
    assert project.design(UNSET).name == "fifo"


def test_a_project_with_several_designs_needs_a_name(tmp_path):
    designs = [{"name": "fifo", "rtl": rtl()}, {"name": "alu", "rtl": rtl()}]
    project = Project.load(write_project(tmp_path, {"designs": designs}))
    with pytest.raises(errors.ProjectError, match="fifo, alu") as raised:
        project.design()
    assert raised.value.code == "design_name_required"
    assert raised.value.path == project.path


def test_an_unknown_design_name_lists_the_designs(tmp_path):
    project = Project.load(write_project(tmp_path, {"designs": [{"name": "fifo", "rtl": rtl()}]}))
    with pytest.raises(errors.ProjectError, match="'fifa'.*Its designs are: fifo") as raised:
        project.design("fifa")
    assert raised.value.code == "unknown_design"


def test_a_project_without_designs_has_nothing_to_select(tmp_path):
    project = Project.load(write_project(tmp_path, {"flows": {}}))
    for call in (project.design, lambda: project.design("x")):
        with pytest.raises(errors.ProjectError, match="has no designs") as raised:
            call()
        assert raised.value.code == "invalid_project"


TARGETS = {"arty": {"rtl": {"defines": {"BOARD": 1}}}, "ulx3s": {}}


def targeted_project(tmp_path: Path, **keys) -> Project:
    design = {"name": "fifo", "rtl": rtl(), "targets": TARGETS}
    plain = {"name": "plain", "rtl": rtl()}
    return Project.load(write_project(tmp_path, {"designs": [design, plain], **keys}))


def test_an_omitted_target_selects_the_default_and_none_selects_no_target(tmp_path):
    p = targeted_project(tmp_path, default_target="arty")
    assert p.design("fifo").target == "arty"
    assert p.design("fifo", target=UNSET).target == "arty"
    assert p.design("fifo", target=None).target is None
    assert p.design("fifo", target="ulx3s").target == "ulx3s"
    assert p.design("fifo", target=None).targets == ("arty", "ulx3s")


def test_without_a_default_target_an_omitted_target_selects_none(tmp_path):
    p = targeted_project(tmp_path)
    assert p.design("fifo").target is None and p.design("plain").target is None


def test_a_design_without_the_default_target_is_refused(tmp_path):
    p = targeted_project(tmp_path, default_target="arty")
    with pytest.raises(errors.DesignError, match="'arty'.*plain") as raised:
        p.design("plain")
    assert raised.value.code == "target_required"
    # Asking for no target is the way out.
    assert p.design("plain", target=None).target is None


def test_a_target_that_the_design_does_not_have_is_refused(tmp_path):
    from xeda.flow_runner.error_codes import public_error

    p = targeted_project(tmp_path)
    for name, message in (("fifo", "not a target of the design"), ("plain", "no `targets`")):
        with pytest.raises(Exception, match=message) as raised:
            p.design(name, target="nexys")
        error = public_error(raised.value)
        assert isinstance(error, errors.DesignError) and error.code == "unknown_target"


def test_each_call_gives_a_new_design_of_the_same_configuration(tmp_path):
    p = targeted_project(tmp_path, default_target="arty")
    first, second = p.design("fifo"), p.design("fifo")
    assert first is not second and first == second
    assert pickle.loads(pickle.dumps(p.flows)) == p.flows


def test_the_root_of_an_inline_entry_is_the_directory_of_the_project_file(tmp_path):
    path = write_project(tmp_path / "hw", {"designs": [{"name": "d", "rtl": rtl()}]})
    design = Project.load(path).design("d")
    assert design.root == path.parent.resolve()
    assert design.rtl.sources[0].path == path.parent.resolve() / "knight.v"


def test_a_design_with_another_root_is_a_file_entry(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.v").write_text("module a; endmodule\n")
    design_file(tmp_path / "sub", "a.yaml", {"name": "a", "rtl": {"sources": ["a.v"]}})
    path = write_project(tmp_path, {"designs": [{"file": "sub/a.yaml"}]})
    assert Project.load(path).design("a").root == (tmp_path / "sub").resolve()


# ---------------------------------------------------------------------------- invalid files


def test_a_missing_project_file_is_refused(tmp_path):
    with pytest.raises(errors.ProjectError, match="no such file") as raised:
        Project.load(tmp_path / PROJECT_FILE)
    assert raised.value.code == "project_file_not_found"
    assert raised.value.path == tmp_path / PROJECT_FILE


@pytest.mark.parametrize("text", ["", "[]\n", "just text\n"])
def test_a_project_file_that_holds_no_mapping_is_refused(tmp_path, text):
    error = invalid_project(tmp_path, text)
    assert error.code == "invalid_project" and "nonempty mapping" in str(error)
    assert str(tmp_path / PROJECT_FILE) in str(error)


def test_a_file_that_does_not_parse_is_an_invalid_project(tmp_path):
    error = invalid_project(tmp_path, "flows: [unclosed\n")
    assert error.code == "invalid_project" and "Cannot load project file" in str(error)


def test_the_suffix_of_a_project_file_is_read_as_strictly_as_a_design_file_s(tmp_path):
    shouting = tmp_path / "xedaproject.TOML"
    shouting.write_text("[flows.ghdl_sim]\nwerror = true\n")
    with pytest.raises(errors.ProjectError, match=r"case-sensitive.*'\.toml'"):
        Project.load(shouting)
    other = tmp_path / "project.txt"
    other.write_text("flows: {}\n")
    with pytest.raises(errors.ProjectError, match="TOML, JSON or YAML"):
        Project.load(other)


@pytest.mark.parametrize("suffix", [".yaml", ".yml", ".toml", ".json"])
def test_each_format_of_a_design_file_is_a_project_format_too(tmp_path, suffix):
    flows = {"ghdl_sim": {"werror": True}}
    path = tmp_path / f"project{suffix}"
    if suffix == ".toml":
        path.write_text("[flows.ghdl_sim]\nwerror = true\n")
    elif suffix == ".json":
        path.write_text(json.dumps({"flows": flows}))
    else:
        path.write_text(yaml.safe_dump({"flows": flows}))
    assert Project.load(path).flows == flows


@pytest.mark.parametrize(
    ("text", "suggestion"),
    [("flowz: {}\n", "flows"), ("desings: []\n", "designs"), ("workspace: {}\n", None)],
)
def test_a_key_that_nothing_reads_is_refused_naming_the_keys(tmp_path, text, suggestion):
    error = invalid_project(tmp_path, text)
    message = str(error)
    assert error.code == "invalid_project" and "unknown key(s)" in message
    assert "'designs', 'flows', 'run_root', 'default_target'" in message
    assert text.split(":")[0] in message
    if suggestion:
        assert f"did you mean '{suggestion}'?" in message


@pytest.mark.parametrize(("old", "new"), [("design", "designs"), ("flow", "flows")])
def test_the_legacy_keys_are_refused_with_their_successor(tmp_path, old, new):
    assert set(REMOVED_PROJECT_KEYS) == {"design", "flow"}
    error = invalid_project(tmp_path, f"{old}: {{}}\n")
    assert error.code == "removed_alias"
    assert f"`{old}` was removed: use `{new}`" in str(error)


def test_a_dotted_key_at_the_root_reaches_the_flows(tmp_path):
    error = Project.load(
        write_project(tmp_path, {"flows.vivado_synth.clocks.main_clock.period": "5 ns"})
    )
    assert error.flows == {"vivado_synth": {"clocks": {"main_clock": {"period": "5 ns"}}}}


def test_a_dotted_key_cannot_reach_into_the_design_entries(tmp_path):
    error = invalid_project(tmp_path, "designs.description: text\n")
    assert "cannot reach into the design entries" in str(error)


def test_a_flows_table_of_the_wrong_shape_is_refused(tmp_path):
    error = invalid_project(tmp_path, "flows: [a]\n")
    assert error.code == "invalid_project" and "`flows`" in str(error)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("default_target: 12\n", "default_target"),
        ("default_target: 'not a name'\n", "default_target"),
        ("run_root: 12\n", "run_root"),
        ("run_root: ''\n", "run_root"),
    ],
)
def test_a_default_target_and_a_run_root_must_be_what_they_are(tmp_path, text, message):
    error = invalid_project(tmp_path, text)
    assert error.code == "invalid_project" and message in str(error)


def test_designs_is_a_list_and_a_single_table_shows_the_list_form(tmp_path):
    error = invalid_project(tmp_path, "designs:\n  name: d\n  rtl: {sources: []}\n")
    assert error.code == "invalid_project"
    assert "`designs` is a list" in str(error) and "designs: [{file:" in str(error)
    assert str(tmp_path / PROJECT_FILE) in str(error)
    for text in ("designs: fifo.yaml\n", "designs: 3\n"):
        assert invalid_project(tmp_path, text).code == "invalid_project"


def test_an_inline_entry_cannot_give_the_root(tmp_path):
    error = invalid_project(
        tmp_path, "designs: [{name: d, design_root: sub, rtl: {sources: []}}]\n"
    )
    assert error.code == "removed_key"
    assert "design entry 0 (d)" in str(error) and "`design_root` was removed" in str(error)


def test_a_design_entry_that_is_not_a_table_names_its_position_and_type(tmp_path):
    error = invalid_project(tmp_path, "designs: [a.yaml]\n")
    assert error.code == "invalid_project"
    assert "design entry 0 must be a mapping" in str(error) and "str" in str(error)


def test_a_key_conflict_in_a_design_entry_names_the_file_and_the_entry(tmp_path):
    entry = {"name": "d", "rtl": rtl(), "tb": 3, "tb.top": "x"}
    error = invalid_project(tmp_path, json.dumps({"designs": [entry]}))
    message = str(error)
    assert str(tmp_path / PROJECT_FILE) in message and "design entry 0 (d)" in message
    assert "`tb` is set to a value, and `tb.top`" in message


def test_a_design_entry_without_a_name_is_refused(tmp_path):
    error = invalid_project(tmp_path, "designs: [{rtl: {sources: []}}]\n")
    assert error.code == "invalid_project" and "design entry 0" in str(error)
    assert "name" in str(error)


def test_two_design_entries_cannot_share_a_name(tmp_path):
    entry = {"name": "d", "rtl": rtl()}
    error = invalid_project(tmp_path, json.dumps({"designs": [entry, entry]}))
    assert error.code == "invalid_project" and "'d'" in str(error)


def test_a_file_entry_holds_nothing_but_the_file(tmp_path):
    design_file(tmp_path, "fifo.yaml")
    error = invalid_project(tmp_path, "designs: [{file: fifo.yaml, rtl.top: x}]\n")
    assert error.code == "invalid_project" and "design entry 0" in str(error)
    assert "`file`" in str(error)


def test_a_file_entry_whose_file_cannot_be_read_is_refused(tmp_path):
    error = invalid_project(tmp_path, "designs: [{file: missing.yaml}]\n")
    assert error.code == "invalid_project"
    assert "design entry 0" in str(error) and "missing.yaml" in str(error)


# ------------------------------------------------------------------------------ discovery


def test_two_project_files_in_one_directory_are_refused(tmp_path):
    (tmp_path / "xedaproject.yaml").write_text("flows: {}\n")
    (tmp_path / "xedaproject.toml").write_text("[flows]\n")
    with pytest.raises(errors.ProjectError, match="keep one project file") as raised:
        find_default_xedaproject(tmp_path)
    assert raised.value.code == "several_project_files"
    assert "xedaproject.yaml" in str(raised.value) and "xedaproject.toml" in str(raised.value)


def test_a_project_file_that_is_given_must_exist(tmp_path):
    with pytest.raises(errors.ProjectError) as raised:
        resolve_project_file(tmp_path / "nothing.yaml")
    assert raised.value.code == "project_file_not_found"


def test_discovery_finds_each_conventional_name(tmp_path):
    assert PROJECT_FILE_NAMES == ("xedaproject.yaml", "xedaproject.yml", "xedaproject.toml")
    for name in PROJECT_FILE_NAMES:
        directory = tmp_path / f"in_{name}"
        directory.mkdir()
        (directory / name).write_text("flows: {}\n")
        assert find_default_xedaproject(directory) == directory / name
        assert resolve_project_file(None, directory) == directory / name
    assert find_default_xedaproject(tmp_path) is None


# ---------------------------------------------------------------- the launcher and the CLI


def invoke(*args: str):
    result = CliRunner().invoke(cli, [*args, "--json"], catch_exceptions=False)
    return result, json.loads(result.stdout)


def cli_project(tmp_path: Path, monkeypatch, **keys) -> Path:
    monkeypatch.chdir(tmp_path)
    design_file(tmp_path / "hw", "fifo.yaml")
    design_file(tmp_path, "other.yaml")
    return write_project(
        tmp_path,
        {
            "designs": [
                {"file": "hw/fifo.yaml"},
                {"name": "inline", "rtl": rtl(), "targets": TARGETS},
            ],
            "flows": {"vivado_synth": {"fpga": {"part": "xc7a100tcsg324-1"}}},
            **keys,
        },
    )


def test_a_positional_file_that_the_project_references_is_refused(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch)

    result, document = invoke("run", "vivado_synth", "hw/fifo.yaml", "--dry-run")

    assert result.exit_code == 2, document
    assert document["outcome"] is None and document["error"]["code"] == "use_design_name"
    assert "--design-name fifo" in document["error"]["message"]
    assert not (tmp_path / "xeda_run").exists()


def test_each_way_to_name_a_listed_file_is_refused(tmp_path, monkeypatch):
    """A relative path, an absolute path, a path with `..`, a symbolic link to the file and a path
    through a linked directory name one file, so each is the design the project lists."""
    cli_project(tmp_path, monkeypatch)
    real = tmp_path / "hw" / "fifo.yaml"
    (tmp_path / "link.yaml").symlink_to(real)
    (tmp_path / "linked").symlink_to(tmp_path / "hw", target_is_directory=True)
    (tmp_path / "other_dir").mkdir()
    spellings = [
        "hw/fifo.yaml",
        "./hw/fifo.yaml",
        "hw/../hw/fifo.yaml",
        str(real),
        str(tmp_path / "hw" / ".." / "hw" / "fifo.yaml"),
        "link.yaml",
        str(tmp_path / "link.yaml"),
        "linked/fifo.yaml",
    ]
    for spelling in spellings:
        result, document = invoke("run", "vivado_synth", spelling, "--dry-run", *PART)
        assert result.exit_code == 2, (spelling, document)
        assert document["error"]["code"] == "use_design_name", spelling
        assert "--design-name fifo" in document["error"]["message"], spelling
    # from another directory, by the path that leads to it
    monkeypatch.chdir(tmp_path / "other_dir")
    project = str(tmp_path / PROJECT_FILE)
    for spelling in ("../hw/fifo.yaml", str(real)):
        result, document = invoke(
            "run", "vivado_synth", spelling, "--xedaproject", project, "--dry-run", *PART
        )
        assert document["error"]["code"] == "use_design_name", spelling
    assert not (tmp_path / "xeda_run").exists()


def test_a_link_in_the_project_to_a_file_is_the_file_it_links_to(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    real = design_file(tmp_path / "real", "fifo.yaml")
    (tmp_path / "hw").mkdir()
    (tmp_path / "hw" / "fifo.yaml").symlink_to(real)
    write_project(tmp_path, {"designs": [{"file": "hw/fifo.yaml"}]})
    result, document = invoke("run", "vivado_synth", str(real), "--dry-run", *PART)
    assert document["error"]["code"] == "use_design_name"


def test_a_file_the_project_does_not_list_gets_neither_the_project_nor_a_name_error(
    tmp_path, monkeypatch
):
    """Not listed: it runs on its own, whatever the project holds, and the project still
    supplies the run root."""
    cli_project(tmp_path, monkeypatch, run_root="runs")
    design_file(tmp_path / "hw", "again.yaml")
    for spelling in ("other.yaml", "hw/again.yaml"):
        result, document = invoke("run", "vivado_synth", spelling, "--dry-run", *PART)
        assert result.exit_code == 0, document
        assert run_path_of(document).is_relative_to((tmp_path / "runs").resolve())


def test_the_launcher_does_not_look_for_a_project_or_check_a_listed_file(tmp_path, monkeypatch):
    """Only the command line discovers the project file and refuses a listed design file. The
    launcher reads the files it is given."""
    path = cli_project(tmp_path, monkeypatch)
    (tmp_path / "xedaproject.toml").write_text("[flows]\n")  # two project files here now
    runner = DefaultRunner(tmp_path / "run")
    request = runner._request("vivado_synth", "hw/fifo.yaml", xedaproject=str(path))
    assert str(path) not in dict(request.origins)
    # no project is given, so a design name has no project to come from
    with pytest.raises(errors.ProjectError, match="no project file") as raised:
        runner._request("vivado_synth", "fifo")
    assert raised.value.code == "project_file_not_found"


def test_a_library_launch_of_a_project_design_ignores_the_files_of_the_start_directory(
    tmp_path, monkeypatch
):
    path = cli_project(tmp_path, monkeypatch)
    (tmp_path / "xedaproject.toml").write_text("[flows]\n")
    design = Project.load(path).design("fifo")
    request = DefaultRunner(tmp_path / "run")._request("vivado_synth", design)
    assert str(path) in dict(request.origins)
    plan = DefaultRunner(tmp_path / "run").plan("vivado_synth", design)
    assert plan.node("vivado_synth").settings.fpga.part == "xc7a100tcsg324-1"


def test_the_command_line_finds_one_project_file_and_refuses_two(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch)
    (tmp_path / "xedaproject.toml").write_text("[flows]\n")
    for arguments in (("--design-name", "fifo"), ("other.yaml",)):
        result, document = invoke("run", "vivado_synth", *arguments, "--dry-run", *PART)
        assert result.exit_code == 2 and document["outcome"] is None, document
        assert document["error"]["code"] == "several_project_files"
        assert "keep one project file" in document["error"]["message"]
    result, document = invoke(
        "run",
        "vivado_synth",
        "--design-name",
        "fifo",
        "--dry-run",
        "--xedaproject",
        "xedaproject.yaml",
    )
    assert result.exit_code == 0, document


@pytest.mark.parametrize("name", PROJECT_FILE_NAMES)
def test_the_command_line_finds_a_lone_project_file_by_any_accepted_name(
    tmp_path, monkeypatch, name
):
    """A broken project file is reported, not skipped, whichever name it has."""
    monkeypatch.chdir(tmp_path)
    design_file(tmp_path, "other.yaml")
    (tmp_path / name).write_text("[flows\n" if name.endswith(".toml") else "flows: [a\n")
    result, document = invoke("run", "vivado_synth", "other.yaml", "--dry-run", *PART)
    assert result.exit_code == 2 and document["error"]["code"] == "invalid_project"
    assert name in document["error"]["message"]


def test_a_positional_design_is_always_a_file(tmp_path, monkeypatch):
    """`--design-name` is the one way to reach a project design: a name given as `DESIGN` is
    the path of a design file, with or without a suffix."""
    cli_project(tmp_path, monkeypatch)
    # no such file, and no design of that name: the file is missing
    result, document = invoke("run", "vivado_synth", "nothing", "--dry-run", *PART)
    assert result.exit_code == 2 and document["error"]["code"] == "design_file_not_found"
    assert "nothing" in document["error"]["message"]
    # no such file, but the project has a design of that name
    for name in ("fifo", "inline"):
        result, document = invoke("run", "vivado_synth", name, "--dry-run", *PART)
        assert result.exit_code == 2, document
        assert document["error"]["code"] == "use_design_name"
        assert f"--design-name {name}" in document["error"]["message"]
    # a file of that name is a file, whatever the project holds
    (tmp_path / "fifo").write_text("name: fifo\nrtl: {sources: []}\n")
    result, document = invoke("run", "vivado_synth", "fifo", "--dry-run", *PART)
    assert (
        document["error"]["code"] == "invalid_design" and "suffix" in document["error"]["message"]
    )


def test_a_positional_name_without_a_project_is_a_missing_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result, document = invoke("run", "vivado_synth", "fifo", "--dry-run", *PART)
    assert result.exit_code == 2 and document["error"]["code"] == "design_file_not_found"


def test_a_positional_file_the_project_does_not_reference_gets_no_project_layer(
    tmp_path, monkeypatch
):
    path = cli_project(tmp_path, monkeypatch)
    runner = DefaultRunner(tmp_path / "run")

    request = runner._request("vivado_synth", "other.yaml", xedaproject=str(path))

    assert str(path) not in dict(request.origins)
    assert (
        "part" not in (request.settings.get("fpga") or {}) or not request.settings["fpga"]["part"]
    )
    named = runner._request("vivado_synth", "fifo", xedaproject=str(path))
    assert str(path) in dict(named.origins)
    assert named.settings["fpga"]["part"] == "xc7a100tcsg324-1"


def test_a_design_that_is_built_in_code_gets_no_project_layer(tmp_path, monkeypatch):
    from xeda import Design

    path = cli_project(tmp_path, monkeypatch)
    built = Design(name="d", root=tmp_path, rtl=rtl())
    request = DefaultRunner(tmp_path / "run")._request("vivado_synth", built, xedaproject=str(path))
    assert str(path) not in dict(request.origins)


def test_design_name_selects_a_project_design(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch)
    result, document = invoke("run", "vivado_synth", "--design-name", "fifo", "--dry-run")
    assert result.exit_code == 0, document
    assert document["design"] == "fifo" and document["target"] is None


def test_the_default_target_applies_unless_target_or_no_target_is_given(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch, default_target="arty")
    base = ("run", "vivado_synth", "--design-name", "inline", "--dry-run", *PART)
    result, document = invoke(*base)
    assert result.exit_code == 0, document
    assert document["target"] == "arty"
    result, document = invoke(*base, "--target", "ulx3s")
    assert result.exit_code == 0, document
    assert document["target"] == "ulx3s"
    result, document = invoke(*base, "--no-target")
    assert result.exit_code == 0, document
    assert document["target"] is None


def test_a_design_without_the_default_target_is_a_document(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch, default_target="arty")
    result, document = invoke("run", "vivado_synth", "--design-name", "fifo", "--dry-run")
    assert result.exit_code == 2 and document["error"]["code"] == "target_required"
    result, document = invoke(
        "run", "vivado_synth", "--design-name", "fifo", "--dry-run", "--no-target"
    )
    assert result.exit_code == 0, document


def test_a_design_file_does_not_take_the_default_target(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch, default_target="arty")
    result, document = invoke("run", "vivado_synth", "other.yaml", "--dry-run", *PART)
    assert result.exit_code == 0, document
    assert document["target"] is None


def test_target_and_no_target_exclude_each_other(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch)
    for command in ("run", "dse"):
        result = CliRunner().invoke(
            cli, [command, "vivado_synth", "other.yaml", "--target", "arty", "--no-target"]
        )
        assert result.exit_code == 2
        assert "--target" in result.output and "--no-target" in result.output


def test_an_unknown_design_name_is_a_document(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch)
    result, document = invoke("run", "vivado_synth", "--design-name", "nope", "--dry-run")
    assert result.exit_code == 2 and document["error"]["code"] == "unknown_design"


def test_a_design_name_without_a_project_is_a_document(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result, document = invoke("run", "vivado_synth", "--design-name", "fifo", "--dry-run")
    assert result.exit_code == 2 and document["error"]["code"] == "project_file_not_found"


def test_the_launcher_needs_a_name_for_a_project_with_several_designs(tmp_path, monkeypatch):
    path = cli_project(tmp_path, monkeypatch)
    with pytest.raises(errors.ProjectError, match="fifo, inline") as raised:
        DefaultRunner(tmp_path / "run")._request("vivado_synth", None, xedaproject=str(path))
    assert raised.value.code == "design_name_required"


def test_a_malformed_project_is_reported_by_the_launcher(tmp_path):
    path = tmp_path / PROJECT_FILE
    path.write_text("designs: [{name: d, rtl: {sources: []}, tb: 3, tb.top: x}]\n")
    with pytest.raises(
        errors.ProjectError, match=r"Cannot load project file .*design entry 0 \(d\)"
    ):
        DefaultRunner(tmp_path / "run").plan("ghdl_sim", None, xedaproject=str(path))


# ------------------------------------------------------------------------- what is gone


def test_no_interactive_prompt_exists():
    assert not hasattr(xeda.cli_utils, "select_design_in_project")


def test_the_attrs_class_of_a_project_is_gone():
    import xeda.xedaproject as discovery

    assert not hasattr(discovery, "XedaProject")


def test_unset_is_one_value_that_prints_as_its_name():
    assert [member.name for member in Unset] == ["UNSET"] and UNSET is Unset.UNSET
    assert repr(UNSET) == "UNSET"


# ---------------------------------------------------------------- the files a launch was given


def test_a_launch_protects_the_design_file_and_the_project_file_it_selected(tmp_path):
    path = project_listing_two(tmp_path)
    runner = DefaultRunner(tmp_path / "run")
    runner._request("vivado_synth", "fifo", xedaproject=str(path))
    assert set(runner._launch_inputs) == {
        (tmp_path / "hw" / "fifo.yaml").resolve(),
        path.resolve(),
    }


def test_a_remote_launch_protects_the_same_files(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from xeda.deliver import ReadInputs
    from xeda.remote import client

    path = project_listing_two(tmp_path)
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    prepared = runner.prepare_request("vivado_synth", "fifo", xedaproject=str(path))
    seen = []

    def inputs(design, settings, sections, launch_inputs, **kwargs):
        seen.extend(launch_inputs)
        return ReadInputs(launch_inputs)

    monkeypatch.setattr(client, "remote_read_inputs", inputs)
    run = SimpleNamespace(mirror_root=tmp_path / "mirror", design="fifo")
    client._check_outputs_to(
        runner,
        prepared,
        run,
        outputs_to=tmp_path / "out",
        overwrite_outputs=False,
        confirm_overwrite=None,
    )
    assert set(seen) == {(tmp_path / "hw" / "fifo.yaml").resolve(), path.resolve()}


def project_listing_two(tmp_path: Path) -> Path:
    design_file(tmp_path / "hw", "fifo.yaml")
    return write_project(
        tmp_path,
        {
            "designs": [{"file": "hw/fifo.yaml"}],
            "flows": {"vivado_synth": {"fpga": {"part": "xc7a100tcsg324-1"}}},
        },
    )


# ------------------------------------------------- the paths of the project layer


def layered_project_with_paths(tmp_path: Path) -> Path:
    design_file(tmp_path / "hw", "fifo.yaml")
    return write_project(
        tmp_path,
        {
            "designs": [{"file": "hw/fifo.yaml"}],
            "flows": {
                "nextpnr": {"custom_boards_file": "boards.yaml", "seed": 3},
                "vivado_synth": {"xdc_files": ["top.xdc", "$DESIGN_ROOT/own.xdc", "/abs/a.xdc"]},
                "yosys": {"memory": {"libmap": ["lib/m.map"]}},
            },
        },
    )


def test_a_path_setting_of_the_project_resolves_against_the_project_file(tmp_path):
    path = layered_project_with_paths(tmp_path)
    root = path.parent.resolve()
    request = DefaultRunner(tmp_path / "run")._request("nextpnr", "fifo", xedaproject=str(path))
    section = dict(request.origins)[str(path)]["nextpnr"]
    assert section == {"custom_boards_file": str(root / "boards.yaml"), "seed": 3}
    assert request.settings["custom_boards_file"] == str(root / "boards.yaml")
    # the design's own file is below the project, and its root is another directory
    assert request.design.root == (root / "hw")


def test_every_kind_of_path_setting_of_the_project_is_resolved_and_the_rest_is_left(tmp_path):
    path = layered_project_with_paths(tmp_path)
    root = str(path.parent.resolve())
    runner = DefaultRunner(tmp_path / "run")
    sections = dict(runner._request("vivado_synth", "fifo", xedaproject=str(path)).origins)
    assert sections[str(path)]["vivado_synth"]["xdc_files"] == [
        f"{root}/top.xdc",
        "$DESIGN_ROOT/own.xdc",  # a variable is expanded where it is read
        "/abs/a.xdc",
    ]
    sections = dict(runner._request("yosys", "fifo", xedaproject=str(path)).origins)
    assert sections[str(path)]["yosys"]["memory"]["libmap"] == [f"{root}/lib/m.map"]


def test_the_paths_of_the_design_stay_relative_to_the_design_root(tmp_path):
    path = layered_project_with_paths(tmp_path)
    (tmp_path / "hw" / "fifo.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "fifo",
                "rtl": rtl(),
                "flows": {"nextpnr": {"custom_boards_file": "boards.yaml"}},
            }
        )
    )
    request = DefaultRunner(tmp_path / "run")._request("nextpnr", "fifo", xedaproject=str(path))
    assert request.settings["custom_boards_file"] == "boards.yaml"  # the design wins, as written
    assert request.design.root == (tmp_path / "hw").resolve()


def test_a_remote_request_ships_the_resolved_paths_of_the_project(tmp_path):
    path = layered_project_with_paths(tmp_path)
    prepared = DefaultRunner(tmp_path / "run", display_results=False).prepare_request(
        "nextpnr", "fifo", xedaproject=str(path), flow_settings=["fpga.part=LFE5U-25F-6BG256C"]
    )
    root = path.parent.resolve()
    assert prepared.sections["nextpnr"]["custom_boards_file"] == str(root / "boards.yaml")


def test_a_file_that_a_flow_writes_stays_a_name_in_the_run_directory(tmp_path):
    design_file(tmp_path / "hw", "fifo.yaml")
    path = write_project(
        tmp_path,
        {
            "designs": [{"file": "hw/fifo.yaml"}],
            "flows": {
                "yosys_fpga": {"netlist_json": "out/n.json"},
                "nextpnr": {"custom_boards_file": "boards.yaml"},
            },
        },
    )
    request = DefaultRunner(tmp_path / "run")._request("yosys_fpga", "fifo", xedaproject=str(path))
    sections = dict(request.origins)[str(path)]
    assert sections["yosys_fpga"]["netlist_json"] == "out/n.json"
    assert sections["nextpnr"]["custom_boards_file"] == str(path.parent.resolve() / "boards.yaml")


def test_a_project_and_a_design_in_one_directory_leave_the_paths_as_written(tmp_path):
    path = write_project(
        tmp_path,
        {
            "designs": [{"name": "d", "rtl": rtl()}],
            "flows": {"nextpnr": {"custom_boards_file": "boards.yaml"}},
        },
    )
    request = DefaultRunner(tmp_path / "run")._request("nextpnr", "d", xedaproject=str(path))
    assert dict(request.origins)[str(path)]["nextpnr"]["custom_boards_file"] == "boards.yaml"


# ------------------------------------------------------------------------------ the run root


def run_path_of(document) -> Path:
    return Path(document["plan"]["nodes"][-1]["run_path"])


def test_the_run_root_of_the_project_is_used_from_another_directory(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    design_file(repo, "other.yaml")
    project = write_project(
        repo, {"designs": [{"name": "d", "rtl": rtl()}], "run_root": "build/runs"}
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    arguments = ("run", "vivado_synth", "--xedaproject", str(project), "--dry-run", *PART)
    for design in (("--design-name", "d"), (str(repo / "other.yaml"),)):
        result, document = invoke(*arguments, *design)
        assert result.exit_code == 0, document
        assert run_path_of(document).is_relative_to((repo / "build" / "runs").resolve())


def test_the_run_root_option_and_variable_win_over_the_project(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    project = write_project(repo, {"designs": [{"name": "d", "rtl": rtl()}], "run_root": "build"})
    monkeypatch.chdir(tmp_path)
    arguments = (
        "run", "vivado_synth", "--xedaproject", str(project), "--design-name", "d", "--dry-run",
        *PART,
    )  # fmt: skip
    result = CliRunner().invoke(cli, [*arguments, "--json"], env={"XEDA_RUN_ROOT": "from_env"})
    assert run_path_of(json.loads(result.stdout)).is_relative_to((tmp_path / "from_env").resolve())
    result, document = invoke(*arguments, "--run-root", str(tmp_path / "given"))
    assert run_path_of(document).is_relative_to((tmp_path / "given").resolve())
    result = CliRunner().invoke(
        cli,
        [*arguments, "--run-root", str(tmp_path / "given"), "--json"],
        env={"XEDA_RUN_ROOT": "from_env"},
    )
    assert run_path_of(json.loads(result.stdout)).is_relative_to((tmp_path / "given").resolve())


def test_a_project_without_a_run_root_key_supplies_xeda_run_beside_it(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    design_file(tmp_path, "other.yaml")
    project = write_project(repo, {"designs": [{"name": "d", "rtl": rtl()}]})
    monkeypatch.chdir(tmp_path)
    for design in (("--design-name", "d"), (str(tmp_path / "other.yaml"),)):
        result, document = invoke(
            "run", "vivado_synth", "--xedaproject", str(project), *design, "--dry-run", *PART
        )
        assert result.exit_code == 0, document
        assert run_path_of(document).is_relative_to((repo / "xeda_run").resolve())


def test_without_a_project_the_run_root_is_xeda_run_here(tmp_path, monkeypatch):
    design_file(tmp_path, "other.yaml")
    monkeypatch.chdir(tmp_path)
    result, document = invoke("run", "vivado_synth", "other.yaml", "--dry-run", *PART)
    assert run_path_of(document).is_relative_to((tmp_path / "xeda_run").resolve())


def test_the_project_run_root_is_used_by_dse_and_the_remote_mirror(tmp_path, monkeypatch):
    from xeda.remote import client

    repo = tmp_path / "repo"
    project = write_project(repo, {"designs": [{"name": "d", "rtl": rtl()}], "run_root": "runs"})
    monkeypatch.chdir(tmp_path)
    seen = {}

    def fake_run_remote(chain, **arguments):
        seen.update(arguments)
        raise SystemExit(0)

    monkeypatch.setattr(client, "run_remote", fake_run_remote)
    CliRunner().invoke(
        cli,
        ["run", "vivado_synth", "--xedaproject", str(project), "--design-name", "d",
         "--remote", "host", *PART],
    )  # fmt: skip
    assert seen["run_root"] == (repo / "runs").resolve()
    seen.clear()
    CliRunner().invoke(
        cli,
        ["run", "vivado_synth", "--xedaproject", str(project), "--design-name", "d",
         "--remote", "host", "--run-root", str(tmp_path / "given"), *PART],
    )  # fmt: skip
    assert seen["run_root"] == (tmp_path / "given").resolve()


# -------------------------------------------------------- the design a command selects


def test_a_positional_design_and_design_name_exclude_each_other(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch)
    arguments = ("run", "vivado_synth", "other.yaml", "--design-name", "inline", "--dry-run")
    result, document = invoke(*arguments, *PART)
    assert result.exit_code == 2 and document["outcome"] is None and document["spec"] is None
    assert document["error"]["code"] == "usage_error"
    assert "give DESIGN or --design-name, not both" in document["error"]["message"]
    result = CliRunner().invoke(cli, list(arguments))
    assert result.exit_code == 2 and "give DESIGN or --design-name, not both" in result.output
    assert not (tmp_path / "xeda_run").exists()


def test_dse_takes_the_same_exclusion(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        cli, ["dse", "vivado_synth", "other.yaml", "--design-name", "inline"]
    )
    assert result.exit_code == 2 and "give DESIGN or --design-name, not both" in result.output


def test_the_only_design_of_a_project_is_selected_without_a_name_on_the_command_line(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    write_project(tmp_path, {"designs": [{"name": "solo", "rtl": rtl()}]})
    result, document = invoke("run", "vivado_synth", "--dry-run", *PART)
    assert result.exit_code == 0, document
    assert document["design"] == "solo"


def test_a_project_with_several_designs_needs_a_name_on_the_command_line(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch)
    result, document = invoke("run", "vivado_synth", "--dry-run", *PART)
    assert result.exit_code == 2 and document["outcome"] is None
    assert document["error"]["code"] == "design_name_required"
    assert "fifo, inline" in document["error"]["message"]


def test_no_design_and_no_project_is_the_usage_error_it_was(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result, document = invoke("run", "vivado_synth", "--dry-run", *PART)
    assert result.exit_code == 2 and document["error"]["code"] == "usage_error"
    assert "No design specified" in document["error"]["message"]


# ------------------------------------------- the project file of a design file is protected


def test_the_command_line_hands_the_launcher_the_project_of_a_design_file(tmp_path, monkeypatch):
    """The project supplies the run root, so it is a file of the launch, whichever design runs."""
    path = cli_project(tmp_path, monkeypatch)
    seen = []

    class Recording(DefaultRunner):
        def resolve_run(self, flow, design=None, xedaproject=None, *args, **kwargs):
            seen.append((design, xedaproject))
            raise RuntimeError("recorded")

    monkeypatch.setattr(xeda.cli, "DefaultRunner", Recording)
    CliRunner().invoke(cli, ["run", "vivado_synth", "other.yaml", *PART])
    CliRunner().invoke(cli, ["run", "vivado_synth", "--design-name", "fifo", *PART])
    assert [Path(project) for _design, project in seen] == [path, path]


def test_a_project_is_a_launch_input_of_a_design_file_without_a_layer(tmp_path, monkeypatch):
    path = cli_project(tmp_path, monkeypatch)
    runner = DefaultRunner(tmp_path / "run")
    request = runner._request("vivado_synth", "other.yaml", xedaproject=str(path))
    assert str(path) not in dict(request.origins), "its settings stay out of the design's layers"
    assert set(runner._launch_inputs) == {path.resolve(), (tmp_path / "other.yaml").resolve()}


def test_a_remote_run_without_a_design_takes_the_only_design_of_the_project(tmp_path, monkeypatch):
    from xeda.remote import client

    monkeypatch.chdir(tmp_path)
    project = write_project(tmp_path, {"designs": [{"name": "solo", "rtl": rtl()}]})
    seen = {}

    def fake_run_remote(chain, **arguments):
        seen.update(arguments)
        raise SystemExit(0)

    monkeypatch.setattr(client, "run_remote", fake_run_remote)
    CliRunner().invoke(cli, ["run", "vivado_synth", "--remote", "host", *PART])
    assert seen["design"] is None and Path(seen["xedaproject"]) == project
    assert seen["run_root"] == project.parent.resolve() / "xeda_run"
