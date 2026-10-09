"""`xeda.edif.modules_used_as_library_cells`: the one thing `vivado_impl` asks of an EDIF netlist
before it hands the file to Vivado."""

import random
import tracemalloc
from pathlib import Path

import pytest

from xeda import edif
from xeda.edif import modules_used_as_library_cells

HEADER = "(edif top (edifVersion 2 0 0) (edifLevel 0) (keywordMap (keywordLevel 0))\n"


def _cell(name: str, interface: str, contents: str | None = None) -> str:
    """A cell of a library: an interface, and the contents of one that is defined."""
    body = f"(interface {interface})" + (f" (contents {contents})" if contents else "")
    return f"(cell {name} (cellType GENERIC) (view VIEW_NETLIST (viewType NETLIST) {body}))"


def _instance(name: str, cell: str, library: str) -> str:
    return f"(instance {name} (viewRef VIEW_NETLIST (cellRef {cell} (libraryRef {library}))))"


def netlist(*, modules: tuple[str, ...] = (), through: str = "LIB", declared: str = "") -> str:
    """A netlist of the top `top` that instantiates a flip-flop and each of `modules`, which it
    defines in its own library, `DESIGN`, and refers to in the library `through`. Yosys writes
    its hierarchy through the external library `LIB`, which is where Vivado cannot find it."""
    ports = "(port clk (direction INPUT))"
    contents = " ".join(
        [_instance("ff", "FDRE", "LIB")]
        + [_instance(f"u_{module}", module, through) for module in modules]
    )
    defined = " ".join(_cell(module, ports, "(net n)") for module in modules)
    return (
        HEADER
        + "  (external LIB (edifLevel 0) (technology (numberDefinition))\n"
        + f"    {_cell('FDRE', '(port C (direction INPUT))')}\n"
        + f"    {_cell('top', ports)}\n"
        + "".join(f"    {_cell(module, ports)}\n" for module in modules)
        + (f"    {declared}\n" if declared else "")
        + "  )\n"
        + "  (library DESIGN (edifLevel 0) (technology (numberDefinition))\n"
        + f"    {defined}\n"
        + f"    {_cell('top', ports, contents)}\n"
        + "  )\n"
        + "  (design top (cellRef top (libraryRef DESIGN))))\n"
    )


def test_a_flat_netlist_uses_no_module_as_a_library_cell() -> None:
    assert modules_used_as_library_cells(netlist()) == []


def test_a_module_the_netlist_defines_and_refers_to_as_a_library_cell_is_found() -> None:
    """The yosys form of a hierarchy. Vivado looks a library cell up among its primitives, and
    takes the module for a black box."""
    assert modules_used_as_library_cells(netlist(modules=("core",))) == ["core"]
    assert modules_used_as_library_cells(netlist(modules=("core", "alu"))) == ["core", "alu"]


def test_a_hierarchy_the_netlist_refers_to_in_its_own_library_is_vivados_to_read() -> None:
    """Other writers refer to a module in the library that defines it. That is a hierarchy Vivado
    resolves, and the flat netlist is yosys's to write, not an EDIF rule."""
    assert modules_used_as_library_cells(netlist(modules=("core",), through="DESIGN")) == []


def test_a_cell_only_an_external_library_declares_is_no_module_the_netlist_defines() -> None:
    """A primitive, and also a black box: the file cannot tell them apart, and Vivado can."""
    declared = _cell("hard_ip", "(port x (direction INPUT))")
    text = netlist(declared=declared).replace(
        _instance("ff", "FDRE", "LIB"),
        _instance("ff", "FDRE", "LIB") + _instance("u_ip", "hard_ip", "LIB"),
    )
    assert "hard_ip (libraryRef LIB)" in text
    assert modules_used_as_library_cells(text) == []


def test_a_name_is_shown_as_the_netlist_spells_it() -> None:
    """A module whose name an identifier cannot hold is defined as `(rename id "name")`, and
    referred to by `id`."""
    text = netlist(modules=("id00001",)).replace(
        "(cell id00001 ", '(cell (rename id00001 "$paramod\\core\\W=4") ', 2
    )
    assert modules_used_as_library_cells(text) == ["$paramod\\core\\W=4"]


def test_a_parenthesis_in_a_string_does_not_end_a_form() -> None:
    text = netlist(modules=("core",)).replace(
        "(cell top ", '(cell (rename top "to(p") (property note (string "a (b")) ', 1
    )
    assert modules_used_as_library_cells(text) == ["core"]


