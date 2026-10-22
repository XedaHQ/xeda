from __future__ import annotations

import inspect
import logging
import os
import re
import shutil
import tempfile
import uuid
import weakref
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Sequence, Tuple, Union

from .console import console
from .dataclass import Field, PrivateAttr, XedaBaseModel, field_validator
from .flow import Flow
from .proc_utils import (
    DOCKER_IMAGE_PREFIX,
    ProcessTimeout,
    note_program,
    run_process,
    tool_output_stream,
)
from .utils import (
    ExecutableNotFound,
    NonZeroExitCode,
    ToolException,
    cached_property,
    replacing_file,
    try_convert,
)

log = logging.getLogger(__name__)

#: The longest time in seconds that `docker kill` of a stopped run's container may take. It is the
#: only limit on `Docker.run`'s stop hook, which `run_process` runs on the waiting thread.
DOCKER_KILL_TIMEOUT = 30

__all__ = [
    "Docker",
    "ExecutableNotFound",
    "NonZeroExitCode",
    "Tool",
    "ToolException",
    "run_process",
]


OptionalPath = Union[None, str, os.PathLike]
OptionalBoolOrPath = Union[None, bool, str, os.PathLike]


def _placed(directory: Path, path: OptionalBoolOrPath) -> OptionalBoolOrPath:
    """Return `path` as an absolute path: a relative one is relative to `directory`. A missing
    path and a flag (`stdout=True`) stay as they are. `run_process` opens a relative file against
    the working directory of the process, so a file of a tool is named in full."""
    if path is None or isinstance(path, bool):
        return path
    return directory / path


