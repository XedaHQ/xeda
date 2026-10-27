"""The `lambdapdk` provider of `sky130hd`: a sparse checkout of one commit of lambdapdk.

lambdapdk has a layout and values of its own, so its entry in the residue carries its own `files`
and `values`, and `Residue.for_provider` gives the residue as the provider sees it. These tests
use a fake repository (a local Git repository with the files of the entry) and never reach the
network. The checks against the real files are in `test_platform_conformance.py`.
"""

import ast
import gzip
import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any, Optional

import pytest
import yaml
from click.testing import CliRunner

from xeda.cli import cli
from xeda.flow import FlowSettingsException
from xeda.flow.asic import AsicPlatformSettings, check_platform_settings
from xeda.platforms import (
    PdkNotInstalled,
    Residue,
    build_platform,
    load_residue,
    locate,
)
from xeda.platforms.derive import derive
from xeda.platforms.providers import (
    GIT_PROVIDERS,
    LAMBDAPDK_URL,
    PROVIDERS,
    PdkInstallFailed,
    git_checkout_directory,
    install,
    install_plan,
    installed,
    plan_install,
    platform_directory,
    resolve_corner,
)
from xeda.platforms.residue import ResidueError, effective_residue, selected_provider

from .test_pdk_cli import quiet_git  # noqa: F401  (a fixture)

pytestmark = pytest.mark.python_compat

TINY = Path(__file__).parent / "resources" / "platforms" / "tiny"
SRC = Path(__file__).parent.parent / "src" / "xeda"


def sky130() -> Residue:
    return load_residue("sky130hd")


def entry() -> Any:
    found = sky130().providers.lambdapdk
    assert found is not None
    return found


def view() -> Residue:
    return sky130().for_provider("lambdapdk")


def relative_files() -> list[str]:
    """Every file that the install checks, relative to the directory of the platform."""
    names: list[str] = []
    residue = view()
    for corner in residue.files.liberty:
        for paths in residue.files.role_paths(corner).values():
            names += paths
    return list(dict.fromkeys(names))


def populate(directory: Path) -> None:
    """The files of the entry, with content that the derivation can read."""
    for name in relative_files():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name.endswith(".lib.gz"):
            path.write_bytes(gzip.compress((TINY / "tiny.lib").read_bytes()))
        elif name.endswith("tlef"):
            shutil.copyfile(TINY / "tiny.tlef", path)
        elif name.endswith("merged.lef"):
            shutil.copyfile(TINY / "tiny.lef", path)
        else:
            path.write_text("")


