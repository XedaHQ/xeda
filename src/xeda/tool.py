from __future__ import annotations
import contextlib
import inspect
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from sys import stderr
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .dataclass import Field, XedaBaseModel, validator
from .utils import cached_property, try_convert, ToolException, NonZeroExitCode, ExecutableNotFound
from .flow import Flow
from .proc_utils import run_process

log = logging.getLogger(__name__)

__all__ = [
    "ToolException",
    "NonZeroExitCode",
    "ExecutableNotFound",
    "Docker",
    "Tool",
    "run_process",
]


class RemoteSettings(XedaBaseModel):
    enabled: bool = False
    junest_path: str  # FIXME REMOVE
    junest_method: str = "ns"
    junest_mounts: Dict[str, str] = {}
    exec_path: Optional[str] = None
    hostname: str
    port: int = 22


OptionalPath = Union[None, str, os.PathLike]
OptionalBoolOrPath = Union[None, bool, str, os.PathLike]


class Docker(XedaBaseModel):
    image: str = Field(description="Docker image name")
    command: List[str] = []
    platform: Optional[str] = Field(
        None,
        description="Set platform (e.g. 'linux/amd64'), if server is multi-platform capable",
    )
    tag: Optional[str] = Field("latest", description="Docker image tag")
    registry: Optional[str] = Field(None, description="Docker image registry")
    privileged: bool = True
    fix_cpuinfo: bool = False
    cli: str = "docker"
    mounts: Dict[str, str] = {}

    # NOTE only works for Linux containers
    @cached_property
    def cpuinfo(self) -> Optional[List[List[str]]]:
        try:
            ret = self.run("cat", "/proc/cpuinfo", stdout=True)
        except:  # noqa
            ret = None
        if ret is not None:
            return [x.split("\n") for x in re.split(r"\n\s*\n", ret, re.MULTILINE)]
        return None

    @cached_property
    def nproc(self) -> int:
        return len(self.cpuinfo) if self.cpuinfo else 1

    @cached_property
    def name(self) -> str:
        return self.command[0].rsplit("/")[0] if self.command else "???"

    def run(
        self,
        executable,
        *args: Any,
        env: Optional[Dict[str, Any]] = None,
        stdout: OptionalBoolOrPath = None,
        check: bool = True,
        root_dir: OptionalPath = None,
        print_command: bool = True,
        highlight_rules: Optional[Dict[str, str]] = None,
    ) -> Union[None, str]:
        """Run the tool from a docker container"""
        if self.fix_cpuinfo and self.cpuinfo:
            cpuinfo_file = Path(".cpuinfo").resolve()
            with open(cpuinfo_file, "w") as f:
                for proc in self.cpuinfo:
                    for line in proc:
                        assert isinstance(line, str)
                        if line.startswith("Features"):
                            line += " sse sse2"
                        f.write(line + "\n")
                    f.write("\n")
            self.mounts[str(cpuinfo_file)] = "/proc/cpuinfo"

        cwd = Path.cwd()
        docker_args = [
            "--rm",
            f"--workdir={cwd}",
        ]

        if self.privileged:
            docker_args.append("--privileged")
        self.mounts[str(cwd)] = str(cwd)
        if root_dir:
            self.mounts[str(root_dir)] = str(root_dir)
        if not stdout and sys.stdout.isatty():
            docker_args += ["--tty", "--interactive"]
        if self.platform:
            docker_args += ["--platform", self.platform]
        selinux_perm = True
        cap = ":z" if selinux_perm else ""
        for k, v in self.mounts.items():
            docker_args.append(f"--volume={k}:{v}{cap}")
        if env:
            env_file = cwd / f".{self.name}_docker.env"
            with open(env_file, "w") as f:
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
        cmd = ["run", *docker_args, image, *command, *args]
        try:
            return run_process(
                self.cli,
                cmd,
                env=None,
                stdout=stdout,
                check=check,
                print_command=print_command,
                highlight_rules=highlight_rules,
            )
        except FileNotFoundError as e:
            path = env["PATH"] if env and "PATH" in env else os.environ.get("PATH", "")
            raise ExecutableNotFound(e.filename, self.__class__.__qualname__, path, *e.args) from None


