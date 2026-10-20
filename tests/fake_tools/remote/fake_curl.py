#!/usr/bin/env python3
"""A fake `curl` for the tests of remote runs. It copies a local file instead of using the network.
It needs nothing but the standard library of Python 3.9.

The fake reads the object `curl_files` of the file `fake_remote.json` in the parent directory of
`$HOME` (the remote home of the fake `ssh`): it maps a URL to the path of the file to copy. It
supports the call that `bootstrap.sh` makes, `curl -fsSL -o <file> <url>`. A URL that the map does
not name fails with status 22 and curl's message for a 404 response.

It records its calls in `fake_curl.calls` beside the settings file, as the other fakes do: one
`CALL <n>` line per call, then one `ARG` line per argument, then an `ENV` line for each of
`HTTPS_PROXY` and `HTTP_PROXY` that is set.
"""

import json
import os
import shutil
import sys

RECORDED_ENV = ("HTTPS_PROXY", "HTTP_PROXY")


def parent_of_home() -> str:
    return os.path.dirname(os.path.abspath(os.environ["HOME"]))


def record(argv: list) -> None:
    lines = [f"CALL {len(argv)}"] + [f"ARG {a}".replace("\n", "\\n") for a in argv]
    lines += [f"ENV {name}={os.environ[name]}" for name in RECORDED_ENV if name in os.environ]
    log = os.path.join(parent_of_home(), "fake_curl.calls")
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, ("\n".join(lines) + "\n").encode())
    finally:
        os.close(fd)


def main(argv: list) -> int:
    record(argv)
    output = None
    urls = []
    i = 0
    while i < len(argv):
        if argv[i] == "-o":
            output = argv[i + 1]
            i += 2
            continue
        if not argv[i].startswith("-"):
            urls.append(argv[i])
        i += 1
    if output is None or len(urls) != 1:
        sys.stderr.write("fake curl: it supports only `curl -fsSL -o <file> <url>`\n")
        return 2
    try:
        with open(os.path.join(parent_of_home(), "fake_remote.json")) as handle:
            files = json.load(handle).get("curl_files", {})
    except FileNotFoundError:
        files = {}
    if urls[0] not in files:
        sys.stderr.write("curl: (22) The requested URL returned error: 404\n")
        return 22
    shutil.copyfile(files[urls[0]], output)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
