"""The dependencies of a design, read at planning.

A design lists the designs that it depends on (`Design.dependencies`). Loading a design reads
none of them. Planning reads them: it fetches each Git dependency into the run root and merges the
sources and the metadata of every dependency into the design (`merged_design`). So a dependency
supplies `rtl.sources`, `rtl.top`, `rtl.parameters`, `rtl.clocks`, `tb.sources` and `tb.top` to
the plan and the launch, and the `Design` that the caller gave keeps its references as written.

A Git dependency is kept in the run root, in storage that its commit names:
`<run root>/.dependencies/<key>/`, where `<key>` is the first 16 hexadecimal digits of the SHA-256
of the repository (without user name and password) and the full commit hash. The storage holds
the files of the commit in `checkout/` and the record `dependency.yaml` beside it, so a repository
can hold a file of that name. It is complete or absent: a fetch
writes into a temporary directory and renames it into place. The key names the content, so Xeda
never writes a complete storage again. Xeda removes no storage. It is a cache, and the directory
can be deleted by hand while no launch runs.

A reference is resolved to a full commit hash at every planning. A `commit` is that hash, and
needs no network. A branch, a tag or the `HEAD` of the repository is resolved by `git ls-remote`,
so a branch that moved is seen. A commit that is in the storage already needs no network either.

The functions never import `default_runner`. No message or log line shows the user name or the
password of a URL: they pass through `redacted_url`.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import logging
import os
import re
import secrets
from collections.abc import Callable, Iterator, Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import yaml

from ..design import (
    Design,
    DependencyRecord,
    DesignReference,
    GitReference,
    MergedDependency,
    generator_of,
    redacted_url,
)
from ..errors import DesignError
from ..proc_utils import run_process
from ..run_dir import RunDirectory
from ..cancel import launch_canceled
from ..utils import Canceled, NonZeroExitCode, replacing_file
from .run_lock import storage_lock

log = logging.getLogger(__name__)

__all__ = [
    "DEPENDENCIES_DIRECTORY",
    "CHECKOUT",
    "RECORD_NAME",
    "dependency_key",
    "dependency_storage",
    "fetch",
    "merged_design",
    "resolve_reference",
    "using_pinned_commits",
]

#: The directory of dependency storage in the run root.
DEPENDENCIES_DIRECTORY = ".dependencies"
#: The record in each storage, beside the checkout: `{repository, commit, fetched}`, the repository
#: redacted.
RECORD_NAME = "dependency.yaml"
#: The directory of the files of the commit in each storage.
CHECKOUT = "checkout"
#: The number of hexadecimal digits of the key of a storage.
KEY_LENGTH = 16

#: The user name and the password of a URL's authority, with the `@` after them.
_USER_INFO = re.compile(r"(?<=//)[^/?#]*@")
#: Git variables that would send a command of Xeda to another repository than the one it names.
_GIT_REPOSITORY_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
)
#: The name of the remote that every command of a fetch uses. The URL reaches Git through the
#: environment (`_git`), so it is on no command line, and so in no log.
_REMOTE = "dependency"
#: The settings of every Git command: no hook runs, and no maintenance starts in the background,
#: where it could outlive the fetch and write into a directory that the fetch renames.
_GIT_SETTINGS = (f"core.hooksPath={os.devnull}", "maintenance.auto=false", "gc.auto=0")


def dependency_key(repository: str, commit: str) -> str:
    """Return the key of the storage of `commit` of `repository`.

    The URL counts without its user name and password, so two users of one repository share a
    storage. The key is the same wherever the run root is."""
    plain = _USER_INFO.sub("", repository)
    digest = hashlib.sha256(json.dumps([plain, commit]).encode()).hexdigest()
    return digest[:KEY_LENGTH]


def _owner(run_root: Path) -> RunDirectory:
    root = Path(os.path.realpath(run_root))
    return RunDirectory(root, root)


def dependency_storage(run_root: Path, key: str) -> Path:
    """Return the storage `key` of a run root, `<run root>/.dependencies/<key>`.

    The path is named as every cache of Xeda under a run root is: through no symbolic link."""
    return _owner(run_root).unlinked(run_root / DEPENDENCIES_DIRECTORY / key)


def _complete(storage: Path) -> bool:
    return (storage / RECORD_NAME).is_file()


def _git(arguments: list[str], *, repository: str, cwd: Path) -> str:
    """Run `git` in `cwd` with `arguments` and return its output.

    The URL of the repository is given as the setting `remote.dependency.url` in the environment
    of the command, so it is on no command line and in no log. A failure raises `NonZeroExitCode`
    whose message holds the output of Git: the caller shows it through `redacted_url`."""
    if launch_canceled():
        raise Canceled("the launch was canceled before it ran `git`")
    env = {k: v for k, v in os.environ.items() if k not in _GIT_REPOSITORY_VARIABLES}
    env["GIT_TERMINAL_PROMPT"] = "0"
    count = int(env["GIT_CONFIG_COUNT"]) if env.get("GIT_CONFIG_COUNT", "").isdigit() else 0
    env["GIT_CONFIG_COUNT"] = str(count + 1)
    env[f"GIT_CONFIG_KEY_{count}"] = f"remote.{_REMOTE}.url"
    env[f"GIT_CONFIG_VALUE_{count}"] = repository
    settings = [word for setting in _GIT_SETTINGS for word in ("-c", setting)]
    output = run_process(
        "git",
        [*settings, *arguments],
        env=env,
        cwd=cwd,
        stdout=True,
        merge_stderr=True,
    )
    assert output is not None
    return output


def _reference_text(ref: GitReference) -> str:
    return f"branch or tag `{ref.branch}`" if ref.branch else "HEAD"


def _failure(ref: GitReference, field: str, problem: str, *, pin: bool = False) -> DesignError:
    """Return the error for a Git dependency that could not be resolved or fetched.

    The message names the repository, the reference and `problem`, all without credentials."""
    text = (
        f"cannot fetch the Git dependency {redacted_url(ref.repo_url)} "
        f"({ref.commit and 'commit ' + ref.commit or _reference_text(ref)}): "
        f"{redacted_url(problem)}"
    )
    if pin:
        text += (
            ". A branch is resolved by the network at every planning: pin the full `commit` to "
            "plan offline"
        )
    return DesignError(text, code="fetch_failed", field=field)


def _output_problem(error: NonZeroExitCode) -> str:
    """Return what Git said when it failed, as one line without credentials."""
    said = " ".join(str(word).strip() for word in error.args if str(word).strip())
    return redacted_url(said or f"git ended with status {error.exit_code}")


def resolve_reference(ref: GitReference, *, run_root: Path, field: str = "dependencies") -> str:
    """Return the full commit hash that `ref` names.

    A `commit` is returned as written, with no process and no network. Any other reference asks
    the repository (`git ls-remote`), every time: a tag gives the commit that it points to, a
    branch its tip, and no reference the `HEAD` of the repository. A failure raises
    `DesignError` with the code `fetch_failed` and `field`. `run_root` is a directory that
    exists, in which Git runs."""
    if ref.commit:
        return ref.commit
    name = ref.branch
    # A tag that carries a message is listed twice: the tag, and (with `^{}`) the commit it names.
    wanted = (
        ["HEAD"]
        if name is None
        else [f"refs/heads/{name}", f"refs/tags/{name}^{{}}", f"refs/tags/{name}"]
    )
    try:
        output = _git(["ls-remote", _REMOTE, *wanted], repository=ref.repo_url, cwd=Path(run_root))
    except NonZeroExitCode as error:
        raise _failure(ref, field, _output_problem(error), pin=True) from None
    except OSError as error:
        raise _failure(ref, field, f"cannot run `git`: {error}") from None
    found: dict[str, str] = {}
    for line in output.splitlines():
        commit, _, refname = line.partition("\t")
        if commit and refname:
            found[refname.strip()] = commit.strip()
    for refname in wanted:
        if refname in found:
            return found[refname].lower()
    raise _failure(ref, field, "the repository has no such branch or tag")


def fetch(ref: GitReference, commit: str, *, run_root: Path, field: str = "dependencies") -> Path:
    """Return the checkout of `commit` of the repository of `ref`, fetching it if it is not there.

    The storage of the commit holds the checkout, `<storage>/checkout/`, and the record of the
    fetch beside it. A complete storage is never written again. Otherwise the fetch holds the
    exclusive lock of the storage, so two launches fetch one commit once. It writes into a
    temporary directory under `.dependencies/`, checks out `commit` there, and renames the
    directory into place. A failure removes the temporary directory and raises `DesignError` with
    the code `fetch_failed` and `field`. The run root exists."""
    storage = dependency_storage(run_root, dependency_key(ref.repo_url, commit))
    if _complete(storage):
        return storage / CHECKOUT
    with storage_lock(storage, exclusive=True, run_root=run_root):
        if _complete(storage):
            return storage / CHECKOUT
        owner = _owner(run_root)
        partial = owner.unlinked(
            Path(run_root)
            / DEPENDENCIES_DIRECTORY
            / f".partial-{storage.name}-{secrets.token_hex(4)}"
        )
        owner.writable(partial / CHECKOUT).mkdir(parents=True)
        try:
            _clone(ref, commit, partial / CHECKOUT, owner, field)
            record = {
                "repository": redacted_url(ref.repo_url),
                "commit": commit,
                "fetched": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
            with replacing_file(owner.writable(partial / RECORD_NAME)) as stream:
                yaml.safe_dump(record, stream, sort_keys=True)
            log.info("fetched %s at %s", redacted_url(ref.repo_url), commit)
            os.rename(partial, storage)
        except BaseException:
            owner.remove(partial)
            raise
    return storage / CHECKOUT


def _clone(ref: GitReference, commit: str, into: Path, owner: RunDirectory, field: str) -> None:
    """Put the files of `commit` into the empty directory `into`.

    The commit is fetched alone, as shallow as the server allows, and falls back to every branch
    and tag. The directory of Git is removed afterwards: the files are the content, and the
    directory could hold the URL."""
    try:
        # A hash of 64 digits names a commit of a SHA-256 repository, and Git fetches it only into
        # one.
        object_format = ["--object-format=sha256"] if len(commit) == 64 else []
        _git(
            ["init", "--quiet", *object_format, "."],
            repository=ref.repo_url,
            cwd=into,
        )
        try:
            _git(
                ["fetch", "--quiet", "--depth", "1", _REMOTE, commit],
                repository=ref.repo_url,
                cwd=into,
            )
        except NonZeroExitCode:
            _git(
                [
                    "fetch",
                    "--quiet",
                    _REMOTE,
                    "+refs/heads/*:refs/remotes/dependency/*",
                    "+refs/tags/*:refs/tags/*",
                ],
                repository=ref.repo_url,
                cwd=into,
            )
        _git(["checkout", "--quiet", "--detach", commit], repository=ref.repo_url, cwd=into)
        head = _git(["rev-parse", "HEAD"], repository=ref.repo_url, cwd=into).strip().lower()
        if head != commit:
            raise DesignError(
                f"cannot fetch the Git dependency {redacted_url(ref.repo_url)}: the checkout is "
                f"at {head}, not at {commit}",
                code="fetch_failed",
                field=field,
            )
    except NonZeroExitCode as error:
        raise _failure(ref, field, _output_problem(error)) from None
    except OSError as error:
        raise _failure(ref, field, f"cannot run `git`: {error}") from None
    owner.remove(into / ".git")


def _within(root: Path, relative: str) -> Path | None:
    """Return the file `relative` below `root`, or None if the name leaves `root` or is a link."""
    path = root / relative
    resolved = Path(os.path.realpath(path))
    if not resolved.is_relative_to(Path(os.path.realpath(root))):
        return None
    return path


def _generator_text(design: Design) -> str:
    generator = generator_of(design.rtl, root=design.root)
    return "" if generator is None else generator.describe(design.name, design.root)


def _local_design(design: Design, ref: DesignReference, field: str) -> Design:
    """Load the design that the local reference `ref` of `design` names.

    The path is relative to the root of `design`. The file must exist now: a generator of the
    design runs later, in the launch, so it cannot write it."""
    path = design.root / ref.uri
    if not path.is_file():
        generator = _generator_text(design)
        hint = (
            f". {generator} runs in the launch, after planning, so it cannot write a "
            "dependency's design file: list the files that it writes as sources of the design"
            if generator
            else ""
        )
        raise DesignError(
            f"the design file of the dependency {redacted_url(str(ref.uri))!r} does not exist: "
            f"{path}{hint}",
            code="design_file_not_found",
            field=field,
            path=path,
        )
    return Design.load(path)


def _git_design(ref: GitReference, checkout: Path, field: str) -> Design:
    """Load the design file of a Git dependency from its checkout."""
    path = _within(checkout, ref.design_file)
    if path is None or not path.is_file():
        raise _failure(
            ref,
            field,
            (
                f"the repository has no design file `{ref.design_file}`"
                if path is None or not path.exists()
                else f"`{ref.design_file}` is not a file"
            ),
        )
    return Design.load(path)


#: The commits that planning fetches instead of resolving a reference, by the path of the entry
#: in the tree of dependencies (`DependencyRecord.path`).
_pinned: ContextVar[Mapping[str, str]] = ContextVar("pinned_commits", default={})


@contextlib.contextmanager
def using_pinned_commits(commits: Mapping[str, str]) -> Iterator[None]:
    """Fetch the Git dependencies that `commits` name at those commits while the block runs.

    `commits` maps the path of an entry in the tree of dependencies (`DependencyRecord.path`, as
    `0` or `1-0`) to a commit. A remote host plans with the commits that the client resolved, which
    the package carries beside the design. An entry that the table does not name, or that gives its
    own `commit`, is resolved as usual. The client and the host walk the same tree, so a path
    names one entry on both."""
    token = _pinned.set(dict(commits))
    try:
        yield
    finally:
        _pinned.reset(token)


#: Where Git dependencies are fetched: a run root, or a function that makes it when the first Git
#: dependency needs it, so that a design without one makes none.
RunRoot = Path | Callable[[], Path] | None


# Each entry is one design on the active path: its identity (`_identity`) and its name.
DependencyChain = tuple[tuple[tuple[str, tuple[Any, ...]], str], ...]


def _identity(design: Design) -> tuple[str, tuple[Any, ...]]:
    """Return what decides the dependencies below `design`: its design file and the entries of
    its `dependencies`.

    The file alone is not enough: a target can add dependencies, so the design of a file with its
    target and the same file loaded as a dependency, which takes no target, are two designs. When
    both parts repeat on the active path, so does everything below, which is a cycle."""
    return os.path.realpath(design.generation_label), tuple(design._dependency_references)


def _extend_chain(design: Design, chain: DependencyChain, field: str) -> DependencyChain:
    identity = _identity(design)
    for previous, label in chain:
        if previous == identity:
            names = [name for _, name in chain] + [label]
            raise DesignError(
                "dependency cycle: " + " -> ".join(names), code="dependency_cycle", field=field
            )
    return (*chain, (identity, design.name))


def declared_designs(
    design: Design,
    *,
    chain: DependencyChain = (),
    field: str = "dependencies",
    missing_ok: bool = False,
) -> Iterator[Design]:
    """Walk local dependencies before any Git fetch, refusing cycles on the active path.

    The root's design file participates too. A shared dependency on another branch is allowed.
    Remote checks leave missing files to the merge when `missing_ok` is true."""
    active = _extend_chain(design, chain, field)
    yield design
    for position, ref in enumerate(design._dependency_references):
        if isinstance(ref, GitReference):
            continue
        if missing_ok and not (design.root / ref.uri).is_file():
            continue
        field = f"dependencies[{position}]"
        yield from declared_designs(
            _local_design(design, ref, field), chain=active, field=field, missing_ok=missing_ok
        )


def _merge(
    design: Design,
    run_root: RunRoot,
    slots: list[DependencyRecord | None],
    first: int | None,
    pins: Mapping[str, str],
    where: tuple[int, ...] = (),
    chain: DependencyChain = (),
    field: str = "dependencies",
) -> Design:
    """Merge the dependencies of `design`, in the order they are listed.

    Each entry has a slot in `slots`, its index. The entries of the design that planning
    started from take the slots `0..n-1` at their positions (`first` is `None`). The entries of
    a dependency come after, in the order they are met (`first` is `len(slots)` then)."""
    active = _extend_chain(design, chain, field)
    merged: list[MergedDependency] = []
    for position, ref in enumerate(design._dependency_references):
        field = f"dependencies[{position}]"
        index = position if first is None else len(slots)
        if first is not None:
            slots.append(None)
        if isinstance(ref, GitReference):
            assert run_root is not None, "a Git dependency is fetched into a run root"
            root = run_root() if callable(run_root) else run_root
            path = "-".join(str(number) for number in (*where, position))
            commit = (
                ref.commit or pins.get(path) or resolve_reference(ref, run_root=root, field=field)
            )
            files = fetch(ref, commit, run_root=root, field=field)
            loaded = _git_design(ref, files, field)
            # Check newly available local branches before any nested Git fetch.
            for _ in declared_designs(loaded, chain=active, field=field):
                pass
            checkout = Path(os.path.realpath(files))
            slots[index] = DependencyRecord(
                redacted_url(ref.repo_url),
                ref.branch,
                commit,
                ref.design_file,
                checkout,
                path,
                given=bool(ref.commit),
            )
            marked: tuple[int | None, Path | None] = (index, checkout)
        else:
            loaded = _local_design(design, ref, field)
            slots[index] = DependencyRecord(None, None, None, str(ref.uri), None)
            # its written files count by their path in its own root
            marked = (index, loaded.root)
        loaded = _merge(
            loaded, run_root, slots, len(slots), pins, (*where, position), active, field
        )
        if isinstance(ref, GitReference) and (entries := loaded._tree_entries()):
            raise _failure(
                ref,
                field,
                f"its generator writes `{entries[0].text}` into its design tree, which is the "
                f"checkout {marked[1]}, and Xeda never changes a checkout. Write "
                f"`$XEDA_GENERATED/{entries[0].text}` and let the generator write there, or use "
                "a local clone as a local dependency",
            )
        merged.append(MergedDependency(ref, loaded, *marked))
    resolved = tuple(slot for slot in slots if slot is not None) if first is None else ()
    return design._merged_with(merged, resolved)


def merged_design(design: Design, *, run_root: RunRoot) -> Design:
    """Return `design` with the sources and the metadata of its dependencies merged in.

    The entries of `design.dependencies` merge in the order they are listed. A Git reference that
    the commits of `using_pinned_commits` name is fetched at that commit, at any depth, and is
    not resolved again (a remote host gets the commits that the client resolved). A local dependency
    is a design file on disk, relative to the root of the design that lists it. It must exist
    now. A Git dependency is resolved to a commit (`resolve_reference`) and fetched (`fetch`), and
    its design file is read from the checkout. The dependencies of a dependency merge before it
    does.

    The result lists every entry in `Design.resolved_dependencies` at its index, its position in
    `design.dependencies`. The sources of a Git dependency carry that index. The entries of a
    dependency come after those of the design.

    `run_root` can be `None` for a design that has no Git dependency, at any depth. It can be a
    function that returns the run root: a Git dependency calls it when it is met. `design` itself does not
    change, and a design without dependencies is returned as it is."""
    if not design.dependencies:
        return design
    for _ in declared_designs(design):
        pass
    return _merge(design, run_root, [None] * len(design.dependencies), None, _pinned.get())
