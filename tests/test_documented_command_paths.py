"""A command in the documentation, the agent skill or an example README runs from the repository root.

The text names a design file by its path from the root of the repository
(`xeda run bsc_sim examples/bluespec/gcd/gcd.yaml`), so a reader can copy the command into a shell
that stands in the checkout. A bare file name (`gcd.yaml`) works only in the directory of the file.
A comment inside a design file may name the file alone, and a name that no example has
(`design.yaml`, `blinky.yaml`) stands for the reader's own file. The same holds for the project
file of an example (`--xedaproject examples/boards/ulx3s/blinky/xedaproject.yaml`), for a script
(`python examples/bluespec/uart_echo/host/uart_echo_test.py`), and for the directory: no command
block of an example changes into the directory of the example first.
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


SCRIPT = re.compile(r"\bpython3? ((?:[\w.-]+/)*[\w.-]+\.py)\b")
EXAMPLE_DOCUMENTS = [d for d in DOCUMENTS if "examples" in d.relative_to(ROOT).parts]


@pytest.mark.parametrize("document", EXAMPLE_DOCUMENTS, ids=lambda p: str(p.relative_to(ROOT)))
def test_a_script_of_an_example_is_named_from_the_repository_root(document):
    names = SCRIPT.findall(document.read_text(encoding="utf-8"))
    wrong = [n for n in names if not n.startswith(("/", "~")) and not (ROOT / n).is_file()]
    assert not wrong, wrong


@pytest.mark.parametrize("document", EXAMPLE_DOCUMENTS, ids=lambda p: str(p.relative_to(ROOT)))
def test_a_command_of_an_example_does_not_change_the_directory_first(document):
    lines = document.read_text(encoding="utf-8").splitlines()
    assert not [line for line in lines if re.match(r"\s*cd examples/", line)]


@pytest.mark.parametrize(
    "document", [ROOT / "README.md", *EXAMPLE_DOCUMENTS], ids=lambda p: str(p.relative_to(ROOT))
)
def test_a_command_with_a_design_name_names_its_project_file(document):
    """`--design-name` picks a design of the project file that Xeda finds in the current
    directory, so a command run from the repository root names the file."""
    found = [
        f"{number}: {line.strip()}"
        for number, line in enumerate(document.read_text(encoding="utf-8").splitlines(), 1)
        if re.match(r"\s*xeda run\b", line)
        and "--design-name" in line
        and "--xedaproject" not in line
    ]
    assert not found, "\n".join(found)


@pytest.mark.parametrize("document", EXAMPLE_DOCUMENTS, ids=lambda p: str(p.relative_to(ROOT)))
def test_a_bluespec_source_of_a_compiler_command_is_named_from_the_repository_root(document):
    """The raw `bsc` recipes of the READMEs run in the root directory too."""
    wrong = [
        name
        for line in document.read_text(encoding="utf-8").splitlines()
        if re.match(r"\s*bsc\s|\s+-bdir|\s+examples/\S+\.bsv", line)
        for name in re.findall(r"(?<![\w$/.-])((?:[\w.-]+/)+[\w.-]+\.bsv)\b", line)
        if not (ROOT / name).is_file()
    ]
    assert not wrong, wrong
