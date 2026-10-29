"""The PDK providers: locate the files of a platform by path arithmetic and say how to install them.

A provider maps the role names of a residue to files in a store. It needs a root and the pinned
version, and it uses stat only: no process, no network, no write. A missing file is refused with a
message that names where the provider looked and the command that installs the files.
"""

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pytest

from xeda.flow import FlowFatalError
from xeda.platforms import PdkNotInstalled, Residue, locate
from xeda.platforms.providers import (
    CIEL_VERSION,
    ORFS_URL,
    PROVIDERS,
    PdkInstallerMissing,
    PdkVersionMismatch,
    default_root,
    install_plan,
    platform_directory,
    verify_pin,
)
from xeda.platforms.residue import effective_residue

TINY = Path(__file__).parent / "resources" / "platforms" / "tiny"

COMMIT = "6b305a712966be72d8908695c04398acdbe273ca"
OTHER_COMMIT = "1111111111111111111111111111111111111111"
CIEL_HASH = "8afc8346a57f0123456789abcdef0123456789ab"
OTHER_HASH = "2222222222222222222222222222222222222222"

KINDS = ["orfs", "ciel", "directory-orfs", "directory-open_pdks"]


@dataclass
class Store:
    """A fixture store: a residue for `tiny`, the provider that serves it, the root and the
    directory of the platform in the store."""

    kind: str
    provider: str
    residue: Residue
    root: Path
    directory: Path
    version: str
    #: The files of the platform: role paths and the upstream file, relative to `directory`.
    liberty: str
    liberty_fast: str
    tech_lef: str
    cell_lef: str
    config: str

    def relative_files(self) -> list[str]:
        return [self.liberty, self.liberty_fast, self.tech_lef, self.cell_lef, self.config]


def tiny_residue(kind: str) -> tuple[Residue, dict[str, str]]:
    """A residue for `tiny` that every provider serves, and its relative file names."""
    orfs_like = kind in ("orfs", "directory-orfs")
    prefix = "" if orfs_like else "libs.ref/tinylib/"
    names = {
        "liberty": f"{prefix}lib/tiny.lib",
        "liberty_fast": f"{prefix}lib/tiny_fast.lib",
        "tech_lef": f"{prefix}lef/tiny.tlef",
        "cell_lef": f"{prefix}lef/tiny.lef",
        "config": "config.mk" if orfs_like else "libs.tech/openlane/config.tcl",
    }
    providers: dict[str, Any] = {
        "orfs": {"version": COMMIT, "directory": "flow/platforms/tiny", "download_mib": 1.0},
        "ciel": {
            "family": "tinyfam",
            "variant": "tinyA",
            "library": "tinylib",
            "version": CIEL_HASH,
        },
        "directory": {"layout": "orfs"},
    }
    if kind == "orfs":
        providers.pop("ciel")
        providers["default"] = "orfs"
    elif kind == "ciel":
        providers.pop("orfs")
        providers["default"] = "ciel"
    elif kind == "directory-orfs":
        providers.pop("ciel")
        providers["default"] = "directory"
    else:
        providers.pop("orfs")
        providers.pop("ciel")
        providers["default"] = "directory"
        providers["directory"] = {"layout": "open_pdks", "variant": "tinyA", "library": "tinylib"}
    residue = Residue.model_validate(
        {
            "name": "tiny",
            "providers": providers,
            "upstream": {
                "adapter": "orfs_config_mk" if orfs_like else "open_pdks_config",
                "files": [names["config"]],
            },
            "corners": {"default": "typical", "aliases": {"tt": "typical"}},
            "files": {
                "liberty": {"typical": [names["liberty"]], "fast": [names["liberty_fast"]]},
                "tech_lef": names["tech_lef"],
                "cell_lef": [names["cell_lef"]],
            },
        }
    )
    return residue, names


def populate(directory: Path, names: dict[str, str]) -> None:
    sources = {
        "liberty": "tiny.lib",
        "liberty_fast": "tiny.lib",
        "tech_lef": "tiny.tlef",
        "cell_lef": "tiny.lef",
        "config": "config.mk",
    }
    for role, name in names.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TINY / sources[role], target)


