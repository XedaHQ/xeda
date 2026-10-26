"""Package the inputs of a request for the remote host.

`package_inputs` decides which local files go to the remote, where each one lies there, and what
the design document says about them. It writes nothing and sends nothing. The result lists the
files and the generated content by their path relative to the directory of the inputs on the
remote (`remote_inputs`), as `manifest.build_manifest` and `manifest.write_tar` take them.
"""

import gzip
import hashlib
import json
import os
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict

from ..dataclass import AtomicName, BaseModel, is_locator, opaque_key_fields_of, written_role
from ..design import (
    Design,
    DesignPart,
    DesignValidationError,
    FileResource,
    Generator,
    Source,
    generator_of,
)
from ..flow import FlowSettingsError, FlowSettingsException
from ..flow.asic import AsicPlatformSettings
from ..flow.flow import map_keyed_path_leaves
from ..flow_runner.bindings import INPUTS_KEY, BindingLayer, written_bindings
from ..flow_runner.chains import FlowRequest
from ..flow_runner.default_runner import _get_flow_class_if_known
from ..listing import VCS_METADATA, directory_files
from ..frozen import thaw
from ..utils import json_encodable, location_free, location_roots


@dataclass(frozen=True)
class InputPackage:
    """What goes to the remote host.

    `design_file` names the design document. `files` maps a path relative to the inputs directory
    of the remote to the local file that goes there. `generated` maps a path to content made
    here: the design document itself (`generated[design_file]`) and the Liberty files with
    rewritten include names. `path_identities` maps the absolute remote path of a source, a
    parameter value or a setting's input to the text that the local run counts for it, by role
    (`utils.PATH_IDENTITY_ROLES`): one file can be a source and a parameter value, with two texts.
    `directories` lists the empty directories of directory settings."""

    design_file: str
    files: dict[str, Path]
    generated: dict[str, bytes]
    path_identities: dict[str, dict[str, str]]
    #: The directories of the inputs that hold no file, relative to the inputs directory. The
    #: remote makes them, since a manifest lists files only.
    directories: list[str] = field(default_factory=list)


def refuse_unsendable_generator(design: Design) -> Generator | None:
    """Return the generator of `design`, or None if it has none. Refuse a generator that the
    package cannot carry, also one of a dependency: one that declares no `sources`.

    The host runs the generators, and only the files and directories that a recipe declares
    travel with it. A generator that declares none would run on the host without what it reads,
    and its output would be wrong or fail far from the cause. The refusal names the generator and
    says what to declare (`DesignValidationError`). It needs no connection, so a request makes it
    at planning."""
    for unit in (*design.generation_units, design):
        generator = generator_of(unit.rtl, root=unit.root_path)
        if generator is not None and not generator.sources:
            raise DesignValidationError(
                [
                    (
                        "rtl -> generator",
                        f"{generator.describe(unit.name, unit.root_path)} declares no "
                        "`sources`, so `--remote` cannot know which files to send with it: "
                        "declare its `sources` (the files and directories it reads) in "
                        "`rtl.generator`",
                        "",
                        "value_error",
                    )
                ],
                data=unit.model_dump(mode="json"),
                design_root=unit.root_path,
                design_name=unit.name,
            )
    return generator_of(design.rtl, root=design.root_path)


