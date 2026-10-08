"""What `vivado_impl` asks of an EDIF netlist before it hands the file to Vivado.

An EDIF netlist keeps its cells in libraries. An `external` library only declares cells, the
primitives of the target technology. A `library` defines the cells of the design, each with its
`contents`. An instance names its cell and the library to find it in:
`(cellRef core (libraryRef LIB))`.

The reader streams the netlist: `modules_used_as_library_cells_in_file` reads the file a chunk at
a time, and what the reader holds does not grow with the number of instances. It holds:

- the chunk, and the word or string that ends it, which the next chunk completes (at most
  `_TOKEN_LIMIT` characters are held over);
- the forms that are open (at most `_DEPTH_LIMIT`);
- each distinct cell that the netlist defines with contents, and each distinct pair of a cell and
  a library that an instance names.

So a netlist of millions of instances of a few cells takes the memory of one chunk and of those few
cells, and a netlist of many distinct cells takes the memory of their names. Given the text itself
(`modules_used_as_library_cells`), the reader makes no copy of it.

Text that is no EDIF can go beyond the limits, and the reader holds no more for it: it reads a word
that is longer than the limit as several words, reads the text after a quote that has no pair as the
text it is, and ignores the forms below the depth.
"""

import re
from collections.abc import Iterable, Iterator
from os import PathLike
from typing import Optional, Union

__all__ = ["modules_used_as_library_cells", "modules_used_as_library_cells_in_file"]

#: a parenthesis, a string (which may hold parentheses), or a word
_TOKEN = re.compile(r'\(|\)|"[^"]*"|[^\s()"]+')
#: the characters read from a file at a time
_CHUNK = 1 << 14
#: the most characters of a word or a string that the reader holds across two chunks. EDIF names
#: are short, and so are its strings.
_TOKEN_LIMIT = 1 << 20
#: the most forms that the reader holds open. An EDIF netlist nests about a dozen.
_DEPTH_LIMIT = 1000


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


def modules_used_as_library_cells(source: Union[str, Iterable[str]]) -> list[str]:
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

    `source` is the text of the netlist, or the chunks of it, cut anywhere: the verdict is the
    same. The module doc says what the reader holds in memory.
    """
    return _modules(_tokens([source] if isinstance(source, str) else source))


def modules_used_as_library_cells_in_file(
    path: Union[str, PathLike[str]], *, chunk_size: int = _CHUNK
) -> list[str]:
    """`modules_used_as_library_cells` of the netlist in the file `path`, read `chunk_size`
    characters at a time, so that the file is never held. A byte that is no UTF-8 reads as the
    replacement character."""
    if chunk_size < 1:
        raise ValueError(f"chunk_size must be a positive number of characters, not {chunk_size}")
    with open(path, encoding="utf-8", errors="replace") as stream:
        return _modules(_tokens(iter(lambda: stream.read(chunk_size), "")))


def _tokens(chunks: Iterable[str]) -> Iterator[str]:
    """The tokens of the text that `chunks` make up: the ones that one scan of the whole text
    makes (`_TOKEN`), whatever the chunks are.

    A chunk can end in a token. The reader keeps the end of the text from the start of a token
    that may go on in the next chunk -- the last token, if it touches the end, or a string that
    is open -- and scans it again with the next chunk. Quotes pair from the start of a token, in
    order, so a string is open at the end of a text exactly when the quotes in it are odd in
    number.
    """
    held = ""
    for chunk in chunks:
        text = held + chunk
        end = len(text)
        if text.count('"') % 2:  # the last quote opens a string that a later chunk ends
            opening = text.rfind('"')
            if len(text) - opening <= _TOKEN_LIMIT:
                end = opening
        held = text[end:]
        last = None
        for match in _TOKEN.finditer(text, 0, end):
            if last is not None:
                yield last.group()
            last = match
        if last is not None:
            token = last.group()
            if end == len(text) and last.end() == end and len(token) <= _TOKEN_LIMIT:
                held = token  # the last token ends the text: a word may go on in the next chunk
            else:
                yield token
    for match in _TOKEN.finditer(held):  # the end of the text: what is left is whole
        yield match.group()


def _modules(tokens: Iterable[str]) -> list[str]:
    """The verdict of `modules_used_as_library_cells` on the tokens of a netlist."""
    external: set[str] = set()  # the names of the external libraries
    defined: dict[str, str] = {}  # the cells defined with contents: name -> as spelled
    references: dict[tuple[str, str], None] = {}  # the distinct (cell, library) of each `cellRef`
    forms: list[_Form] = []
    ignored = 0  # the forms opened below `_DEPTH_LIMIT`
    for token in tokens:
        if token == "(":
            if len(forms) < _DEPTH_LIMIT:
                forms.append(_Form())
            else:
                ignored += 1
        elif token == ")":
            if ignored:
                ignored -= 1
            elif forms:
                form = forms.pop()
                parent = forms[-1] if forms else None
                _close(form, parent, external, defined, references)
                if parent is not None:
                    parent.arguments += 1
        elif forms and not ignored:
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
        if library in external and cell in defined:
            found.setdefault(cell, defined[cell])
    return list(found.values())


def _close(
    form: _Form,
    parent: Optional[_Form],
    external: set[str],
    defined: dict[str, str],
    references: dict[tuple[str, str], None],
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
            references[(form.name.lower(), form.library.lower())] = None
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