def make_store(tmp_path: Path, kind: str, *, populated: bool = True) -> Store:
    residue, names = tiny_residue(kind)
    root = tmp_path / "store"
    if kind == "orfs":
        provider, version = "orfs", COMMIT
        directory = root / "xeda" / "orfs" / COMMIT / "flow" / "platforms" / "tiny"
    elif kind == "ciel":
        provider, version = "ciel", CIEL_HASH
        directory = root / "ciel" / "tinyfam" / "versions" / CIEL_HASH / "tinyA"
    elif kind == "directory-orfs":
        # <root> is the `flow/platforms` directory of an ORFS checkout.
        root = tmp_path / "orfs-checkout" / "flow" / "platforms"
        provider, version = "directory", "unpinned"
        directory = root / "tiny"
    else:
        provider, version = "directory", "unpinned"
        directory = root / "tinyA"
    if populated:
        populate(directory, names)
    return Store(
        kind=kind,
        provider=provider,
        residue=residue,
        root=root,
        directory=directory,
        version=version,
        liberty=names["liberty"],
        liberty_fast=names["liberty_fast"],
        tech_lef=names["tech_lef"],
        cell_lef=names["cell_lef"],
        config=names["config"],
    )


def locate_store(store: Store, corner: Optional[str] = None, version: Optional[str] = None):
    return locate(store.residue, store.provider, version, store.root, corner)


# ---------------------------------------------------------------------------------------------
# Resolving the files
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_locate_resolves_every_role_to_an_absolute_path(tmp_path, kind):
    store = make_store(tmp_path, kind)
    found = locate_store(store)
    assert found.provider == store.provider
    assert found.version == store.version
    assert found.root == store.root
    assert found.directory == store.directory
    assert found.corner == "typical"
    assert found.files == {
        "liberty": [store.directory / store.liberty],
        "tech_lef": [store.directory / store.tech_lef],
        "cell_lef": [store.directory / store.cell_lef],
    }
    assert found.upstream_files == [store.directory / store.config]
    assert all(path.is_absolute() for paths in found.files.values() for path in paths)


@pytest.mark.parametrize("kind", KINDS)
def test_locate_applies_the_corner_to_the_liberty_files_only(tmp_path, kind):
    store = make_store(tmp_path, kind)
    fast = locate_store(store, "fast")
    assert fast.corner == "fast"
    assert fast.files["liberty"] == [store.directory / store.liberty_fast]
    assert fast.files["tech_lef"] == [store.directory / store.tech_lef]
    alias = locate_store(store, "tt")
    assert alias.corner == "typical"
    assert alias.files["liberty"] == [store.directory / store.liberty]


def test_locate_refuses_an_unknown_corner_naming_the_corners(tmp_path):
    store = make_store(tmp_path, "orfs")
    with pytest.raises(ValueError, match=r"unknown corner 'slow'.*typical, fast.*tt \(typical\)"):
        locate_store(store, "slow")


def test_a_corner_whose_file_is_missing_is_refused_only_for_that_corner(tmp_path):
    store = make_store(tmp_path, "orfs")
    (store.directory / store.liberty_fast).unlink()
    assert locate_store(store).corner == "typical"
    with pytest.raises(PdkNotInstalled, match=r"missing: lib/tiny_fast\.lib"):
        locate_store(store, "fast")


def test_the_provider_defaults_to_the_one_of_the_residue(tmp_path):
    store = make_store(tmp_path, "orfs")
    found = locate(store.residue, None, None, store.root, None)
    assert found.provider == "orfs"
    assert found.version == COMMIT


def test_a_provider_without_an_entry_is_a_value_error_naming_the_providers_it_has(tmp_path):
    store = make_store(tmp_path, "orfs")
    with pytest.raises(ValueError, match=r"platform 'tiny'.*'ciel'.*serves.*orfs, directory"):
        locate(store.residue, "ciel", None, store.root, None)
    with pytest.raises(
        ValueError, match="unknown provider 'volare'.*orfs, ciel, lambdapdk, directory"
    ):
        locate(store.residue, "volare", None, store.root, None)


def test_a_residue_without_a_default_provider_needs_the_provider_named(tmp_path):
    store = make_store(tmp_path, "orfs")
    providers = store.residue.providers.model_copy(update={"default": None})
    bare = store.residue.model_copy(update={"providers": providers})
    with pytest.raises(ValueError, match="names no default provider"):
        locate(bare, None, None, store.root, None)
    assert locate(bare, "orfs", None, store.root, None).provider == "orfs"


@pytest.mark.parametrize("provider", ["orfs", "ciel"])
def test_a_version_that_is_no_commit_hash_is_refused_before_it_names_a_directory(
    tmp_path, provider
):
    store = make_store(tmp_path, provider)
    with pytest.raises(ValueError, match="not a full commit hash"):
        locate(store.residue, provider, "../../etc", store.root, None)


