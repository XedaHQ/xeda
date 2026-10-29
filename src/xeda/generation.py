"""What a design generator leaves in its storage, and the decision whether it must run again.

A generator is an external program that writes the sources of a design. It runs inside a launch
(`flow_runner.launch_generation`), never when a design loads. Its output lives in a storage
under the run root, `<run root>/.generated/<design>/<key>/`, and the record of the generation,
`generation.yaml`, lies in that directory. The key names a configuration: one generator, one
design configuration, one set of inputs. Another configuration has another storage, so two
variants of a design keep their own generated files side by side.

The decision to run is made on content. The record holds the digest of every generated source
that the last generation left, and the digest of the environment that the generator inherited. The
generator runs again when the storage has no record of this key, when a recorded file is gone or
changed, or when the inherited environment is another. A modification time decides nothing: a
`touch`, a `cp -p` or a branch round-trip costs a hash and gives no wrong answer, and an edit
given back its old modification time is caught.

A generator is an external tool, and Xeda assumes nothing about its language. What it reads is
what the recipe names in `sources`; a directory counts as every file in it, outside the design
root too (a library tree, an editable clone). Xeda cannot see a change such as an upgraded
package: use `--rebuild-all`, or declare `always_runs`. A generator that declares no `sources`,
or declares `always_runs`, cannot be judged, so it runs on every launch. Xeda always takes this
direction when it cannot prove that something is up to date.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from glob import glob
from os.path import isfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .digest import content_digest, record_file
from .listing import VCS_METADATA, directory_files
from .run_dir import RunDirectory
from .utils import XedaException, semantic_hash

if TYPE_CHECKING:  # a design's generator; `design` does not import this module
    from .design import Generator

log = logging.getLogger(__name__)

#: The directory of the run root that holds the storage of every design generator.
GENERATION_DIRECTORY = ".generated"

#: The record in a storage, which says what the last generation left there.
RECORD_NAME = "generation.yaml"

#: The format of a record: an entry of another format is not read.
RECORD_FORMAT = 1

#: The number of hexadecimal digits of a key.
KEY_LENGTH = 16

#: Why a generator cannot be judged at all, so that it runs on every launch.
ALWAYS_RUNS = "it declares `always_runs`"
NOTHING_TO_JUDGE_BY = "it declares no `sources`, so nothing says when it is out of date"
NO_STORAGE = "it has not run for this configuration"
REBUILD_ALL = "this launch rebuilds everything"

#: The variables of a shell session, a terminal or a login. They change with every session, so the
#: digest of the inherited environment leaves them out. Xeda sets the two variables of a
#: generator itself.
SESSION_VARIABLES = frozenset(
    {
        "PWD",
        "OLDPWD",
        "SHLVL",
        "_",
        "COLUMNS",
        "LINES",
        "TERM_SESSION_ID",
        "ITERM_SESSION_ID",
        "WINDOWID",
        "TMUX",
        "TMUX_PANE",
        "STY",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "SSH_CLIENT",
        "SSH_CONNECTION",
        "SSH_TTY",
        "XDG_SESSION_ID",
        "XEDA_RUN_ROOT",
        "XEDA_REMOTE",
        "XEDA_LOG_LEVEL",
        "XEDA_DETAILED_LOGS",
    }
)


class GenerationError(XedaException):
    """The generator of a design failed, or could not be started or judged.

    It is an error of the whole launch (the code `generator_failed`): no node has run."""


class MissingGenerated(XedaException):
    """A generated source that a node reads names no file after the generation.

    `declaration` is the entry of `rtl.sources` (a name or a pattern) that matched nothing."""

    def __init__(self, declaration: str, generator: str) -> None:
        super().__init__(
            f"the generated source `{declaration}` matches no file in the output of "
            f"{generator}: check that the generator writes it, or remove it from `rtl.sources`. "
            "If you wrote the file yourself, list only the generated entries in "
            "`generated_sources` of the generator: with none, every entry is the generator's"
        )
        self.declaration = declaration


def generation_storage(run_root: Path, design_name: str, key: str) -> Path:
    """Return the storage of the generation `key` of a design, `<run root>/.generated/<design>/<key>`.

    The path is named as every cache of Xeda under a run root is: through no symbolic link."""
    owner = RunDirectory(run_root, run_root)
    return owner.unlinked(run_root / GENERATION_DIRECTORY / design_name / key)


def matches_in(storage: Path, name: str) -> list[str]:
    """Return the names of the files in `storage` that the declaration `name` stands for.

    `name` is a name or a pattern, where `*` is the only pattern character and it never crosses a
    `/`. The names are relative to `storage`, in POSIX form and sorted. Only files match, and the
    record of the storage is none of them."""
    written = re.sub(r"[?\[]", r"[\g<0>]", name)
    found = (m for m in glob(written, root_dir=storage) if isfile(storage / m))
    return sorted(m.replace("\\", "/") for m in found if m != RECORD_NAME)


def _named(path: Path, root: Path) -> str:
    """Return the name of a file in an identity or a record.

    The name is relative to the design root where the file lies under it, so moving a whole
    design tree changes nothing. Otherwise it is the location the path names."""
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()


def _digests(paths: Sequence[Path], root: Path) -> tuple[tuple[str, str], ...]:
    """Return the name and content digest of each path, sorted by name, with one entry per name.

    A directory is expanded entry by entry (`listing.directory_files`, links followed,
    version-control metadata left out), as the trace treats a directory that a setting names. The
    digest of a directory is a constant, so a file edited or added inside it would otherwise be
    invisible. Each entry is recorded as itself (`follow_symlinks=False`), as the trace records a
    listing: a link by its target and, for a link to a file, also by the content of that file.

    A file can be named twice, by a pattern and by its own path, or by a directory and by itself.
    This function makes the names unique, so the digest does not depend on the order of the
    paths."""
    named: dict[str, str] = {}
    for path in paths:
        named[_named(path, root)] = record_file(path).sha
    for path in paths:
        if path.is_dir():
            for child in directory_files(path, skip=VCS_METADATA, follow_links=True):
                name = _named(child, root)
                if name not in named:
                    named[name] = record_file(child, follow_symlinks=False).sha
    return tuple(sorted(named.items()))


def input_digests(generator: Generator, design_root: Path) -> tuple[tuple[str, str], ...]:
    """Return the names and content digests of what the generator reads (its `sources`).

    A source that cannot be read (removed since the design was validated, a link to nothing) is a
    `GenerationError`, so it is an error of the design and not a traceback from inside a hash."""
    try:
        return _digests(generator.sources, design_root)
    except OSError as error:
        raise GenerationError(
            f"cannot read the generator source {error.filename or '?'}: {error.strerror or error}"
        ) from error


def executable_identity(
    generator: Generator, design_root: Path, environ: Mapping[str, str]
) -> tuple[str, str]:
    """Return the first word of the command and the content digest of the program it selects."""
    try:
        command = generator.execution_command(design_root)
        executable = generator.execution_executable_path(design_root, command, environ)
        return command[0], content_digest(executable)
    except (ValueError, OSError) as error:
        raise GenerationError(
            f"cannot identify the executable of the generator: {error}"
        ) from error


def recipe_identity(generator: Generator) -> dict[str, Any]:
    """Return the recipe as it is written: the part of the generation key that the generator
    states. Its `env` is part of it, as is its working directory. Its inputs are not: their
    content is, in `input_digests`. The place that receives copies of its output (`outputs_to`)
    is not: it does not change what the generator writes."""
    return {
        "class": type(generator).__name__,
        "recipe": generator.model_dump(
            mode="json", exclude={"sources", "generated_sources", "outputs_to"}
        ),
    }


def environment_evidence(generator: Generator, environ: Mapping[str, str]) -> dict[str, Any]:
    """Return the digest of the environment that the generator inherits, and the names in it.

    A generator whose recipe has an `env` inherits nothing (`env` is its whole environment), so
    its evidence is empty. Otherwise the generator inherits `environ`, less the variables of a
    session (`SESSION_VARIABLES`) and the two that Xeda sets. No value is kept, only the digest."""
    if generator.env is not None:
        items: list[tuple[str, str]] = []
    else:
        from .design import GENERATOR_VARIABLES

        items = sorted(
            (name, value)
            for name, value in environ.items()
            if name not in SESSION_VARIABLES and name not in GENERATOR_VARIABLES
        )
    return {"digest": semantic_hash(items), "names": [name for name, _ in items]}


def unjudgeable(generator: Generator) -> str | None:
    """Return why the inputs can never show this generator to be stale, or None if they can."""
    if generator.always_runs:
        return ALWAYS_RUNS
    if not generator.sources:
        return NOTHING_TO_JUDGE_BY
    return None


def _outputs(storage: Path, declarations: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """Return the files that each declaration matches in `storage`."""
    return {name: tuple(matches_in(storage, name)) for name in declarations}


def _digests_of(storage: Path, matches: Mapping[str, tuple[str, ...]]) -> list[list[str]]:
    names = sorted({name for found in matches.values() for name in found})
    return [[name, record_file(storage / name).sha] for name in names]


def _changed_variables(was: Sequence[str], now: Sequence[str]) -> str:
    added = sorted(set(now) - set(was))
    removed = sorted(set(was) - set(now))
    if added:
        return f"`{added[0]}` is set now"
    if removed:
        return f"`{removed[0]}` is not set now"
    return "the value of a variable changed"


def judge(
    storage: Path,
    key: str,
    declarations: Sequence[str],
    generator: Generator,
    environ: Mapping[str, str],
    *,
    judging: bool = True,
) -> tuple[str | None, dict[str, tuple[str, ...]]]:
    """Return why the generator must run for `storage`, or None, and the files of the storage.

    The function reads and writes nothing else than the storage: it creates nothing. It returns
    None when the storage holds a record of this `key`, the inherited environment is the one the
    record names, and every generated source is there with the content that the generator left.
    The files are those that each of `declarations` (names and patterns of generated sources)
    matches now. With `judging` false, a generator that cannot be judged (`unjudgeable`) is
    judged like any other: a launch that has just generated asks that, to see that the storage is
    still what it left."""
    reason = unjudgeable(generator) if judging else None
    if reason is not None:
        return reason, {}
    if not storage.is_dir():
        return NO_STORAGE, {}
    record = storage / RECORD_NAME
    try:
        data = yaml.safe_load(record.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "its last generation did not finish: the storage has no record", {}
    except (OSError, ValueError, yaml.YAMLError) as error:
        return f"the record of its last generation cannot be read: {error}", {}
    if not isinstance(data, dict) or data.get("format") != RECORD_FORMAT:
        return "the record of its last generation is of another format", {}
    if data.get("key") != key:
        return "the record of its last generation is of another configuration", {}
    evidence = data.get("environment")
    now = environment_evidence(generator, environ)
    if (
        not isinstance(evidence, dict)
        or not isinstance(evidence.get("names"), list)
        or not isinstance(evidence.get("digest"), str)
    ):
        return "the record of its last generation is malformed", {}
    if evidence["digest"] != now["digest"]:
        return (
            "the inherited environment changed: "
            + _changed_variables(evidence["names"], now["names"]),
            {},
        )
    recorded = data.get("outputs")
    if not isinstance(recorded, list) or not all(
        isinstance(item, list) and len(item) == 2 and all(isinstance(part, str) for part in item)
        for item in recorded
    ):
        return "the record of its last generation is malformed", {}
    matches = _outputs(storage, declarations)
    try:
        current = _digests_of(storage, matches)
    except OSError as error:
        return f"a generated source cannot be read: {error}", matches
    if [item[0] for item in current] != sorted(item[0] for item in recorded):
        return "the files of the storage are not those its last generation left", matches
    wanted = {name: sha for name, sha in recorded}
    changed = [name for name, sha in current if wanted.get(name) != sha]
    if changed:
        return f"{changed[0]} is not what its last generation left", matches
    return None, matches


def write_record(
    owner: RunDirectory,
    storage: Path,
    key: str,
    generator: Generator,
    environ: Mapping[str, str],
    matches: Mapping[str, tuple[str, ...]],
) -> None:
    """Write the record of a generation that has just ended, into its storage."""
    from .utils import replacing_file

    record = {
        "format": RECORD_FORMAT,
        "key": key,
        "generator": type(generator).__name__,
        "environment": environment_evidence(generator, environ),
        "outputs": _digests_of(storage, matches),
    }
    with replacing_file(owner.writable(storage / RECORD_NAME)) as stream:
        yaml.safe_dump(record, stream, sort_keys=True)
