import contextlib
import errno
import os
import logging
from pathlib import Path
import pty
import re
import subprocess
import signal
import select
import sys
from typing import Any, Dict, List, Optional, Sequence, Union

import colorama

from .utils import NonZeroExitCode, ExecutableNotFound
from .console import console

log = logging.getLogger(__name__)


def proc_output(is_stderr: bool, line):
    print(
        f"{'[E] ' if is_stderr else ''}{line}", end="", file=sys.stderr if is_stderr else sys.stdout
    )


def run_process(
    executable: str,
    args: Optional[Sequence[Any]] = None,
    env: Optional[Dict[str, Any]] = None,
    stdout: Union[None, bool, str, os.PathLike] = None,
    check: bool = True,
    cwd: Union[None, str, os.PathLike] = None,
    print_command: bool = False,
    highlight_rules: Optional[Dict[str, str]] = None,
) -> Union[None, str]:
    if args is None:
        args = []
    args = [str(a) for a in args]
    if env is not None:
        env = {k: str(v) for k, v in env.items() if v is not None}
    command: List[str] = [str(c) for c in (executable, *args)]
    cmd_str = " ".join(map(lambda x: str(x), command))
    if print_command:
        print("Running `%s`" % cmd_str)
    else:
        log.debug("Running `%s`", cmd_str)
    if cwd:
        log.debug("cwd=%s", cwd)
    if highlight_rules:
        assert stdout is None, "stdout redirection is not supported with filter_rules"
        with subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            env=env,
            cwd=cwd,
            universal_newlines=True,
            bufsize=1,
        ) as proc:
            assert proc.stdout is not None, f"Popen for '{cmd_str}' failed: stdout is None!"

            with open(proc.stdout.fileno(), errors="ignore", closefd=False) as proc_stdout:
                for line in proc_stdout:
                    for pattern, subs in highlight_rules.items():
                        line, matches = re.subn(pattern, subs + colorama.Style.RESET_ALL, line, count=1)
                        if matches > 0:
                            break
                    print(line, end="\r")
            ret = proc.wait()
            if check and ret != 0:
                raise NonZeroExitCode(command, ret)
            return None
    if False and not stdout:
        for is_stderr, line in run_capture_pty(command, env=env, cwd=cwd, check=check):
            proc_output(is_stderr, line)
        return None
    if stdout and isinstance(stdout, (str, os.PathLike)):
        stdout = Path(stdout)

        def cm_call():
            assert stdout
            return open(stdout, "w")

        cm = cm_call
    else:
        cm = contextlib.nullcontext

    with cm() as f:
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
                            print(err, file=sys.stderr)
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

    return None


def _terminate_process(process):
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        process.wait(10)
    if process.poll() is None:
        process.terminate()
        process.wait(100)
        process.kill()


def _subprocess_tty(command, env, cwd, check):
    """`subprocess.Popen` yielding stdout lines acting as a TTY"""
    timeout = None
    mo, so = pty.openpty()  # provide tty to enable line-buffering
    me, se = pty.openpty()
    readable = [mo, me]
    data = None
    try:
        process = subprocess.Popen(
            command, stdout=so, stderr=se, bufsize=1, close_fds=True, env=env, cwd=cwd
        )
    except FileNotFoundError:
        path = env["PATH"] if env and "PATH" in env else os.environ.get("PATH")
        raise ExecutableNotFound(command[0], path=path)
    for fd in [so, se]:
        os.close(fd)
    try:
        while readable:
            ready, _, _ = select.select(readable, [], [], timeout)
            for fd in ready:
                try:
                    data = os.read(fd, 64)
                except OSError as e:
                    # EIO means EOF on some systems
                    if e.errno != errno.EIO:
                        raise
                    data = None
                if data:
                    yield (fd == me, data)
                else:
                    readable.remove(fd)
    except KeyboardInterrupt:
        _terminate_process(process)
        raise
    finally:
        _terminate_process(process)
        for fd in [mo, me]:
            os.close(fd)
    if check and process.returncode != 0:
        raise NonZeroExitCode(process.args, process.returncode)


def run_capture_pty(command, env=None, cwd=None, check=True, encoding="utf-8"):
    remainder = ""
    err_remainder = ""
    for is_stderr, data in _subprocess_tty(command, env, cwd, check=check):
        if not data:
            break
        data_str = data.decode(encoding)
        if is_stderr:
            if err_remainder:
                data_str = err_remainder + data_str
                err_remainder = ""
        elif remainder:
            data_str = remainder + data_str
            remainder = ""
        # spl = re.split(r"\r?\n", data_str) #
        spl = data_str.splitlines(keepends=True)
        if spl and not spl[-1].endswith(os.linesep) and not spl[-1].endswith("\n"):
            if is_stderr:
                err_remainder = spl[0]
            else:
                remainder = spl[0]
            spl = spl[1:]
        for line in spl:
            yield (is_stderr, line + os.linesep)
    if remainder:
        yield (False, remainder + os.linesep)
    if err_remainder:
        yield (True, err_remainder + os.linesep)
    return None
