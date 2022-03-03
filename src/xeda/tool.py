import os
import subprocess
import logging
from abc import ABCMeta
from typing import Any, Callable, Mapping, Optional, Dict, TypeVar, Union
from xmlrpc.client import Boolean
from pydantic import BaseModel, Field, Extra
from pydantic.types import NoneStr
from pathlib import Path
import contextlib

from .flows.design import XedaBaseModel

log = logging.getLogger(__name__)

try:
    nullcontext: Callable = contextlib.nullcontext
except AttributeError:  # Python < 3.7
    def nullcontext_(a=None):
        return contextlib.contextmanager(lambda: (x for x in [a]))()
    nullcontext = nullcontext_


class NonZeroExitCode(Exception):
    def __init__(self, command_args, exit_code: int, *args: object) -> None:
        self.command_args = command_args
        self.exit_code = exit_code
        super().__init__(*args)
    pass


class RemoteToolSettings(BaseModel, extra=Extra.forbid):
    junest_path: str  # FIXME REMOVE
    junest_method: str = 'ns'
    junest_mounts: Optional[Mapping[str, str]] = None
    exec_path: NoneStr = None
    hostname: str
    port: int = 22


class NativeToolSettings(BaseModel, extra=Extra.forbid):
    executable: str


class DockerToolSettings(BaseModel, extra=Extra.forbid):
    executable: NoneStr = None
    image_name: str = Field(description="Docker image name")
    image_tag: NoneStr = Field(None, description="Docker image tag")
    image_registry: NoneStr = Field(None, description="Docker image registry")


ToolSettingsType = TypeVar('ToolSettingsType', bound='Tool.Settings')