def test_a_user_residue_is_located_the_same_way(tmp_path):
    store = make_store(tmp_path, "orfs")
    user = Residue.model_validate(
        {
            "name": "mytiny",
            "extends": "tiny",
            "files": {"liberty": {"typical": ["lib/mine.lib"]}},
        }
    )
    effective = effective_residue(store.residue, user, None)
    (store.directory / "lib" / "mine.lib").write_text("library (mine) {}\n")
    found = locate(effective, "orfs", None, store.root, None)
    assert found.files["liberty"] == [store.directory / "lib" / "mine.lib"]
    assert found.files["tech_lef"] == [store.directory / store.tech_lef]


# ---------------------------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------------------------


def test_locate_never_creates_a_directory_under_a_root_that_does_not_exist(tmp_path):
    root = tmp_path / "no" / "such" / "root"
    for kind in KINDS:
        store = make_store(tmp_path, kind, populated=False)
        with pytest.raises(PdkNotInstalled):
            locate(store.residue, store.provider, None, root, None)
    assert not (tmp_path / "no").exists()


def test_a_store_reached_through_a_symbolic_link_resolves(tmp_path):
    store = make_store(tmp_path, "ciel")
    link = tmp_path / "link"
    link.symlink_to(store.root, target_is_directory=True)
    found = locate(store.residue, "ciel", None, link, None)
    assert found.root == link
    assert found.files["liberty"] == [
        link / "ciel/tinyfam/versions" / CIEL_HASH / "tinyA" / store.liberty
    ]
    assert found.files["liberty"][0].is_file()


def test_a_file_that_is_a_link_to_a_missing_file_is_missing(tmp_path):
    store = make_store(tmp_path, "orfs")
    target = store.directory / store.tech_lef
    target.unlink()
    target.symlink_to(store.directory / "gone.tlef")
    with pytest.raises(PdkNotInstalled, match=r"missing: lef/tiny\.tlef"):
        locate_store(store)


def test_a_relative_root_is_made_absolute(tmp_path, monkeypatch):
    store = make_store(tmp_path, "orfs")
    relative = Path("a-relative-store")
    with pytest.raises(PdkNotInstalled) as raised:
        locate(store.residue, "orfs", None, relative, None)
    assert str(relative.absolute()) in str(raised.value)


