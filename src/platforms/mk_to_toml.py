#! /usr/bin/env python3
import re
import argparse
from pathlib import Path


def escape(s: str):
    try:
        return str(float(s))
    except ValueError:
        pass
    for d in ["$(ADDITIONAL_GDS)", "$(ADDITIONAL_LIBS)", "$(PLATFORM_DIR)/"]:
        s = s.replace(d, "")
    s = s.strip()
    if s.startswith('"') and s.endswith('"'):
        return s
    if not (s.startswith("$(") and s.endswith(")")):
        sp = re.split(r"\s+", s)
        if len(sp) > 1:
            return "[" + ", ".join(escape(s) for s in sp) + "]"
    s = s.replace("\\", "\\\\")
    s = s.replace('"', '\\"')
    return '"' + s + '"'


def comment(s: str):
    return " # overridable" if s.startswith("?") else ""


def callback(pat):
    return f"{pat.group(1).lower()} = {escape(pat.group(3))}{comment(pat.group(2))}\n"


def convert(args):
    with open(args.config_mk) as f:
        content = f.read()
    # line continuations
    content = re.sub(r"[^\S\r\n]*\\[^\S\r\n]*\n[^\S\r\n]*", " ", content, re.MULTILINE | re.UNICODE)

    content = re.sub(r"export\s+(\w+)\s*(\??=)\s*([^\n]+)\s*", callback, content)
    with open(Path(args.config_mk).parent / "config.toml", "w") as f:
        f.write(content)


if __name__ == "__main__":
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument("config_mk", type=Path)
    args = arg_parser.parse_args()
    convert(args)
