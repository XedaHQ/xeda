from __future__ import annotations
import contextlib
import inspect
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from sys import stderr
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .dataclass import Field, XedaBaseModel, validator
from .utils import cached_property, try_convert
from .flow import Flow

log = logging.getLogger(__name__)

__all__ = [
    "ToolException",
    "NonZeroExitCode",
    "ExecutableNotFound",
    "Docker",
    "Tool",
    "run_process",
]


class ToolException(Exception):
    """Super-class of all tool exceptions"""


class NonZeroExitCode(ToolException):
    def __init__(self, command_args: Any, exit_code: int, *args: object) -> None:
        self.command_args = command_args
        self.exit_code = exit_code
        super().__init__(*args)


class ExecutableNotFound(ToolException):
    def __init__(self, executable: str, tool: str, path: str, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.exec = executable
        self.tool = tool
        self.path = path

    def __str__(self) -> str:
        return f"Executable '{self.exec}' (for {self.tool}) was not found (PATH={self.path})"

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}: {self.__str__()}"


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
        return run_process(
            self.cli,
            cmd,
            env=None,
            stdout=stdout,
            check=check,
            tool_name=self.name,
            print_command=print_command,
        )


def run_process(
    executable: str,
    args: Optional[Sequence[Any]] = None,
    env: Optional[Dict[str, Any]] = None,
    stdout: OptionalBoolOrPath = None,
    check: bool = True,
    cwd: OptionalPath = None,
    tool_name: str = "",
    print_command: bool = False,
) -> Union[None, str]:
    if args is None:
        args = []
    args = [str(a) for a in args]
    if env is not None:
        env = {k: str(v) for k, v in env.items() if v is not None}
    cmd = " ".join(map(lambda x: str(x), [executable, *args]))
    if print_command:
        print("Running `%s`" % cmd)
    else:
        log.debug("Running `%s`", cmd)
    if cwd:
        log.debug("cwd=%s", cwd)
    if stdout and isinstance(stdout, (str, os.PathLike)):
        stdout = Path(stdout)

        def cm_call():
            assert stdout
            return open(stdout, "w")

        cm = cm_call
    else:
        cm = contextlib.nullcontext

    with cm() as f:
        try:
            with subprocess.Popen(
                [executable, *args],
                cwd=cwd,
                shell=False,
                stdout=f if f else subprocess.PIPE if stdout else None,
                bufsize=1,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            ) as proc:
                log.debug("Started %s[%d]", executable, proc.pid)
                try:
                    if stdout:
                        if isinstance(stdout, bool):
                            out, err = proc.communicate(timeout=None)
                            if check and proc.returncode != 0:
                                raise NonZeroExitCode(proc.args, proc.returncode)
                            if err:
                                print(err, file=stderr)
                            return out.strip()
                        else:
                            log.info(
                                "Standard output is redirected to: %s",
                                os.path.abspath(stdout),
                            )
                    proc.wait()
                except KeyboardInterrupt as e:
                    try:
                        log.debug(
                            "Received KeyboardInterrupt! Terminating %s(pid=%s)",
                            executable,
                            proc.pid,
                        )
                        proc.terminate()
                    except OSError as e2:
                        log.warning("Terminate failed: %s", e2)
                    finally:
                        proc.wait()
                        raise e from None
            if check and proc.returncode != 0:
                raise NonZeroExitCode(proc.args, proc.returncode)
        except FileNotFoundError as e:
            path = env["PATH"] if env and "PATH" in env else os.environ.get("PATH", "")
            raise ExecutableNotFound(e.filename, tool_name, path, *e.args) from None
    return None


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


def _run_processes(commands: List[List[Any]], cwd: OptionalPath = None, env=None) -> None:
    """Run a list commands to completion. Raises an exception if any of them did not execute and exit normally"""
    for args in commands:
        args = [str(a) for a in args]
    if env:
        env = {str(k): str(v) for k, v in env.items() if v is not None}
    processes: List[subprocess.Popen] = []
    for cmd in commands:
        log.info("Running `%s`", " ".join(cmd))
        with subprocess.Popen(
            cmd,
            cwd=cwd,
            shell=False,
            bufsize=1,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        ) as proc:
            assert isinstance(proc.args, list)
            log.info("Started %s[%d]", str(proc.args[0]), proc.pid)
            processes.append(proc)

    for p in processes:
        p.wait()

    for p in processes:
        if p.returncode != 0:
            raise NonZeroExitCode(p.args, p.returncode)