class Docker(XedaBaseModel):
    image: str = Field(description="Docker image name")
    command: List[str] = []
    platform: Optional[str] = Field(
        None,
        description="Set platform (e.g. 'linux/amd64'), if server is multi-platform capable",
    )
    tag: Optional[str] = Field("latest", description="Docker image tag")
    registry: Optional[str] = Field(None, description="Docker image registry")
    privileged: bool = False
    fix_cpuinfo: bool = False
    cli: str = "docker"
    mounts: Dict[str, str] = {}
    default_env: Dict[str, str] = {"DISPLAY": "host.docker.internal:0"}

    # NOTE only works for Linux containers
    @cached_property
    def cpuinfo(self) -> Optional[List[List[str]]]:
        # The query needs no directory of the user: it runs in a scratch directory, as a probe of
        # the version of a tool does.
        try:
            with tempfile.TemporaryDirectory(prefix="xeda-probe-") as scratch:
                ret = self.run("cat", "/proc/cpuinfo", stdout=True, cwd=Path(scratch))
        except Exception:  # noqa: BLE001 - CPU details are optional Docker metadata
            ret = None
        if ret is not None:
            return [x.split("\n") for x in re.split(r"\n\s*\n", ret)]
        return None

    @cached_property
    def nproc(self) -> int:
        return len(self.cpuinfo) if self.cpuinfo else 1

    @cached_property
    def name(self) -> str:
        # The basename of the containerized executable, so that an absolute command still gives a
        # name.
        return self.command[0].rsplit("/", 1)[-1] if self.command else "???"

    def run(
        self,
        executable,
        *args: Any,
        env: Optional[Dict[str, Any]] = None,
        stdout: OptionalBoolOrPath = None,
        check: bool = True,
        read_only: Sequence[Path] = (),
        print_command: bool = True,
        highlight_rules: Optional[Dict[str, str]] = None,
        merge_stderr: bool = False,
        timeout: float | None = None,
        tee: Path | None = None,
        cwd: Path | None = None,
    ) -> Union[str, None]:
        """Run the tool in a Docker container.

        The container works in `cwd`, which must be an absolute directory, and mounts it
        writable. It sees the resolved path of that directory, as a process that changed into it
        would, and its `PWD` is that path. It mounts each directory of `read_only` read-only: the design's, and those of
        the flow's inputs (`Tool.execute` chooses them). A mount configured in `mounts` stays as
        configured. The `docker` client starts in `cwd` too, and the files that Xeda writes for
        the container (the environment file, the CPU information) go there."""
        if cwd is None or not os.path.isabs(cwd):
            raise ToolException(
                f"{executable!r} in Docker image {self.image!r} has no directory to run in: give "
                f"an absolute `cwd=`, not {cwd!r}."
            )
        cwd = Path(os.path.realpath(cwd))
        stdout = _placed(cwd, stdout)
        tee = None if tee is None else cwd / tee
        note_program(f"{DOCKER_IMAGE_PREFIX}{self.image}:{self.tag or 'latest'}")
        # These are this run's mounts: `mounts` and the directories this run needs. The extra ones
        # stay out of `mounts`, because a later run would mount them again. A version probe's
        # temporary directory (`Tool.probe_stdout`) is gone by then, and Docker would create it
        # anew.
        mounts = dict(self.mounts)
        if self.fix_cpuinfo and self.cpuinfo:
            cpuinfo_file = cwd / ".cpuinfo"
            with replacing_file(cpuinfo_file) as f:
                for proc in self.cpuinfo:
                    for line in proc:
                        assert isinstance(line, str)
                        if line.startswith("Features"):
                            line += " sse sse2"
                        f.write(line + "\n")
                    f.write("\n")
            mounts[str(cpuinfo_file)] = "/proc/cpuinfo"

        docker_args = [
            "--rm",
            f"--workdir={cwd}",
        ]

        if self.privileged:
            docker_args.append("--privileged")
        mounts[str(cwd)] = str(cwd)
        if not stdout and tool_output_stream().isatty():
            docker_args += ["--tty", "--interactive"]
        if self.platform:
            docker_args += ["--platform", self.platform]
        # SELinux: the container runs without a label (`label=disable`), never with the mounts
        # relabeled (`:z`), which would change the attributes of the user's own files.
        docker_args += ["--security-opt", "label=disable"]
        docker_args += [f"--volume={k}:{v}" for k, v in mounts.items()]
        for directory in dict.fromkeys(str(Path(d)) for d in read_only):
            # Skip the run directory and a mount configured at the same path (writable or not).
            if mounts.get(directory, "").split(":")[0] != directory:
                docker_args.append(f"--volume={directory}:{directory}:ro")
        env = {**self.default_env, **(env or {}), "PWD": str(cwd)}
        if env:
            env_file = cwd / f".{self.name}_docker.env"
            with replacing_file(env_file) as f:
                f.write("\n".join(f"{k}={v}" for k, v in env.items()))
            docker_args.extend(["--env-file", str(env_file)])
        image = self.image
        image_sp = image.split(":")
        if len(image_sp) < 2 and self.tag:
            image = f"{image}:{self.tag}"
        if self.command:
            command = self.command
        else:
            command = [executable]
        # Name the container so that stopping the run (a time limit, Ctrl-C) can stop it: killing
        # the `docker` client leaves the container running.
        container = f"xeda-{uuid.uuid4().hex}"
        cmd = ["run", "--name", container, *docker_args, image, *command, *args]

        def kill_container() -> None:
            # This is `run_process`'s `on_stop` hook. The caller waits for it, and
            # `DOCKER_KILL_TIMEOUT` limits the wait.
            try:  # a container that is already gone is fine: `--rm` removed it after it exited
                run_process(
                    self.cli,
                    ["kill", container],
                    stdout=True,
                    check=False,
                    merge_stderr=True,
                    timeout=DOCKER_KILL_TIMEOUT,
                    cwd=cwd,
                )
            except (OSError, ProcessTimeout) as e:
                log.warning("Could not stop container %s: %s", container, e)

        try:
            return run_process(
                self.cli,
                cmd,
                env=None,
                stdout=stdout,
                check=check,
                print_command=print_command,
                highlight_rules=highlight_rules,
                merge_stderr=merge_stderr,
                timeout=timeout,
                tee=tee,
                cwd=cwd,
                on_stop=kill_container,
            )
        except FileNotFoundError as e:
            path = env["PATH"] if env and "PATH" in env else os.environ.get("PATH", "")
            raise ExecutableNotFound(
                e.filename, self.__class__.__qualname__, path, *e.args
            ) from None


