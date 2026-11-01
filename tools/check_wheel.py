"""Build the wheel and check that an installed Xeda has every file it reads at run time.

An editable checkout hides a template, a data file or a remote script that a glob of
`[tool.setuptools.package-data]` forgets. This script builds a wheel from a copy of the tracked
files in a temporary directory, installs it into a fresh virtual environment, and runs Xeda from
there with the checkout neither the working directory nor on `sys.path`. It writes nothing in
the checkout. It exits with status 0, or with 1 and the first step that failed.

    python tools/check_wheel.py

The same file runs inside the new environment (`--installed`): it then reads the installed
package through `importlib.resources`, as the flows do.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import glob
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import tomllib
import venv
import zipfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

#: A tracked file under `src/xeda/` that the wheel does not hold, with the reason. A file that is
#: not here must be in the wheel.
UNSHIPPED: dict[str, str] = {}

#: The agent session of the remote check: a workspace key (24 hex digits) and a design name.
WORKSPACE_KEY = "0123456789abcdef01234567"

TIMEOUT = 300


class CheckFailed(Exception):
    """A step of the check failed. The message names the step and the file or output."""


# ---------------------------------------------------------------------------------------------
# What the wheel must hold
# ---------------------------------------------------------------------------------------------


def tracked_files() -> list[str]:
    """Return the tracked files under `src/xeda/` that exist, as paths relative to `src/`
    (`xeda/flows/openroad/data/def2stream.py`), sorted."""
    listing = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--", "src/xeda"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    names = (name.removeprefix("src/") for name in listing.split("\0") if name)
    return sorted(name for name in names if (SRC / name).is_file())


def package_data_globs() -> dict[str, list[str]]:
    """Return the `[tool.setuptools.package-data]` table of `pyproject.toml`."""
    with (ROOT / "pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)["tool"]["setuptools"]["package-data"]


def package_directories(files: Sequence[str]) -> set[str]:
    """Return the packages of `files` as directories relative to `src/`. A package is a directory
    with an `__init__.py` in a package, or in `src/` itself: the rule of the package search of
    setuptools without namespaces."""
    present = set(files)
    packages: set[str] = set()
    for name in sorted(present, key=lambda n: n.count("/")):
        path = PurePosixPath(name)
        parent = path.parent
        if path.name == "__init__.py" and (
            len(parent.parts) == 1 or str(parent.parent) in packages
        ):
            packages.add(str(parent))
    return packages


def shipped_by_setuptools(name: str, files: Sequence[str]) -> bool:
    """Return whether the build of `[tool.setuptools]` puts the tracked file `name` (relative to
    `src/`) in the wheel: a module of a package, or a file that a glob of the package-data of any
    package above it matches. A glob names files relative to its package and may reach into a
    directory that is a package itself (`xeda.data` names `boards/**/*.xdc`)."""
    packages = package_directories(files)
    path = PurePosixPath(name)
    if path.suffix == ".py" and str(path.parent) in packages:
        return True
    table = package_data_globs()
    for owner in (str(p) for p in path.parents if str(p) in packages):
        relative = path.relative_to(owner).as_posix()
        directory = str(SRC / owner)
        for pattern in table.get(owner.replace("/", "."), []):
            found = glob.glob(pattern, root_dir=directory, recursive=True)
            if relative in {Path(match).as_posix() for match in found}:
                return True
    return False


def expected_manifest() -> list[str]:
    """Return the files the wheel must hold: every tracked file under `src/xeda/` that is not in
    `UNSHIPPED`. Python files count too: some are data, such as the KLayout script
    `flows/openroad/data/def2stream.py`."""
    return [name for name in tracked_files() if name not in UNSHIPPED]


# ---------------------------------------------------------------------------------------------
# The driver: build, install, run
# ---------------------------------------------------------------------------------------------


def clean_environment() -> dict[str, str]:
    """The environment of every command: no Python variable that could put the checkout on the
    path."""
    return {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONSAFEPATH"}
    }


def run(command: Sequence[str], cwd: Path, what: str, env: Optional[dict[str, str]] = None) -> str:
    """Run `command` in `cwd` and return its standard output. Raise `CheckFailed` with `what` and
    the end of the output if it fails."""
    step = subprocess.run(
        list(command),
        cwd=cwd,
        env=clean_environment() if env is None else env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT * 4,
        check=False,
    )
    if step.returncode != 0:
        output = "\n".join((step.stdout + step.stderr).strip().splitlines()[-30:])
        raise CheckFailed(f"{what}: `{' '.join(command)}` exited with {step.returncode}\n{output}")
    return step.stdout


def copy_tracked_sources(destination: Path) -> None:
    """Copy the files that the build reads (the tracked files of the top directory and `src/`) to
    `destination`. The copy has no `.git`, so the wheel holds what the globs of `pyproject.toml`
    name and nothing else, and the build writes its `build/` and egg-info directories there."""
    listing = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"], check=True, capture_output=True, text=True
    ).stdout
    for name in filter(None, listing.split("\0")):
        if ("/" in name and not name.startswith("src/")) or not (ROOT / name).is_file():
            continue
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)


def build_wheel(source: Path, dist: Path) -> Path:
    environment = {**clean_environment(), "SETUPTOOLS_SCM_PRETEND_VERSION": "0.0.0+wheelcheck"}
    run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist), str(source)],
        dist.parent,
        "build the wheel (is `build` installed?)",
        environment,
    )
    (wheel,) = dist.glob("xeda-*.whl")
    return wheel


def check_manifest(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        held = set(archive.namelist())
    missing = [name for name in expected_manifest() if name not in held]
    if missing:
        raise CheckFailed(
            f"the wheel does not hold {missing[0]} ({len(missing)} missing in all; add a glob to "
            "[tool.setuptools.package-data] or an entry with a reason to UNSHIPPED)"
        )
    print(f"manifest: the wheel holds all {len(expected_manifest())} expected files")


def make_environment(directory: Path, wheel: Path) -> Path:
    venv.EnvBuilder(with_pip=True).create(directory)
    python = directory / ("Scripts" if os.name == "nt" else "bin") / "python"
    run(
        [str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", str(wheel)],
        directory,
        "install the wheel with its dependencies",
    )
    return python


def json_output(command: Sequence[str], cwd: Path) -> Any:
    text = run(command, cwd, "command line")
    try:
        return json.loads(text)
    except ValueError as error:
        raise CheckFailed(f"`{' '.join(command)}` did not print one JSON document: {error}")


def check_commands(python: Path, empty: Path) -> None:
    xeda = str(python.with_name("xeda"))
    run([xeda, "--help"], empty, "xeda --help")
    run([str(python), "-I", "-m", "xeda", "--help"], empty, "python -m xeda --help")
    flows = json_output([xeda, "list-flows", "--json"], empty)
    names = [flow["name"] for flow in flows]
    if not names:
        raise CheckFailed("`xeda list-flows --json` lists no flow")
    commands = [[xeda, "list-boards", "--json"], [xeda, "list-platforms", "--json"]]
    commands.append([xeda, "design-schema"])
    for name in names:
        commands += [
            [xeda, "list-settings", name, "--json"],
            [xeda, "list-results", name, "--json"],
        ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 2) as pool:
        for command, _ in zip(commands, pool.map(lambda c: json_output(c, empty), commands)):
            pass
    print(f"commands: {len(commands) + 3} ran and printed what they should ({len(names)} flows)")


def check_installed(python: Path, empty: Path, expected_file: Path) -> None:
    run(
        [str(python), "-I", str(Path(__file__).resolve()), "--installed", str(expected_file)],
        empty,
        "installed package",
    )
    print("installed: loaders, resources and the remote agent work")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--installed", metavar="EXPECTED", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    try:
        if arguments.installed:
            return installed_main(Path(arguments.installed))
        with tempfile.TemporaryDirectory(prefix="xeda-wheel-") as name:
            tmp = Path(name)
            if tmp.resolve().is_relative_to(ROOT):
                raise CheckFailed(f"the temporary directory {tmp} is inside the checkout")
            source, dist, empty = tmp / "source", tmp / "dist", tmp / "empty"
            for directory in (source, dist, empty):
                directory.mkdir()
            copy_tracked_sources(source)
            wheel = build_wheel(source, dist)
            print(f"built {wheel.name}")
            check_manifest(wheel)
            python = make_environment(tmp / "venv", wheel)
            check_commands(python, empty)
            expected_file = tmp / "expected.json"
            expected_file.write_text(json.dumps(expected_manifest()), encoding="utf-8")
            check_installed(python, empty, expected_file)
    except CheckFailed as failure:
        print(f"check_wheel: FAILED: {failure}", file=sys.stderr)
        return 1
    print("check_wheel: the wheel is complete")
    return 0


# ---------------------------------------------------------------------------------------------
# Inside the new environment
# ---------------------------------------------------------------------------------------------


def resource_of(name: str, packages: set[str]) -> Any:
    """Return the traversable of the tracked file `name`, as the package code reaches it: through
    the nearest package of the file."""
    from importlib import resources

    path = PurePosixPath(name)
    owner = next(str(p) for p in path.parents if str(p) in packages)
    return resources.files(owner.replace("/", ".")).joinpath(path.relative_to(owner).as_posix())


def check_resources(expected: list[str]) -> None:
    """Read every data file of the wheel through `importlib.resources`."""
    packages = package_directories(expected)
    data = [name for name in expected if not name.endswith(".py")]
    data += [name for name in expected if name.endswith(".py") and "/data/" in name]
    for name in data:
        try:
            resource_of(name, packages).read_bytes()
        except (OSError, ImportError) as error:
            raise CheckFailed(f"cannot read {name} through importlib.resources: {error}")


def check_templates(expected: list[str]) -> None:
    """Load the source of every template of every flow through the loaders of the flow. (A flow
    adds its own filters when it renders, so the check does not compile.) The flow skips a package without a templates directory. This check does not: a package that has
    template files among the tracked files must have all of them in its loader."""
    from importlib.util import find_spec

    import xeda.flows  # noqa: F401  (registers the flows)
    from xeda.introspect import all_flow_classes

    installed = Path(sys.modules["xeda"].__file__ or "").resolve().parent
    templates: dict[str, set[str]] = {}
    for name in expected:
        parts = PurePosixPath(name).parts
        if "templates" in parts and name.endswith((".j2", ".h", ".tcl", ".cpp", ".c")):
            at = parts.index("templates")
            templates.setdefault("/".join(parts[:at]), set()).add("/".join(parts[at + 1 :]))
    count = 0
    for flow_class in all_flow_classes():
        env = flow_class._create_jinja_env()
        modules = [flow_class.__module__] + [c.__module__ for c in flow_class.__bases__]
        for module in dict.fromkeys(m for mod in modules for m in (mod, mod.rsplit(".", 1)[0])):
            spec = find_spec(module)
            if spec is None or spec.origin is None:
                continue
            origin = Path(spec.origin).resolve()
            directory = origin.parent.relative_to(installed.parent).as_posix()
            for template in sorted(templates.get(directory, ())):
                try:
                    env.loader.get_source(env, template)
                except Exception as error:  # a template that the loader does not find
                    raise CheckFailed(
                        f"flow {flow_class.name}: the template {directory}/templates/{template} "
                        f"is not loaded: {error!r}"
                    )
                count += 1
    flows_with_templates = {d for d in templates if d.startswith("xeda/flows/")}
    if not count or not flows_with_templates:
        raise CheckFailed("no template was loaded")


def check_data_users(expected: list[str]) -> None:
    """Call the code that reads data files, as the flows and the commands do."""
    from importlib import resources

    from xeda.agent_skill import skill_source_dir
    from xeda.board import bundled_boards
    from xeda.flow_catalog import flow_names
    from xeda.platforms.residue import bundled_platform_names, load_residue
    from xeda.remote.provision import boot_script

    if not bundled_boards():
        raise CheckFailed("xeda/data/boards.toml holds no board")
    wanted = sorted(
        PurePosixPath(n).parts[2]
        for n in expected
        if n.startswith("xeda/platforms/") and n.endswith("/platform.yaml")
    )
    if bundled_platform_names() != wanted:
        raise CheckFailed(
            f"the bundled platforms are {bundled_platform_names()}, the checkout has {wanted}"
        )
    for name in wanted:
        load_residue(name)
    with skill_source_dir() as skill:
        for name in expected:
            if (
                name.startswith("xeda/data/agent/")
                and not (skill / name.removeprefix("xeda/data/agent/")).is_file()
            ):
                raise CheckFailed(f"the agent skill lacks {name}")
    if not flow_names():
        raise CheckFailed("the flow catalog lists no flow")
    # The path that `flows/openroad/final.py` hands to KLayout.
    script = Path(str(resources.files("xeda.flows.openroad").joinpath("data", "def2stream.py")))
    if not script.is_file():
        raise CheckFailed("xeda/flows/openroad/data/def2stream.py is not in the wheel")
    if "agent" not in boot_script():
        raise CheckFailed("xeda/remote/bootstrap.sh is not the bootstrap script")


def check_remote_agent() -> None:
    """Start the agent from a code snapshot of the installed package, as the remote host does:
    the interpreter of the environment, `PYTHONPATH` set to the snapshot only, an empty working
    directory. Send the first request of the protocol and expect the reply."""
    from xeda.remote.environment import code_snapshot, write_code_tar
    from xeda.remote.manifest import build_manifest, write_tar
    from xeda.remote.frames import JSON, FrameWriter, decode_json, read_frame, skip_to_magic

    with tempfile.TemporaryDirectory(prefix="xeda-agent-") as name:
        base = Path(name)
        code, work, root = base / "code", base / "work", base / "root"
        for directory in (code, work, root):
            directory.mkdir()
        archive = io.BytesIO()
        write_code_tar(code_snapshot(), archive)
        archive.seek(0)
        with tarfile.open(fileobj=archive) as tar:
            tar.extractall(code, filter="data")
        environment = {**clean_environment(), "PYTHONPATH": str(code)}
        found = run(
            [sys.executable, "-c", "import xeda; print(xeda.__file__)"],
            work,
            "snapshot",
            environment,
        ).strip()
        if not Path(found).is_relative_to(code):
            raise CheckFailed(f"the snapshot is not first on the path: xeda is at {found}")
        stderr_path = base / "agent.stderr"
        with stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                [sys.executable, "-m", "xeda.remote.agent", "session", str(root), WORKSPACE_KEY],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
                cwd=work,
                env=environment,
            )
            assert process.stdin is not None and process.stdout is not None
            watchdog = threading.Timer(TIMEOUT, process.kill)
            watchdog.start()

            def fail(reason: str) -> CheckFailed:
                process.kill()
                return CheckFailed(
                    f"remote agent: {reason}\n{stderr_path.read_text(errors='replace')[-2000:]}"
                )

            def reply(key: str) -> Any:
                while True:
                    frame = read_frame(process.stdout)
                    if frame is None:
                        raise fail(f"the agent ended before it sent `{key}`")
                    if frame[0] != JSON:
                        continue
                    document = decode_json(frame[1])
                    if key in document:
                        return document[key]
                    if "error" in document:
                        raise fail(f"the agent answered with an error: {document['error']}")

            try:
                writer = FrameWriter(process.stdin)
                skip_to_magic(process.stdout)
                hello = reply("hello")
                if "xeda" not in hello:
                    raise fail(f"unexpected hello {hello!r}")
                files = {"top.v": b"module top(input clk, output q); assign q = clk; endmodule\n"}
                entries = build_manifest({}, generated=files)
                writer.send_json(
                    {
                        "sync": {
                            "workspace": WORKSPACE_KEY,
                            "design": "top",
                            "entries": [entry.as_json_value() for entry in entries],
                            "id": "wheel-check",
                        }
                    }
                )
                if reply("need") != ["top.v"]:
                    raise fail("the agent does not ask for the file of the design")
                tar = io.BytesIO()
                write_tar(entries, {}, files, tar)  # type: ignore[arg-type]
                tar.seek(0)
                writer.send_stream(tar)
                if reply("synced").get("written") != 1:
                    raise fail("the agent did not write the file of the design")
                writer.send_json({"bye": {}})
                process.stdin.close()
                if process.wait(timeout=TIMEOUT) != 0:
                    raise fail(f"the agent exited with {process.returncode}")
            finally:
                watchdog.cancel()
                if process.poll() is None:
                    process.kill()
                process.wait()


def installed_main(expected_file: Path) -> int:
    import xeda

    expected = json.loads(expected_file.read_text(encoding="utf-8"))
    prefix = Path(sys.prefix).resolve()
    if not Path(xeda.__file__).resolve().is_relative_to(prefix):
        raise CheckFailed(f"xeda is imported from {xeda.__file__}, not from {prefix}")
    check_resources(expected)
    check_templates(expected)
    check_data_users(expected)
    check_remote_agent()
    return 0


if __name__ == "__main__":
    sys.exit(main())
