#!/usr/bin/env python3
"""A fake `uv` for the tests of remote runs. It does only what `bootstrap.sh` calls, and it never
uses the network. It needs nothing but the standard library of Python 3.9.

The fake reads its settings from the file `fake_remote.json` in the parent directory of `$HOME`
(the remote home of the fake `ssh`), and it records its calls in `fake_uv.calls` beside it, as the
other fakes do: one `CALL <n>` line per call, then one `ARG` line per argument, then `ENV` lines.
The `ENV` lines show the variables that a test looks at: `UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR`
and `HTTPS_PROXY`. Every call is recorded, before it does anything.

Settings (all optional):
  python         the program that the Python of an environment runs (default: this Python)
  uv_venv_delay  seconds that `venv` takes
  uv_pip_fail    a message: `pip install` prints it on the standard error and fails

Commands (the global option `--no-config`, and any other option that is not listed, is ignored):
  --version                              print `uv <version>`
  python install <request>               make the Python `<request>` in `$UV_PYTHON_INSTALL_DIR`
  python find [--managed-python] <req>   print its path, or fail with status 2 if it is not there
  venv [--python <path>] <directory>     make `<directory>/bin/python`, a shell script that runs
                                         the Python of the settings with the arguments it gets
  pip install --python <path> -r <file> -c <file>
                                         remember the lines of both files in the environment
  pip freeze --python <path>             print the remembered lines of the environment
"""

import json
import os
import sys
import time

VERSION = "0.12.23"
RECORDED_ENV = ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "HTTPS_PROXY")


def parent_of_home() -> str:
    return os.path.dirname(os.path.abspath(os.environ["HOME"]))


def settings() -> dict:
    try:
        with open(os.path.join(parent_of_home(), "fake_remote.json")) as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}


def record(argv: list) -> None:
    lines = [f"CALL {len(argv)}"] + [f"ARG {a}".replace("\n", "\\n") for a in argv]
    lines += [f"ENV {name}={os.environ[name]}" for name in RECORDED_ENV if name in os.environ]
    log = os.path.join(parent_of_home(), "fake_uv.calls")
    # one write of the whole call: calls of parallel processes do not interleave
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, ("\n".join(lines) + "\n").encode())
    finally:
        os.close(fd)


def fail(message: str, status: int = 2):
    sys.stderr.write(f"error: {message}\n")
    sys.exit(status)


def script_of_python(config: dict) -> str:
    return f'#!/bin/sh\nexec "{config.get("python", sys.executable)}" "$@"\n'


def write_python(path: str, config: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(script_of_python(config))
    os.chmod(path, 0o755)


def option(args: list, name: str):
    """The value after `name`, or None; the pair is removed from `args`."""
    if name in args:
        i = args.index(name)
        value = args[i + 1]
        del args[i : i + 2]
        return value
    return None


def install_dir() -> str:
    return os.environ.get("UV_PYTHON_INSTALL_DIR") or fail("UV_PYTHON_INSTALL_DIR is not set")


def python(args: list, config: dict) -> None:
    if not args:
        fail("python needs a command")
    command, rest = args[0], [a for a in args[1:] if not a.startswith("--")]
    where = os.path.join(install_dir(), rest[0] if rest else "", "bin", "python")
    if command == "install":
        write_python(where, config)
    elif command == "find":
        if not os.path.exists(where):
            fail(f"No interpreter found for {rest[0] if rest else ''}")
        print(where)
    else:
        fail(f"fake uv: unsupported command python {command}")


def venv(args: list, config: dict) -> None:
    option(args, "--python")
    if len(args) != 1:
        fail("venv needs one directory")
    directory = args[0]
    os.makedirs(directory, exist_ok=True)
    time.sleep(float(config.get("uv_venv_delay", 0)))
    write_python(os.path.join(directory, "bin", "python"), config)
    with open(os.path.join(directory, "pyvenv.cfg"), "w") as handle:
        handle.write("home = fake\n")


def environment_of(args: list) -> str:
    path = option(args, "--python") or fail("pip needs --python")
    return os.path.dirname(os.path.dirname(path))


def pip(args: list, config: dict) -> None:
    command, rest = args[0], args[1:]
    environment = environment_of(rest)
    lines_file = os.path.join(environment, "fake-installed.txt")
    if command == "install":
        if config.get("uv_pip_fail"):
            fail(config["uv_pip_fail"], 1)
        lines = []
        for name in ("-r", "-c"):
            with open(option(rest, name) or fail(f"pip install needs {name}")) as handle:
                lines += handle.read().splitlines()
        with open(lines_file, "w") as handle:
            handle.write("\n".join(lines) + "\n")
    elif command == "freeze":
        with open(lines_file) as handle:
            sys.stdout.write(handle.read())
    else:
        fail(f"fake uv: unsupported command pip {command}")


def main(argv: list) -> None:
    record(argv)
    config = settings()
    args = [a for a in argv if a != "--no-config"]
    if args == ["--version"]:
        print(f"uv {VERSION}")
    elif args and args[0] == "python":
        python(args[1:], config)
    elif args and args[0] == "venv":
        venv(args[1:], config)
    elif args and args[0] == "pip":
        pip(args[1:], config)
    else:
        fail(f"fake uv: unsupported arguments {args}")


if __name__ == "__main__":
    main(sys.argv[1:])