def fake_cpu_info(file=".xeda_cpuinfo", ncores=4):
    with open(file, "w") as f:
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
    """abstraction for an EDA tool"""

    executable: str
    minimum_version: Union[None, Tuple[Union[int, str], ...]] = None
    default_args: List[str] = []
    version_flag: List[str] = ["--version"]
    version_regexps: List[Union[re.Pattern[str], str]] = [VERSION_REGEXP1, VERSION_REGEXP2]

    remote: Optional[RemoteSettings] = Field(None)
    docker: Optional[Docker] = Field(None)
    redirect_stdout: Optional[Path] = Field(None, description="Redirect stdout to a file")
    design_root: Optional[Path] = None
    dockerized: bool = False
    print_command: bool = True
    highlight_rules: Optional[Dict[str, str]] = None

    @validator("version_flag", pre=True, always=True)
    def validate_version_flag(cls, value, values):
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

        super().__init__(**kwargs)

        if flow is not None:
            self.design_root = flow.design_root
            log.debug("flow.settings.dockerized=%s", flow.settings.dockerized)
            self.dockerized = flow.settings.dockerized
            if flow.settings.docker:
                if self.docker:
                    self.docker.image = flow.settings.docker
                else:
                    self.docker = Docker(image=flow.settings.docker)  # type: ignore
            self.print_command = flow.settings.print_commands
        if self.design_root and self.docker:
            self.docker.mounts[str(self.design_root)] = str(self.design_root)

        if self.minimum_version and not self.version_gte(*self.minimum_version):
            log.error(
                "%s version %s is required. Found version: %s",
                self.executable,
                ".".join(str(i) for i in self.minimum_version),
                self.version_str,
            )
            raise ToolException("Minimum version not met")
        if flow is not None:
            if self.info not in flow.results.tools:
                flow.results.tools.append(self.info)

    @validator("docker", pre=True, always=True)
    def validate_docker(cls, value, values):
        default_args = values.get("default_args", [])
        command = [values.get("executable"), *default_args]
        if isinstance(value, dict):
            value = Docker(**value)
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
        except:  # noqa
            version = None
        return {"executable": self.executable, "version": version}

    def _get_version_output(self, *version_flags) -> Optional[str]:
        if not version_flags:
            version_flags = tuple(self.version_flag)
        try:
            return self.run_get_stdout(*version_flags)
        except:  # noqa
            return None

    @cached_property
    def version_output(self) -> Optional[str]:
        return self._get_version_output()

    def process_version_output(self, out: Optional[str]) -> Tuple[str, ...]:
        if not out:
            return ()
        assert isinstance(out, str)
        lines = [line for line in (line.strip() for line in out.splitlines(keepends=False)) if line]
        if not lines:
            return ()
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
            try:
                n = try_convert(self.execute("nproc", stdout=True), int)
            except:  # noqa
                n = None
            assert self.docker
            return n or self.docker.nproc
        else:
            return os.cpu_count() or 1

    @staticmethod
    def _version_is_gte(
        tool_version: Tuple[str, ...], required_version: Tuple[Union[int, str], ...]
    ) -> bool:
        """check if `tool_version` is greater than or equal to `required_version`"""
        log.debug(f"[gte] {tool_version}  ?  {required_version}")
        for tool_part, req_part in zip(tool_version, required_version):
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
        """Tool version is greater than or equal to version specified in args"""
        return self._version_is_gte(self.version, args)

    def executable_path(self) -> Path | None:
        """Return the absolute path if the tool executable is in the PATH"""
        if (
            os.path.isabs(self.executable)
            and os.path.exists(self.executable)
            and os.access(self.executable, os.X_OK)
        ):
            return Path(self.executable).resolve()
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
    ) -> Union[None, str]:
        if env:
            env = {k: str(v) for k, v in env.items() if v is not None}
            env_file = "env.sh"
            with open(env_file, "w") as f:
                f.write("\n".join(f'export {k}="{v}"' for k, v in env.items()))
        return self.execute(
            self.executable, *args, env=env, stdout=stdout, check=check, highlight_rules=highlight_rules
        )

    def execute(
        self,
        executable: str,
        *args: Any,
        env: Optional[Dict[str, Any]] = None,
        stdout: OptionalBoolOrPath = None,
        check: bool = True,
        cwd: Optional[Path] = None,
        highlight_rules: Optional[Dict[str, str]] = None,
    ) -> Union[None, str]:
        if not stdout and self.redirect_stdout:
            stdout = self.redirect_stdout
        args = tuple(list(self.default_args) + list(args))
        highlight_rules = highlight_rules or self.highlight_rules
        if self.docker and self.dockerized:
            return self.docker.run(
                executable,
                *args,
                env=env,
                stdout=stdout,
                check=check,
                root_dir=self.design_root,
                print_command=self.print_command,
                highlight_rules=highlight_rules,
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
                cwd=cwd,
                print_command=self.print_command,
                highlight_rules=highlight_rules,
            )
        except FileNotFoundError as e:
            path = env["PATH"] if env and "PATH" in env else os.environ.get("PATH")
            raise ExecutableNotFound(e.filename, self.__class__.__qualname__, path, *e.args) from None

    def run_get_stdout(
        self, *args: Any, env: Optional[Dict[str, Any]] = None, raise_on_error: bool = True
    ) -> Optional[str]:
        out = self.run(*args, env=env, stdout=True, check=raise_on_error)
        if raise_on_error or out is not None:
            assert isinstance(out, str)
        return out

    def run_stdout_to_file(
        self,
        *args: Any,
        redirect_to: Path,
        env: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.run(*args, env=env, stdout=redirect_to)

    def derive(self, executable, **kwargs) -> "Tool":
        new_tool = self.copy(update=kwargs)
        new_tool.invalidate_cached_properties()
        if "default_args" not in kwargs:
            new_tool.default_args = []
        new_tool.executable = executable
        if "docker" not in kwargs and new_tool.docker:
            new_tool.docker.command = [executable]
            new_tool.docker.invalidate_cached_properties()
        return new_tool
