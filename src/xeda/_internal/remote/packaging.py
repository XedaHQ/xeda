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

from xeda.errors import DesignError

from ..dataclass import AtomicName, BaseModel, is_locator, opaque_key_fields_of, written_role
from ..design import (
    Design,
    DesignPart,
    DependencyRecord,
    DesignValidationError,
    FileResource,
    Generator,
    GitReference,
    MergedDependency,
    Source,
    generator_cwd,
    generator_of,
    redacted_url,
    tree_declarations,
)
from ..flow import FlowSettingsError, FlowSettingsException
from ..flow.asic import AsicPlatformSettings
from ..flow.flow import map_keyed_path_leaves, setting_path_target
from ..flow_runner.bindings import INPUTS_KEY, BindingLayer, written_bindings
from ..flow_runner.chains import FlowRequest
from ..flow_runner.default_runner import _get_flow_class_if_known
from ..flow_runner.dependencies import DependencyChain, _extend_chain, declared_designs
from ..listing import VCS_METADATA, directory_files
from ..frozen import thaw
from ..utils import json_encodable, location_free, location_roots


class DependencyChangedError(ValueError):
    """A design document that lists a dependency changed after planning resolved the dependencies:
    `difference` says how its entry differs from the one that planning resolved."""

    def __init__(self, difference: str) -> None:
        super().__init__(difference)
        self.difference = difference


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
    #: The commit that this side resolved each Git entry to that gave no `commit` of its own, by
    #: the path of the entry in the tree of dependencies (`DependencyRecord.path`): the host
    #: fetches those commits (`using_pinned_commits`), also for the Git dependencies of a Git
    #: dependency, which the design document does not list.
    dependency_commits: dict[str, str] = field(default_factory=dict)


def _unsendable(unit: Design, generator: Generator) -> str | None:
    """Return why the package cannot carry the generator of `unit`, or None."""
    name = generator.describe(unit.name, unit.root)
    if not generator.sources:
        return (
            f"{name} declares no `sources`, so `--remote` cannot know which files to send with "
            "it: declare its `sources` (the files and directories it reads) in `rtl.generator`"
        )
    outside = [path for path in generator.sources if unit._relative_to_root(path) is None]
    if outside:
        return (
            f"{name} reads {outside[0]}, which lies outside the design root {unit.root}, "
            "and `--remote` sends only what lies below the design root: place it below the "
            "design root (a copy or a link inside it) and name it in `sources`"
        )
    if generator.cwd is not None:
        named = generator_cwd(generator.cwd, unit.root)
        directory = generator.lookup_directory(unit.root).resolve()
        if directory != unit.root.resolve() / named:
            # the package holds no links, and files travel under the names they are declared by
            return (
                f"{name} has `cwd` {generator.cwd!r}, which a link leads to {directory}, and "
                "`--remote` sends no links: give the directory itself as `cwd`, below the "
                "design root"
            )
    return None


def tree_parent(root: Path, name: str) -> Path | None:
    """Return the directory below `root` that holds the files a tree entry `name` can name, as far
    as the entry says it, or None.

    It is the literal part of the entry: the parts before the first one with `*`, without the
    file name. The directory is returned only when it exists and no link lies on the way to it
    below `root`, so the host has the directory when this side has it."""
    parts = []
    for part in Path(name).parts[:-1]:
        if "*" in part:
            break
        parts.append(part)
    place = root
    for part in parts:
        place = place / part
        if place.is_symlink():
            return None
    parent = Path(*parts)
    return parent if parts and place.is_dir() else None


