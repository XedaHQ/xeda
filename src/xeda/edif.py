"""What `vivado_impl` asks of an EDIF netlist before it hands the file to Vivado.

An EDIF netlist keeps its cells in libraries. An `external` library only declares cells, the
primitives of the target technology. A `library` defines the cells of the design, each with its
`contents`. An instance names its cell and the library to find it in:
`(cellRef core (libraryRef LIB))`.

The file is read as a stream of forms, so the memory it takes does not grow with the netlist.
"""

import re
from typing import Optional

__all__ = ["modules_used_as_library_cells"]

#: a parenthesis, a string (which may hold parentheses), or a word
_TOKEN = re.compile(r'\(|\)|"[^"]*"|[^\s()"]+')


class _Form:
    """A form of the netlist that is still open: its keyword, its name (the first argument, a word
    or a `(rename id "text")`) and what its children told it."""

    __slots__ = ("keyword", "name", "display", "arguments", "library", "has_contents")

    def __init__(self) -> None:
        self.keyword: Optional[str] = None
        self.name: Optional[str] = None
        self.display: Optional[str] = None  # the text of a renamed name
        self.arguments = 0
        self.library: Optional[str] = None  # of a `cellRef`: the library it names
        self.has_contents = False  # of a `view` or a `cell`: it is defined, not only declared


def modules_used_as_library_cells(text: str) -> list[str]:
    """The modules that the netlist defines and that its instances refer to through an external
    library, in the order they are first referred to; each as the netlist spells it.

    Vivado looks a cell that an instance finds in an external library up among the primitives of
    its device. A module the netlist defines, and refers to this way, is not among them. Vivado
    takes it for a black box, and stops. This is how yosys writes a hierarchy: each module is
    defined in the library `DESIGN` and referred to in the external library `LIB`. A netlist that
    refers to a module in the library that defines it is a hierarchy that Vivado reads.

    A cell that only an external library declares is no module of the netlist: a primitive and a
    black box look alike in the file, and only Vivado knows the primitives. Text that is no EDIF
    netlist has none.
    """
    external: set[str] = set()  # the names of the external libraries
    defined: dict[str, str] = {}  # the cells defined with contents: name -> as spelled
    references: list[tuple[str, str]] = []  # (cell, library) of each `cellRef`
    forms: list[_Form] = []
    for match in _TOKEN.finditer(text):
        token = match.group()
        if token == "(":
            forms.append(_Form())
        elif token == ")":
            if not forms:
                continue
            form = forms.pop()
            parent = forms[-1] if forms else None
            _close(form, parent, external, defined, references)
            if parent is not None:
                parent.arguments += 1
        elif forms:
            form = forms[-1]
            if form.keyword is None:
                form.keyword = token.lower()
            elif form.arguments == 0:
                form.name = token
                form.arguments = 1
            elif form.arguments == 1 and form.keyword == "rename":
                form.display = token.strip('"')
                form.arguments = 2
            else:
                form.arguments += 1
    found: dict[str, str] = {}
    for cell, library in references:
        if library.lower() in external and cell.lower() in defined:
            found.setdefault(cell.lower(), defined[cell.lower()])
    return list(found.values())


def _close(
    form: _Form,
    parent: Optional[_Form],
    external: set[str],
    defined: dict[str, str],
    references: list[tuple[str, str]],
) -> None:
    """What a form that ends tells the netlist, and the form it is in."""
    keyword = form.keyword
    if keyword == "rename":
        if parent is not None and parent.arguments == 0 and form.name is not None:
            parent.name, parent.display = form.name, form.display
    elif keyword == "libraryref":
        if parent is not None:
            parent.library = form.name
    elif keyword == "cellref":
        if form.name is not None and form.library is not None:
            references.append((form.name, form.library))
    elif keyword == "contents":
        if parent is not None:
            parent.has_contents = True
    elif keyword == "view":
        if parent is not None and form.has_contents:
            parent.has_contents = True
    elif keyword == "cell":
        if form.has_contents and form.name is not None:
            defined[form.name.lower()] = form.display or form.name
    elif keyword == "external":
        if parent is not None and parent.keyword == "edif" and form.name is not None:
            external.add(form.name.lower())
