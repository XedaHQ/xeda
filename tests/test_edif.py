"""`xeda.edif.modules_used_as_library_cells`: the one thing `vivado_impl` asks of an EDIF netlist
before it hands the file to Vivado."""

import pytest

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
