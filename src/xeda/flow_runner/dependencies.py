"""The dependencies of a design, read at planning.

A design lists the designs that it depends on (`Design.dependencies`). Loading a design reads
none of them. Planning reads them: it fetches each Git dependency into the run root and merges the
sources and the metadata of every dependency into the design (`merged_design`). So a dependency
supplies `rtl.sources`, `rtl.top`, `rtl.parameters`, `rtl.clocks`, `tb.sources` and `tb.top` to
the plan and the launch, and the `Design` that the caller gave keeps its references as written.

A Git dependency is kept in the run root, in storage that its commit names:
`<run root>/.dependencies/<key>/`, where `<key>` is the first 16 hexadecimal digits of the SHA-256
of the repository (without user name and password) and the full commit hash. The storage holds
the files of the commit and the record `dependency.yaml`. It is complete or absent: a fetch
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

import datetime
import hashlib
import json
import logging
import os
import re
import secrets
from collections.abc import Callable, Mapping
from pathlib import Path

import yaml

from ..design import (
    Design,
    DependencyRecord,
    DesignReference,
    GitReference,
    MergedDependency,
    dependency_pin,
    generator_of,
    redacted_url,
)
from ..errors import DesignError
from ..proc_utils import run_process
from ..run_dir import RunDirectory
from ..utils import NonZeroExitCode, replacing_file
from .run_lock import storage_lock

log = logging.getLogger(__name__)

__all__ = [
    "DEPENDENCIES_DIRECTORY",
    "RECORD_NAME",
    "dependency_key",
    "dependency_storage",
    "fetch",
    "merged_design",
    "resolve_reference",
]

#: The directory of dependency storage in the run root.
DEPENDENCIES_DIRECTORY = ".dependencies"
#: The record in each storage: `{repository, commit, fetched}`, the repository redacted.
RECORD_NAME = "dependency.yaml"
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
    env = {k: v for k, v in os.environ.items() if k not in _GIT_REPOSITORY_VARIABLES}
    env["GIT_TERMINAL_PROMPT"] = "0"
    count = int(env["GIT_CONFIG_COUNT"]) if env.get("GIT_CONFIG_COUNT", "").isdigit() else 0
    env["GIT_CONFIG_COUNT"] = str(count + 1)
    env[f"GIT_CONFIG_KEY_{count}"] = f"remote.{_REMOTE}.url"
    env[f"GIT_CONFIG_VALUE_{count}"] = repository
    settings = [word for setting in _GIT_SETTINGS for word in ("-c", setting)]
    output = run_process(
        "git", [*settings, *arguments], env=env, cwd=cwd, stdout=True, merge_stderr=True
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
    """Return the storage of `commit` of the repository of `ref`, fetching it if it is not there.

    A complete storage is never written again. Otherwise the fetch holds the exclusive lock of
    the storage, so two launches fetch one commit once. It writes into a temporary directory under
    `.dependencies/`, checks out `commit` there, and renames the directory into place. A failure
    removes the temporary directory and raises `DesignError` with the code `fetch_failed` and
    `field`. The run root exists."""
    storage = dependency_storage(run_root, dependency_key(ref.repo_url, commit))
    if _complete(storage):
        return storage
    with storage_lock(storage, exclusive=True, run_root=run_root):
        if _complete(storage):
            return storage
        owner = _owner(run_root)
        partial = owner.unlinked(
            Path(run_root)
            / DEPENDENCIES_DIRECTORY
            / f".partial-{storage.name}-{secrets.token_hex(4)}"
        )
        owner.writable(partial).mkdir(parents=True)
        try:
            _clone(ref, commit, partial, owner, field)
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
    return storage


def _clone(ref: GitReference, commit: str, into: Path, owner: RunDirectory, field: str) -> None:
    """Put the files of `commit` into the empty directory `into`.

    The commit is fetched alone, as shallow as the server allows, and falls back to every branch
    and tag. The directory of Git is removed afterwards: the files are the content, and the
    directory could hold the URL."""
    try:
        _git(["init", "--quiet", "."], repository=ref.repo_url, cwd=into)
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
    generator = generator_of(design.rtl, root=design.root_path)
    return "" if generator is None else generator.describe(design.name, design.root_path)


def _local_design(design: Design, ref: DesignReference, field: str) -> Design:
    """Load the design that the local reference `ref` of `design` names.

    The path is relative to the root of `design`. The file must exist now: a generator of the
    design runs later, in the launch, so it cannot write it."""
    path = design.root_path / ref.uri
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
    return Design.from_file(path)


def _git_design(ref: GitReference, storage: Path, field: str) -> Design:
    """Load the design file of a Git dependency from its storage."""
    path = _within(storage, ref.design_file)
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
    return Design.from_file(path)


#: Where Git dependencies are fetched: a run root, or a function that makes it when the first Git
#: dependency needs it, so that a design without one makes none.
RunRoot = Path | Callable[[], Path] | None


def _merge(
    design: Design,
    run_root: RunRoot,
    slots: list[DependencyRecord | None],
    first: int | None,
    pins: Mapping[str, str],
) -> Design:
    """Merge the dependencies of `design`, in the order they are listed.

    Each entry has a slot in `slots`, its index. The entries of the design that planning
    started from take the slots `0..n-1` at their positions (`first` is `None`). The entries of
    a dependency come after, in the order they are met (`first` is `len(slots)` then)."""
    merged: list[MergedDependency] = []
    for position, ref in enumerate(design.dependencies):
        field = f"dependencies[{position}]"
        index = position if first is None else len(slots)
        if first is not None:
            slots.append(None)
        if isinstance(ref, GitReference):
            assert run_root is not None, "a Git dependency is fetched into a run root"
            root = run_root() if callable(run_root) else run_root
            pinned = pins.get(
                dependency_pin(redacted_url(ref.repo_url), ref.branch, ref.design_file)
            )
            commit = pinned or resolve_reference(ref, run_root=root, field=field)
            storage = fetch(ref, commit, run_root=root, field=field)
            loaded = _git_design(ref, storage, field)
            checkout = Path(os.path.realpath(storage))
            slots[index] = DependencyRecord(
                redacted_url(ref.repo_url), ref.branch, commit, ref.design_file, checkout
            )
            marked: tuple[int | None, Path | None] = (index, checkout)
        else:
            loaded = _local_design(design, ref, field)
            slots[index] = DependencyRecord(None, None, None, str(ref.uri), None)
            # its written files count by their path in its own root
            marked = (index, loaded.root_path)
        loaded = _merge(loaded, run_root, slots, len(slots), pins)
        merged.append(MergedDependency(ref, loaded, *marked))
    resolved = tuple(slot for slot in slots if slot is not None) if first is None else ()
    return design.merged_with(merged, resolved)


def merged_design(design: Design, *, run_root: RunRoot) -> Design:
    """Return `design` with the sources and the metadata of its dependencies merged in.

    The entries of `design.dependencies` merge in the order they are listed. A Git reference that
    `design.dependency_commits` names is fetched at that commit, at any depth, and is not resolved
    again (a remote host gets the commits that the client resolved). A local dependency
    is a design file on disk, relative to the root of the design that lists it. It must exist
    now. A Git dependency is resolved to a commit (`resolve_reference`) and fetched (`fetch`), and
    its design file is read from the storage. The dependencies of a dependency merge before it
    does.

    The result lists every entry in `Design.resolved_dependencies` at its index, its position in
    `design.dependencies`. The sources of a Git dependency carry that index. The entries of a
    dependency come after those of the design.

    `run_root` can be `None` for a design that has no Git dependency, at any depth. It can be a
    function that returns the run root: a Git dependency calls it when it is met. `design` itself does not
    change, and a design without dependencies is returned as it is."""
    if not design.dependencies:
        return design
    return _merge(
        design, run_root, [None] * len(design.dependencies), None, design.dependency_commits
    )