@pytest.fixture
def store(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    populate(git_checkout_directory(root, "lambdapdk", entry().version) / entry().directory)
    return root


# ---------------------------------------------------------------------------- the entry


def test_the_entry_is_pinned_to_a_commit_and_names_the_paths_that_the_install_fetches():
    found = entry()
    assert len(found.version) == 40 and found.directory == "lambdapdk/sky130"
    assert found.fetch == ["base", "libs/sky130hd"]
    assert found.download_mib is not None


def _table() -> dict[str, Any]:
    return yaml.safe_load(
        (SRC / "platforms" / "sky130hd" / "platform.yaml").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    "change, message",
    [
        ({"version": "v0.2.22"}, "not a full commit hash"),
        ({"directory": "/lambdapdk/sky130"}, "not a path relative"),
        ({"fetch": ["../base"]}, "not a path relative"),
        ({"fetch": ["a\\b"]}, "not a path relative"),
    ],
)
def test_an_entry_with_a_tag_or_a_path_that_leaves_the_checkout_is_refused(change, message):
    table = _table()
    table["providers"]["lambdapdk"].update(change)
    with pytest.raises(ValueError, match=message):
        Residue.model_validate(table)


def test_the_default_corner_and_the_aliases_must_exist_in_the_files_of_the_entry():
    table = _table()
    table["corners"] = {"default": "ss_100C_1v60"}
    # ciel has this corner, lambdapdk does not
    with pytest.raises(ValueError, match="providers.lambdapdk: .*ss_100C_1v60"):
        Residue.model_validate(table)
    table = _table()
    table["corners"]["aliases"] = {"fast": "ff_n40C_1v95"}
    with pytest.raises(ValueError, match="providers.lambdapdk"):
        Residue.model_validate(table)


# ---------------------------------------------------------------------------- the view


def test_the_view_takes_the_files_of_the_entry_and_drops_the_upstream():
    residue = sky130()
    assert residue.upstream is not None
    seen = view()
    assert seen.upstream is None
    assert list(seen.files.liberty) == ["tt_025C_1v80", "ss_n40C_1v40", "ff_100C_1v95"]
    assert seen.files.liberty["tt_025C_1v80"] == [
        "libs/sky130hd/nldm/sky130_fd_sc_hd__tt_025C_1v80.lib.gz"
    ]
    assert seen.files.tech_lef == "base/apr/sky130_fd_sc.tlef"
    assert seen.corners == residue.corners


def test_the_view_merges_the_values_of_the_entry_over_the_shared_ones():
    shared = sky130().value_leaves()
    leaves = view().value_leaves()
    assert leaves["place_density"] == shared["place_density"] == 0.6
    assert leaves["pdn.power_net"] == "VPWR"  # shared, not source specific
    assert leaves["endcap_cell"] == "sky130_fd_sc_hd__decap_4"
    assert leaves["tapcell.distance"] == 14
    assert leaves["pdn.pin_layers"] == ["met5"] and shared["pdn.pin_layers"] == ["met4", "met5"]
    assert leaves["routing.layer_adjustments.li1"] == 1.0 and shared[
        "routing.layer_adjustments.li1"
    ] == pytest.approx(0.99)
    assert "endcap_cell" not in shared


def test_the_view_is_idempotent_and_clears_the_entry():
    seen = view()
    again = seen.for_provider("lambdapdk")
    assert again == seen
    cleared = seen.providers.lambdapdk
    assert cleared is not None
    assert (cleared.files, cleared.upstream, cleared.values) == (None, None, None)
    assert cleared.version == entry().version


def test_a_view_is_the_view_of_one_provider_only():
    seen = view()
    assert seen.for_provider(None) is seen and seen.for_provider("lambdapdk") is seen
    with pytest.raises(ValueError, match="view of provider 'lambdapdk', not of 'ciel'"):
        seen.for_provider("ciel")
    with pytest.raises(ValueError, match="view of provider 'lambdapdk'"):
        locate(seen, "ciel", None, Path("/nowhere"), None)


def test_a_provider_without_a_layout_of_its_own_sees_the_residue_itself():
    residue = sky130()
    assert residue.for_provider("ciel") is residue
    assert residue.for_provider("directory") is residue
    assert residue.for_provider(None) is residue  # the default is ciel
    assert residue.for_provider("orfs") is residue  # no entry
    assert load_residue("nangate45").for_provider("lambdapdk").name == "nangate45"


# ---------------------------------------------------------------------------- the layers


def user_file(tmp_path: Path, text: str) -> Residue:
    path = tmp_path / "mine.yaml"
    path.write_text(text, encoding="utf-8")
    return load_residue(path)


def test_a_user_file_that_replaces_a_file_changes_it_for_every_provider(tmp_path):
    user = user_file(tmp_path, "name: sky130hd\nextends: sky130hd\nfiles: {tech_lef: mine.tlef}\n")
    for provider in ("ciel", "lambdapdk"):
        merged = effective_residue(sky130(), user, provider)
        assert merged.files.tech_lef == "mine.tlef", provider
    only = effective_residue(sky130(), user, "lambdapdk")
    assert only.files.cell_lef == ["libs/sky130hd/lef/sky130_fd_sc_hd_merged.lef"]
    assert only.upstream is None
    assert effective_residue(sky130(), user, "ciel").upstream is not None


def test_a_corner_of_the_user_file_is_judged_against_the_provider_that_is_selected(tmp_path):
    user = user_file(
        tmp_path, "name: sky130hd\nextends: sky130hd\ncorners: {default: ff_100C_1v95}\n"
    )
    merged = effective_residue(sky130(), user, "lambdapdk")
    assert merged.resolve_corner(None) == "ff_100C_1v95"
    with pytest.raises(ValueError, match="ff_100C_1v95"):
        effective_residue(sky130(), user, "ciel")


def test_a_corner_of_ciel_only_in_a_user_file_is_valid_with_ciel(tmp_path):
    """The lambdapdk entry of the platform must not judge the corners of a layout that it does
    not describe."""
    user = user_file(
        tmp_path,
        "name: sky130hd\nextends: sky130hd\ncorners: {default: ss_100C_1v60, aliases: {slow: "
        "ss_100C_1v60}}\n",
    )
    merged = effective_residue(sky130(), user, "ciel")
    assert merged.resolve_corner(None) == "ss_100C_1v60" and merged.resolve_corner("slow")
    with pytest.raises(ValueError, match="ss_100C_1v60"):
        effective_residue(sky130(), user, "lambdapdk")
    settings = settings_of(tmp_path, platform="sky130hd", platform_file="mine.yaml")
    assert settings.residue().resolve_corner(None) == "ss_100C_1v60"
    check_platform_settings(settings)
    selected = settings_of(
        tmp_path, platform="sky130hd", platform_file="mine.yaml", pdk_provider="lambdapdk"
    )
    with pytest.raises(FlowSettingsException, match="ss_100C_1v60"):
        check_platform_settings(selected)


def test_the_provider_of_the_layers_is_the_argument_else_the_default_of_the_user_file(tmp_path):
    user = user_file(
        tmp_path, "name: sky130hd\nextends: sky130hd\nproviders: {default: lambdapdk}\n"
    )
    assert selected_provider(sky130(), None, None) == "ciel"
    assert selected_provider(sky130(), user, None) == "lambdapdk"
    assert selected_provider(sky130(), user, "directory") == "directory"
    assert effective_residue(sky130(), user, None).upstream is None
    assert effective_residue(sky130(), None, None).upstream is not None


def test_a_user_residue_with_an_entry_of_its_own_is_viewed_on_its_own(tmp_path):
    text = """
    name: mylib
    providers:
      default: lambdapdk
      lambdapdk:
        version: "1111111111111111111111111111111111111111"
        directory: lambdapdk/mine
        files:
          liberty: {typical: [libs/mine.lib.gz]}
          tech_lef: base/mine.tlef
          cell_lef: [libs/mine.lef]
        values: {place_density: 0.5}
    corners: {default: typical}
    files:
      liberty: {typical: [elsewhere.lib]}
    values: {place_density: 0.7}
    """
    residue = user_file(tmp_path, textwrap.dedent(text))
    seen = effective_residue(None, residue, None)
    assert seen.files.liberty == {"typical": ["libs/mine.lib.gz"]}
    assert residue.for_provider(None).value_leaves()["place_density"] == 0.5


COMPLETE_ENTRY = """
name: mylib
providers:
  default: {default}
  lambdapdk:
    version: "1111111111111111111111111111111111111111"
    directory: lambdapdk/mine
    files:
      liberty: {{typical: [libs/mine.lib.gz]}}
      tech_lef: base/mine.tlef
      cell_lef: [libs/mine.lef]
  directory: {{layout: orfs}}
corners: {{default: typical{aliases}}}
{top}
"""


def complete_file(
    tmp_path: Path, default: str = "lambdapdk", aliases: str = "", top: str = ""
) -> Residue:
    text = COMPLETE_ENTRY.format(default=default, aliases=aliases, top=top)
    return user_file(tmp_path, text)


def test_a_complete_residue_may_give_its_files_only_under_its_default_provider(tmp_path):
    residue = complete_file(tmp_path)
    assert not residue.files.liberty and residue.providers.default == "lambdapdk"
    seen = effective_residue(None, residue, None)
    assert seen.files.liberty == {"typical": ["libs/mine.lib.gz"]}
    assert residue.for_provider("lambdapdk").resolve_corner(None) == "typical"
    root = tmp_path / "store"
    directory = git_checkout_directory(root, "lambdapdk", "1" * 40) / "lambdapdk/mine"
    for name in ("libs/mine.lib.gz", "base/mine.tlef", "libs/mine.lef"):
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text("")
    assert locate(residue, None, None, root, None).provider == "lambdapdk"


def test_a_complete_residue_needs_files_for_the_provider_that_is_its_default(tmp_path):
    with pytest.raises(ResidueError, match="files.liberty is missing"):
        complete_file(tmp_path, default="directory")
    with pytest.raises(ResidueError, match="providers.lambdapdk: .*not a corner of files.liberty"):
        complete_file(tmp_path, aliases=", aliases: {fast: nowhere}")
    top = "files: {liberty: {typical: [top.lib]}}"
    assert complete_file(tmp_path, default="directory", top=top).providers.default == "directory"


def test_a_view_stays_valid_when_it_is_validated_again_or_a_field_changes():
    """The default provider of the view of lambdapdk is ciel. Validating the view again, or
    assigning a field of it, judges its layout and selects no other provider."""
    seen = view()
    assert seen.providers.default == "ciel"
    seen.description = "another line"
    assert seen.description == "another line"
    again = Residue.model_validate(seen)
    assert again.files.liberty == seen.files.liberty
    assert again.for_provider("lambdapdk").resolve_corner(None) == "tt_025C_1v80"


def test_every_call_of_effective_residue_in_the_package_names_a_provider():
    """The residue of a platform depends on the provider that serves it. A call that leaves the
    provider out would read the layout of the default provider for every provider."""
    calls = 0
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = function.id if isinstance(function, ast.Name) else getattr(function, "attr", "")
            if name == "effective_residue":
                calls += 1
                assert len(node.args) + len(node.keywords) == 3, f"{path}:{node.lineno}"
    assert calls >= 5


# ---------------------------------------------------------------------------- finding the files


def test_the_directory_is_below_the_commit_of_the_xeda_part_of_the_store(tmp_path):
    root = tmp_path / "store"
    found = platform_directory(sky130(), "lambdapdk", None, root)
    assert found == root / "xeda" / "lambdapdk" / entry().version / "lambdapdk" / "sky130"
    assert git_checkout_directory(root, "lambdapdk", "a" * 40) == root / "xeda/lambdapdk" / (
        "a" * 40
    )


def test_locate_resolves_every_role_including_the_compressed_liberty_file(store):
    located = locate(sky130(), "lambdapdk", None, store, None)
    assert (located.provider, located.version) == ("lambdapdk", entry().version)
    assert located.corner == "tt_025C_1v80" and located.upstream_files == []
    assert located.files["liberty"][0].name == "sky130_fd_sc_hd__tt_025C_1v80.lib.gz"
    assert all(path.is_file() for paths in located.files.values() for path in paths)
    assert {"liberty", "tech_lef", "cell_lef", "gds", "rcx_rules", "adder_map"} <= set(
        located.files
    )
    fast = locate(sky130(), "lambdapdk", None, store, "ff_100C_1v95")
    assert fast.files["liberty"][0].name == "sky130_fd_sc_hd__ff_100C_1v95.lib.gz"
    assert locate(sky130(), "lambdapdk", None, store, "tt").corner == "tt_025C_1v80"


def test_a_corner_of_ciel_only_is_refused_naming_the_corners_of_lambdapdk(store):
    with pytest.raises(ValueError) as raised:
        locate(sky130(), "lambdapdk", None, store, "ss_100C_1v60")
    message = str(raised.value)
    assert "ss_100C_1v60" in message
    assert "ss_n40C_1v40" in message and "ff_100C_1v95" in message
    assert "provider 'lambdapdk'" in message
    with pytest.raises(ValueError, match="provider 'lambdapdk'"):
        resolve_corner(sky130(), "lambdapdk", "ff_n40C_1v95")
    assert resolve_corner(sky130(), "ciel", "ff_n40C_1v95") == "ff_n40C_1v95"


def test_a_missing_file_is_refused_with_the_command_that_installs_it(tmp_path):
    root = tmp_path / "store"
    with pytest.raises(PdkNotInstalled) as raised:
        locate(sky130(), "lambdapdk", None, root, None)
    lines = str(raised.value).splitlines()
    assert lines[0] == (
        f"platform 'sky130hd' needs the PDK files of lambdapdk/sky130 "
        f"(lambdapdk {entry().version[:12]})."
    )
    assert f"  looked in: {platform_directory(sky130(), 'lambdapdk', None, root)}" in lines[1]
    assert lines[2] == f"  install:   xeda pdk install sky130hd --provider lambdapdk --root {root}"
    assert lines[3].startswith("  or name another location")


def test_a_version_that_is_no_commit_is_refused(tmp_path):
    with pytest.raises(ValueError, match="not a full commit hash"):
        locate(sky130(), "lambdapdk", "v0.2.22", tmp_path, None)


def test_the_install_line_leaves_the_provider_out_when_it_is_the_default(tmp_path):
    residue = sky130().model_copy(deep=True)
    table = residue.model_dump(mode="json", exclude_none=True)
    table["providers"]["default"] = "lambdapdk"
    other = Residue.model_validate(table)
    with pytest.raises(PdkNotInstalled) as raised:
        locate(other, None, None, tmp_path, None)
    assert "  install:   xeda pdk install sky130hd --root" in str(raised.value)
    assert "--provider" not in str(raised.value)


def test_the_provider_is_a_member_of_the_git_family():
    assert "lambdapdk" in PROVIDERS and set(GIT_PROVIDERS) == {"orfs", "lambdapdk"}


# ---------------------------------------------------------------------------- the model


def test_the_model_builds_from_the_files_with_the_values_of_the_entry(store):
    residue = sky130()
    located = locate(residue, "lambdapdk", None, store, None)
    derived = derive(located.files, located.corner)
    model = build_platform(located, residue, None, derived, {})
    assert model.endcap_cell == "sky130_fd_sc_hd__decap_4"
    assert model.sources["endcap_cell"] == "residue"
    assert model.sources["site"] == "derived"
    assert model.pdn.power_net == "VPWR" and model.pdn.pin_layers == ["met5"]
    assert [strap.pitch for strap in model.pdn.straps] == [27.14, 27.2]
    assert model.sources["files.liberty"] == "residue"
    assert model.dont_use_cells[:2] == ["sky130_fd_sc_hd__probe_p_8", "sky130_fd_sc_hd__probec_p_8"]
    assert len(model.dont_use_cells) == 36


def test_the_model_of_ciel_keeps_the_values_that_it_had(tmp_path):
    residue = sky130()
    assert "endcap_cell" not in residue.value_leaves()
    assert residue.for_provider("ciel").value_leaves() == residue.value_leaves()


# ---------------------------------------------------------------------------- the settings


def settings_of(tmp_path: Path, **given: Any) -> AsicPlatformSettings:
    return AsicPlatformSettings.from_input(given, design_root=tmp_path, runner_cwd=tmp_path)


def test_the_settings_select_the_provider_and_its_view(tmp_path, store):
    settings = settings_of(
        tmp_path, platform="sky130hd", pdk_provider="lambdapdk", pdk_root=str(store)
    )
    assert settings.residue().upstream is None
    assert settings.residue().providers.default == "ciel"
    located = settings.locate_platform()
    assert located.provider == "lambdapdk" and located.upstream_files == []
    assert settings.platform_files(["liberty"])[0] == located.files["liberty"][0]
    plain = settings_of(tmp_path, platform="sky130hd")
    assert plain.residue().upstream is not None
    assert plain.residue_layers()[0] is not None


def test_a_corner_of_ciel_only_is_refused_at_planning_with_lambdapdk(tmp_path):
    settings = settings_of(
        tmp_path, platform="sky130hd", pdk_provider="lambdapdk", corner="ss_100C_1v60"
    )
    with pytest.raises(FlowSettingsException, match="ss_n40C_1v40.*provider 'lambdapdk'"):
        check_platform_settings(settings)
    check_platform_settings(
        settings_of(tmp_path, platform="sky130hd", pdk_provider="ciel", corner="ss_100C_1v60")
    )
    check_platform_settings(
        settings_of(tmp_path, platform="sky130hd", pdk_provider="lambdapdk", corner="ss_n40C_1v40")
    )


def test_a_version_that_is_not_the_pin_names_another_directory(tmp_path):
    other = "2" * 40
    settings = settings_of(
        tmp_path,
        platform="sky130hd",
        pdk_provider="lambdapdk",
        pdk_version=other,
        pdk_root=str(tmp_path / "s"),
    )
    with pytest.raises(PdkNotInstalled) as raised:
        settings.locate_platform()
    assert f"--version {other}" in str(raised.value)
    assert f"lambdapdk/{other}" in str(raised.value)


# ---------------------------------------------------------------------------- the install


def run_git(directory: Path, *arguments: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "-c",
            "gc.auto=0",
            *arguments,
        ],
        cwd=directory,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


class Repository:
    """A repository that looks like lambdapdk: the files of the entry, a sibling library that the
    sparse checkout must leave out, and a file directly in an ancestor directory, which a cone
    checkout brings along."""

    def __init__(self, directory: Path, leave_out: Optional[str] = None) -> None:
        self.directory = directory
        directory.mkdir()
        run_git(directory, "init", "--quiet", "--initial-branch=main")
        run_git(directory, "config", "uploadpack.allowFilter", "true")
        run_git(directory, "config", "uploadpack.allowAnySHA1InWant", "true")
        base = entry().directory
        for name in relative_files():
            if name != leave_out:
                self.write(f"{base}/{name}", f"placeholder {name}\n")
        self.write(f"{base}/libs/sky130hdll/sentinel.lib", "another library\n")
        self.write(f"{base}/libs/sky130sc.py", "# an ancestor file\n")
        self.write("lambdapdk/freepdk45/LICENSE", "another pdk\n")
        run_git(directory, "add", ".")
        run_git(directory, "commit", "--quiet", "-m", "files")
        self.commit = run_git(directory, "rev-parse", "HEAD")
        self.url = directory.as_uri()

    def write(self, name: str, text: str) -> None:
        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


@pytest.fixture
def repository(tmp_path, quiet_git, monkeypatch) -> Repository:  # noqa: F811
    repo = Repository(tmp_path / "upstream")
    monkeypatch.setattr("xeda.platforms.providers.LAMBDAPDK_URL", repo.url)
    return repo


def tree(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def test_the_plan_fetches_the_listed_paths_of_the_commit_from_the_top_of_the_repository(tmp_path):
    root = tmp_path / "store"
    plan = install_plan(sky130(), "lambdapdk", None, root, partial="abc")
    work = str(root / "xeda" / ".partial-abc")
    git = ["git", "-C", work]
    assert plan == [
        ["git", "init", "--quiet", work],
        [*git, "remote", "add", "origin", LAMBDAPDK_URL],
        [
            *git,
            *["sparse-checkout", "set", "--cone"],
            *["lambdapdk/sky130/base", "lambdapdk/sky130/libs/sky130hd"],
        ],
        [*git, *"fetch --quiet --depth 1 --filter=blob:none origin".split(), entry().version],
        [*git, "checkout", "--quiet", "FETCH_HEAD"],
    ]
    shown = plan_install(sky130(), "lambdapdk", None, root)
    assert shown.provider == "lambdapdk" and shown.download_mib == entry().download_mib
    assert shown.directory == platform_directory(sky130(), "lambdapdk", None, root)
    assert LAMBDAPDK_URL == "https://github.com/siliconcompiler/lambdapdk"


def test_the_plan_needs_the_partial_name_for_a_git_provider(tmp_path):
    with pytest.raises(ValueError, match="'lambdapdk' needs the name of its partial directory"):
        install_plan(sky130(), "lambdapdk", None, tmp_path)


def test_the_install_fetches_the_listed_paths_only_and_writes_a_receipt(tmp_path, repository):
    root = tmp_path / "store"
    result = install(sky130(), "lambdapdk", repository.commit, root, url=repository.url)
    directory = root / "xeda" / "lambdapdk" / repository.commit / "lambdapdk" / "sky130"
    assert result.already_installed is False and result.located.directory == directory
    for name in relative_files():
        assert (directory / name).read_text() == f"placeholder {name}\n"
    assert not (directory / "libs" / "sky130hdll").exists()  # a sibling library
    assert (directory / "libs" / "sky130sc.py").is_file()  # a file of an ancestor directory
    assert not (
        root / "xeda" / "lambdapdk" / repository.commit / "lambdapdk" / "freepdk45"
    ).exists()
    assert [p for p in (root / "xeda").iterdir() if p.name.startswith(".partial")] == []
    receipt = json.loads(result.receipt.read_text())
    assert receipt["provider"] == "lambdapdk" and receipt["version"] == repository.commit
    assert receipt["directory"] == str(directory)
    assert result.receipt == root / "xeda" / "receipts" / f"sky130hd-{repository.commit}.json"


def test_the_install_uses_the_url_of_the_provider_by_default(tmp_path, repository):
    root = tmp_path / "store"
    result = install(sky130(), "lambdapdk", repository.commit, root)
    assert result.commands[1][-1] == repository.url


def test_a_second_install_runs_nothing(tmp_path, repository):
    root = tmp_path / "store"
    install(sky130(), "lambdapdk", repository.commit, root)
    before = tree(root)
    again = install(sky130(), "lambdapdk", repository.commit, root)
    assert again.already_installed is True and again.commands == []
    assert tree(root) == before


def test_installed_needs_every_corner(tmp_path, repository):
    root = tmp_path / "store"
    install(sky130(), "lambdapdk", repository.commit, root)
    assert installed(sky130(), "lambdapdk", repository.commit, root) is not None
    directory = platform_directory(sky130(), "lambdapdk", repository.commit, root)
    (directory / "libs/sky130hd/nldm/sky130_fd_sc_hd__ss_n40C_1v40.lib.gz").unlink()
    assert installed(sky130(), "lambdapdk", repository.commit, root) is None
    assert locate(sky130(), "lambdapdk", repository.commit, root, None).corner == "tt_025C_1v80"


def test_a_commit_that_lacks_a_file_is_not_installed_and_leaves_the_store_as_it_was(
    tmp_path, quiet_git  # noqa: F811
):
    name = "libs/sky130hd/lef/sky130_fd_sc_hd_merged.lef"
    repo = Repository(tmp_path / "upstream", leave_out=name)
    root = tmp_path / "store"
    with pytest.raises(PdkInstallFailed, match=name):
        install(sky130(), "lambdapdk", repo.commit, root, url=repo.url)
    assert tree(root) == ["xeda", "xeda/.lock"]


def test_a_failed_fetch_leaves_the_store_as_it_was(tmp_path, repository):
    root = tmp_path / "store"
    with pytest.raises(PdkInstallFailed, match="fetch"):
        install(sky130(), "lambdapdk", "3" * 40, root, url=repository.url)
    assert tree(root) == ["xeda", "xeda/.lock"]


def test_a_link_in_the_store_stops_the_install(tmp_path, repository):
    root = tmp_path / "store"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "xeda").mkdir(parents=True)
    os.symlink(elsewhere, root / "xeda" / "lambdapdk")
    with pytest.raises(PdkInstallFailed, match="symbolic link"):
        install(sky130(), "lambdapdk", repository.commit, root, url=repository.url)
    assert tree(elsewhere) == []


def test_a_receipt_that_cannot_be_written_publishes_nothing(tmp_path, repository, monkeypatch):
    from xeda.platforms import providers

    def fail(*arguments: Any, **keywords: Any) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(providers, "_write_receipt", fail)
    root = tmp_path / "store"
    with pytest.raises(PdkInstallFailed, match="disk full"):
        install(sky130(), "lambdapdk", repository.commit, root, url=repository.url)
    assert not (root / "xeda" / "lambdapdk").exists()
    assert [p.name for p in (root / "xeda").iterdir()] == [".lock"]


def test_a_rename_that_fails_restores_the_earlier_receipt(tmp_path, repository, monkeypatch):
    root = tmp_path / "store"
    receipt = root / "xeda" / "receipts" / f"sky130hd-{repository.commit}.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text("earlier\n")
    original = Path.rename

    def fail(self: Path, target: Any) -> Path:
        if ".partial-" in str(self):
            raise OSError("cannot rename")
        return original(self, target)

    monkeypatch.setattr(Path, "rename", fail)
    with pytest.raises(PdkInstallFailed, match="cannot rename"):
        install(sky130(), "lambdapdk", repository.commit, root, url=repository.url)
    assert receipt.read_text() == "earlier\n"
    assert not (root / "xeda" / "lambdapdk").exists()


def snapshot(root: Path) -> dict[str, bytes]:
    """The content of every file below `root` except the lock, by relative name."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != ".lock"
    }


@pytest.mark.parametrize("first", ["orfs", "lambdapdk"])
def test_both_git_providers_share_a_store_without_touching_each_other(
    tmp_path, repository, quiet_git, monkeypatch, first  # noqa: F811
):
    from xeda.platforms.providers import orfs_checkout_directory

    from .test_pdk_cli import Upstream

    orfs_repository = Upstream(tmp_path / "orfs-upstream")
    monkeypatch.setattr("xeda.platforms.providers.ORFS_URL", orfs_repository.url)
    nangate = load_residue("nangate45")
    installs = {
        "orfs": lambda root: install(nangate, "orfs", orfs_repository.commit, root),
        "lambdapdk": lambda root: install(sky130(), "lambdapdk", repository.commit, root),
    }
    second = "lambdapdk" if first == "orfs" else "orfs"
    root = tmp_path / "store"
    installs[first](root)
    before = snapshot(root)
    installs[second](root)
    after = snapshot(root)
    assert {name: after[name] for name in before} == before  # the first install is unchanged
    assert set(after) - set(before)  # the second one added its files
    assert sorted(p.name for p in (root / "xeda").iterdir()) == [
        ".lock",
        "lambdapdk",
        "orfs",
        "receipts",
    ]
    assert installed(nangate, "orfs", orfs_repository.commit, root) is not None
    assert installed(sky130(), "lambdapdk", repository.commit, root) is not None
    assert orfs_checkout_directory(root, orfs_repository.commit).is_dir()
    assert git_checkout_directory(root, "lambdapdk", repository.commit).is_dir()


def test_the_default_url_of_the_other_git_provider_is_unchanged():
    from xeda.platforms.providers import ORFS_URL, git_url

    assert git_url("orfs") == ORFS_URL and git_url("lambdapdk") == LAMBDAPDK_URL
    with pytest.raises(ResidueError):
        load_residue("no-such-platform")


# ---------------------------------------------------------------------------- the commands


def command(monkeypatch, tmp_path: Path, *arguments: str, input: Optional[str] = None):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return CliRunner().invoke(cli, ["pdk", *arguments], input=input)


def test_the_plan_shows_the_provider_the_size_and_the_paths_to_fetch(
    monkeypatch, tmp_path, repository
):
    root = tmp_path / "store"
    result = command(
        monkeypatch, tmp_path, "install", "sky130hd", "--provider", "lambdapdk",
        "--root", str(root), "--version", repository.commit, input="n\n",
    )  # fmt: skip
    assert result.exit_code != 0 and not root.exists()
    text = result.stdout
    assert "from provider lambdapdk" in text and "about 8 MiB" in text
    assert "sparse-checkout set --cone lambdapdk/sky130/base lambdapdk/sky130/libs/sky130hd" in text
    assert repository.url in text


def test_the_install_command_installs_and_reports_the_receipt_in_json(
    monkeypatch, tmp_path, repository
):
    root = tmp_path / "store"
    result = command(
        monkeypatch, tmp_path, "install", "sky130hd", "--provider", "lambdapdk",
        "--root", str(root), "--version", repository.commit, "--json",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["success"] is True and document["provider"] == "lambdapdk"
    assert document["version"] == repository.commit and document["already_installed"] is False
    directory = root.resolve() / "xeda" / "lambdapdk" / repository.commit / "lambdapdk" / "sky130"
    assert document["directory"] == str(directory)
    again = command(
        monkeypatch, tmp_path, "install", "sky130hd", "--provider", "lambdapdk",
        "--root", str(root), "--version", repository.commit, "--json",
    )  # fmt: skip
    assert json.loads(again.stdout)["already_installed"] is True


def test_the_path_command_gives_the_files_of_lambdapdk(monkeypatch, tmp_path, store):
    result = command(
        monkeypatch, tmp_path, "path", "sky130hd", "--provider", "lambdapdk",
        "--root", str(store), "--corner", "ss_n40C_1v40", "--json",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["provider"] == "lambdapdk" and document["upstream_files"] == []
    assert document["files"]["liberty"][0].endswith("sky130_fd_sc_hd__ss_n40C_1v40.lib.gz")
    refused = command(
        monkeypatch, tmp_path, "path", "sky130hd", "--provider", "lambdapdk",
        "--root", str(store), "--corner", "ss_100C_1v60",
    )  # fmt: skip
    assert refused.exit_code != 0 and "ss_n40C_1v40" in refused.output


def test_the_path_command_without_the_files_says_how_to_install_them(monkeypatch, tmp_path):
    result = command(
        monkeypatch, tmp_path, "path", "sky130hd", "--provider", "lambdapdk",
        "--root", str(tmp_path / "empty"),
    )  # fmt: skip
    assert result.exit_code != 0
    assert "xeda pdk install sky130hd --provider lambdapdk" in result.output


def test_a_corner_of_ciel_only_in_a_user_file_is_valid_for_the_commands_of_ciel(
    monkeypatch, tmp_path, store
):
    mine = tmp_path / "mine.yaml"
    mine.write_text(
        "name: mine\nextends: sky130hd\ncorners: {default: ss_100C_1v60}\n", encoding="utf-8"
    )
    result = command(
        monkeypatch, tmp_path, "path", "--platform-file", str(mine), "--root", str(tmp_path / "no")
    )
    # the corner is accepted; only the missing PDK files stop the command
    assert result.exit_code != 0 and "xeda pdk install --platform-file" in result.output
    assert "unknown corner" not in result.output


def test_a_user_file_over_the_platform_is_viewed_for_the_provider_of_the_command(
    monkeypatch, tmp_path, store
):
    mine = tmp_path / "mine.yaml"
    mine.write_text(
        "name: mine\nextends: sky130hd\ncorners: {default: ff_100C_1v95}\n", encoding="utf-8"
    )
    ok = command(
        monkeypatch, tmp_path, "path", "--platform-file", str(mine), "--provider", "lambdapdk",
        "--root", str(store), "--json",
    )  # fmt: skip
    assert ok.exit_code == 0, ok.output
    assert json.loads(ok.stdout)["corner"] == "ff_100C_1v95"
    refused = command(
        monkeypatch, tmp_path, "path", "--platform-file", str(mine), "--provider", "ciel",
        "--root", str(store),
    )  # fmt: skip
    assert refused.exit_code != 0 and "--platform-file" in refused.output


def test_the_list_shows_a_row_for_the_provider_and_the_other_installed_commits(
    monkeypatch, tmp_path, store
):
    pin = entry().version
    other = git_checkout_directory(store, "lambdapdk", "2" * 40) / entry().directory
    populate(other)
    partial = git_checkout_directory(store, "lambdapdk", "3" * 40) / entry().directory
    populate(partial)
    (partial / "libs/sky130hd/nldm/sky130_fd_sc_hd__ff_100C_1v95.lib.gz").unlink()
    result = command(monkeypatch, tmp_path, "list", "--json", "--root", str(store))
    assert result.exit_code == 0, result.output
    rows = {(row["platform"], row["provider"]): row for row in json.loads(result.stdout)}
    row = rows["sky130hd", "lambdapdk"]
    assert row["default"] is False and row["version"] == pin and row["installed"] is True
    assert {item["version"]: item["installed"] for item in row["other_versions"]} == {
        "2" * 40: True,
        "3" * 40: False,  # one corner is missing
    }
    assert rows["sky130hd", "ciel"]["default"] is True


# ---------------------------------------------------------------------------- invalid layers


BAD_FILE = "name: sky130hd\nextends: sky130hd\ncorners: {aliases: {fast: nowhere}}\n"


@pytest.mark.parametrize("provider", ["ciel", "lambdapdk"])
def test_a_user_file_that_is_invalid_for_its_provider_is_a_settings_error_at_planning(
    tmp_path, monkeypatch, provider
):
    """The error reaches the user as a settings error naming `platform_file`, and `--json` gives
    its document, whichever provider's view is invalid."""
    (tmp_path / "top.v").write_text("module top(input clk, output reg q);\nendmodule\n")
    (tmp_path / "d.yaml").write_text(
        "name: top\nrtl: {sources: [top.v], top: top, clock.port: clk}\n", encoding="utf-8"
    )
    (tmp_path / "mine.yaml").write_text(BAD_FILE, encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    result = CliRunner().invoke(
        cli,
        [
            *["run", "--json", "--dry-run", "yosys", str(tmp_path / "d.yaml")],
            *["-s", f"platform_file={tmp_path / 'mine.yaml'}", "-s", "clock.period=10ns"],
            *["-s", f"pdk_provider={provider}"],
        ],
    )
    document = json.loads(result.stdout)
    assert result.exit_code == 2 and document["outcome"] is None
    assert document["error"]["code"] == "shared_setting_conflict"
    assert "corners.aliases.fast" in document["error"]["message"]
    assert "platform_file" in document["error"]["message"]


def test_the_residue_functions_raise_a_residue_error_for_layers_that_do_not_fit(tmp_path):
    user = user_file(tmp_path, BAD_FILE)
    for provider in ("ciel", "lambdapdk"):
        with pytest.raises(ResidueError, match="corners.aliases.fast"):
            effective_residue(sky130(), user, provider)