def package_inputs(
    design: Design,
    sections: Mapping[str, Any],
    planned_flows: Collection[str],
    *,
    run_root: Path,
    remote_inputs: PurePosixPath,
    host_flows: Mapping[str, Mapping[str, Any]] | None = None,
    bindings: Sequence[BindingLayer] = (),
    request: FlowRequest | None = None,
) -> InputPackage:
    """Return the files and the design document that the remote needs for this request.

    `sections` are the merged flow sections of the request, as the remote has to see them: it
    does not get the project file. `planned_flows` names the flows of the plan. The Liberty files
    of those flows that include others travel with the files they include. The settings of any
    other flow are not used, so they travel as path settings do and are not read. `run_root` is
    the local run root: a directory setting travels without it. `remote_inputs` is the absolute
    directory of the inputs on the remote. The remote names the files below it, and the includes
    of a Liberty file name them by absolute path.

    A locator setting (`pdk_root`) names a place on the machine that reads it. It travels as
    `None`, unless the host profile gives it (`host_flows`, by flow): then the remote gets that
    value as it is.

    The sections hold settings only: a section does not hold `inputs`, since the planner took
    the bindings out of every origin. `bindings` are the binding layers of the local plan
    (`Plan.bindings`). Their winning bindings travel as the `inputs` of the section of their
    flow, so the remote plans the same edges. A binding that the chain `request` makes does not
    travel, because the remote gets the chain.

    The host runs the whole launch, so it runs the generator of the design too. The recipe of the
    generator travels with the files and directories it declares in `sources`, and nothing else
    of what it reads: a generator that declares none cannot be sent, and the request is refused
    (`DesignValidationError`) before anything is read or sent. The sources that the generator
    writes travel as the declarations the design makes of them."""
    host_flows = host_flows or {}
    refuse_unsendable_generator(design)
    path_identities: dict[str, dict[str, str]] = {}
    # `mode="json"`: the fields kept as they are (`dependencies` and its `local_cache` /
    # `clone_dir`) hold `Path`s, which a python-mode dump leaves as objects for the encoder
    # to stringify by luck rather than by rule.
    # The design a target yielded, with the name of the target. The remote plans the same run
    # directories as this side (`<design>/<target>/<flow>`), and a design file may not write
    # `target`, but a packaged document may (`Design.from_file`).
    new_design: Dict[str, Any] = {
        **design.model_dump(mode="json", exclude={"target"}),
        "design_root": None,
    }
    if design.target is not None:
        new_design["target"] = design.target
    design_file = f"{design.name}.xeda.json"
    # Every place below the inputs directory, and what goes there: a local file, or content made
    # here. One table for both kinds of place (flat among the sources, or where a file lies under
    # the design root), so neither can be handed a place the other already holds. The design
    # document takes its place first.
    places: Dict[Path, Path | bytes] = {Path(design_file): b""}
    flat_places: Dict[tuple[Path, Path], Path] = {}
    directories: set[str] = set()  # the directories of directory settings

    def identified(remote: str | Path, role: str, text: str) -> None:
        """Record that the host sees `remote` (a place below the inputs directory, or an absolute
        path) in `role`, and that this side counts `text` for it. The host counts the same text.
        Two different texts for one path in one role are an error, never decided by order."""
        key = (
            str(remote_inputs / Path(remote).as_posix())
            if not os.path.isabs(remote)
            else str(remote)
        )
        known = path_identities.setdefault(key, {}).setdefault(role, text)
        if known != text:
            raise FlowSettingsException(
                f"one file, {key}, is named as `{known}` and as `{text}` in the same role "
                f"({role}) of the request, and the remote host counts it once: name it one way"
            )

    def archived(resource: FileResource, unit: Design, base: Path) -> Path:
        """Return where `resource` lands among the packaged sources of `unit`, below `base`.

        The first time a resource is seen, add it. Two files may share a name under
        different directories, and this part of the inputs is flat, so a repeated name is
        made unique by content. The same file listed twice still travels once.
        """
        local = resource.file.resolve()
        place = flat_places.get((base, local))
        if place is not None:
            return place
        remote_sources_path = base / unit.name / "sources"
        place = remote_sources_path / local.name
        if place in places:
            digest = resource.content_hash[:8]
            place = remote_sources_path / f"{local.stem}_{digest}{local.suffix}"
            suffix = 2
            while place in places:
                place = remote_sources_path / f"{local.stem}_{digest}_{suffix}{local.suffix}"
                suffix += 1
        places[place] = local
        flat_places[(base, local)] = place
        return place

    def archived_in_place(local: Path, relative: Path, base: Path) -> bool:
        """Package `local` at `relative` below `base`, where it lies under the design root.

        Then the remote finds it where this side does. Return False when that place holds
        another file."""
        return places.setdefault(base / relative, local.resolve()) == local.resolve()

    def packaged_sources(
        sources: Iterable[Source], unit: Design, base: Path
    ) -> list[Dict[str, Any]]:
        """Return the sources as the remote has to see them.

        A source under the design root keeps its own place relative to the root, as it has
        here. A Verilog `include` finds the header beside the including file first, so a
        flattened layout could build another design, and it would hash as another
        (`Design._source_fingerprint`). A source outside the root has no place there, and
        travels among the flat sources."""
        packaged: list[Dict[str, Any]] = []
        for src in sources:
            if src.generated and not src.located:
                # The generator of the host writes it: no file travels, the declaration does.
                declared: Dict[str, Any] = {"path": src.output_name, "type": str(src.type)}
                if src.owner is not None:
                    declared["generated_by"] = unit_index(src.owner)
                packaged.append(declared)
                continue
            relative = unit.relative_to_root(src.file)
            if relative is not None and archived_in_place(src.file, relative, base):
                place = base / relative
            else:
                place = archived(src, unit, base)
                identified(place, "source", unit.source_place(src))
            # a path of the document is relative to the root of the unit
            spec: Dict[str, Any] = {"file": place.relative_to(base).as_posix()}
            spec["type"] = str(src.type)
            packaged.append(spec)
        return packaged

    def packaged_parameters(dv: DesignPart, unit: Design, base: Path) -> Dict[str, Any]:
        """Return the parameters as the remote has to see them.

        A parameter is text to the tool, but text that names a path on this machine names
        nothing on the remote. So a path is re-rooted from its value alone:

        - Under the design root, it keeps its place relative to the root. An existing file
          is packaged at that same place. A path with no file yet (an output) is only a
          place for the remote to write. Counted relative to the root on both sides, the
          remote hashes the design exactly as this side did.
        - Elsewhere, an existing file is packaged among the sources and repointed there.

        Either way it travels as a table (`{file: ...}`, `{path: ...}`), which the remote
        resolves against its own design root and not against whatever directory it runs in.
        Anything else is a value, and travels as it is (an absolute directory, for example).

        For every absolute path, the host gets the text that this side counts for it
        (`Design.part_metadata`), keyed by the path as the host sees it: the new place, or the
        value itself when it travels as it is.
        """
        parameters: Dict[str, Any] = {}
        counted = unit.part_metadata("rtl" if dv is unit.rtl else "tb")["parameters"]
        for name, value in dv.parameters.items():
            parameters[name] = thaw(value)
            if not (isinstance(value, str) and os.path.isabs(value)):
                continue
            path = Path(value)
            relative = unit.relative_to_root(path)
            seen: str | Path = value
            if relative is not None and not path.exists():
                parameters[name] = {"path": relative.as_posix()}
                seen = base / relative
            elif (
                relative is not None and path.is_file() and archived_in_place(path, relative, base)
            ):
                parameters[name] = {"file": relative.as_posix()}
                seen = base / relative
            elif path.is_file():
                seen = archived(FileResource(path), unit, base)
                parameters[name] = {"file": seen.relative_to(base).as_posix()}
            identified(seen, "parameter", counted[name])
        return parameters

    def unit_index(owner: str) -> int:
        """The index of the unit of generation whose root is `owner`."""
        return [unit.generation_label for unit in design.generation_units].index(owner)

    def packaged_generator_input(local: Path, unit: Design, base: Path) -> str:
        """Package a file or a directory that the generator of `unit` reads, below `base` in the
        inputs of the host, and return its name for the recipe: relative to the root of the unit
        where it lies under it, else in a place of its own.

        A directory counts as every file in it, and its empty directories travel too."""
        relative = unit.relative_to_root(local)
        if relative is None:
            digest = hashlib.sha256(str(local).encode()).hexdigest()[:16]
            relative = Path("_generator_inputs") / digest / local.name
        if local.is_dir():
            directories.add((base / relative).as_posix())
            for entry in directory_files(local, [run_root], VCS_METADATA, follow_links=True):
                place = base / relative / entry.relative_to(local)
                if entry.is_dir():
                    directories.add(place.as_posix())
                elif entry.is_file():
                    places.setdefault(place, entry)
        elif local.is_file():
            places.setdefault(base / relative, local)
        return relative.as_posix()

    def packaged_parts(unit: Design, base: Path) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """The `rtl` and `tb` of `unit` as the remote has to see them, with their files packaged
        below `base`: the one packaging of a design's parts, for the design and for a dependency
        whose generator the host runs."""
        rtl: Dict[str, Any] = {}
        tb: Dict[str, Any] = {}
        rtl["sources"] = packaged_sources(unit.rtl.sources, unit, base)
        rtl["defines"] = thaw(unit.rtl.defines)
        rtl["attributes"] = thaw(unit.rtl.attributes)
        rtl["parameters"] = packaged_parameters(unit.rtl, unit, base)
        rtl["top"] = unit.rtl.top
        rtl["clocks"] = [dict(clk) for clk in unit.rtl.clocks]
        recipe_of = generator_of(unit.rtl, root=unit.root_path)
        if recipe_of is not None:
            recipe = thaw(unit.rtl.generator)
            recipe["sources"] = [
                packaged_generator_input(path, unit, base) for path in recipe_of.sources
            ]
            rtl["generator"] = recipe
            if recipe_of.cwd is not None:
                # The generator starts there, so the directory exists on the host even when no
                # file of the package lies in it. It is the directory the recipe names, as the
                # recipe spells it: a link is not resolved, and a name that leaves the root, or is
                # absolute, is not made.
                named = Path(os.path.normpath(recipe_of.cwd))
                if (
                    not named.is_absolute()
                    and named.parts[:1] not in ((".",), ("..",))
                    and (unit.root_path / named).is_dir()
                ):
                    directories.add((base / named).as_posix())
        tb["sources"] = packaged_sources(unit.tb.sources, unit, base)
        tb["top"] = unit.tb.top
        tb["cocotb"] = thaw(unit.tb.cocotb)
        if unit.tb.uut:
            tb["uut"] = unit.tb.uut
        if unit.tb.parameters:
            tb["parameters"] = packaged_parameters(unit.tb, unit, base)
        if unit.tb.defines:
            tb["defines"] = thaw(unit.tb.defines)
        if unit.tb.python_packages:
            tb["python_packages"] = list(unit.tb.python_packages)
        return rtl, tb

    def packaged_unit(index: int, unit: Design) -> Dict[str, Any]:
        """The document of a dependency whose generator the host runs, with its content packaged
        below `_units/<index>`, which is its design root there. The host rebuilds the design from
        it and computes the key of its generation from the same configuration."""
        base = Path("_units") / str(index)
        unit_rtl, unit_tb = packaged_parts(unit, base)
        return {
            "name": unit.name,
            "design_root": base.as_posix(),
            "rtl": unit_rtl,
            "tb": unit_tb,
            "language": thaw(unit.language),
        }

    rtl, tb = packaged_parts(design, Path("."))
    new_design["rtl"] = rtl
    new_design["tb"] = tb
    if design.generation_units:
        new_design["generation_units"] = [
            packaged_unit(index, unit) for index, unit in enumerate(design.generation_units)
        ]

    def packaged_path(value: Any) -> Any:
        if not isinstance(value, (str, os.PathLike)) or not os.fspath(value):
            return value
        path = Path(value)
        if not path.is_absolute():
            choices = [design.root_path / path, Path.cwd() / path]
            path = next((p for p in choices if p.exists()), choices[0])
        local = path.resolve()
        relative = design.relative_to_root(path)
        if relative is None:
            digest = hashlib.sha256(str(path).encode()).hexdigest()[:16]
            relative = Path("_setting_inputs") / digest / local.name
        roots = location_roots(design.root_path, Path.cwd())
        identified(relative, "setting", str(location_free(value, roots)))
        if local.is_file():
            places.setdefault(relative, local)
        elif local.is_dir():
            # The files travel, and the directories are listed: the remote makes the empty ones
            # (`InputPackage.directories`). A special file or a broken link is no input.
            directories.add(relative.as_posix())
            for entry in directory_files(local, [run_root], VCS_METADATA, follow_links=True):
                place = relative / entry.relative_to(local)
                if entry.is_dir():
                    directories.add(place.as_posix())
                elif entry.is_file():
                    places.setdefault(place, entry)
        return "$DESIGN_ROOT/" + relative.as_posix()

    # whether the section that is packaged now belongs to a flow of the plan
    in_plan = [True]

    def packaged_liberty(value: Any) -> Any:
        """Return the Liberty file `value` as the remote receives it.

        A file without `include_file` travels as any path setting does. For a file that
        includes others, the included files travel too, each in a place of its own, and the
        copies of all the files name the included files by their places on the remote, because
        OpenSTA opens an include by its absolute name. The shipped value is the copy of the
        file named by the setting, and the identity of the run is that of the original."""
        from ..platforms.derive import (
            liberty_included_files,
            liberty_text_with_includes_renamed,
        )

        if not in_plan[0]:
            return packaged_path(value)
        path = Path(value)
        if not path.is_absolute():
            choices = [design.root_path / path, Path.cwd() / path]
            path = next((p for p in choices if p.exists()), choices[0])
        try:
            included = liberty_included_files(path) if path.is_file() else []
        except ValueError as error:
            raise FlowSettingsException(f"`liberty` file {path}: {error}") from None
        if not included:
            return packaged_path(value)

        def place(local: Path) -> Path:
            digest = hashlib.sha256(str(local.resolve()).encode()).hexdigest()[:16]
            return Path("_liberty_inputs") / digest / local.name

        def remote_name(local: Path) -> str:
            return str(remote_inputs / place(local).as_posix())

        for local in (path, *included):
            try:
                if local.suffix.lower() == ".gz" and not liberty_included_files(local):
                    places[place(local)] = local  # nothing to rename
                    continue
                text = liberty_text_with_includes_renamed(local, lambda inc: remote_name(inc))
            except ValueError as error:
                raise FlowSettingsException(
                    f"`liberty` file {path} cannot be shipped: {error}"
                ) from None
            content = text.encode("utf-8")
            if local.suffix.lower() == ".gz":
                content = gzip.compress(content, mtime=0)  # the same bytes on every run
            places[place(local)] = content
        roots = location_roots(design.root_path, Path.cwd())
        identified(place(path), "setting", str(location_free(value, roots)))
        return "$DESIGN_ROOT/" + place(path).as_posix()

    def packaged_value(value: Any, host: Any = None) -> Any:
        """Return `value` as the remote receives it: its paths packaged and repointed.

        `host` is what the host profile gives for the same place in the settings, if anything.

        A locator (`pdk_root`) is a place on this machine: the remote host uses its own. Its
        key stays, with the value the host profile gives, else None."""
        if isinstance(value, BaseModel):
            values: Dict[str, Any] = {}
            for name, field in type(value).model_fields.items():
                if name not in value.model_fields_set:
                    continue
                item = getattr(value, name)
                given = host.get(name) if isinstance(host, Mapping) else None
                if is_locator(type(value), name):
                    values[name] = given
                    continue
                if name == "liberty" and isinstance(value, AsicPlatformSettings):
                    values[name] = [packaged_liberty(leaf) for leaf in item]
                    continue
                if written_role(type(value), name) is None:
                    item = map_keyed_path_leaves(
                        item, field.annotation, lambda _key, leaf: packaged_path(leaf), name
                    )
                if name in opaque_key_fields_of(type(value)) and isinstance(item, Mapping):
                    # The names of the tool keep their dots whole: the table may hold `A` and
                    # `A.B`, which the document cannot write as dotted keys of one origin.
                    item = {AtomicName.of(str(key)): entry for key, entry in item.items()}
                values[name] = packaged_value(item, given)
            return values
        if isinstance(value, Mapping):
            return {
                key: packaged_value(item, host.get(key) if isinstance(host, Mapping) else None)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [packaged_value(item) for item in value]
        return value

    shipped_sections: Dict[str, Any] = {}
    for name, section in sections.items():
        if isinstance(section, Mapping) and INPUTS_KEY in section:
            raise ValueError(
                f"The section of {name!r} holds {INPUTS_KEY!r}: bindings travel in `bindings`."
            )
        cls = _get_flow_class_if_known(name)
        if cls is None:
            shipped_sections[name] = section
            continue
        try:
            model = cls.Settings.from_input(
                section, design_root=design.root_path, runner_cwd=Path.cwd()
            )
        except FlowSettingsError:
            shipped_sections[name] = section
        else:
            in_plan[0] = name in planned_flows
            # Preserve explicitness: a section contributes only the keys it states. The keys
            # keep the order of the model, so the document is the same bytes on every run.
            rewritten = packaged_value(model, host_flows.get(name))
            shipped_sections[name] = {
                key: rewritten[key] for key in type(model).model_fields if key in rewritten
            }
    for name, inputs in written_bindings(bindings, request).items():
        shipped_sections[name] = {**shipped_sections.get(name, {}), INPUTS_KEY: inputs}
    new_design["flows"] = shipped_sections
    # The encoder that `settings.json` uses: a pydantic model serializes through pydantic,
    # so the serializers of its fields run.
    document = json.dumps(new_design, default=json_encodable).encode("utf-8")

    files: dict[str, Path] = {}
    generated: dict[str, bytes] = {design_file: document}
    for place, content in places.items():
        if place == Path(design_file):
            continue
        if isinstance(content, bytes):
            generated[place.as_posix()] = content
        else:
            files[place.as_posix()] = content
    packaged = {PurePosixPath(place) for place in (*files, *generated)}
    holding = {parent for place in packaged for parent in place.parents}
    candidates = {
        PurePosixPath(place)
        for place in directories
        if PurePosixPath(place) not in holding and PurePosixPath(place) not in packaged
    }
    # the innermost ones: making a directory makes the ones it lies in
    inner = {parent for place in candidates for parent in place.parents}
    empty = sorted(place.as_posix() for place in candidates - inner)
    return InputPackage(design_file, files, generated, path_identities, empty)
