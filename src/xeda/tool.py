import contextlib
import logging
import os
import re
import subprocess
from pathlib import Path
from sys import stderr
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from .dataclass import Field, XedaBaseModel, XedaBaseModeAllowExtra
from .utils import unique, cached_property
from .dataclass import validator

log = logging.getLogger(__name__)

try:
    nullcontext: Callable[..., Any] = contextlib.nullcontext
except AttributeError:  # Python < 3.7

    def nullcontext_(a=None):  # type: ignore
        return contextlib.contextmanager(lambda: (x for x in [a]))()

    nullcontext = nullcontext_


class NonZeroExitCode(Exception):
    def __init__(self, command_args: Any, exit_code: int, *args: object) -> None:
        self.command_args = command_args
        self.exit_code = exit_code
        super().__init__(*args)


class ExecutableNotFound(Exception):
    def __init__(
        self, exec: str, tool: str, path: str, *args: Any, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.exec = exec
        self.tool = tool
        self.path = path


class RemoteToolSettings(XedaBaseModel):
    junest_path: str  # FIXME REMOVE
    junest_method: str = "ns"
    junest_mounts: Dict[str, str] = {}
    exec_path: Optional[str] = None
    hostname: str
    port: int = 22


class DockerToolSettings(XedaBaseModel):
    executable: Optional[str] = None
    image_name: str = Field(description="Docker image name")
    image_tag: Optional[str] = Field(None, description="Docker image tag")
    image_registry: Optional[str] = Field(None, description="Docker image registry")


OptionalPath = Union[None, str, os.PathLike[Any]]
OptionalBoolOrPath = Union[None, bool, str, os.PathLike[Any]]


class Tool(XedaBaseModeAllowExtra):
    """abstraction for an EDA tool"""

    executable: str
    minimum_version: Union[None, Tuple[Union[int, str], ...]] = None
    default_args: Optional[List[str]] = None

    remote: Optional[RemoteToolSettings] = Field(None, hidden_from_schema=True)
    docker: Optional[DockerToolSettings] = Field(None, hidden_from_schema=True)
    log_stdout: bool = Field(
        False, description="Log stdout to a file", hidden_from_schema=True
    )
    log_stderr: bool = Field(
        False, description="Log stderr to a file", hidden_from_schema=True
    )
    bin_path: str = Field(
        None, description="Path to the tool binary", hidden_from_schema=True
    )
    dockerized: bool = Field(
        False, description="Run the tool dockerized", hidden_from_schema=True
    )

    @cached_property
    def _info(self) -> Dict[str, str]:
        log.warning(f"Tool.info is not implemented for {self.__class__.__name__}!")
        return {}

    @property  # pydantic can't handle cached_property as private
    def info(self) -> Dict[str, str]:
        inf = self._info
        inf["version"] = ".".join(self.version)
        return inf

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
        return self._version_is_gte(self._version, args)

    def _run_process(
        self,
        executable: str,
        args: Optional[Sequence[Any]] = None,
        env: Optional[Dict[str, Any]] = None,
        stdout: OptionalBoolOrPath = None,
        check: bool = True,
        cwd: OptionalPath = None,
    ) -> Union[None, str]:
        if args is None:
            args = []
        args = [str(a) for a in args]
        if env is not None:
            env = {k: str(v) for k, v in env.items()}
        log.info(f'Running `{" ".join([executable, *args])}`')
        if cwd:
            log.info("cwd=%s", cwd)
        if stdout and isinstance(stdout, (str, os.PathLike)):
            stdout = Path(stdout)
            log.info(f"redirecting stdout to {stdout}")
            cm = open(stdout, "w")
        else:
            cm = nullcontext()
        with (cm) as f:
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
                    log.info(f"Started {executable}[{proc.pid}]")
                    if stdout:
                        if isinstance(stdout, bool):
                            out, err = proc.communicate(timeout=None)
                            if check and proc.returncode != 0:
                                raise NonZeroExitCode(proc.args, proc.returncode)
                            print(err, file=stderr)
                            return out.strip()
                        else:  # FIXME
                            log.info("Standard output is logged to: %s", stdout)
                    else:
                        proc.wait()
            except FileNotFoundError as e:
                path = (
                    env["PATH"] if env and "PATH" in env else os.environ.get("PATH", "")
                )
                raise ExecutableNotFound(
                    e.filename, self.__class__.__name__, path, *e.args
                ) from None
        if check and proc.returncode != 0:
            raise NonZeroExitCode(proc.args, proc.returncode)
        return None

    def _run_processes(
        self, commands: List[List[str]], cwd: OptionalPath = None
    ) -> Union[None, str]:
        # if args:
        #     args = [str(a) for a in args]
        # if env:
        #     env = {str(k): str(v) for k, v in env.items()}
        processes = []
        for cmd in commands:
            log.info("Running `%s`", " ".join(cmd))
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                shell=False,
                #   stdout=subprocess.PIPE if stdout else None,
                bufsize=1,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
                #   env=env
            )
            assert isinstance(proc.args, list)
            log.info("Started %s[%d]", str(proc.args[0]), proc.pid)
            processes.append(proc)

        for p in processes:
            p.wait()

        for p in processes:
            if p.returncode != 0:
                raise Exception(f"Process exited with return code {p.returncode}")
        return None

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
        return self._run_process(
            self.executable, args, env=env, stdout=stdout, check=check, cwd=cwd
        )

    def _run_remote(
        self,
        *args: Any,
        env: Optional[Dict[str, Any]] = None,
        stdout: OptionalBoolOrPath = None,
        check: bool = True,
        cwd: OptionalPath = None,
    ) -> Union[None, str]:
        # FIXME NOT WORKING
        # FIXME: env
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
        return self._run_processes([sshfs_proc, ncat_proc])

    def _run_docker(
        self,
        docker: DockerToolSettings,
        *args: Any,
        env: Optional[Dict[str, Any]] = None,
        stdout: OptionalBoolOrPath = None,
        check: bool = True,
        root_dir: OptionalPath = None,
    ) -> Union[None, str]:
        """Run the tool from a docker container"""
        cwd = Path.cwd()
        wd = root_dir if root_dir else cwd
        if not isinstance(wd, Path):
            wd = Path(wd)
        docker_args = [
            f"--rm",
            "--interactive",
            "--tty",
            f"--workdir={wd}",
            f"--volume={wd}:{wd}",
        ]
        if root_dir:
            docker_args.append(f"--volume={cwd}:{cwd}")
        if env:
            env_file = wd / f"{self.executable}_docker.env"
            with open(env_file, "w") as f:
                f.write(f"\n".join(f"{k}={v}" for k, v in env.items()))
            docker_args.extend(["--env-file", str(env_file)])

        return self._run_process(
            "docker",
            ["run", *docker_args, docker.image_name, self.executable, *args],
            env=None,
            stdout=stdout,
            check=check,
        )

    def _run(
        self,
        *args: Any,
        env: Optional[Dict[str, Any]] = None,
        stdout: OptionalBoolOrPath = None,
        check: bool = True,
    ) -> Union[None, str]:
        if self.default_args:
            args = tuple(unique(self.default_args + list(args)))
        if self.remote:
            return self._run_remote(*args, env=env, stdout=stdout, check=check)
        if self.dockerized and self.docker:
            return self._run_docker(
                self.docker, *args, env=env, stdout=stdout, check=check
            )
        else:
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
