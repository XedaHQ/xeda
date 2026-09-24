"""A source type compared with text must be compared with a name the type has.

`SourceType` values are their names (`"Verilog"`), so a comparison with any other text -- a
lowercase `"verilog"`, a stale `"xdc"` -- is silently never true. ModelSim's template compared
`src.type == 'verilog'` and compiled no source at all; `vivado_project` filtered `p.type == "xdc"`
and never read a design's XDC sources. This sweeps the flows' code and templates for such
comparisons.
"""

import re
from pathlib import Path

from xeda.design import SourceType

SRC = Path(__file__).parent.parent / "src" / "xeda"
NAMES = {member.name for member in SourceType}
# `x.type == "..."`, `x.type.name != '...'`, `x.type.name in ("...", "...")`
COMPARISON = re.compile(
    r"""\.type(?:\.name)?\s*(?:==|!=)\s*(['"])(?P<one>[^'"]*)\1"""
    r"""|\.type(?:\.name)?\s+(?:not\s+)?in\s*[(\[](?P<many>[^)\]]*)[)\]]"""
)


def test_every_source_type_compared_with_text_is_a_name_the_type_has() -> None:
    wrong = []
    for path in sorted(SRC.rglob("*")):
        if path.suffix not in (".py", ".tcl", ".ys", ".xdc", ".sdc", ".fdc", ".ldc"):
            continue
        for n, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            for match in COMPARISON.finditer(line):
                texts = (
                    [match["one"]]
                    if match["one"] is not None
                    else re.findall(r"""['"]([^'"]*)['"]""", match["many"])
                )
                wrong += [f"{path.relative_to(SRC)}:{n}: {t!r}" for t in texts if t not in NAMES]
    assert not wrong, "not a SourceType name:\n" + "\n".join(wrong)
