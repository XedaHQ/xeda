"""The README shows a real example file, and says what Xeda is in the words the other documents use.

The README prints `examples/vhdl/sqrt/sqrt.yaml`. A copy of a file drifts when one of the two
changes alone, so the printed block must load to the same data as the file. The quickstart and the
tutorial include the file itself (`literalinclude`), so only the README holds a copy. The first
sentence of the README, of `docs/index.rst`, of `docs/about.rst` and the `description` of the
package are one sentence.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

from xeda.yaml_loader import load_yaml

ROOT = Path(__file__).parent.parent
README = ROOT / "README.md"
EXAMPLE = ROOT / "examples" / "vhdl" / "sqrt" / "sqrt.yaml"
EXAMPLE_LINK = "examples/vhdl/sqrt/sqrt.yaml"


def load_yaml_text(text: str, directory: Path) -> Any:
    """The data of YAML text, read as every design file is (`load_yaml` takes a file)."""
    file = directory / "example.yaml"
    file.write_text(text + "\n", encoding="utf-8")
    return load_yaml(file)


def printed_example(readme: str, directory: Path) -> Any:
    """The data of the first `yaml` code block after the README's link to the example file."""
    start = readme.index(f"[`{EXAMPLE_LINK}`]")
    block = re.search(r"```yaml\n(.*?)\n```", readme[start:], re.S)
    assert block, "the README shows the example in a yaml code block after its link"
    return load_yaml_text(block.group(1), directory)


def test_the_readme_prints_the_example_file(tmp_path):
    assert printed_example(README.read_text(encoding="utf-8"), tmp_path) == load_yaml(EXAMPLE)


def test_the_comparison_sees_a_difference_between_the_copy_and_the_file(tmp_path):
    readme = README.read_text(encoding="utf-8")
    assert "clock.port: clk" in readme
    changed = readme.replace("clock.port: clk", "clock.port: clock", 1)
    assert printed_example(changed, tmp_path) != load_yaml(EXAMPLE)
    dropped = readme.replace("    clock.period: 5.0\n", "", 1)
    assert printed_example(dropped, tmp_path) != load_yaml(EXAMPLE)


def first_sentence(text: str) -> str:
    """The first sentence of a text, on one line and without markup."""
    sentence = re.split(r"(?<=\.)\s", " ".join(text.split()), maxsplit=1)[0]
    return sentence.replace("**", "")


def tagline_of(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    body = text[text.index("**Xeda**") :]
    return first_sentence(body)


@pytest.mark.parametrize("name", ["README.md", "docs/index.rst", "docs/about.rst"])
def test_the_documents_and_the_package_say_what_xeda_is_in_one_sentence(name):
    with open(ROOT / "pyproject.toml", "rb") as f:
        description = tomllib.load(f)["project"]["description"]
    assert tagline_of(ROOT / name) == description
