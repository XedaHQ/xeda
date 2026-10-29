"""Text that Xeda ships or documents uses American spelling.

This is a sweep for the British spellings that came in before (`centre`, `behavioural`, ...). It
is not a spelling checker: add a word here when a British form of it shows up again.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
TREES = ("README.md", "CLAUDE.md", "docs", "examples", "src", "tools", "changelog.d", ".claude")
SUFFIXES = {
    ".md", ".rst", ".txt", ".py", ".yaml", ".yml", ".toml", ".bsv", ".bs", ".tcl", ".j2", ".sv",
    ".v", ".cc", ".cpp", ".h",
}  # fmt: skip
#: Files that are not Xeda's own text: licenses and notices of other projects, third-party designs.
THIRD_PARTY = ("LICENSE", "NOTICE", "tmds_encoder.vhd")

BRITISH = re.compile(
    r"\b(?:centre[sd]?|centring|behaviour\w*|colour\w*"
    r"|(?:optimis|synchronis|initialis|serialis|normalis|recognis|utilis|organis|summaris"
    r"|minimis|maximis|standardis|categoris|generalis|customis|finalis)(?:e|es|ed|er|ers|ing|ations?)"
    r"|cancell(?:ed|ing)|modell(?:ed|ing)|labell(?:ed|ing)|catalogues?|grey|neighbour\w*"
    r"|favour\w*|artefacts?|whilst|amongst|licence[sd]?|judgement|programmes?)\b",
    re.IGNORECASE,
)
#: Words of a tool that Xeda quotes as the tool writes them: `CANCELLED` is a status of SBY.
QUOTED = {"CANCELLED"}


def _files():
    for name in TREES:
        tree = ROOT / name
        paths = [tree] if tree.is_file() else sorted(tree.rglob("*"))
        for path in paths:
            relative = path.relative_to(ROOT)
            if (
                path.is_file()
                and path.suffix in SUFFIXES
                and not any(part in {"xeda_run", "__pycache__"} for part in relative.parts)
                and not path.name.startswith(THIRD_PARTY)
            ):
                yield path


def test_the_text_uses_american_spelling():
    found = []
    for path in _files():
        for number, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            found.extend(
                f"{path.relative_to(ROOT)}:{number}: {match.group(0)}"
                for match in BRITISH.finditer(line)
                if match.group(0) not in QUOTED
            )
    assert not found, "British spelling:\n" + "\n".join(found)


@pytest.mark.parametrize(
    "text", ["a centre", "behavioural", "the colour", "initialised", "cancelled", "a licence"]
)
def test_the_sweep_finds_a_british_spelling(text):
    assert BRITISH.search(text)


@pytest.mark.parametrize(
    "text",
    ["center", "behavioral", "behavior", "color", "initialized", "CancelledError", "organism"],
)
def test_the_sweep_accepts_an_american_spelling(text):
    assert not BRITISH.search(text)
