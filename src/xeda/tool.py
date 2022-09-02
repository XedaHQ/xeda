import contextlib
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from sys import stderr
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .dataclass import Field, XedaBaseModeAllowExtra, XedaBaseModel, validator
from .utils import cached_property, unique

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
    def __init__(
        self, executable: str, tool: str, path: str, *args: Any, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.exec = executable
        self.tool = tool
        self.path = path


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
    enabled: bool = False
    command: List[str] = []
    platform: Optional[str]
    image: str = Field(description="Docker image name")
    tag: str = Field("latest", description="Docker image tag")
    registry: Optional[str] = Field(None, description="Docker image registry")
    mounts: Dict[str, str] = {}

    # TODO this is only for a Linux container
    @cached_property
    def cpuinfo(self) -> Optional[List[List[str]]]:
        try:
            ret = self._run_docker("cat", "/proc/cpuinfo", stdout=True)
        except Exception:
            return None
        assert ret is not None
        return [x.split("\n") for x in re.split(r"\n\s*\n", ret, re.MULTILINE)]

    @property
    def nproc(self) -> Optional[int]:
        return len(self.cpuinfo) if self.cpuinfo else 1

    @property
    def name(self) -> str:
        return self.command[0].split("/")[0] if self.command else "xeda_tool"

    def run(
        self,
        *args: Any,
        env: Optional[Dict[str, Any]] = None,
        stdout: OptionalBoolOrPath = None,
        check: bool = True,
        root_dir: OptionalPath = None,
    ) -> Union[None, str]:
        """Run the tool from a docker container"""
        if self.cpuinfo:
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
        return self._run_docker(
            *self.command, *args, env=env, stdout=stdout, check=check, root_dir=root_dir
        )

    def _run_docker(
        self,
        *args: Any,
        env: Optional[Dict[str, Any]] = None,
        stdout: OptionalBoolOrPath = None,
        check: bool = True,
        root_dir: OptionalPath = None,
    ) -> Union[None, str]:
        cwd = Path.cwd()
        docker_args = [
            "--rm",
            f"--workdir={cwd}",
        ]
        self.mounts[str(cwd)] = str(cwd)
        if root_dir:
            self.mounts[str(root_dir)] = str(root_dir)
        if not stdout and sys.stdout.isatty():
            docker_args += ["--tty", "--interactive"]
        if self.platform:
            docker_args += ["--platform", self.platform]
        for k, v in self.mounts.items():
            docker_args.append(f"--volume={k}:{v}")
        if env:
            env_file = cwd / f".{self.name}_docker.env"
            with open(env_file, "w") as f:
                f.write("\n".join(f"{k}={v}" for k, v in env.items()))
            docker_args.extend(["--env-file", str(env_file)])
        return run_process(
            "docker",
            ["run", *docker_args, f"{self.image}:{self.tag}", *args],
            env=None,
            stdout=stdout,
            check=check,
            tool_name=self.name,
        )


def run_process(
    executable: str,
    args: Optional[Sequence[Any]] = None,
    env: Optional[Dict[str, Any]] = None,
    stdout: OptionalBoolOrPath = None,
    check: bool = True,
    cwd: OptionalPath = None,
    tool_name: str = "",
) -> Union[None, str]:
    if args is None:
        args = []
    args = [str(a) for a in args]
    if env is not None:
        env = {k: str(v) for k, v in env.items()}
    log.info("Running `%s`", " ".join([executable, *args]))
    if cwd:
        log.info("cwd=%s", cwd)
    if stdout and isinstance(stdout, (str, os.PathLike)):
        stdout = Path(stdout)
        log.info("redirecting stdout to %s", stdout)

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
                log.info("Started %s[%d]", executable, proc.pid)
                if stdout:
                    if isinstance(stdout, bool):
                        out, err = proc.communicate(timeout=None)
                        if check and proc.returncode != 0:
                            raise NonZeroExitCode(proc.args, proc.returncode)
                        if err:
                            print(err, file=stderr)
                        return out.strip()
                    log.info("Standard output is logged to: %s", stdout)
                else:
                    proc.wait()
        except FileNotFoundError as e:
            path = env["PATH"] if env and "PATH" in env else os.environ.get("PATH", "")
            raise ExecutableNotFound(e.filename, tool_name, path, *e.args) from None
    if check and proc.returncode != 0:
        raise NonZeroExitCode(proc.args, proc.returncode)
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


def _run_processes(commands: List[List[str]], cwd: OptionalPath = None) -> None:
    """Run a list commands to completion. Throws if any of them did not execute and exit normally"""
    # if args:
    #     args = [str(a) for a in args]
    # if env:
    #     env = {str(k): str(v) for k, v in env.items()}
    processes = []
    for cmd in commands:
        log.info("Running `%s`", " ".join(cmd))
        with subprocess.Popen(
            cmd,
            cwd=cwd,
            shell=False,
            #   stdout=subprocess.PIPE if stdout else None,
            bufsize=1,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            #   env=env
        ) as proc:
            assert isinstance(proc.args, list)
            log.info("Started %s[%d]", str(proc.args[0]), proc.pid)
            processes.append(proc)

    for p in processes:
        p.wait()

    for p in processes:
        if p.returncode != 0:
            raise Exception(f"Process exited with return code {p.returncode}")


class Tool(XedaBaseModeAllowExtra):
    """abstraction for an EDA tool"""

    executable: str
    minimum_version: Union[None, Tuple[Union[int, str], ...]] = None
    default_args: Optional[List[str]] = None

    remote: Optional[RemoteSettings] = Field(None, hidden_from_schema=True)
    docker: Optional[Docker] = Field(None, hidden_from_schema=True)
    log_stdout: bool = Field(
        False, description="Log stdout to a file", hidden_from_schema=True
    )
    log_stderr: bool = Field(
        False, description="Log stderr to a file", hidden_from_schema=True
    )
    bin_path: str = Field(
        None, description="Path to the tool binary", hidden_from_schema=True
    )
    design_root: Optional[Path] = None

    def __init__(self, executable: Optional[str] = None, **kwargs):
        if executable:
            assert "executable" not in kwargs, "executable specified twice"
            kwargs["executable"] = executable
        super().__init__(**kwargs)

    @validator("docker", pre=True, always=True)
    def validate_docker(cls, value, values):
        if value and not value.command:
            value.command = [values.get("executable")]
        return value

    @cached_property
    def info(self) -> Dict[str, str]:
        return {"version": ".".join(self.version)}

    @cached_property
    def _version(self) -> Tuple[str, ...]:
        out = self.run_get_stdout(
            "--version",
        )
        assert isinstance(out, str)
        so = re.split(r"\s+", out)
        version_string = so[1] if len(so) > 1 else so[0] if len(so) > 0 else ""
        return tuple(version_string.split("."))

    @property
    def version(self) -> Tuple[str, ...]:
        return self._version

    @staticmethod
    def _version_is_gte(v1: Tuple[str, ...], v2: Tuple[Union[int, str], ...]) -> bool:
        """version v1 is greater than or equal to v2"""
        for tv_s, sv_s in zip(v2, v1):
            try:
                tv = int(tv_s)
            except ValueError:
                tv = -1
            try:
                sv = int(sv_s)
            except ValueError:
                sv = -1
            if sv < tv:
                return False
            if sv > tv:
                return True
        return True

    def version_gte(self, *args: Union[int, str]) -> bool:
        """Tool version is greater than or equal to version specified in args"""
        return self._version_is_gte(self.version, args)

    def _run_process(
        self,
        args: Optional[Sequence[Any]] = None,
        env: Optional[Dict[str, Any]] = None,
        stdout: OptionalBoolOrPath = None,
        check: bool = True,
        cwd: OptionalPath = None,
    ):
        return run_process(
            self.executable,
            args,
            env=env,
            stdout=stdout,
            check=check,
            cwd=cwd,
            tool_name=self.__class__.__name__,
        )

    def _run_system(
        self,
        *args: Any,
        stdout: OptionalBoolOrPath = None,
        env: Optional[Dict[str, Any]] = None,
        cwd: OptionalPath = None,
        check: bool = True,
    ) -> Union[None, str]:
        """Run the tool if locally installed on the system and available on the current user's PATH"""
        if env is not None:
            env = {**os.environ, **env}
        return self._run_process(args, env=env, stdout=stdout, check=check, cwd=cwd)

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
            [self.remote.junest_path]
            + junest_backend_opts
            + ["--", executable]
            + list(*args)
        )

        remote_cmd = junest_cmd
        client_nc_port = 34567
        reverse_sftp_port = 10000

        sshfs_opts = ["directport=10000", "idmap=user", "exec", "compression=yes"]

        sshfs_cmd = [
            "sshfs",
            "-o",
            ",".join(sshfs_opts),
            f"localhost:{wd}",
            remote_sshfs_mount_dir,
        ]

        ssh_proc = [
            "ssh",
            self.remote.hostname,
            "-p",
            str(self.remote.port),
        ]

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

        ncat_proc = [
            "ncat",
            "-l",
            "-p",
            f"{client_nc_port}",
            "-e",
            "/usr/libexec/sftp-server",
        ]
        _run_processes([sshfs_proc, ncat_proc])

    def _run(
        self,
        *args: Any,
        env: Optional[Dict[str, Any]] = None,
        stdout: OptionalBoolOrPath = None,
        check: bool = True,
    ) -> Union[None, str]:
        if self.default_args:
            args = tuple(unique(self.default_args + list(args)))
        if self.remote and self.remote.enabled:
            self._run_remote(*args, env=env, stdout=stdout, check=check)
            return None
        if self.docker and self.docker.enabled:
            return self.docker.run(
                *args, env=env, stdout=stdout, check=check, root_dir=self.design_root
            )
        return self._run_system(*args, env=env, stdout=stdout, check=check)

    def run(self, *args: Any, env: Optional[Dict[str, Any]] = None) -> None:
        self._run(*args, env=env, stdout=None)

    def run_get_stdout(self, *args: Any, env: Optional[Dict[str, Any]] = None) -> str:
        out = self._run(*args, env=env, stdout=True)
        assert isinstance(out, str)
        return out

    def run_stdout_to_file(
        self,
        *args: Any,
        stdout: OptionalBoolOrPath,
        env: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._run(*args, env=env, stdout=stdout)
