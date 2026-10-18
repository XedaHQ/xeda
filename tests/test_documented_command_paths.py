"""A command in the documentation, the agent skill or an example README runs from the repository root.

The text names a design file by its path from the root of the repository
(`xeda run bsc_sim examples/bluespec/gcd/gcd.yaml`), so a reader can copy the command into a shell
that stands in the checkout. A bare file name (`gcd.yaml`) works only in the directory of the file.
A comment inside a design file may name the file alone, and a name that no example has
(`design.yaml`, `blinky.yaml`) stands for the reader's own file.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
DOCUMENTS = sorted(
    [
        ROOT / "README.md",
        *ROOT.glob("docs/**/*.rst"),
        *ROOT.glob(".claude/skills/xeda/**/*.md"),
        *ROOT.glob("src/xeda/data/agent/**/*.md"),
        *ROOT.glob("examples/**/*.md"),
    ]
)
COMMAND = re.compile(r"\bxeda (?:run|dse|scrub)\b[^`\n]*")
DESIGN_FILE = re.compile(r"(?<![\w./$-])((?:[\w.-]+/)*[\w.-]+\.(?:yaml|yml|toml))(?![\w/-])")
EXAMPLE_NAMES = {
    path.name
    for path in (ROOT / "examples").rglob("*")
    if path.suffix in {".yaml", ".yml", ".toml"} and "xeda_run" not in path.parts
}


def _design_files(text):
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for command in COMMAND.finditer(line):
            for name in DESIGN_FILE.findall(command.group(0)):
                yield number, name


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda p: str(p.relative_to(ROOT)))
def test_a_documented_command_names_example_files_from_the_repository_root(document):
    problems = []
    for number, name in _design_files(document.read_text(encoding="utf-8")):
        if "/" in name:
            if name.startswith(("/", "~")):
                continue  # a path of the reader's own
            if not (ROOT / name).is_file():
                problems.append(f"{number}: {name} is not a file under the repository root")
        elif name in EXAMPLE_NAMES:
            problems.append(f"{number}: {name} is an example file: give its path from the root")
    assert not problems, "\n".join(problems)


def test_the_sweep_sees_the_commands_of_the_documents():
    found = {
        name
        for document in DOCUMENTS
        for _, name in _design_files(document.read_text(encoding="utf-8"))
    }
    assert "examples/bluespec/gcd/gcd.yaml" in found
    assert "examples/vhdl/sqrt/sqrt.yaml" in found


def test_the_sweep_flags_a_bare_example_file():
    assert list(_design_files("    xeda run openroad gcd.yaml")) == [(1, "gcd.yaml")]
    assert list(_design_files("# `xeda run openroad gcd.yaml`")) == []