class Tool(metaclass=ABCMeta):
    """abstraction for an EDA tool"""

    class Settings(XedaBaseModel, metaclass=ABCMeta, extra=Extra.allow):
        docker: Optional[DockerToolSettings] = None
        remote: Optional[RemoteToolSettings] = None
        native: Optional[NativeToolSettings] = None
        log_stdout: bool = Field(False, description="Log stdout to a file")
        log_stderr: bool = Field(False, description="Log stderr to a file")
        bin_path: str = Field(None, description="Path to the tool binary")
        require_version: NoneStr = Field(
            None, description="Require tool version")
        dockerized: bool = Field(False, description="Run the tool dockerized")
        quiet: bool = False
        verbose: int = 0
        debug: bool = False

    default_native: NativeToolSettings
    default_docker: Optional[DockerToolSettings] = None

    def __init__(self, settings: Settings, run_path):
        self.run_path = run_path
        assert isinstance(settings, self.Settings)
        self.settings = settings
        self._version = None
        self._info: Optional[Dict[str, Any]] = None

    @property
    def info(self):
        if self._info is None:
            self._info = self.get_info()
        return self._info

    def get_info(self):
        log.critical(f"Tool.info is not implemented for {self.__class__.__name__}!")
        return {}

    def get_version(self):
        """return the version of the tool"""
        out = self.run_tool(
            self.default_executable,
            ["--version"],
            stdout=True,
        )
        return out

    @property
    def version(self):
        if self._version is None:
            self._version = self.get_version()

        return self._version

    @property
    def version_major(self):
        return int(self.version.split(".")[0])

    @property
    def version_minor(self):
        return int(self.version.split(".")[1])

    def has_min_version(self, target: str) -> Boolean:
        for tv, sv in zip(target.split("."), self.version.split(".")):
            if sv < tv:
                return False
            if sv > tv:
                return True
        return True

    def _run_process(self, executable, args, env, stdout: Union[bool, str, os.PathLike], check):
        if args:
            args = [str(a) for a in args]
        if env:
            env = {str(k): str(v) for k, v in env.items()}
        log.info(f'Running `{" ".join([executable, *args])}`')
        if stdout and isinstance(stdout, str) or isinstance(stdout, os.PathLike):
            stdout = Path(stdout)
            if not stdout.is_absolute():
                stdout = self.run_path / stdout
            log.info(f"redirecting stdout to {stdout}")
            cm = open(stdout, "w")
        else:
            cm = nullcontext()
        with (cm) as f:
            with subprocess.Popen([executable, *args],
                                  cwd=self.run_path,
                                  shell=False,
                                  stdout=f if f else subprocess.PIPE if stdout else None,
                                  bufsize=1,
                                  universal_newlines=True,
                                  encoding='utf-8',
                                  errors='replace',
                                  env=env
                                  ) as proc:
                log.info(f"Started {executable}[{proc.pid}]")
                if stdout:
                    if stdout == True:
                        out, err = proc.communicate(timeout=None)
                        if check and proc.returncode != 0:
                            raise NonZeroExitCode(proc.args, proc.returncode)
                        return out.strip()  # FIXME
                    else:  # FIXME
                        log.info(f" Standard output is logged to: {stdout}")
                else:
                    proc.wait()
        if check and proc.returncode != 0:
            raise NonZeroExitCode(proc.args, proc.returncode)

    def _run_processes(self, commands):
        # if args:
        #     args = [str(a) for a in args]
        # if env:
        #     env = {str(k): str(v) for k, v in env.items()}
        processes = []
        for cmd in commands:
            log.info(f'Running `{" ".join(cmd)}`')
            proc = subprocess.Popen(cmd,
                                    cwd=self.run_path,
                                    shell=False,
                                    #   stdout=subprocess.PIPE if stdout else None,
                                    bufsize=1,
                                    universal_newlines=True,
                                    encoding='utf-8',
                                    errors='replace',
                                    #   env=env
                                    )
            log.info(f'Started {proc.args[0]}[{proc.pid}]')
            processes.append(proc)

        for p in processes:
            p.wait()

        for p in processes:
            if p.returncode != 0:
                raise Exception(
                    f"Process exited with return code {proc.returncode}")

    def _run_system(self, executable, args, env, stdout, check):
        """Run the tool if locally installed on the system and available on the current user's PATH"""
        if env is not None:
            env = {**os.environ, **env}
        return self._run_process(executable, args, env, stdout, check)

    def _run_remote(self, executable, args, env, stdout, check):
        # FIXME: env
        # if env is not None:
        # env = {**os.environ, **env}

        remote_sshfs_mount_dir = '~/mnt'
        self.settings.remote.junest_mounts[remote_sshfs_mount_dir] = str(
            self.run_path)
        mount_opts = [f"--bind {k} {v}" for k,
                      v in self.settings.remote.junest_mounts.items()]
        junest_backend_opts = ["-b"] + mount_opts
        junest_cmd = [self.settings.remote.junest_path] + junest_backend_opts + \
            ["--", self.settings.remote.exec_path] + args

        remote_cmd = junest_cmd
        client_nc_port = 34567
        reverse_sftp_port = 10000

        sshfs_opts = ["directport=10000", "idmap=user",
                      "exec", "compression=yes"]

        sshfs_cmd = ['sshfs', '-o',
                     ','.join(sshfs_opts), f'localhost:{self.run_path}', remote_sshfs_mount_dir]

        ssh_proc = ['ssh', self.settings.remote.hostname, "-p",
                    str(self.settings.remote.port)]

        sshfs_proc = ssh_proc + ["-R", f"{reverse_sftp_port}:localhost:{client_nc_port}",
                                       "mkdir", "-p", remote_sshfs_mount_dir, "&&"] + sshfs_cmd + ["&&"] + remote_cmd

        ncat_proc = ["ncat", "-l", "-p",
                     f"{client_nc_port}", "-e", "/usr/libexec/sftp-server"]

        self._run_processes([sshfs_proc, ncat_proc])

    def _run_docker(self, docker, executable, args, env, stdout, check):
        """Run the tool from a docker container"""
        wd = self.run_path
        cwd = Path.cwd()
        docker_args = [f"--rm",  "--interactive", "--tty",
                       f"--workdir={wd}", f"--volume={cwd}:{cwd}", f"--volume={wd}:{wd}"]
        if env:
            env_file = wd / f"{executable}_docker.env"
            with open(env_file, "w") as f:
                f.write(f"\n".join(f"{k}={v}" for k, v in env.items()))
            docker_args.extend(["--env-file", str(env_file)])

        return self._run_process("docker", [
            "run", *docker_args, docker.image_name, executable, *args], env=None, stdout=stdout, check=check)

    # FIXME
    def run_tool(self, executable, args, env=None, stdout=None, check=True):
        if self.settings.remote:
            return self._run_remote(executable, args, env, stdout, check)
        if self.settings.dockerized and self.settings.docker:
            return self._run_docker(self.settings.docker, executable, args, env, stdout, check)
        else:
            return self._run_system(executable, args, env, stdout, check)
