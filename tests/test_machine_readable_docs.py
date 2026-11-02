"""The error codes and the record fields in docs/machine-readable.rst are complete.

The "Error codes" section lists the codes of `ALL_CODES` under the category that holds each one,
and the section "The record of a run" names every field of the record dataclasses.
"""

import dataclasses
import re
from pathlib import Path

import pytest

from xeda.flow_runner.error_codes import ALL_CODES
from xeda.records import ErrorInfo, InputRecord, InputSource, MetricInfo, OutputRecord, RunRecord

DOCUMENT = Path(__file__).resolve().parent.parent / "docs" / "machine-readable.rst"
UNDERLINE = re.compile(r"^([=\-~])\1{3,}$")

#: Fields that the document does not hold: the place of the record follows from the file, and
#: the key of an entry of `outputs` or `metric_info` is its name or path.
NOT_IN_DOCUMENT = {
    (RunRecord, "run_root"),
    (RunRecord, "run_dir"),
    (OutputRecord, "name"),
    (MetricInfo, "path"),
}

#: The nested record that each field of the document holds.
NESTED = {
    "error": [ErrorInfo],
    "metric_info": [MetricInfo],
    "outputs": [OutputRecord],
    "inputs": [InputRecord, InputSource],
}


def section(title: str, levels: str) -> list[str]:
    """Return the lines below the heading `title`, up to the next heading of one of `levels`."""
    lines = DOCUMENT.read_text(encoding="utf-8").splitlines()
    start = next(
        i + 2
        for i, line in enumerate(lines[:-1])
        if line == title and UNDERLINE.match(lines[i + 1]) and lines[i + 1][0] in levels
    )
    end = len(lines)
    for i in range(start, len(lines) - 1):
        if lines[i].strip() and UNDERLINE.match(lines[i + 1]) and lines[i + 1][0] in levels:
            end = i
            break
    return lines[start:end]


def literals(text: str) -> list[str]:
    return re.findall(r"``([^`]+)``", text)


def definitions(lines: list[str]) -> dict[str, list[str]]:
    """Return the definition list in `lines`: each term line (at no indent, starting with a
    literal) maps to the indented lines below it. A term line gives one key per literal."""
    found: dict[str, list[str]] = {}
    terms: list[str] = []
    for line in lines:
        if line.startswith("``"):
            terms = literals(line)
            for term in terms:
                found[term] = []
        elif line.startswith(" ") and terms:
            for term in terms:
                found[term].append(line)
        elif line.strip():
            terms = []
    return found


def terms(lines: list[str]) -> list[str]:
    """Return every term of the definition list in `lines`, in order, repeats included."""
    return [term for line in lines if line.startswith("``") for term in literals(line)]


def error_categories() -> dict[str, list[str]]:
    """Return the codes that the section lists under each category heading.

    A category heading is a title that ends with its category in a literal, underlined with `~`."""
    lines = section("Error codes", "=-")
    categories: dict[str, list[str]] = {}
    name: str | None = None
    block: list[str] = []
    for i, line in enumerate(lines):
        if line.strip() and i + 1 < len(lines) and lines[i + 1].startswith("~~~~"):
            if name is not None:
                categories[name] = terms(block)
            match = re.search(r"\(``(\w+)``\)$", line)
            assert match, f"the heading `{line}` does not end with its category in a literal"
            name, block = match.group(1), []
        elif name is not None:
            block.append(line)
    if name is not None:
        categories[name] = terms(block)
    return categories


def test_error_codes_list_every_category():
    assert set(error_categories()) == set(ALL_CODES)


@pytest.mark.parametrize("category", sorted(ALL_CODES))
def test_error_codes_of_a_category(category):
    listed = error_categories().get(category, [])
    assert len(listed) == len(set(listed)), f"{category} lists a code twice"
    assert set(listed) == ALL_CODES[category]


def record_lines() -> list[str]:
    return section("The record of a run", "=-")


def test_record_names_every_top_level_field():
    terms = definitions(record_lines())
    missing = [
        field.name
        for field in dataclasses.fields(RunRecord)
        if (RunRecord, field.name) not in NOT_IN_DOCUMENT and field.name not in terms
    ]
    assert not missing, f"the record section has no entry for: {missing}"


@pytest.mark.parametrize("parent", sorted(NESTED))
def test_record_names_every_nested_field(parent):
    block = " ".join(definitions(record_lines()).get(parent, []))
    holds = {word for text in literals(block) for word in re.findall(r"\w+", text)}
    missing = [
        f"{cls.__name__}.{field.name}"
        for cls in NESTED[parent]
        for field in dataclasses.fields(cls)
        if (cls, field.name) not in NOT_IN_DOCUMENT and field.name not in holds
    ]
    assert not missing, f"the entry `{parent}` does not name: {missing}"


def test_record_section_links_the_schema():
    assert "results.schema.json" in "\n".join(record_lines())
