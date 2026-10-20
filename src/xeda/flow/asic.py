"""The settings and the flow mixin that every ASIC flow shares.

An ASIC flow works on one platform: a standard cell library with its technology files. The
platform is a name (`platform`) of a platform that Xeda ships, or a residue file of the user
(`platform_file`). The PDK files of the platform live in a store (`pdk_root`) that one provider
serves (`pdk_provider`) at one version (`pdk_version`). Together these settings select one PDK, so
they agree along every declared edge of a chain (`flow_runner.resolver.SHARED_SETTINGS`), and the
resolver fills in the provider and the version from the residue when the user gives none. Then
the pin is part of the identity of every node, and the place of the store is not (`LOCATOR`).

The settings read the residue files and find the PDK files with `stat`. They start no process
and write nothing, so planning and `--dry-run` can use them.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Literal, Mapping, Optional

from ..dataclass import LOCATOR, Field, field_validator
from ..utils import unique
from .flow import Flow, FlowFatalError, FlowSettingsException

if TYPE_CHECKING:
    from ..platforms import Located, PlatformModel, Residue

    _FlowBase = Flow
else:
    _FlowBase = object

__all__ = [
    "AsicPlatformFlow",
    "AsicPlatformSettings",
    "DERIVATION_ROLES",
    "check_platform_settings",
    "platform_residues",
]

#: The roles of the files that the derivation of a platform model reads (the derived layer: the
#: site, the tie cells, the supply pins, the tracks and the units).
DERIVATION_ROLES = ("liberty", "tech_lef", "cell_lef")

_NO_TOML = (
    "`platform` takes a bundled name, not the path of a config.toml: see `xeda list-platforms`, "
    "or give a residue file in `platform_file`"
)


def _bundled_residue(name: str) -> "Residue":
    from ..platforms.residue import ResidueError, bundled_platform_names, load_residue

    names = bundled_platform_names()
    if name not in names:
        raise FlowSettingsException(
            f"no platform '{name}' is bundled; the bundled platforms are: {', '.join(names)}. "
            "Give the residue file of another platform in `platform_file`."
        )
    try:
        return load_residue(name)
    except ResidueError as error:
        raise FlowSettingsException(str(error)) from None


def platform_residues(
    platform: Optional[str], platform_file: Optional[Path]
) -> "tuple[Optional[Residue], Optional[Residue]]":
    """Read the residue files of a platform: the bundled one and the one of the user.

    Return `(bundled, user)`. The bundled residue is that of `platform`, or the one that the user
    file extends, and it is `None` for a user file that extends none. The user residue is `None`
    without `platform_file`. Raise `FlowSettingsException` for a file that cannot be read, for
    a name that no bundled platform has, and for a `platform` that is not the `name` of the
    user file. The function reads nothing else."""
    from ..platforms.residue import ResidueError, load_residue

    if platform is None and platform_file is None:
        raise FlowSettingsException(
            "no platform: give `platform` (a bundled name, see `xeda list-platforms`), or a "
            "residue file in `platform_file`"
        )
    user: Optional["Residue"] = None
    if platform_file is not None:
        try:
            user = load_residue(Path(platform_file))
        except ResidueError as error:
            raise FlowSettingsException(f"`platform_file` is not usable: {error}") from None
        if platform is not None and platform != user.name:
            raise FlowSettingsException(
                f"`platform` is '{platform}', but `platform_file` {platform_file} names the "
                f"platform '{user.name}': give one platform, or make the names equal"
            )
        base = user.extends
    else:
        base = platform
    return (None if base is None else _bundled_residue(base)), user


def check_platform_settings(settings: "AsicPlatformSettings") -> None:
    """Refuse a platform that the settings cannot select. Pure: it reads the residue files only.

    That is a platform that is unknown or unreadable, a `platform` that disagrees with the file,
    an unknown corner, a provider that does not serve the platform, and a version that is no
    pin of that provider. Raise `FlowSettingsException`. Settings without a platform pass, unless
    they give a corner: a corner needs a platform."""
    if settings.platform is None and settings.platform_file is None:
        if settings.corner is not None:
            raise FlowSettingsException(
                "`corner` selects a corner of a platform: give `platform` or `platform_file` too"
            )
        return
    from ..platforms.providers import platform_directory, resolve_corner

    residue = settings.residue()
    try:
        resolve_corner(residue, settings.pdk_provider, settings.corner)
        platform_directory(
            residue, settings.pdk_provider, settings.pdk_version, settings.store_root()
        )
    except ValueError as error:
        raise FlowSettingsException(str(error)) from None


class AsicPlatformSettings(Flow.Settings):
    """The settings that select the platform and the PDK of an ASIC flow."""

    platform: Optional[str] = Field(
        None,
        description="Name of a bundled ASIC platform (see `xeda list-platforms`). It names the "
        "standard cell library and the technology files that the flow works on. Give "
        "`platform_file` for a platform that Xeda does not ship; then `platform` can be left out, "
        "and if given it must be the `name` of that file.",
    )
    platform_file: Optional[Path] = Field(
        None,
        description="Residue file of a platform (YAML). A file that sets `extends` to a bundled "
        "platform changes only what it gives. A file without `extends` is a complete platform. "
        "A relative path is relative to the design root.",
    )
    corner: Optional[str] = Field(
        None,
        description="Name of the corner of the platform, or one of its aliases. The default is "
        "the default corner of the platform. A flow works on one corner: a list of corners is "
        "refused.",
    )
    liberty: List[Path] = Field(
        [],
        alias="library",
        description="Standard cell (Liberty) libraries that the flow reads for mapping and timing, "
        "instead of the Liberty files of the corner of the platform. Xeda reads each file as it "
        "is (`yosys` gives each to `dfflibmap` and to `abc`). Without them, the flow uses the "
        "Liberty files of the platform. The platform still gives everything else, and its files "
        "must be installed. The files of a producer and its consumer agree, in the same order.",
    )
    dont_use_cells: List[str] = Field(
        [],
        description="Standard cells that the flow must not use, in addition to the cells that "
        "the platform lists. The default is none.",
    )
    pdk_provider: Optional[Literal["orfs", "ciel", "lambdapdk", "directory"]] = Field(
        None,
        description="Where the PDK files of the platform come from. `orfs`: a sparse checkout "
        "of OpenROAD-flow-scripts. `ciel`: a PDK build that `ciel` installs. `lambdapdk`: a "
        "sparse checkout of lambdapdk. `directory`: a directory that holds the files in a known "
        "layout. The default is the provider that the platform names; the platform must have an "
        "entry for the provider.",
    )
    pdk_version: Optional[str] = Field(
        None,
        description="Version of the PDK files: the commit (`orfs`, `lambdapdk`) or the hash "
        "(`ciel`). For `directory`, a version that Xeda checks against the directory when it can, and "
        "`unpinned` when you give none. The default is the version that the platform pins for "
        "`pdk_provider`. The version is part of the identity of the run.",
    )
    pdk_root: Optional[Path] = Field(
        None,
        json_schema_extra=LOCATOR,
        description="Directory of the PDK store. `None` means `~/.ciel`. Variables such as "
        "`$HOME` expand in it, and a relative path is relative to the design root. It tells "
        "where the files are, so it is no part of the identity of the run: the run depends on "
        "the content of the files it reads.",
    )

    @field_validator("platform", mode="after")
    @classmethod
    def _platform_is_a_name(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value.lower().endswith(".toml"):
            raise ValueError(_NO_TOML)
        return value

    @field_validator("corner", mode="before")
    @classmethod
    def _corner_is_one_name(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            raise ValueError(
                "`corner` takes one corner name: multi-corner analysis is not supported"
            )
        return value

    # --- where things are ---------------------------------------------------------------------

    def _in_design(self, path: Path) -> Path:
        """Return `path`, made relative to the design root as the other path settings are."""
        root = self.context.get("design_root")
        return path if path.is_absolute() or root is None else Path(root) / path

    def store_root(self) -> Path:
        """Return the root of the PDK store: `pdk_root`, or `~/.ciel` (`~` is `HOME`)."""
        from ..platforms.providers import default_root

        return default_root() if self.pdk_root is None else self._in_design(self.pdk_root)

    def liberty_files(self) -> List[Path]:
        """Return the files of `liberty`, a relative path against the design root, in order."""
        return [self._in_design(path) for path in self.liberty]

    def user_platform_file(self) -> Optional[Path]:
        """Return the residue file of the user, or `None`."""
        return None if self.platform_file is None else self._in_design(self.platform_file)

    # --- the residue and the files ------------------------------------------------------------

    def residue_layers(self) -> "tuple[Optional[Residue], Optional[Residue]]":
        """Return the bundled residue and the residue of the user (see `platform_residues`), each
        as the selected provider sees it (`pdk_provider`, else the default provider)."""
        from ..platforms.residue import selected_provider

        bundled, user = platform_residues(self.platform, self.user_platform_file())
        provider = selected_provider(bundled, user, self.pdk_provider)
        try:
            return (
                None if bundled is None else bundled.for_provider(provider),
                None if user is None else user.for_provider(provider),
            )
        except ValueError as error:
            raise FlowSettingsException(str(error)) from None

    def residue(self) -> "Residue":
        """Return the effective residue: the bundled one of `platform`, or the user file of
        `platform_file` over the bundled one that it extends, as the selected provider sees it.
        This reads the residue files only."""
        from ..platforms.residue import effective_residue

        bundled, user = self.residue_layers()
        try:
            return effective_residue(bundled, user, self.pdk_provider)
        except ValueError as error:
            # The file loads alone and is wrong over the platform, or for this provider.
            raise FlowSettingsException(str(error)) from None

    def locate_platform(self) -> "Located":
        """Find the PDK files of the platform in the store, with `stat` only.

        The provider, the version, the root and the corner are those of the settings. Raise
        `PdkNotInstalled` if a file is missing, and `FlowSettingsException` for a provider or a
        corner that the platform does not have."""
        from ..platforms import locate

        try:
            return locate(
                self.residue(),
                self.pdk_provider,
                self.pdk_version,
                self.store_root(),
                self.corner,
                platform_file=self.user_platform_file(),
            )
        except ValueError as error:
            raise FlowSettingsException(str(error)) from None

    @staticmethod
    def included_by(liberty: Path) -> List[Path]:
        """Return the files that the Liberty file `liberty` includes (`include_file(...)`), which
        the derivation and the time-unit check read through. A file that is not there gives none:
        the check that needs it reports it. Raise `FlowSettingsException` for an include that the
        reader refuses or cannot find."""
        from ..platforms.derive import liberty_included_files

        if not liberty.is_file():
            return []
        try:
            return liberty_included_files(liberty)
        except ValueError as error:
            raise FlowSettingsException(str(error)) from None

    def platform_files(
        self, roles: Iterable[str], located: "Optional[Located]" = None
    ) -> List[Path]:
        """Return every file that a flow may open for the platform, once, in a fixed order.

        First the files of `roles` (the role names of the residue, such as `liberty`, `gds`), in
        the order given. Then the files that the derivation of the platform model reads
        (`DERIVATION_ROLES`) and that are not listed. Then the files of the upstream
        configuration. Then the files that a Liberty file of the platform or of `liberty`
        includes with `include_file(...)`, which the derivation and the check of the time unit
        read. Last comes the residue file of the user. A role that the platform has no files for
        adds none: the flow that needs it refuses when it prepares its inputs.
        `located` is the result of `locate_platform()` when the caller has it."""
        if located is None:
            located = self.locate_platform()
        listed: dict[Path, None] = {}
        for role in (*roles, *DERIVATION_ROLES):
            listed.update(dict.fromkeys(located.files.get(role, [])))
        listed.update(dict.fromkeys(located.upstream_files))
        for liberty in (*located.files.get("liberty", []), *self.liberty_files()):
            listed.update(dict.fromkeys(self.included_by(liberty)))
        user = self.user_platform_file()
        if user is not None:
            listed.setdefault(user)
        return list(listed)


class AsicPlatformFlow(_FlowBase):
    """A mixin for a flow that works on an ASIC platform. List it before the flow base class,
    and give the flow settings that inherit `AsicPlatformSettings`.

    A flow that requires `platform` (`Flow.required_settings`) also runs when only
    `platform_file` is given, since the residue file names its platform. The flow refuses the
    platform that its settings cannot select when it is planned (`check_platform_settings`).

    The flow finds the PDK files and builds the platform model in `prepare_inputs()`
    (`prepare_platform`), never in `init()`. `locate_prepared_inputs()` returns the same files
    without building anything (`locate_platform_inputs`). The model is `platform_model`, and
    `platform_located` tells where its files are."""

    #: The platform model that `prepare_platform` built. It is `None` before.
    platform_model: "Optional[PlatformModel]" = None
    #: Where `prepare_platform` found the PDK files. It is `None` before.
    platform_located: "Optional[Located]" = None

    @classmethod
    def required_settings_for(cls, settings: Flow.Settings) -> Mapping[str, str]:
        required = super().required_settings_for(settings)
        if getattr(settings, "platform_file", None) is not None:
            return {name: how for name, how in required.items() if name != "platform"}
        return required

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        super().check_settings_supported(settings)
        if isinstance(settings, AsicPlatformSettings):
            check_platform_settings(settings)

    def _platform_settings(self) -> AsicPlatformSettings:
        settings = self.settings
        assert isinstance(settings, AsicPlatformSettings)
        return settings

    def _verified_location(self) -> "Located":
        """Find the PDK files, and check the version marker of the store against the pin."""
        from ..platforms.providers import verify_pin

        settings = self._platform_settings()
        located = settings.locate_platform()
        verify_pin(located, settings.residue())
        return located

    def locate_platform_inputs(self, roles: Iterable[str]) -> List[Path]:
        """Return the files that `prepare_platform(roles)` registers as inputs, in its order.

        This looks at the file system with `stat` and reads the residue files and the version
        marker of the store. It builds nothing and writes nothing. Raise `PdkNotInstalled` if
        a file is missing."""
        return self._platform_settings().platform_files(roles, self._verified_location())

    def prepare_platform(self, roles: Iterable[str]) -> "PlatformModel":
        """Find the PDK files, build the platform model, and register the files as inputs.

        The layers of the model come from the files themselves (derivation), the upstream
        configuration of the platform, the residue of the platform and the residue of the user.
        The flow reads `roles` and the files that the derivation reads, and
        `locate_platform_inputs(roles)` returns the same list. Raise `PdkNotInstalled` if a file
        is missing, `PdkVersionMismatch` if the store holds another version than the pin, and
        `FlowFatalError` for a file that cannot be read and for a residue that is invalid for
        the provider."""
        from ..platforms import build_platform
        from ..platforms.derive import derive
        from ..platforms.residue import effective_residue
        from ..platforms.upstream import read_upstream

        settings = self._platform_settings()
        located = self._verified_location()
        bundled, user = settings.residue_layers()
        try:
            upstream = effective_residue(bundled, user, located.provider).upstream
            derived = derive(located.files, located.corner)
            read = (
                {}
                if upstream is None
                else read_upstream(
                    upstream.adapter, located.upstream_files, located.directory, located.library
                )
            )
            model = build_platform(located, bundled, user, derived, read)
        except ValueError as error:
            raise FlowFatalError(str(error)) from None
        self.platform_located, self.platform_model = located, model
        self.implicit_inputs.extend(settings.platform_files(roles, located))
        return model

    def platform_result(self) -> Optional[Dict[str, Any]]:
        """Return the platform part of the results, or `None` before `prepare_platform` ran.

        It names the platform, the provider and the version of its PDK files, and the corner.
        `sources` says which layer of the model gave each value: `derived`, `upstream`,
        `residue` or `user`."""
        model, located = self.platform_model, self.platform_located
        if model is None or located is None:
            return None
        return {
            "name": model.name,
            "provider": located.provider,
            "version": located.version,
            "corner": model.corner,
            "sources": model.model_dump(include={"sources"})["sources"],
        }

    def dont_use_cells(self) -> List[str]:
        """Return the cells the flow must not use: those of the platform model, then those of
        `dont_use_cells`, without repeats. Before `prepare_platform`, only the latter."""
        model = self.platform_model
        return unique(
            [*(model.dont_use_cells if model else []), *self._platform_settings().dont_use_cells]
        )