def test_keywords_and_names_are_read_as_EDIF_does_without_regard_to_case() -> None:
    text = netlist(modules=("core",)).replace("cellRef", "CELLREF").replace("LIB", "lib")
    assert modules_used_as_library_cells(text) == ["core"]


@pytest.mark.parametrize(
    "text",
    ["", "not an EDIF netlist", "(edif top", ")))((", '(edif top (cell "unterminated'],
    ids=["empty", "text", "unclosed", "unbalanced", "string"],
)
def test_what_is_no_netlist_is_left_for_vivado_to_reject(text) -> None:
    assert modules_used_as_library_cells(text) == []


# ------------------------------------------------------------------------- reading a stream


def _chunks(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


#: texts the verdict has to be the same for, however they are cut into chunks: netlists that
#: hold what a chunk boundary can split (a keyword, a name, a string with a parenthesis in it, a
#: renamed name, a letter of another case, a character of more than one byte), and text that is
#: no netlist (a quote without its pair, forms never closed or never opened)
STREAMED_TEXTS = {
    "flat": netlist(),
    "hierarchy": netlist(modules=("core", "alu")),
    "own library": netlist(modules=("core",), through="DESIGN"),
    "renamed": netlist(modules=("id00001",)).replace(
        "(cell id00001 ", '(cell (rename id00001 "$paramod\\core\\W=4") '
    ),
    "string": netlist(modules=("core",)).replace(
        "(cell top ", '(cell (rename top "to(p") (property note (string "a (b\n")) '
    ),
    "case": netlist(modules=("core",)).replace("cellRef", "CELLREF").replace("LIB", "lib"),
    "multibyte": netlist(modules=("core",)).replace(
        "(cell top ", '(cell (rename top "\u00e9\u4e2d") '
    ),
    "unclosed": netlist(modules=("core",))[:-12],
    "odd quote": 'x "a (cellRef b (libraryRef c)) d',
    "stray": ")))((" + netlist(modules=("core",)),
    "unterminated": '(edif top (cell "unterminated',
    "empty": "",
}


@pytest.mark.parametrize("name", STREAMED_TEXTS)
def test_a_text_read_in_chunks_gives_the_verdict_of_the_text_whole(name) -> None:
    """However the text is cut -- into pieces of every size from one character up, so that every
    place in it is a boundary -- the verdict is that of the text whole."""
    text = STREAMED_TEXTS[name]
    whole = modules_used_as_library_cells(text)
    for size in [*range(1, 70), len(text) - 1, len(text), len(text) + 1]:
        if size > 0:
            assert modules_used_as_library_cells(_chunks(text, size)) == whole, (name, size)


def test_a_token_split_by_a_chunk_boundary_is_read_whole() -> None:
    """The boundary falls inside the keyword `cellRef`, inside the name `core` that it refers to,
    and inside the string of the renamed module: each token is what it is without the cut."""
    text = netlist(modules=("core",)).replace("(cell core ", '(cell (rename core "co(re") ')
    assert modules_used_as_library_cells(text) == ["co(re"]
    for token in ("cellRef core", "(cellRef core", 'rename core "co(re"'):
        start = text.index(token)
        for cut in range(start + 1, start + len(token)):
            assert modules_used_as_library_cells([text[:cut], text[cut:]]) == ["co(re"], text[
                start:cut
            ]


def test_the_chunks_of_a_text_are_tokenized_as_the_text_whole_is() -> None:
    """The tokens, not only the verdict: text of the characters that make tokens and that make
    them end (parentheses, quotes, spaces, letters), cut at random places and in chunks of
    random sizes, gives the tokens of the one scan of the whole text. That includes text with
    quotes that pair differently from one scan to the other if the cut were careless."""
    rng = random.Random(20251007)
    for _ in range(400):
        text = "".join(rng.choice('()" \n\tab\u00e9') for _ in range(rng.randrange(0, 60)))
        whole = [match.group() for match in edif._TOKEN.finditer(text)]
        cuts = sorted(rng.sample(range(len(text) + 1), min(len(text) + 1, rng.randrange(1, 6))))
        chunks = [text[a:b] for a, b in zip([0, *cuts], [*cuts, len(text)])]
        assert list(edif._tokens(chunks)) == whole, (text, chunks)


def _write_large_netlist(path: Path, instances: int, modules: tuple[str, ...] = ()) -> int:
    """A netlist of `instances` instances of two cells (and one of each of `modules`), each
    instance with a name of its own, written line by line. The size of the file."""
    ports = "(port clk (direction INPUT))"
    with path.open("w", encoding="utf-8") as stream:
        stream.write(HEADER + "  (external LIB (edifLevel 0) (technology (numberDefinition))\n")
        for cell in ("FDRE", "LUT2", *modules):
            stream.write(f"    {_cell(cell, '(port C (direction INPUT))')}\n")
        stream.write("  )\n  (library DESIGN (edifLevel 0) (technology (numberDefinition))\n")
        for module in modules:
            stream.write(f"    {_cell(module, ports, '(net n)')}\n")
        stream.write(
            "    (cell top (cellType GENERIC) (view VIEW_NETLIST (viewType NETLIST)"
            f" (interface {ports}) (contents\n"
        )
        for module in modules:
            stream.write(f"      {_instance(f'u_{module}', module, 'LIB')}\n")
        for n in range(instances):
            cell = ("FDRE", "LUT2")[n % 2]
            name = f'(rename id{n:08d} "$auto$ff.cc:337:slice${n}")'
            stream.write(f"      {_instance(name, cell, 'LIB')}\n")
        stream.write("    )))\n  )\n  (design top (cellRef top (libraryRef DESIGN))))\n")
    return path.stat().st_size


def _peak_memory(call) -> int:
    """The most memory, in bytes, that Python holds at once while `call()` runs."""
    tracemalloc.start()
    try:
        call()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_a_large_netlist_of_few_distinct_cells_takes_a_fraction_of_its_size_in_memory(
    tmp_path,
) -> None:
    """Instances by the tens of thousands, of two cells and a module: what the reader holds is a
    chunk, the open forms and the few distinct cells, however many instances there are. A file
    four times as long takes the same memory, a tenth of its size at most. (The floor is the
    buffer of `open`, which is 128 KiB in Python 3.14.)"""
    sizes, peaks = [], []
    for instances in (10_000, 40_000):
        path = tmp_path / f"{instances}.edif"
        sizes.append(_write_large_netlist(path, instances, ("core",)))
        found: list[str] = []
        peaks.append(
            _peak_memory(
                lambda: found.extend(
                    edif.modules_used_as_library_cells_in_file(path, chunk_size=1 << 14)
                )
            )
        )
        assert found == ["core"]
    assert sizes[1] > 4_000_000 and sizes[1] > 3 * sizes[0]
    assert peaks[1] < sizes[1] // 10, f"{peaks[1]} bytes were held for a file of {sizes[1]}"
    assert peaks[1] < peaks[0] + 50_000, f"{peaks}: the memory grew with the file {sizes}"
    flat = tmp_path / "flat.edif"
    _write_large_netlist(flat, 10_000)
    assert edif.modules_used_as_library_cells_in_file(flat) == []


def test_the_text_a_caller_holds_is_not_copied(tmp_path) -> None:
    """Given the text, the reader takes the memory of its state and no copy of the text."""
    path = tmp_path / "text.edif"
    size = _write_large_netlist(path, 10_000, ("core",))
    text = path.read_text(encoding="utf-8")
    found: list[str] = []
    peak = _peak_memory(lambda: found.extend(modules_used_as_library_cells(text)))
    assert found == ["core"]
    assert peak < size // 20, f"{peak} bytes were held for a text of {size}"


@pytest.mark.parametrize("garbage", ["word", "string", "forms"])
def test_text_that_is_no_netlist_is_held_no_longer_than_the_limits_allow(
    tmp_path, monkeypatch, garbage
) -> None:
    """One word without an end, a quote without its pair, forms that never close: the reader
    cuts the word, reads the text after the quote as it is, and ignores the forms below the
    depth it follows. A file four times as long takes the same memory."""
    monkeypatch.setattr(edif, "_TOKEN_LIMIT", 4096)
    monkeypatch.setattr(edif, "_DEPTH_LIMIT", 32)
    unit, opening = {"word": ("x", ""), "string": ("(a b) ", '"'), "forms": ("(", "")}[garbage]
    sizes, peaks = [], []
    for length in (100_000, 400_000):
        path = tmp_path / f"{length}.edif"
        path.write_text(opening + unit * (length // len(unit)), encoding="utf-8")
        sizes.append(path.stat().st_size)
        found: list[str] = []
        peaks.append(
            _peak_memory(
                lambda: found.extend(
                    edif.modules_used_as_library_cells_in_file(path, chunk_size=1 << 14)
                )
            )
        )
        assert found == []
    assert peaks[1] < peaks[0] + 100_000, f"{peaks}: the memory grew with the file {sizes}"
