#!/usr/bin/env python3
"""Process model of the ASIC synthesis tool yosys.

It stands in for a tool that cannot be relied on to be installed, or to give the same answer at
every release, and it is not on `PATH` unless a test puts it there
(`tool_utils.use_fake_asic_tools`): the tests that run the real yosys must keep finding it.

What the model writes is a function of what it was handed, never of where or when: it reads the
files its command line or script names -- sources, liberty files, maps, constraints -- and the text
it writes (netlist, reports, log) carries a digest of their *contents*, and of the other words of
each command, with the working directory left out. So a flow that hands it a different library
or a different list of black boxes gets different output, and the same request in another
directory gets the same bytes.

Every command is recorded, as the fake Vivado records its Tcl commands, in `fake_yosys.calls` in
the working directory (`tool_utils.fake_calls` reads it): `CALL <n>` followed by `ARG` lines.
yosys records the invocation, then each command of the script it is given.
"""

import hashlib
import json
import os
import shlex
import sys
from pathlib import Path

#: What yosys prints for its version probe.
VERSIONS = {
    "yosys": "Yosys 0.63 (fake)",
}


def digest(data) -> str:
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def file_digest(path: str) -> str | None:
    """The content digest of a file the word names, if it names one."""
    try:
        if os.path.isfile(path):
            return digest(Path(path).read_bytes())
    except OSError:
        pass
    return None


def relative_to_here(word: str) -> str:
    """A word without the working directory, which differs from one run to the next."""
    here = os.getcwd()
    for prefix in {here, os.path.realpath(here)}:
        word = word.replace(prefix, ".")
    return word


def canonical(word: str) -> str:
    """What a word of a command stands for: a file's content, else the word itself."""
    return f"file:{found}" if (found := file_digest(word)) else relative_to_here(word)


def record(tool: str, commands: list[list[str]]) -> None:
    with open(f"fake_{tool}.calls", "a") as calls:
        for words in commands:
            calls.write(f"CALL {len(words)}\n")
            for word in words:
                calls.write(f"ARG {word}\n")


def write(path: str, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def number(seed: str, low: int, span: int) -> int:
    """A number between `low` and `low + span` that follows from `seed` alone."""
    return low + int(seed[:8], 16) % span


#: Commands of a yosys script that write a file, by the position of the word that names it.
YOSYS_WRITERS = {"write_verilog", "write_json", "write_blif"}


def yosys(args: list[str]) -> int:
    if "-s" not in args:
        print("fake yosys: only `-s script` is modeled", file=sys.stderr)
        return 1
    script = Path(args[args.index("-s") + 1])
    commands = [
        shlex.split(line)
        for line in script.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    record("yosys", [["yosys", *map(str, args)], *commands])
    # what the netlist is a function of: every command that reads or transforms, in order
    reads = [
        [words[0], *map(canonical, words[1:])]
        for words in commands
        if words[0] not in YOSYS_WRITERS and words[0] not in ("tee", "log", "logger", "echo")
    ]
    seed = digest(json.dumps(reads))
    for words in commands:
        if words[0] in YOSYS_WRITERS:
            netlist = (
                json.dumps({"creator": "fake yosys", "digest": seed, "modules": {}})
                if words[0] == "write_json"
                else f"// fake yosys netlist {words[0]}\n// digest {seed}\nmodule top ();\nendmodule\n"
            )
            write(words[-1], netlist)
        elif words[0] == "tee" and "-o" in words:
            output = words[words.index("-o") + 1]
            if "stat" in words:
                cells = number(seed, 20, 400)
                write(
                    output,
                    json.dumps(
                        {
                            "design": {
                                "num_cells": cells,
                                "num_cells_by_type": {"fake_cell": cells},
                                "area": float(cells) * 1.5,
                            },
                            "modules": {},
                        }
                    ),
                )
            else:
                write(output, f"fake yosys: {words[-1]}\n")
    for flag, text in (("-L", f"fake yosys log {seed}\n"), ("-E", "")):
        if flag in args:
            write(args[args.index(flag) + 1], text)
    return 0


def main() -> int:
    tool, args = Path(sys.argv[0]).name, sys.argv[1:]
    if args and args[0] in ("-V", "-version", "--version"):
        print(VERSIONS[tool])
        return 0
    return {"yosys": yosys}[tool](args)


if __name__ == "__main__":
    sys.exit(main())