def fake_cpu_info(file=".xeda_cpuinfo", ncores=4):
    with replacing_file(file) as f:
        for i in range(ncores):
            cpuinfo: Dict[str, Any] = {
                "processor": i,
                "vendor_id": "GenuineIntel",
                "family": 6,
                "model": 60,
                "core id": i,
                "cpu cores": ncores,
                "fpu": "yes",
                "model name": "Intel(R) Core(TM) i7-4790K CPU @ 4.00GHz",
                "flags": "fpu vme tsc msr pae mce cx8 mmx fxsr sse sse2 avx2",
            }
            for k, v in cpuinfo.items():
                ws = "\\t" * (1 if len(k) >= 8 else 2)
                f.write(f"{k}{ws}: {v}")


VERSION_PATTERN = r"(\d+)((\.[a-zA-Z\d]+)*)(-\w+)+"
VERSION_REGEXP1 = re.compile(r"version[:\s]?\s*" + VERSION_PATTERN, flags=re.IGNORECASE)
VERSION_REGEXP2 = re.compile(VERSION_PATTERN)


class Tool(XedaBaseModel):
    """An EDA tool executable, run natively or in a Docker container."""

    executable: str
    minimum_version: Union[Tuple[Union[int, str], ...], None] = None
    default_args: List[str] = []
    version_flag: Optional[List[str]] = ["--version"]
    version_regexps: List[Union[re.Pattern[str], str]] = [VERSION_REGEXP1, VERSION_REGEXP2]
    docker: Optional[Docker] = Field(None)
    redirect_stdout: Optional[Path] = Field(None, description="Redirect stdout to a file")
    dockerized: bool = False
    print_command: bool = True
    highlight_rules: Optional[Dict[str, str]] = None
    console_colors: bool = True
    #: Whether the tool wants a terminal when Xeda's own output is one, for its colors and for a
    #: progress bar that it redraws in place (`run_process(terminal=...)`). It gets one only if
    #: colors are wanted too (`console_colors`) and it runs here, not in a container.
    pseudo_terminal: ClassVar[bool] = False
    #: Why the tool needs `minimum_version`. The message about an older version shows it, so the
    #: user sees what that release lacks and not only that Xeda refuses it.
    minimum_version_reason: ClassVar[str] = ""

    design_root_: Optional[Path] = Field(None, json_schema_extra={"hidden_from_schema": True})
    flow_settings_: Optional[Flow.Settings] = Field(
        None, json_schema_extra={"hidden_from_schema": True}
    )
    source_dirs_: List[Path] = Field(
        default_factory=list, json_schema_extra={"hidden_from_schema": True}
    )
    #: The run directory of the flow the tool runs for (`Flow.run_directory`). The files that Xeda
    #: writes for the tool (`env.sh`) go through it. It is None outside a flow.
    _run_directory: Any = PrivateAttr(default=None)
    #: A weak reference to the flow the tool runs for. A dockerized run asks the flow for the
    #: directories of its inputs when it starts, because the launcher fills the inputs after the
    #: tool is made. It is None outside a flow.
    _flow: Any = PrivateAttr(default=None)

    @field_validator("version_flag", mode="before")
    @classmethod
    def validate_version_flag(cls, value):
        if isinstance(value, str):
            return [value]
        return value

    def __init__(
        self,
        executable: Optional[str] = None,
        flow: Optional[Flow] = None,
        **kwargs,
    ):
        if executable:
            assert "executable" not in kwargs, "executable specified twice"
            kwargs["executable"] = executable
        if flow is None:
            for s in inspect.stack(0)[1:]:
                caller_inst = s.frame.f_locals.get("self")
                if isinstance(caller_inst, Flow):
                    flow = caller_inst
                    break

        super().__init__(**kwargs)

        self.console_colors = (
            self.console_colors and not console.no_color and console.color_system is not None
        )

        if flow is not None:
            self._run_directory = flow.run_directory
            self._flow = weakref.ref(flow)
            self.design_root_ = flow.design_root
            design = flow.design
            source_dirs_ = {src.path.parent for src in design.rtl.sources}
            source_dirs_ |= {src.path.parent for src in design.tb.sources}
            self.source_dirs_ = list(source_dirs_)
            self.print_command = flow.settings.print_commands
            self.console_colors = self.console_colors and flow.settings.console_colors
            log.debug("flow.settings.dockerized=%s", flow.settings.dockerized)
            self.dockerized = flow.settings.dockerized
            if flow.settings.docker:
                if self.docker:
                    self.docker.image = flow.settings.docker
                else:
                    self.docker = Docker(image=flow.settings.docker)  # type: ignore

        self.require_minimum_version()
        if flow is not None:
            if self.info not in flow.results.tools:
                flow.results.tools.append(self.info)

    @field_validator("docker", mode="before")
    @classmethod
    def validate_docker(cls, value, info):
        """Normalize Docker settings around the tool executable."""
        values = info.data if isinstance(info.data, dict) else {}
        # The command is the executable alone. `execute` passes `default_args` with every run, as
        # for a tool run natively, so a command that held them too would give the container each
        # of them twice.
        executable = values.get("executable")
        command: List[str] = [executable] if executable else []
        if isinstance(value, dict):
            value = Docker(**value)
        elif isinstance(value, Docker):
            # Isolate the caller's nested containers too. Tool initialization adds mounts and
            # derived tools replace the command, so a shallow copy still mutates caller-owned
            # `mounts`, `default_env`, and `command` objects.
            value = value.model_copy(deep=True)
            # The copy inherits the caller's `cached_property` cache, and `command` may be
            # filled in below, and `Docker.name` is derived from it.
            value.invalidate_cached_properties()
        if isinstance(value, str):
            split = value.split(":")
            value = Docker(
                image=split[0],
                tag=split[1] if len(split) > 1 else None,
                command=command,
            )  # type: ignore
        if value and not value.command:
            value.command = command
        return value

    @cached_property
    def info(self) -> Dict[str, Optional[str]]:
        try:
            version = self.version_str
        except Exception:  # noqa: BLE001 - version metadata must not prevent a tool run
            version = None
        return {"executable": self.executable, "version": version}

    def require_minimum_version(self) -> None:
        """Raise a `ToolException` that names the version found and the version needed, if the
        tool is older than `minimum_version`. A version that could not be read is not compared
        (`_version_is_gte`). `derive` copies a model and runs no constructor, so a flow that
        derives a tool with a floor asks for this check itself."""
        if not self.minimum_version or self.version_gte(*self.minimum_version):
            return
        needed = ".".join(str(part) for part in self.minimum_version)
        message = (
            f"Minimum version not met: {self.executable} {self.version_str} was found. "
            f"Xeda needs {needed} or newer."
        )
        if self.minimum_version_reason:
            message += f" {self.minimum_version_reason}"
        raise ToolException(message)

    def _get_version_output(self, *version_flags) -> Optional[str]:
        if self.version_flag is None:
            return None
        if not version_flags:
            version_flags = tuple(self.version_flag)
        try:
            out = self.probe_stdout(*version_flags)
        except Exception:  # noqa: BLE001 - an unavailable version is represented by None
            return None
        if out and out.strip():
            return out
        # Some tools (nextpnr, among others) print their version banner to stderr, so stdout
        # comes back empty. Retry with stderr merged in before giving up on a version.
        try:
            return self.probe_stdout(*version_flags, merge_stderr=True)
        except Exception:  # noqa: BLE001 - retain the first attempt's output when probing fails
            return out

    @cached_property
    def version_output(self) -> Optional[str]:
        return self._get_version_output()

    def process_version_output(self, out: Optional[str]) -> Tuple[str, ...]:
        if not out:
            return tuple()
        assert isinstance(out, str)
        lines = [line for line in (line.strip() for line in out.splitlines(keepends=False)) if line]
        if not lines:
            return tuple()
        for line in lines:
            for reg_expr in self.version_regexps:
                match = (
                    re.search(reg_expr, line)
                    if isinstance(reg_expr, str)
                    else reg_expr.search(line)
                )
                if match:
                    version = match.groupdict().get("version", None)
                    if version:
                        return tuple(x for x in re.split(r"\.|-", version.strip()) if x)
                    num_groups = len(match.groups())
                    log.debug(f"Matched version string: {match.group(0)} with {num_groups} groups")
                    if num_groups == 1:
                        return tuple(match.group(1).strip().removeprefix(".").split("."))
                    elif num_groups >= 5:
                        return (
                            match.group(1),
                            *match.group(2).removeprefix(".").split("."),
                            *match.group(4).removeprefix("-").split("-"),
                        )
        l0_splt = re.split(r"\s+", lines[0])
        version_string = l0_splt[1] if len(l0_splt) > 1 else l0_splt[0] if len(l0_splt) > 0 else ""
        return tuple(re.split(r"\.|-", version_string))

    @cached_property
    def version(self) -> Tuple[str, ...]:
        return self.process_version_output(self.version_output)

    @cached_property
    def version_str(self) -> str:
        return (".").join(self.version)

    @cached_property
    def nproc(self) -> int:
        if self.dockerized:
            # A query about the machine, as a version probe is (`probe_stdout`): it runs in a
            # scratch directory and leaves nothing where the flow runs.
            try:
                with tempfile.TemporaryDirectory(prefix="xeda-probe-") as scratch:
                    n = try_convert(self.execute("nproc", stdout=True, cwd=Path(scratch)), int)
            except Exception:  # noqa: BLE001 - Docker CPU detection has a safe fallback
                n = None
            assert self.docker
            return n or self.docker.nproc
        else:
            return os.cpu_count() or 1

    @staticmethod
    def _version_is_gte(
        tool_version: Tuple[str, ...], required_version: Tuple[Union[int, str], ...]
    ) -> bool:
        """Return whether `tool_version` is at least `required_version`.

        A component that the tool's version lacks counts as 0, so 2026.07 is below 2026.07.1.
        A version that could not be read at all (empty) is not compared.
        """
        log.debug(f"[gte] {tool_version}  ?  {required_version}")
        if not tool_version:
            return True
        missing = max(0, len(required_version) - len(tool_version))
        for tool_part, req_part in zip((*tool_version, *("0",) * missing), required_version):
            req_part_val = try_convert(req_part, int, default=-1)
            assert req_part_val is not None
            tool_part_val = try_convert(tool_part, int)
            if tool_part_val is None:
                match = re.match(r"(\d+)[+.-\._](\w+)", tool_part)
                if match:
                    tool_part = match.group(1)
                    tool_part_val = try_convert(tool_part, int)
                    if tool_part_val is not None and req_part_val > -1:
                        return tool_part_val >= req_part_val
                tool_part_val = -1

            if tool_part_val < req_part_val:  # if equal, continue
                return False
            if tool_part_val > req_part_val:  # if equal, continue
                return True
        return True

    def version_gte(self, *args: Union[int, str]) -> bool:
        """Return whether the tool version is at least the version given in `args`."""
        return self._version_is_gte(self.version, args)

    def executable_path(self) -> Path | None:
        """Return the resolved absolute path of the executable, or None if it is not found.

        An absolute executable must exist and be executable. A relative path (`./simv`) is
        relative to the working directory of the tool, where it starts; a tool without a flow has
        none, and this is a `ToolException`. Any other name is looked up on `PATH`."""
        executable = self.executable
        if not os.path.isabs(executable) and os.sep in executable:
            executable = str(self.working_directory() / executable)
        if (
            os.path.isabs(executable)
            and os.path.exists(executable)
            and os.access(executable, os.X_OK)
        ):
            return Path(executable).resolve()
        if executable != self.executable:
            return None
        which = shutil.which(self.executable)
        if which is not None:
            return Path(which).resolve()
        return None

    def run(
        self,
        *args: Any,
        env: Optional[Dict[str, Any]] = None,
        stdout: OptionalBoolOrPath = None,
        check: bool = True,
        highlight_rules: Optional[Dict[str, str]] = None,
        merge_stderr: bool = False,
        timeout: float | None = None,
        tee: Path | None = None,
        cwd: Path | None = None,
    ) -> Union[str, None]:
        """Run the tool with `args` in its working directory (`execute`)."""
        if env:
            env = {k: str(v) for k, v in env.items() if v is not None}
            env_file = self._directory(cwd) / "env.sh"
            if self._in_run_directory(env_file):
                self._run_directory.writable(env_file)
            with replacing_file(env_file) as f:
                f.write("\n".join(f'export {k}="{v}"' for k, v in env.items()))
        return self.execute(
            self.executable,
            *args,
            env=env,
            stdout=stdout,
            check=check,
            cwd=cwd,
            highlight_rules=highlight_rules,
            merge_stderr=merge_stderr,
            timeout=timeout,
            tee=tee,
        )

    def _live_flow(self) -> Optional[Flow]:
        """Return the flow the tool runs for, or None outside a flow or once the flow is gone."""
        return self._flow() if self._flow is not None else None

    def working_directory(self) -> Path:
        """Return the absolute directory where the tool works.

        It is the run path of the flow that the tool belongs to, as the flow has it (it may be a
        link into the run root, and the tool sees it so). A tool without a flow, or whose flow is
        gone, has no working directory: this is a `ToolException`, and each start of the tool
        names an absolute directory (`cwd=`). Xeda never uses its own current directory."""
        flow = self._live_flow()
        if flow is None:
            raise ToolException(
                f"Tool {self.executable!r} runs for no flow, so it has no directory to run in: "
                "give an absolute `cwd=`."
            )
        return flow.run_path

    def _directory(self, cwd: OptionalPath = None) -> Path:
        """Return the absolute directory for one start of the tool: `cwd`, if it is absolute,
        else `cwd` relative to the working directory of the tool, else the working directory."""
        if cwd is not None and os.path.isabs(cwd):
            return Path(cwd)
        base = self.working_directory()
        return base if cwd is None else base / cwd

    def _in_run_directory(self, path: Path) -> bool:
        """Return whether `path` lies in the run directory of the flow of this tool, by its name
        or by where it leads. A file in a probe's scratch directory does not."""
        run_directory = self._run_directory
        if run_directory is None:
            return False
        lexical = Path(os.path.abspath(path))
        flow = self._live_flow()
        return (flow is not None and lexical.is_relative_to(flow.run_path)) or (
            run_directory.named_within(lexical) is not None
        )

    def _read_only_directories(self, cwd: Path) -> List[Path]:
        """Return the directories that a container mounts read-only in this run.

        These are the directories of the design and the directories of the flow's inputs
        (`Flow.input_directories`: the files of its producers, the implicit inputs, and the files
        and directories that its settings name). The tool writes only in the directory it runs in
        (the run directory) and in a mount that the user configured writable. A directory is left
        out when the run directory, a directory of the design, or a configured mount at the same
        path already holds it. A read-only mount inside the run directory would make a part of it
        read-only. `cwd` is the directory the tool runs in."""
        design = [d for d in (self.design_root_, *self.source_dirs_) if d is not None]
        flow = self._live_flow()
        inputs = flow.input_directories() if flow is not None else []
        held = [
            cwd,
            *design,
            # A configured mount holds a directory only when the container path is the host path.
            *(
                Path(host)
                for host, target in (self.docker.mounts if self.docker else {}).items()
                if target.split(":")[0] == host
            ),
        ]
        added: List[Path] = []
        for directory in sorted(inputs, key=lambda d: len(d.parts)):
            if not any(directory.is_relative_to(parent) for parent in held):
                held.append(directory)
                added.append(directory)
        return [*design, *added]

    def execute(
        self,
        executable: str,
        *args: Any,
        env: Optional[Dict[str, Any]] = None,
        stdout: OptionalBoolOrPath = None,
        check: bool = True,
        cwd: Optional[Path] = None,
        highlight_rules: Optional[Dict[str, str]] = None,
        merge_stderr: bool = False,
        timeout: float | None = None,
        tee: Path | None = None,
    ) -> Union[str, None]:
        """Start `executable` with `args` in an explicit directory and return what `run_process`
        returns.

        The directory is `cwd` (a relative one is relative to the working directory of the tool),
        else `working_directory()`. A tool without a flow needs an absolute `cwd`. The process starts there, and a relative `stdout` or `tee`
        file and the files that Xeda writes for the tool are relative to it too."""
        directory = self._directory(cwd)
        if not stdout and self.redirect_stdout:
            stdout = self.redirect_stdout
        stdout = _placed(directory, stdout)
        if tee is not None:
            tee = directory / tee
        args = tuple(list(self.default_args) + list(args))
        if self.console_colors:
            highlight_rules = highlight_rules or self.highlight_rules
        else:
            highlight_rules = None
        if self.docker and self.dockerized:
            # The container sees the physical directory (`Docker.run`), and so do its read-only
            # mounts, which are compared with it.
            container_directory = Path(os.path.realpath(directory))
            return self.docker.run(
                executable,
                *args,
                env=env,
                stdout=stdout,
                check=check,
                read_only=self._read_only_directories(container_directory),
                print_command=self.print_command,
                highlight_rules=highlight_rules,
                merge_stderr=merge_stderr,
                timeout=timeout,
                tee=tee,
                cwd=container_directory,
            )
        if env is not None:
            env = {**os.environ, **env}
        try:
            return run_process(
                executable,
                args,
                env=env,
                stdout=stdout,
                check=check,
                cwd=directory,
                print_command=self.print_command,
                highlight_rules=highlight_rules,
                merge_stderr=merge_stderr,
                timeout=timeout,
                tee=tee,
                terminal=self.pseudo_terminal and self.console_colors,
            )
        except FileNotFoundError as e:
            path = env["PATH"] if env and "PATH" in env else os.environ.get("PATH")
            raise ExecutableNotFound(
                e.filename, self.__class__.__qualname__, path, *e.args
            ) from None

    def run_get_stdout(
        self,
        *args: Any,
        env: Optional[Dict[str, Any]] = None,
        raise_on_error: bool = True,
        merge_stderr: bool = False,
        timeout: float | None = None,
        cwd: Optional[Path] = None,
    ) -> Optional[str]:
        out = self.run(
            *args,
            env=env,
            stdout=True,
            check=raise_on_error,
            merge_stderr=merge_stderr,
            timeout=timeout,
            cwd=cwd,
        )
        if raise_on_error or out is not None:
            assert isinstance(out, str)
        return out

    def probe_stdout(
        self,
        *args: Any,
        env: dict[str, Any] | None = None,
        raise_on_error: bool = True,
        merge_stderr: bool = False,
        timeout: float | None = None,
    ) -> str | None:
        """Run `run_get_stdout` for a query about the tool itself (its version) in a temporary
        working directory, so that nothing stays behind where the flow runs. A flow also asks the
        tool this way for what it sees of the machine (the probes that `openFPGALoader` finds),
        with a `timeout` so that a query that hangs does not hang the launch.

        A flow creates its tools in `init()`, and that probes their version. `init()` runs in the
        run directory, before the launcher checks whether the last run is up to date. Every file
        of a run directory is an output of that run, and some tools write into their working
        directory even for a version (`vivado -version` creates vivado.jou and vivado.log). A
        relative executable path would be looked up from the temporary directory, so a tool is
        named by its name or by an absolute path."""
        with tempfile.TemporaryDirectory(prefix="xeda-probe-") as scratch:
            return self.run_get_stdout(
                *args,
                env=env,
                raise_on_error=raise_on_error,
                merge_stderr=merge_stderr,
                timeout=timeout,
                cwd=Path(scratch),
            )

    def run_stdout_to_file(
        self,
        *args: Any,
        redirect_to: Path,
        env: Optional[Dict[str, Any]] = None,
        cwd: Optional[Path] = None,
    ) -> None:
        self.run(*args, env=env, stdout=redirect_to, cwd=cwd)

    def derive(
        self,
        executable,
        *,
        sibling: bool = False,
        source_name: str | None = None,
        **kwargs,
    ) -> Tool:
        """Derive a command, optionally requiring it beside this installation's executable.

        Ordinary commands retain their own PATH lookup. With `sibling`, native symlinks are
        resolved before locating the related program; container lookup happens inside the
        selected image. `source_name` identifies the canonical suffix of the source executable;
        any prefix before it is retained for the sibling (for example, a `PROGRAM_PREFIX`
        applied to both `yosys` and `yosys-config`). An absent sibling is an error, never another
        installation's helper.
        """
        if sibling:
            executable = self._sibling_executable(executable, source_name=source_name)
        elif source_name is not None:
            raise ToolException("`source_name` can only be used when deriving a sibling tool.")
        # A derived tool changes its Docker command and may later add mounts/environment entries.
        # It must not share those containers with the source tool.
        new_tool = self.model_copy(deep=True, update=kwargs)
        new_tool._run_directory = self._run_directory  # the flow's own, never a copy of it
        new_tool._flow = self._flow
        new_tool.invalidate_cached_properties()
        if "default_args" not in kwargs:
            new_tool.default_args = []
        new_tool.executable = executable
        if "docker" not in kwargs and new_tool.docker:
            new_tool.docker.command = [executable]
            new_tool.docker.invalidate_cached_properties()
        return new_tool

    def _sibling_executable(self, name: str, *, source_name: str | None = None) -> str:
        if Path(name).name != name or name in ("", ".", ".."):
            raise ToolException(f"A sibling executable must be a name, got {name!r}.")
        if source_name is not None and (
            Path(source_name).name != source_name or source_name in ("", ".", "..")
        ):
            raise ToolException(f"A source executable name must be a name, got {source_name!r}.")
        if self.dockerized and self.docker:
            # Docker.run executes Docker.command[0] when configured, regardless of the
            # Tool.executable argument it receives. Resolve the sibling from that same command.
            source_executable = self.docker.command[0] if self.docker.command else self.executable
            # Pass names as positional parameters, never interpolate them into shell code.
            script = (
                'source=$(command -v "$1") || exit 1; '
                'if [ -L "$source" ]; then source=$(readlink -f "$source") || exit 1; fi; '
                'case "$source" in /*) ;; *) source="$PWD/$source" ;; esac; '
                'source_base="${source##*/}"; '
                'prefix=""; '
                'if [ -n "$3" ]; then '
                'case "$source_base" in *"$3") prefix="${source_base%"$3"}" ;; esac; '
                "fi; "
                'helper="${source%/*}/$prefix$2"; '
                '[ -f "$helper" ] && [ -x "$helper" ] || exit 1; '
                'printf "%s\\n" "$helper"'
            )
            try:
                result = self.derive("sh").probe_stdout(
                    "-c", script, "xeda-sibling", source_executable, name, source_name or ""
                )
            except ToolException as error:
                raise ToolException(
                    f"Cannot locate sibling `{name}` beside `{source_executable}` in the container."
                ) from error
            if result and result.strip().startswith("/"):
                return result.strip()
        else:
            source = self.executable_path()
            if source is not None:
                source_base = source.name
                prefix = ""
                if source_name is not None and source_base.endswith(source_name):
                    prefix = source_base[: -len(source_name)]
                helper = source.parent / f"{prefix}{name}"
                if helper.is_file() and os.access(helper, os.X_OK):
                    return str(helper)
        raise ToolException(f"Cannot locate sibling `{name}` beside `{self.executable}`.")