def test_the_default_root_follows_home_and_ignores_pdk_root(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PDK_ROOT", str(tmp_path / "elsewhere"))
    assert default_root() == home / ".ciel"
    monkeypatch.delenv("PDK_ROOT")
    assert default_root() == home / ".ciel"


def test_platform_directory_for_each_layout(tmp_path):
    for kind in KINDS:
        store = make_store(tmp_path, kind, populated=False)
        assert (
            platform_directory(store.residue, store.provider, None, store.root) == store.directory
        )


# ---------------------------------------------------------------------------------------------
# Names that become store paths
# ---------------------------------------------------------------------------------------------

BAD_COMPONENTS = ["", ".", "..", "../../x", "/abs", "a/b", "a\\b", "x/", "./x"]


def source_table(**changes: str) -> dict[str, Any]:
    ciel = {"family": "sky130", "variant": "sky130A", "library": "lib_hd", "version": CIEL_HASH}
    return {"default": "ciel", "ciel": {**ciel, **changes}}


@pytest.mark.parametrize("name", BAD_COMPONENTS)
@pytest.mark.parametrize("field", ["family", "variant", "library"])
def test_a_ciel_name_that_is_no_single_path_component_is_refused(field, name):
    with pytest.raises(ValueError, match=rf"(?s)ciel\.{field}.*single path component"):
        Residue.model_validate({"name": "t", "providers": source_table(**{field: name})})


@pytest.mark.parametrize("name", BAD_COMPONENTS)
@pytest.mark.parametrize("field", ["variant", "library"])
def test_a_directory_name_that_is_no_single_path_component_is_refused(field, name):
    directory = {"layout": "open_pdks", "variant": "v", "library": "l", field: name}
    with pytest.raises(ValueError, match=rf"(?s)directory\.{field}.*single path component"):
        Residue.model_validate({"name": "t", "providers": {"directory": directory}})


@pytest.mark.parametrize("name", ["sky130A", "sg13g2_stdcell", "ihp-sg13g2", "gf180mcuD", "a.b"])
def test_an_ordinary_pdk_name_is_a_path_component(name):
    providers = source_table(family=name, variant=name)
    Residue.model_validate({"name": "t", "extends": "tiny", "providers": providers})


@pytest.mark.parametrize("directory", ["../x", "a/../b", "/abs", ""])
def test_an_orfs_directory_cannot_leave_the_checkout(directory):
    orfs = {"version": COMMIT, "directory": directory}
    with pytest.raises(ValueError, match=r"(?s)orfs\.directory"):
        Residue.model_validate({"name": "t", "providers": {"orfs": orfs}})


# ---------------------------------------------------------------------------------------------
# The refusal
# ---------------------------------------------------------------------------------------------


def test_orfs_refusal_text(tmp_path):
    store = make_store(tmp_path, "orfs")
    (store.directory / store.liberty).unlink()
    with pytest.raises(PdkNotInstalled) as raised:
        locate_store(store)
    assert isinstance(raised.value, FlowFatalError)
    assert str(raised.value) == (
        "platform 'tiny' needs the PDK files of flow/platforms/tiny (orfs 6b305a712966).\n"
        f"  looked in: {store.directory} (missing: lib/tiny.lib)\n"
        f"  install:   xeda pdk install tiny --root {store.root}\n"
        "  or name another location: -s <flow>.pdk_root=<directory>"
    )


def test_ciel_refusal_text(tmp_path):
    store = make_store(tmp_path, "ciel")
    (store.directory / store.tech_lef).unlink()
    with pytest.raises(PdkNotInstalled) as raised:
        locate_store(store)
    assert str(raised.value) == (
        "platform 'tiny' needs the PDK files of tinylib (ciel 8afc8346a57f).\n"
        f"  looked in: {store.directory} (missing: libs.ref/tinylib/lef/tiny.tlef)\n"
        f"  install:   xeda pdk install tiny --root {store.root}\n"
        "  or name another location: -s <flow>.pdk_root=<directory>"
    )


def test_directory_refusal_text_has_no_install_command(tmp_path):
    store = make_store(tmp_path, "directory-orfs")
    (store.directory / store.liberty).unlink()
    with pytest.raises(PdkNotInstalled) as raised:
        locate_store(store)
    assert str(raised.value) == (
        "platform 'tiny' needs the PDK files of orfs (directory, unpinned).\n"
        f"  looked in: {store.directory} (missing: lib/tiny.lib)\n"
        "  install:   none; the directory must hold the files of the platform in the orfs layout\n"
        "  or name another location: -s <flow>.pdk_root=<directory>"
    )


def test_open_pdks_directory_refusal_names_the_layout_and_the_pin(tmp_path):
    store = make_store(tmp_path, "directory-open_pdks")
    (store.directory / store.liberty).unlink()
    with pytest.raises(PdkNotInstalled) as raised:
        locate_store(store, version="abc123")
    assert str(raised.value) == (
        "platform 'tiny' needs the PDK files of open_pdks (directory, abc123).\n"
        f"  looked in: {store.directory} (missing: libs.ref/tinylib/lib/tiny.lib)\n"
        "  install:   none; the directory must hold the files of the platform in the open_pdks"
        " layout\n"
        "  or name another location: -s <flow>.pdk_root=<directory>"
    )


def test_the_refusal_counts_the_other_missing_files(tmp_path):
    store = make_store(tmp_path, "orfs")
    for name in (store.liberty, store.tech_lef, store.cell_lef, store.config):
        (store.directory / name).unlink()
    with pytest.raises(PdkNotInstalled) as raised:
        locate_store(store)
    assert f"  looked in: {store.directory} (missing: lib/tiny.lib, and 3 more)\n" in str(
        raised.value
    )
    (store.directory / store.liberty).write_text("x")
    (store.directory / store.tech_lef).write_text("x")
    (store.directory / store.cell_lef).write_text("x")
    with pytest.raises(PdkNotInstalled) as raised:
        locate_store(store)
    assert "(missing: config.mk)\n" in str(raised.value)


def test_a_missing_upstream_file_is_refused_like_a_role_file(tmp_path):
    store = make_store(tmp_path, "ciel")
    (store.directory / store.config).unlink()
    with pytest.raises(PdkNotInstalled, match=r"missing: libs\.tech/openlane/config\.tcl"):
        locate_store(store)


def test_the_install_line_names_the_default_root_by_leaving_it_out(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    store = make_store(tmp_path, "ciel", populated=False)
    with pytest.raises(PdkNotInstalled) as raised:
        locate(store.residue, "ciel", None, default_root(), None)
    assert "\n  install:   xeda pdk install tiny\n" in str(raised.value)


def test_the_install_line_names_a_version_that_is_not_the_pin(tmp_path):
    store = make_store(tmp_path, "orfs", populated=False)
    with pytest.raises(PdkNotInstalled) as raised:
        locate(store.residue, "orfs", OTHER_COMMIT, store.root, None)
    lines = str(raised.value).splitlines()
    assert len(lines) == 4
    assert lines[0] == (
        "platform 'tiny' needs the PDK files of flow/platforms/tiny (orfs 111111111111)."
    )
    assert (
        lines[2]
        == f"  install:   xeda pdk install tiny --root {store.root} --version {OTHER_COMMIT}"
    )


def test_the_install_line_quotes_a_root_with_a_space(tmp_path):
    store = make_store(tmp_path, "orfs", populated=False)
    with pytest.raises(PdkNotInstalled) as raised:
        locate(store.residue, "orfs", None, tmp_path / "my pdks", None)
    assert f"--root '{tmp_path / 'my pdks'}'\n" in str(raised.value)


def both_providers(store: Store) -> Residue:
    """The residue of the `orfs` store, which a `ciel` build serves too, with `orfs` the default."""
    ciel = tiny_residue("ciel")[0].providers.ciel
    providers = store.residue.providers.model_copy(update={"ciel": ciel})
    return store.residue.model_copy(update={"providers": providers})


def install_line(raised: pytest.ExceptionInfo) -> str:
    return str(raised.value).splitlines()[2]


def test_the_install_line_names_a_provider_that_is_not_the_default(tmp_path):
    store = make_store(tmp_path, "orfs", populated=False)
    residue = both_providers(store)
    with pytest.raises(PdkNotInstalled) as raised:
        locate(residue, "ciel", None, store.root, None)
    assert install_line(raised) == (
        f"  install:   xeda pdk install tiny --provider ciel --root {store.root}"
    )
    with pytest.raises(PdkNotInstalled) as raised:
        locate(residue, "orfs", None, store.root, None)
    assert "--provider" not in install_line(raised)
    # A residue without a default names the provider whichever it is.
    providers = residue.providers.model_copy(update={"default": None})
    with pytest.raises(PdkNotInstalled) as raised:
        locate(residue.model_copy(update={"providers": providers}), "orfs", None, store.root, None)
    assert "--provider orfs" in install_line(raised)


def test_the_install_line_of_a_platform_file_names_the_file_not_the_platform(tmp_path):
    store = make_store(tmp_path, "orfs", populated=False)
    real = tmp_path / "real" / "mine.yaml"
    write(real, "name: tiny\n")
    link = tmp_path / "mine.yaml"
    link.symlink_to(real)
    with pytest.raises(PdkNotInstalled) as raised:
        locate(store.residue, "orfs", None, store.root, None, platform_file=link)
    assert install_line(raised) == (
        f"  install:   xeda pdk install --platform-file {real.resolve()} --root {store.root}"
    )


def test_the_install_line_names_every_option_in_a_fixed_order(tmp_path):
    store = make_store(tmp_path, "orfs", populated=False)
    residue = both_providers(store)
    file = write(tmp_path / "my dir" / "mine.yaml", "name: tiny\n")
    with pytest.raises(PdkNotInstalled) as raised:
        locate(residue, "ciel", OTHER_HASH, store.root, None, platform_file=file)
    assert install_line(raised) == (
        f"  install:   xeda pdk install --platform-file '{file.resolve()}' --provider ciel"
        f" --root {store.root} --version {OTHER_HASH}"
    )


def test_a_directory_has_no_install_command_even_for_a_platform_file(tmp_path):
    store = make_store(tmp_path, "directory-orfs", populated=False)
    file = write(tmp_path / "mine.yaml", "name: tiny\n")
    with pytest.raises(PdkNotInstalled) as raised:
        locate(store.residue, "directory", None, store.root, None, platform_file=file)
    assert install_line(raised).startswith("  install:   none; the directory must hold")


# ---------------------------------------------------------------------------------------------
# The version marker
# ---------------------------------------------------------------------------------------------


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def checked(store: Store, pin: Optional[str]) -> None:
    verify_pin(locate_store(store, version=pin), store.residue)


def test_an_orfs_checkout_with_a_commit_in_head(tmp_path):
    store = make_store(tmp_path, "directory-orfs")
    git = store.root.parent.parent / ".git"
    write(git / "HEAD", f"{COMMIT}\n")
    checked(store, COMMIT)
    with pytest.raises(PdkVersionMismatch) as raised:
        checked(store, OTHER_COMMIT)
    assert isinstance(raised.value, FlowFatalError)
    message = str(raised.value)
    assert OTHER_COMMIT in message and COMMIT in message and "HEAD" in message


def test_an_orfs_checkout_with_a_symbolic_ref(tmp_path):
    store = make_store(tmp_path, "directory-orfs")
    git = store.root.parent.parent / ".git"
    write(git / "HEAD", "ref: refs/heads/main\n")
    write(git / "refs" / "heads" / "main", f"{COMMIT}\n")
    checked(store, COMMIT)
    with pytest.raises(PdkVersionMismatch, match=f"{OTHER_COMMIT}.*{COMMIT}"):
        checked(store, OTHER_COMMIT)


def test_an_orfs_checkout_with_a_packed_ref(tmp_path):
    store = make_store(tmp_path, "directory-orfs")
    git = store.root.parent.parent / ".git"
    write(git / "HEAD", "ref: refs/heads/main\n")
    write(
        git / "packed-refs",
        f"# pack-refs with: peeled fully-peeled sorted\n{OTHER_COMMIT} refs/heads/dev\n"
        f"{COMMIT} refs/heads/main\n",
    )
    checked(store, COMMIT)
    with pytest.raises(PdkVersionMismatch, match=f"{OTHER_COMMIT}.*{COMMIT}"):
        checked(store, OTHER_COMMIT)


def test_a_ref_file_wins_over_the_packed_refs(tmp_path):
    store = make_store(tmp_path, "directory-orfs")
    git = store.root.parent.parent / ".git"
    write(git / "HEAD", "ref: refs/heads/main\n")
    write(git / "refs" / "heads" / "main", f"{COMMIT}\n")
    write(git / "packed-refs", f"{OTHER_COMMIT} refs/heads/main\n")
    checked(store, COMMIT)


@pytest.mark.parametrize("head", ["ref: refs/heads/nowhere\n", "", "garbage\n"])
def test_a_head_that_names_no_commit_is_no_error(tmp_path, head):
    store = make_store(tmp_path, "directory-orfs")
    write(store.root.parent.parent / ".git" / "HEAD", head)
    checked(store, COMMIT)


def test_unpinned_checks_nothing_and_a_missing_marker_passes(tmp_path):
    store = make_store(tmp_path, "directory-orfs")
    checked(store, None)  # no marker, no pin
    checked(store, COMMIT)  # no marker, a pin
    write(store.root.parent.parent / ".git" / "HEAD", f"{OTHER_COMMIT}\n")
    checked(store, None)  # a marker, no pin
    checked(store, "unpinned")


def test_an_open_pdks_directory_checks_the_open_pdks_line_of_sources(tmp_path):
    store = make_store(tmp_path, "directory-open_pdks")
    write(store.directory / "SOURCES", f"magic {OTHER_HASH}\nopen_pdks {CIEL_HASH}\n")
    checked(store, CIEL_HASH)
    with pytest.raises(PdkVersionMismatch) as raised:
        checked(store, OTHER_COMMIT)
    message = str(raised.value)
    assert OTHER_COMMIT in message and CIEL_HASH in message and "SOURCES" in message


def test_an_unrelated_line_of_sources_does_not_satisfy_the_pin(tmp_path):
    store = make_store(tmp_path, "directory-open_pdks")
    write(store.directory / "SOURCES", f"open_pdks {CIEL_HASH}\nmagic {OTHER_HASH}\n")
    with pytest.raises(PdkVersionMismatch, match=f"{OTHER_HASH}.*{CIEL_HASH}"):
        checked(store, OTHER_HASH)


def test_a_sources_file_without_an_open_pdks_line_names_no_version(tmp_path):
    store = make_store(tmp_path, "directory-open_pdks")
    write(store.directory / "SOURCES", f"magic {OTHER_HASH}\nskywater_pdk {OTHER_COMMIT}\n")
    checked(store, CIEL_HASH)
    checked(store, OTHER_HASH)


def nodeinfo_text(**commits: str) -> str:
    return json.dumps({"node": "tinyA", "commit": commits, "libraries": {"x": {"commit": "y"}}})


def test_an_open_pdks_directory_checks_nodeinfo_when_sources_names_no_version(tmp_path):
    store = make_store(tmp_path, "directory-open_pdks")
    write(
        store.directory / ".config" / "nodeinfo.json",
        nodeinfo_text(open_pdks=CIEL_HASH, magic=OTHER_HASH),
    )
    checked(store, CIEL_HASH)
    with pytest.raises(PdkVersionMismatch, match=f"{OTHER_HASH}.*nodeinfo"):
        checked(store, OTHER_HASH)
    write(store.directory / "SOURCES", f"magic {OTHER_HASH}\n")
    with pytest.raises(PdkVersionMismatch, match="nodeinfo"):
        checked(store, OTHER_HASH)


def test_a_nodeinfo_without_an_open_pdks_commit_names_no_version(tmp_path):
    store = make_store(tmp_path, "directory-open_pdks")
    write(store.directory / ".config" / "nodeinfo.json", nodeinfo_text(magic=OTHER_HASH))
    checked(store, CIEL_HASH)
    checked(store, OTHER_HASH)


def test_sources_wins_over_nodeinfo(tmp_path):
    store = make_store(tmp_path, "directory-open_pdks")
    write(store.directory / "SOURCES", f"open_pdks {CIEL_HASH}\n")
    write(store.directory / ".config" / "nodeinfo.json", nodeinfo_text(open_pdks=OTHER_HASH))
    checked(store, CIEL_HASH)
    with pytest.raises(PdkVersionMismatch):
        checked(store, OTHER_HASH)


def test_a_ciel_store_compares_the_open_pdks_line_only(tmp_path):
    store = make_store(tmp_path, "ciel")
    write(store.directory / "SOURCES", f"open_pdks {OTHER_HASH}\nmagic {CIEL_HASH}\n")
    with pytest.raises(PdkVersionMismatch, match=f"{CIEL_HASH}.*{OTHER_HASH}"):
        checked(store, None)


@pytest.mark.parametrize(
    "text", ["", "not json", "[1, 2]", '{"commit": 5}', '{"commit": {"open_pdks": 5}}']
)
def test_a_nodeinfo_without_commits_is_no_error(tmp_path, text):
    store = make_store(tmp_path, "directory-open_pdks")
    write(store.directory / ".config" / "nodeinfo.json", text)
    checked(store, CIEL_HASH)


def test_a_ciel_store_checks_sources_when_present(tmp_path):
    store = make_store(tmp_path, "ciel")
    checked(store, None)  # no SOURCES
    write(store.directory / "SOURCES", f"open_pdks {CIEL_HASH}\n")
    checked(store, None)
    write(store.directory / "SOURCES", f"open_pdks {OTHER_HASH}\n")
    with pytest.raises(PdkVersionMismatch, match=f"{CIEL_HASH}.*{OTHER_HASH}"):
        checked(store, None)


def test_an_orfs_store_has_nothing_to_check(tmp_path):
    store = make_store(tmp_path, "orfs")
    checked(store, None)


# ---------------------------------------------------------------------------------------------
# The install plan
# ---------------------------------------------------------------------------------------------


def which_only(monkeypatch, *available: str) -> None:
    monkeypatch.setattr(
        "shutil.which", lambda name, *a, **k: f"/bin/{name}" if name in available else None
    )


def test_the_plan_pins_the_ciel_version():
    assert CIEL_VERSION == "3.0.1"


def fetch_arguments(root: Path) -> list[str]:
    return ["fetch", "--pdk-root", str(root), "--pdk-family", "tinyfam", "-l", "tinylib", CIEL_HASH]


def test_ciel_plan_prefers_uv_then_pipx_then_a_ciel_on_the_path(tmp_path, monkeypatch):
    store = make_store(tmp_path, "ciel", populated=False)
    root = tmp_path / "pdks"
    which_only(monkeypatch, "uv", "pipx", "ciel")
    assert install_plan(store.residue, "ciel", None, root) == [
        ["uv", "tool", "run", "--from", "ciel==3.0.1", "ciel", *fetch_arguments(root)]
    ]
    which_only(monkeypatch, "pipx", "ciel")
    assert install_plan(store.residue, "ciel", None, root) == [
        ["pipx", "run", "--spec", "ciel==3.0.1", "ciel", *fetch_arguments(root)]
    ]
    which_only(monkeypatch, "ciel")
    assert install_plan(store.residue, "ciel", None, root) == [["ciel", *fetch_arguments(root)]]


def test_ciel_plan_without_any_installer_says_what_to_install(tmp_path, monkeypatch):
    store = make_store(tmp_path, "ciel", populated=False)
    which_only(monkeypatch)
    with pytest.raises(PdkInstallerMissing, match=r"uv.*pipx.*ciel==3\.0\.1"):
        install_plan(store.residue, "ciel", None, tmp_path)


def test_ciel_plan_takes_the_version_that_is_asked_for(tmp_path, monkeypatch):
    store = make_store(tmp_path, "ciel", populated=False)
    which_only(monkeypatch, "uv")
    plan = install_plan(store.residue, "ciel", OTHER_HASH, tmp_path)
    assert plan[0][-1] == OTHER_HASH


def test_orfs_plan_fetches_the_commit_into_the_partial_directory(tmp_path):
    store = make_store(tmp_path, "orfs", populated=False)
    root = tmp_path / "pdks"
    partial = str(root / "xeda" / ".partial-x7")
    assert install_plan(store.residue, "orfs", None, root, partial="x7") == [
        ["git", "init", "--quiet", partial],
        ["git", "-C", partial, "remote", "add", "origin", ORFS_URL],
        ["git", "-C", partial, "sparse-checkout", "set", "--cone", "flow/platforms/tiny"],
        [
            "git",
            "-C",
            partial,
            "fetch",
            "--quiet",
            "--depth",
            "1",
            "--filter=blob:none",
            "origin",
            COMMIT,
        ],
        ["git", "-C", partial, "checkout", "--quiet", "FETCH_HEAD"],
    ]


def test_orfs_plan_is_deterministic_and_needs_the_partial_name(tmp_path):
    store = make_store(tmp_path, "orfs", populated=False)
    first = install_plan(store.residue, "orfs", None, tmp_path, partial="a")
    assert first == install_plan(store.residue, "orfs", None, tmp_path, partial="a")
    with pytest.raises(ValueError, match="partial"):
        install_plan(store.residue, "orfs", None, tmp_path)


def test_orfs_plan_takes_the_version_that_is_asked_for(tmp_path):
    store = make_store(tmp_path, "orfs", populated=False)
    plan = install_plan(store.residue, "orfs", OTHER_COMMIT, tmp_path, partial="a")
    assert plan[3][-1] == OTHER_COMMIT


def test_a_directory_has_nothing_to_install(tmp_path):
    store = make_store(tmp_path, "directory-orfs", populated=False)
    with pytest.raises(ValueError, match="directory.*nothing to install"):
        install_plan(store.residue, "directory", None, tmp_path)


def test_providers_are_the_four_named_in_the_residue():
    assert PROVIDERS == ("orfs", "ciel", "lambdapdk", "directory")


# ---------------------------------------------------------------------------------------------
# No process, no network, no write
# ---------------------------------------------------------------------------------------------

_AUDIT: dict[str, Any] = {}
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def _audit(event: str, args: tuple) -> None:
    seen = _AUDIT.get("seen")
    if seen is None:
        return
    if event in ("subprocess.Popen", "socket.connect", "os.mkdir", "os.remove", "os.rename"):
        seen.append(event)
    elif event == "open":
        path, mode, flags = args
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & _WRITE_FLAGS
        )
        if writes:
            seen.append(f"open {path} {mode}")


sys.addaudithook(_audit)


def test_locate_starts_no_process_and_opens_no_connection_and_writes_nothing(tmp_path):
    stores = [make_store(tmp_path / kind, kind) for kind in ("orfs", "ciel", "directory-orfs")]
    stores.append(make_store(tmp_path / "open", "directory-open_pdks"))
    missing = make_store(tmp_path / "missing", "orfs", populated=False)
    scratch = tmp_path / "teeth"
    scratch.mkdir()
    _AUDIT["seen"] = seen = []
    try:
        # The hook sees a write, so an empty list below means something.
        (scratch / "probe").write_text("x")
        assert seen
        seen.clear()
        for store in stores:
            locate_store(store)
            locate_store(store, "fast")
        with pytest.raises(PdkNotInstalled):
            locate_store(missing)
        with pytest.raises(PdkNotInstalled):
            locate(missing.residue, "orfs", None, tmp_path / "no" / "root", None)
    finally:
        _AUDIT.pop("seen", None)
    assert seen == []