class Tool(XedaBaseModel):
    """abstraction for an EDA tool"""

    executable: str
    minimum_version: Union[None, Tuple[Union[int, str], ...]] = None
    default_args: List[str] = []
    version_flag: str = "--version"

    remote: Optional[RemoteSettings] = Field(None)
    docker: Optional[Docker] = Field(None)
    redirect_stdout: Optional[Path] = Field(None, description="Redirect stdout to a file")
    bin_path: Optional[str] = Field(None, description="Path to the tool binary")
    design_root: Optional[Path] = None
    dockerized: bool = False
    print_command: bool = True

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
    def info(self) -> Dict[str, str]:
        return {"executable": self.executable, "version": self.version_str}

    @cached_property
    def version(self) -> Tuple[str, ...]:
        out = self.run_get_stdout(self.version_flag)
        assert isinstance(out, str)
        so = re.split(r"\s+", out)
        version_string = so[1] if len(so) > 1 else so[0] if len(so) > 0 else ""
        return tuple(version_string.split("."))

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

    def _run_remote(
        self,
        *args: Any,
        env: Optional[Dict[str, Any]] = None,
        stdout: OptionalBoolOrPath = None,
        check: bool = True,
        cwd: OptionalPath = None,
    ) -> None:
        # FIXME remote execution is broken
        #
        # if env is not None:
        # env = {**os.environ, **env}
        assert self.remote, "tool.remote settings not available!"

        remote_sshfs_mount_dir = "~/mnt"
        wd = str(cwd) if cwd else os.curdir
        self.remote.junest_mounts[remote_sshfs_mount_dir] = wd
        mount_opts = [f"--bind {k} {v}" for k, v in self.remote.junest_mounts.items()]
        junest_backend_opts = ["-b"] + mount_opts
        executable = self.executable
        if self.remote.exec_path:
            executable = os.path.join(self.remote.exec_path, executable)
        junest_cmd = (
            [self.remote.junest_path] + junest_backend_opts + ["--", executable] + list(*args)
        )

        remote_cmd = junest_cmd
        client_nc_port = 34567
        reverse_sftp_port = 10000

        sshfs_opts = ["directport=10000", "idmap=user", "exec", "compression=yes"]

        sshfs_cmd = ["sshfs", "-o", ",".join(sshfs_opts), f"localhost:{wd}", remote_sshfs_mount_dir]

        ssh_proc = ["ssh", self.remote.hostname, "-p", str(self.remote.port)]

        sshfs_proc = (
            ssh_proc
            + [
                "-R",
                f"{reverse_sftp_port}:localhost:{client_nc_port}",
                "mkdir",
                "-p",
                remote_sshfs_mount_dir,
                "&&",
            ]
            + sshfs_cmd
            + ["&&"]
            + remote_cmd
        )

        ncat_proc = ["ncat", "-l", "-p", f"{client_nc_port}", "-e", "/usr/libexec/sftp-server"]
        _run_processes([sshfs_proc, ncat_proc])

    def run(
        self,
        *args: Any,
        env: Optional[Dict[str, Any]] = None,
        stdout: OptionalBoolOrPath = None,
        check: bool = True,
    ) -> Union[None, str]:
        if env:
            env = {k: str(v) for k, v in env.items() if v is not None}
            env_file = "env.sh"
            with open(env_file, "w") as f:
                f.write("\n".join(f'export {k}="{v}"' for k, v in env.items()))
        return self.execute(self.executable, *args, env=env, stdout=stdout, check=check)

    def execute(
        self,
        executable: str,
        *args: Any,
        env: Optional[Dict[str, Any]] = None,
        stdout: OptionalBoolOrPath = None,
        check: bool = True,
        cwd: Optional[Path] = None,
    ) -> Union[None, str]:
        if not stdout and self.redirect_stdout:
            stdout = self.redirect_stdout
        args = tuple(list(self.default_args) + list(args))
        if self.docker and self.dockerized:
            return self.docker.run(
                executable,
                *args,
                env=env,
                stdout=stdout,
                check=check,
                root_dir=self.design_root,
                print_command=self.print_command,
            )
        if env is not None:
            env = {**os.environ, **env}
        return run_process(
            executable,
            args,
            env=env,
            stdout=stdout,
            check=check,
            cwd=cwd,
            tool_name=self.__class__.__name__,
            print_command=self.print_command,
        )

    def run_get_stdout(self, *args: Any, env: Optional[Dict[str, Any]] = None) -> str:
        out = self.run(*args, env=env, stdout=True)
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
