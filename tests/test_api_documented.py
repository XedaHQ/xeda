"""The frozen public API is the documented API.

`tools/api_baseline.py` freezes each public method, property and attribute of a public class.
`docs/api/*.rst` documents them. A member that the snapshot freezes and the reference does not
render is an accident of the code (a loader hook, a fingerprint), and it would stay for the whole
major version. Rename such a member with a leading underscore, or document it.
"""

import inspect
import re
from pathlib import Path

import pytest

from tests.test_public_api import tool

pytestmark = pytest.mark.python_compat

API_DOCS = Path(__file__).parent.parent / "docs" / "api"

#: `class name -> {member: reason}`: frozen members that the reference does not render. Each row is
#: a reviewed exception.
REVIEWED_EXCEPTIONS: dict[str, dict[str, str]] = {}

_DIRECTIVE = re.compile(r"^\.\. auto(?:class|exception):: (?P<name>\S+)\s*$")
_OPTION = re.compile(r"^\s+:(?P<key>[a-z-]+):\s*(?P<value>.*)$")


def _rendered() -> dict[str, tuple[bool, bool, set[str], set[str]]]:
    """For each class in the reference: whether it renders members at all (`:members:`; without it
    Sphinx renders none), whether it renders every member (`:undoc-members:`), the members it
    lists, and the members it excludes."""
    result: dict[str, tuple[bool, bool, set[str], set[str]]] = {}
    name = ""
    for path in sorted(API_DOCS.glob("*.rst")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if match := _DIRECTIVE.match(line):
                name = match["name"]
                result[name] = (False, False, set(), set())
            elif name and (option := _OPTION.match(line)):
                any_members, everything, listed, excluded = result[name]
                names = {n.strip() for n in option["value"].split(",") if n.strip()}
                if option["key"] == "undoc-members":
                    everything = True
                elif option["key"] == "members":
                    any_members, listed = True, names
                elif option["key"] == "exclude-members":
                    excluded = names
                result[name] = (any_members, everything, listed, excluded)
            elif line.strip() and not line.startswith(" "):
                name = ""
    return result


def _has_text(cls: type, member: str) -> bool:
    """Whether autodoc finds a description of `member` in `cls`: a docstring, or for an attribute a
    `#:` comment or a string below it."""
    own = vars(cls).get(member)
    if own is not None and not isinstance(own, (str, int, float, bool, tuple, frozenset)):
        target = getattr(own, "__func__", None) or getattr(own, "fget", None) or own
        if getattr(target, "__doc__", None):
            return True
    source = inspect.getsource(cls)
    return bool(
        re.search(rf"^\s*#:.*\n\s*{member}\b", source, re.M)
        or re.search(rf"^\s*{member}\s*:[^\n]*#:", source, re.M)
        or re.search(rf"^\s*{member}\s*[:=][^\n]*\n\s*(\"|\')", source, re.M)
    )


def test_every_frozen_member_is_documented() -> None:
    import xeda
    import xeda.errors

    snapshot = tool.public_api_snapshot()["modules"]
    rendered = _rendered()
    problems = []
    for module in ("xeda", "xeda.errors"):
        for name, entry in snapshot[module]["names"].items():
            if entry["kind"] not in ("class", "enum"):
                continue
            cls = getattr(xeda if module == "xeda" else xeda.errors, name)
            frozen = set(entry["methods"]) | {a["name"] for a in entry["attributes"]}
            frozen |= set(entry.get("members", ()))  # the names of an enum
            # A member documented on a public base class counts for its subclasses.
            documented: set[str] = set()
            for klass in cls.__mro__:
                key = f"{module}.{klass.__name__}"
                if key not in rendered:
                    continue
                any_members, everything, listed, excluded = rendered[key]
                if not any_members:
                    continue
                if listed:
                    documented |= {
                        m for m in listed - excluded if everything or _has_text(klass, m)
                    }
                elif everything:
                    documented |= {m for m in frozen if m not in excluded}
                else:
                    documented |= {m for m in frozen if m not in excluded and _has_text(klass, m)}
            excused = set(REVIEWED_EXCEPTIONS.get(name, {}))
            problems += [
                f"{name}.{m}: a reviewed exception that is documented or gone"
                for m in sorted(excused & documented | excused - frozen)
            ]
            missing = frozen - documented - excused
            problems += [f"{module}.{name}.{m}" for m in sorted(missing)]
    assert not problems, "frozen but not documented:\n" + "\n".join(problems)