def refuse_unsendable_generator(design: Design) -> Generator | None:
    """Return the generator of `design`, or None if it has none. Refuse a generator that the
    package cannot carry, also one of a dependency: one that declares no `sources`, or one with a
    declared input outside the root of its design.

    The host runs the generators, and only the files and directories that a recipe declares
    travel with it, each at its place below the root of its design. A generator that declares none
    would run on the host without what it reads, and its output would be wrong or fail far from
    the cause. The refusal names the generator and says what to do (`DesignValidationError`). It
    needs no connection and runs nothing, so a request makes it at planning. A local launch has no
    such limit."""
    for unit in (*design.generation_units, design):
        generator = generator_of(unit.rtl, root=unit.root)
        problem = None if generator is None else _unsendable(unit, generator)
        if problem is not None:
            raise DesignValidationError(
                [("rtl -> generator", problem, "", "value_error")],
                data=unit.model_dump(mode="json"),
                root=unit.root,
                design_name=unit.name,
            )
    return generator_of(design.rtl, root=design.root)


def refuse_unsendable_declared(design: Design) -> None:
    """Refuse a generator that the package cannot carry (`refuse_unsendable_generator`), in the
    design and in its local dependencies, with no Git dependency read.

    A request makes this check at planning before any Git dependency is resolved or fetched: it
    needs no value that such a dependency supplies. The generator of a Git dependency is judged
    after the fetch, on the merged design. A local dependency whose file is missing is left to the
    merge, which reports it."""
    for unit in declared_designs(design, missing_ok=True):
        refuse_unsendable_generator(unit)


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
    resolved: Sequence[DependencyRecord] | None = None,
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
    writes travel as the declarations the design makes of them.

    `design` is the design as the caller gave it, with its dependencies not merged. The host
    merges them once, as this side does, so the package lists them in the order they are listed.
    A Git dependency travels as its reference, and the host fetches it. A local dependency is a
    file on this side only: it travels as a design document of its own, with the files and the
    generator recipe it declares. Its root on the host is the place of its root below the root of
    the request, or `_dependencies/<n>` when its root lies outside that root (`place_dependency_roots`).
    The host reads that document where it would read the local file. A file that several designs
    list has one place, so the host lists it once, as this side does.

    `resolved` are the dependencies as this side resolved them (`Design.resolved_dependencies` of
    the planned design). The package holds, beside the design document, the commit that this side
    resolved each Git entry to that gave no `commit` of its own (`InputPackage.dependency_commits`),
    also for the entries of a Git dependency, which the document does not list. The host fetches
    those commits, so a branch that moves between the two plans changes nothing. The branch of an
    entry stays as written (the provenance).

    The package holds each local dependency document as one read of its file. When `resolved` is
    given, every Git entry of those documents, and of the design, must be the entry that planning
    resolved at its place: a design file that was edited after planning raises
    `DependencyChangedError`, also when the edit names another explicit commit."""
    host_flows = host_flows or {}
    path_identities: dict[str, dict[str, str]] = {}
    # `mode="json"`: a field kept as it is may hold a `Path`, which a python-mode dump leaves as an
    # object for the encoder to stringify by luck rather than by rule. The Git dependencies that
    # the design still has travel as references; the host fetches them.
    # The design a target yielded, with the name of the target. The remote plans the same run
    # directories as this side (`<design>/<target>/<flow>`), and a design file may not write
    # `target`, but a packaged document may (`Design._load_file`).
    new_design: Dict[str, Any] = design._document("json", plain_names=False)
    if design.target is not None:
        new_design["target"] = design.target
    design_file = f"{design.name}.xeda.json"
    # Every place below the inputs directory, and what goes there: a local file, or content made
    # here. One table for both kinds of place (flat among the sources, or where a file lies under
    # the design root), so neither can be handed a place the other already holds. The design
    # document takes its place first.
    places: Dict[Path, Path | bytes] = {Path(design_file): b""}
    flat_places: Dict[tuple[Path, Path], Path] = {}
    # The place of every local file that a design lists as a source: one file has one place,
    # whichever design lists it (`place_of_source`). The sources of the host are then one list, as
    # they are here.
    source_places: Dict[Path, Path] = {}
    # The root of each local dependency that the walk of `merged_sources` meets, by its index in
    # the merge that the walk makes: it is the owner that `Source._dependency` names.
    merged_owners: Dict[int, Path] = {}
    documents: Dict[Path, Dict[str, Any]] = {}
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
            digest = resource._content_hash[:8]
            place = remote_sources_path / f"{local.stem}_{digest}{local.suffix}"
            suffix = 2
            while place in places:
                place = remote_sources_path / f"{local.stem}_{digest}_{suffix}{local.suffix}"
                suffix += 1
        places[place] = local
        flat_places[(base, local)] = place
        return place

    def archived_in_place(local: Path, relative: Path) -> bool:
        """Package `local` at `relative`, where it lies under the design root.

        Then the remote finds it where this side does. Return False when that place holds
        another file."""
        return places.setdefault(relative, local.resolve()) == local.resolve()

    def inside(place: Path, base: Path) -> str:
        """Return `place` (below the inputs of the host) as the document at `base` names it:
        relative to the root of that document."""
        return Path(os.path.relpath(place, base)).as_posix()

    def packaged_sources(sources: Iterable[Source], base: Path) -> list[Dict[str, Any]]:
        """Return `sources`, the sources of a design, as the remote has to see them in the document
        at `base`: each one names the place of its file (`place_of_source`), relative to `base`."""
        packaged: list[Dict[str, Any]] = []
        for src in sources:
            if src.generated and not src._located:
                # The generator of the host writes it: no file travels, the declaration does.
                packaged.append({"path": src.output_name, "type": str(src.type)})
                continue
            packaged.append({"file": inside(place_of_source(src), base), "type": str(src.type)})
        return packaged

    def packaged_parameters(dv: DesignPart, unit: Design, base: Path) -> Dict[str, Any]:
        """Return the parameters of `dv`, a part of `unit`, as the remote has to see them.

        A parameter is text to the tool, but text that names a path on this machine names
        nothing on the remote. So a path is re-rooted from its value alone:

        - Under the root of the unit, it keeps its place relative to the root. An existing file
          is packaged at that same place. A path with no file yet (an output) is only a
          place for the remote to write. Counted relative to the root on both sides, the
          remote hashes the design exactly as this side did.
        - Elsewhere, an existing file is packaged among the sources and repointed there.

        Either way it travels as a table (`{file: ...}`, `{path: ...}`), which the remote
        resolves against its own root of the unit and not against whatever directory it runs in.
        Anything else is a value, and travels as it is (an absolute directory, for example).

        For every absolute path, the host gets the text that this side counts for it, keyed by
        the path as the host sees it: the new place, or the value itself when it travels as it
        is.
        """
        parameters: Dict[str, Any] = {}
        roots = [("DESIGN_ROOT", design.root)]
        for name, value in dv.parameters.items():
            parameters[name] = thaw(value)
            if not (isinstance(value, str) and os.path.isabs(value)):
                continue
            path = Path(value)
            relative = unit._relative_to_root(path)
            seen: str | Path = value
            # a parameter is any text: `os.path` answers no for a name the system refuses (too
            # long, say), where `Path.exists` and `Path.is_file` raise before Python 3.14
            if relative is not None and not os.path.exists(path):
                parameters[name] = {"path": relative.as_posix()}
                seen = base / relative
            elif os.path.isfile(path) and path.resolve() in source_places:
                # a file that a design lists as a source has the place of that source
                seen = source_places[path.resolve()]
                parameters[name] = {"file": inside(seen, base)}
            elif (
                relative is not None
                and os.path.isfile(path)
                and archived_in_place(path, base / relative)
            ):
                parameters[name] = {"file": relative.as_posix()}
                seen = base / relative
            elif os.path.isfile(path):
                seen = archived(FileResource(path), unit, base)
                parameters[name] = {"file": inside(seen, base)}
            identified(seen, "parameter", str(location_free(value, roots)))
        return parameters

    def packaged_generator_input(local: Path, unit: Design, base: Path) -> str:
        """Package a file or a directory that the generator of `unit` reads, below `base` in the
        inputs of the host, and return its name for the recipe: relative to the root of the unit
        where it lies under it, else in a place of its own.

        A directory counts as every file in it, and its empty directories travel too."""
        relative = unit._relative_to_root(local)
        assert relative is not None, "an input outside the design root is refused at planning"
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

    dependency_bases: Dict[Path, Path] = {}
    dependency_documents: Dict[str, str] = {}
    # The design of each local dependency file as it was read for the package: the documents and
    # the check against `resolved` use this one read.
    dependency_designs: Dict[str, Design] = {}

    def dependency_design(unit: Design, ref: Any) -> Design:
        file = unit.root / ref.uri
        key = os.path.realpath(file)
        if key not in dependency_designs:
            dependency_designs[key] = Design.load(file)
        return dependency_designs[key]

    def place_dependency_roots() -> None:
        """Give the root of every local dependency design its place on the host.

        A root that lies in the root of the requested design (that root itself, or a directory
        below it) is at the same place on the host. A root outside it that lies below another
        dependency root is at the same place below that root. In both cases a file has the same
        place whether the design or the dependency lists it, and the places do not depend on the
        order of the dependencies. Any other root, one that holds the root of the request too,
        has a directory of its own, `_dependencies/<n>`, numbered in the order of the walk. A file
        of the request root that such a root lists has one place all the same
        (`place_of_source`)."""
        roots: list[Path] = []
        walked: set[str] = set()

        def walk(unit: Design) -> None:
            for ref in unit._dependency_references:
                if isinstance(ref, GitReference):
                    continue
                key = os.path.realpath(unit.root / ref.uri)
                if key in walked:
                    continue
                walked.add(key)
                dependency = dependency_design(unit, ref)
                if dependency.root not in roots:
                    roots.append(dependency.root)
                walk(dependency)

        walk(design)
        outside = [root for root in roots if not root.is_relative_to(design.root)]
        tops = [r for r in outside if not any(o != r and r.is_relative_to(o) for o in outside)]
        for root in roots:
            if root in tops:
                dependency_bases[root] = Path("_dependencies") / str(tops.index(root))
            elif root in outside:
                top = next(other for other in tops if root.is_relative_to(other))
                dependency_bases[root] = Path("_dependencies") / str(tops.index(top))
                dependency_bases[root] /= root.relative_to(top)
            else:
                dependency_bases[root] = root.relative_to(design.root)

    def merged_sources() -> Design:
        """Return the design merged with its local dependencies, as planning merges them.

        It tells which entry of a file that several designs list is the one the merge keeps
        (the first in the merged order), whether it is marked as the file of a dependency
        (`Source._dependency`), and its place in the root of that dependency. The host merges
        the same designs in the same order, so its merge keeps the same entry. A Git dependency
        has no file on this side that another design could list, so it is left out."""

        def merge(unit: Design, active: frozenset[str]) -> Design:
            merged: list[MergedDependency] = []
            for ref in unit._dependency_references:
                key = (
                    None if isinstance(ref, GitReference) else os.path.realpath(unit.root / ref.uri)
                )
                if key is None or key in active:  # a cycle is reported by `check_dependencies`
                    continue
                dependency = dependency_design(unit, ref)
                index = len(merged_owners)
                merged_owners[index] = dependency.root
                merged.append(
                    MergedDependency(ref, merge(dependency, active | {key}), index, dependency.root)
                )
            return unit._merged_with(merged) if merged else unit

        return merge(design, frozenset({os.path.realpath(design.root)}))

    def place_of_source(src: Source) -> Path:
        """Return the place on the host of the file of `src`, below the inputs directory.

        This is the one rule for the place of a source file: one local file has one place,
        whichever design lists it, so that the host merges one source where this side merges one.
        Each file is placed once, by the entry that the merge keeps (`merged_sources`):

        - A file in the root of the dependency that lists it is below that root on the host, so
          the host marks it as the file of that dependency, with the place this side counts.
        - Any other file keeps its place below the root of the request when it lies there. Else
          it keeps its place below a dependency root that holds it, else it goes among the flat
          sources. The host counts such a file by the text that this side counts for it
          (`identified`)."""
        local = src.file.resolve()
        place = source_places.get(local)
        if place is not None:
            return place
        candidates: list[tuple[Path, bool]] = []
        if src._dependency is not None and src._dependency_place is not None:
            owner = dependency_bases[merged_owners[src._dependency]]
            candidates.append((owner / src._dependency_place, False))
        else:
            relative = design._relative_to_root(src.file)
            if relative is not None:
                candidates.append((relative, False))
            for root, root_base in dependency_bases.items():
                if src.file.is_relative_to(root):
                    candidates.append((root_base / src.file.relative_to(root), True))
        for candidate, named in candidates:
            if places.setdefault(candidate, local) == local:
                place = candidate
                break
        else:
            named = True
            place = archived(src, design, Path("."))
        source_places[local] = place
        if named:
            identified(place, "source", design._source_place(src))
        return place

    def check_dependencies(
        unit: Design, where: tuple[int, ...], chain: DependencyChain, unmet: set[str]
    ) -> None:
        """Check the Git entries of `unit` and of its local dependencies against `resolved`, by
        their path in the tree of dependencies. The entries below a Git dependency come from its
        checkout at a commit, so they cannot change."""
        assert resolved is not None
        by_path = {record.path: record for record in resolved if record.repository is not None}
        for position, ref in enumerate(unit._dependency_references):
            path = (*where, position)
            if not isinstance(ref, GitReference):
                dependency = dependency_design(unit, ref)
                try:
                    deeper = _extend_chain(dependency, chain, "dependencies")
                except DesignError as error:
                    raise DependencyChangedError(
                        f"a dependency design file now has a cycle: {error}"
                    )
                check_dependencies(dependency, path, deeper, unmet)
                continue
            name = "-".join(str(number) for number in path)
            unmet.discard(name)
            record = by_path.get(name)
            if record is None:
                raise DependencyChangedError(f"dependency {redacted_url(ref.repo_url)} was added")
            if (record.repository, record.reference, record.design_file, record.given) != (
                redacted_url(ref.repo_url),
                ref.branch,
                ref.design_file,
                bool(ref.commit),
            ) or (ref.commit and record.commit != ref.commit):
                raise DependencyChangedError(
                    f"dependency {record.repository} was resolved at {record.commit} and now "
                    f"names {ref.commit or ref.branch or 'its HEAD'}"
                )

    def packaged_dependencies(unit: Design, base: Path) -> list[Any]:
        """Return the `dependencies` of `unit` as the remote has to see them, in the order they
        are listed, so that the host merges them at the same point, in the same order, with the
        same indexes as this side.

        A Git dependency travels as its reference: the host fetches it into its own run root. A
        local dependency is a design file on this side only, so it travels as a design document of
        its own, with the files it declares, and its entry names that document. Its root is given
        by `place_dependency_roots`: dependency designs in one directory share one root there, so a file
        that lies in the roots of several of them is one file on the host, listed once, as it is
        on this side."""
        entries: list[Any] = []
        dumped = unit.model_dump(mode="json")["dependencies"]
        for ref, entry in zip(unit._dependency_references, dumped):
            if isinstance(ref, GitReference):
                entries.append(entry)
                continue
            dependency = dependency_design(unit, ref)
            file = dependency._generation_label
            if file not in dependency_documents:
                root = dependency_bases[dependency.root]
                document_file = root / f"{Path(file).stem}.xeda.json"
                while document_file in places:
                    document_file = document_file.with_name(document_file.stem + "_2.json")
                places[document_file] = b""
                dependency_documents[file] = document_file.as_posix()
                documents[document_file] = packaged_document(dependency, root)
            entries.append({**entry, "uri": inside(Path(dependency_documents[file]), base)})
        return entries

    def packaged_document(unit: Design, base: Path) -> Dict[str, Any]:
        """Return what the merge of `unit` as a dependency reads: its sources, top, parameters,
        clocks, testbench and generator, and its own dependencies. Its files are packaged below
        `base`, which is its root on the host."""
        generator = refuse_unsendable_generator(unit)
        rtl: Dict[str, Any] = {"sources": packaged_sources(unit.rtl.sources, base)}
        rtl["defines"] = thaw(unit.rtl.defines)
        rtl["attributes"] = thaw(unit.rtl.attributes)
        rtl["parameters"] = packaged_parameters(unit.rtl, unit, base)
        rtl["top"] = unit.rtl.top
        rtl["clocks"] = [dict(clk) for clk in unit.rtl.clocks]
        if generator is not None:
            recipe = thaw(unit.rtl.generator)
            recipe["sources"] = [
                packaged_generator_input(path, unit, base) for path in generator.sources
            ]
            rtl["generator"] = recipe
            if generator.cwd is not None:
                # The generator starts there, so the directory exists on the host even when no
                # file of the package lies in it. The design refuses a `cwd` that leaves its
                # root when it loads, and `_unsendable` one that goes through a link.
                named = generator_cwd(generator.cwd, unit.root)
                recipe["cwd"] = named.as_posix()
                if named != Path(".") and (unit.root / named).is_dir():
                    directories.add((base / named).as_posix())
        for name in tree_declarations(unit):
            if (parent := tree_parent(unit.root, name)) is not None:
                directories.add((base / parent).as_posix())
        tb: Dict[str, Any] = {"sources": packaged_sources(unit.tb.sources, base)}
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
        return {
            "name": unit.name,
            "rtl": rtl,
            "tb": tb,
            "language": thaw(unit.language),
            "dependencies": packaged_dependencies(unit, base),
        }

    place_dependency_roots()
    for src in (*(merged := merged_sources()).rtl.sources, *merged.tb.sources):
        if not (src.generated and not src._located):
            place_of_source(src)
    new_design.update(packaged_document(design, Path(".")))
    for document_file, dependency_document in documents.items():
        places[document_file] = json.dumps(dependency_document, default=json_encodable).encode(
            "utf-8"
        )
    if resolved is not None:
        # The Git entries that planning resolved and that the walk must meet: those with no Git
        # dependency above them (the entries of a Git dependency come from its checkout).
        paths = {record.path for record in resolved if record.repository is not None}
        unmet = {name for name in paths if not any(name.startswith(other + "-") for other in paths)}
        check_dependencies(design, (), _extend_chain(design, (), "dependencies"), unmet)
        for name in sorted(unmet):
            record = next(r for r in resolved if r.path == name and r.repository is not None)
            raise DependencyChangedError(f"dependency {record.repository} was removed")
    commits = {
        d.path: d.commit
        for d in resolved or ()
        if d.repository is not None and d.commit and not d.given
    }

    def packaged_path(value: Any) -> Any:
        if not isinstance(value, (str, os.PathLike)) or not os.fspath(value):
            return value
        target = setting_path_target(value, design.root)
        if target is None:  # a `$` variable that nothing expanded, or a yosys `+/` path
            return value
        path = target
        local = path.resolve()
        relative = design._relative_to_root(path)
        if relative is None:
            digest = hashlib.sha256(str(path).encode()).hexdigest()[:16]
            relative = Path("_setting_inputs") / digest / local.name
        roots = location_roots(design.root, Path.cwd())
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
        path = setting_path_target(value, design.root)
        if path is None:
            return packaged_path(value)
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
        roots = location_roots(design.root, Path.cwd())
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
            model = cls.Settings.from_input(section, design_root=design.root, runner_cwd=Path.cwd())
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
    return InputPackage(design_file, files, generated, path_identities, empty, commits)
