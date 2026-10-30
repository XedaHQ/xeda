"""Where a design's source paths point: relative to the design root, however it is spelled."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from xeda import Design
from xeda.design import DesignValidationError, Generator, loading_in_design_root
from .design_edits import design_from_dump

ROOT_SPELLINGS = ["a.vhd", "$DESIGN_ROOT/a.vhd", "${DESIGN_ROOT}/a.vhd", "$DESIGN_DIR/a.vhd"]


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A design root holding `a.vhd` and `sub/b.vhd`; xeda is started somewhere else, and the
    environment has an unrelated `DESIGN_ROOT` that must not leak into the design."""
    design_root = tmp_path / "design"
    (design_root / "sub").mkdir(parents=True)
    (design_root / "a.vhd").write_text("entity a is end;\n")
    (design_root / "sub" / "b.vhd").write_text("entity b is end;\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("DESIGN_ROOT", str(elsewhere))
    monkeypatch.setenv("DESIGN_DIR", str(elsewhere))
    return design_root.resolve()


@pytest.mark.parametrize("source", ROOT_SPELLINGS)
def test_every_spelling_of_the_design_root_names_the_same_file(root, source):
    """`$DESIGN_ROOT` in a source was never expanded: the design root is a field of `Design`,
    not of the nested `rtl` settings whose validator looked for it, so the path kept a literal
    `$DESIGN_ROOT` component and only failed once something read the file."""
    design = Design(name="d", root=root, rtl={"sources": [source], "top": "a"})
    assert [s.file for s in design.rtl.sources] == [root / "a.vhd"]
    assert design.rtl_hash  # reads the file


@pytest.mark.parametrize(
    "pattern",
    [
        "sub/*.vhd",
        "$DESIGN_ROOT/sub/*.vhd",
        "$DESIGN_DIR/*/*.vhd",
        "sub/b*.vhd",
    ],
)
def test_a_glob_under_the_design_root_matches_its_files(root, pattern):
    """Variables are expanded before globbing, or the glob looks for a `$DESIGN_ROOT` directory
    and silently matches no sources at all."""
    design = Design(name="d", root=root, rtl={"sources": [pattern], "top": "b"})
    assert [s.file for s in design.rtl.sources] == [root / "sub" / "b.vhd"]


@pytest.mark.parametrize("source", ROOT_SPELLINGS)
def test_testbench_sources_resolve_the_same_way(root, source):
    """Testbench sources resolve the same way."""
    design = Design(
        name="d",
        root=root,
        rtl={"sources": ["sub/b.vhd"], "top": "b"},
        tb={"sources": [source], "top": "a"},
    )
    assert [s.file for s in design.tb.sources] == [root / "a.vhd"]


def test_a_design_file_resolves_the_design_root_from_its_own_location(root):
    """A design file resolves the design root from its own location."""
    (root / "d.toml").write_text(
        'name = "d"\n[rtl]\nsources = ["$DESIGN_ROOT/a.vhd", "$DESIGN_DIR/sub/*.vhd"]\ntop = "a"\n'
    )
    design = Design.load(root / "d.toml")
    assert [s.file for s in design.rtl.sources] == [root / "a.vhd", root / "sub" / "b.vhd"]


@pytest.mark.parametrize("source", ROOT_SPELLINGS)
def test_generator_sources_resolve_the_same_way(root, source):
    """Generator sources resolve the same way."""
    with loading_in_design_root(root):  # as `Design` builds its generator
        generator = Generator(command="true", sources=[source])
    assert generator.sources == [Path(root / "a.vhd")]


@pytest.mark.parametrize("key", ["file", "path"])
@pytest.mark.parametrize("prefix", ["", "$DESIGN_ROOT/", "$DESIGN_DIR/"])
def test_a_source_table_resolves_the_design_root_too(root, key, prefix):
    """A source table resolves the design root too."""
    design = Design(
        name="d",
        root=root,
        rtl={"sources": [{key: f"{prefix}a.vhd", "type": "Vhdl"}], "top": "a"},
    )
    assert [s.file for s in design.rtl.sources] == [root / "a.vhd"]


# ---------------------------------------------------------------------------------------------
# A source must exist when the design is loaded, unless it is given as `{ path = ... }`
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "section,source",
    [
        ("rtl", "missing.vhd"),
        ("rtl", "$DESIGN_ROOT/missing.vhd"),
        ("rtl", {"file": "missing.vhd", "type": "Vhdl"}),
        ("tb", "missing_tb.py"),
    ],
)
def test_a_missing_source_file_is_a_validation_error_naming_it(root, section, source):
    """A source that does not exist fails at load, not when a flow first reads it -- after the
    run directory is set up, as a bare `FileNotFoundError`."""
    sections = {"rtl": {"sources": ["a.vhd"], "top": "a"}}
    sections[section] = {"sources": [source], "top": "t"}
    with pytest.raises(DesignValidationError, match=f"{section}.sources") as e:
        Design(name="d", root=root, **sections)
    assert "does not exist" in str(e.value) and "missing" in str(e.value)


def test_a_source_given_as_a_path_is_not_checked(root):
    """For a file a generator creates later."""
    design = Design(
        name="d", root=root, rtl={"sources": ["a.vhd", {"path": "gen/top.v"}], "top": "a"}
    )
    assert design.rtl.sources[1].file == root / "gen" / "top.v"


def test_a_missing_parameter_file_is_a_validation_error(root):
    """A `FileNotFoundError` is neither a `ValueError` nor a `TypeError`, so from a validator it
    would have escaped as a traceback."""
    with pytest.raises(DesignValidationError, match="does not exist"):
        Design(
            name="d",
            root=root,
            rtl={"sources": ["a.vhd"], "top": "a", "parameters": {"INIT": {"file": "init.mem"}}},
        )


@pytest.mark.parametrize("spelling", ["init.mem", "$DESIGN_ROOT/init.mem"])
def test_a_parameter_file_resolves_like_a_source(root, spelling):
    """A parameter file resolves like a source."""
    (root / "init.mem").write_text("00\n")
    design = Design(
        name="d",
        root=root,
        rtl={"sources": ["a.vhd"], "top": "a", "parameters": {"INIT": {"file": spelling}}},
    )
    assert design.rtl.parameters["INIT"] == str(root / "init.mem")


def test_a_missing_generator_source_is_a_validation_error(root):
    """They are what decides whether the generator reruns: a missing one was a traceback from
    reading its modification time."""
    with pytest.raises(DesignValidationError, match="generator source file does not exist"):
        Design(
            name="d",
            root=root,
            rtl={
                "sources": ["a.vhd"],
                "top": "a",
                "generator": {"command": "true", "sources": ["gen.py"]},
            },
        )


def test_xeda_run_reports_a_missing_source_as_a_design_error(root):
    """The launcher swallowed a design file's validation error, so `--json` said only that the
    flow "did not complete successfully"."""
    (root / "d.toml").write_text('name = "d"\n[rtl]\nsources = ["missing.vhd"]\ntop = "a"\n')
    proc = subprocess.run(
        [sys.executable, "-m", "xeda", "run", "ghdl_sim", str(root / "d.toml"), "--json"],
        capture_output=True,
        text=True,
        cwd=root,
    )
    document = json.loads(proc.stdout)
    assert proc.returncode == 2 and document["outcome"] is None
    assert document["error"]["code"] == "invalid_design"
    assert "missing.vhd" in document["error"]["message"]


# ---------------------------------------------------------------------------------------------
# A pattern expands the same way every time, or not at all
# ---------------------------------------------------------------------------------------------


def test_a_glob_expands_in_a_stable_order(root):
    """`glob` returns filesystem order. Source order is semantic -- it is the order VHDL units
    are compiled in, and `test_run_identity` pins it into the design hash -- so an unsorted
    expansion gives one design a different identity on a different filesystem.
    """
    many = root / "many"
    many.mkdir()
    # Created in an order that is neither sorted nor reverse-sorted, and names that sort
    # lexicographically rather than numerically ("a10" before "a2"), so the test says which
    # order is meant.
    names = ["c.vhd", "a10.vhd", "a.vhd", "b.vhd", "a2.vhd"]
    for name in names:
        (many / name).write_text(f"-- {name}\n")

    design = Design(name="d", root=root, rtl={"sources": ["many/*.vhd"], "top": "a"})

    assert [s.file.name for s in design.rtl.sources] == sorted(names)
    # ... and a glob is worth exactly the sorted list of files written out by hand:
    spelled_out = Design(
        name="d",
        root=root,
        rtl={"sources": [f"many/{name}" for name in sorted(names)], "top": "a"},
    )
    assert design.rtl_hash == spelled_out.rtl_hash


@pytest.mark.parametrize(
    "pattern",
    ["sub/*.sv", "$DESIGN_ROOT/sub/*.sv", "nowhere/*.vhd", "sub/b*.sv"],
)
def test_a_glob_that_matches_nothing_is_an_error(root, pattern):
    """A mistyped pattern is an error naming it, not a load that contributes no sources and
    leaves a tool missing its top-level unit."""
    with pytest.raises(DesignValidationError, match="no file matches the source pattern") as e:
        Design(name="d", root=root, rtl={"sources": [pattern], "top": "b"})
    assert pattern in str(e.value)


def test_a_generator_glob_expands_in_a_stable_order_and_must_match(root):
    """A generator glob expands in a stable order and must match."""
    with loading_in_design_root(root):  # as `Design` builds its generator
        generator = Generator(command="true", sources=["$DESIGN_ROOT/*.vhd", "sub/*.vhd"])
        assert generator.sources == [root / "a.vhd", root / "sub" / "b.vhd"]
        with pytest.raises(ValueError, match="no file matches the generator source pattern"):
            Generator(command="true", sources=["*.sv"])
    with pytest.raises(ValueError, match="no design root says where it lies"):
        Generator(command="true", sources=["sub/b.vhd"])


def test_pwd_is_not_expanded_in_a_source(root):
    """Flow settings expand `$PWD`; a design source deliberately does not, so a source names the
    same file wherever xeda was started. The variable survives unexpanded into the error rather
    than quietly resolving against whichever directory that was."""
    with pytest.raises(DesignValidationError, match=r"\$PWD"):
        Design(name="d", root=root, rtl={"sources": ["$PWD/a.vhd"], "top": "a"})


# ---------------------------------------------------------------------------------------------
# Whether a generator has to run again is read from the same paths
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def generated(tmp_path):
    """A design root with a generator script, whose output `out.vhd` is declared and not there."""
    design_root = tmp_path / "design"
    design_root.mkdir()
    (design_root / "gen.py").write_text("pass\n")
    return design_root.resolve()


def _generated_design(root: Path, sources, generator=None) -> Design:
    recipe = generator or {"command": f"{sys.executable} gen.py", "sources": ["gen.py"]}
    return Design(name="d", root=root, rtl={"sources": sources, "top": "out_", "generator": recipe})


@pytest.mark.parametrize(
    "sources",
    [
        ["out.vhd"],
        ["$DESIGN_ROOT/out.vhd"],
        ["$DESIGN_DIR/out.vhd"],
        [{"path": "out.vhd"}],
        "out.vhd",  # the scalar shorthand
    ],
    ids=["relative", "design_root", "design_dir", "path_table", "scalar"],
)
def test_a_generated_source_is_the_same_declaration_however_it_is_spelled(generated, sources):
    """The generator has not run when the design loads, so its output is a declaration: the path
    below the design root that the design gives, which the generator writes. Every spelling of one
    path is one declaration, and the design has one storage key for them all."""
    from xeda.flow_runner.launch_generation import generation_key

    design = _generated_design(generated, sources)
    assert [(s.output_name, s.type.name, s.located) for s in design.rtl.sources] == [
        ("out.vhd", "Vhdl", False)
    ]
    reference = _generated_design(generated, ["out.vhd"])
    assert design.rtl.sources == reference.rtl.sources
    assert generation_key(design)[0] == generation_key(reference)[0]


@pytest.mark.parametrize("pattern", ["*.vhd", "o*.vhd"])
def test_a_generated_pattern_is_not_expanded_by_the_design(generated, pattern):
    (generated / "out.vhd").write_text("entity out_ is end;\n")  # a file the pattern could match
    design = _generated_design(generated, [pattern])
    assert [s.output_name for s in design.rtl.sources] == [pattern]


# ---------------------------------------------------------------------------------------------
# Expanding a path is a pure function of the path and the environment
# ---------------------------------------------------------------------------------------------


def test_expanding_a_path_accumulates_no_state():
    """`expand_env_vars` filtered the environment through a module-level list that it *appended*
    to on every call, so the list -- read on every path expansion -- grew without bound as a
    design's sources, parameters and generator inputs were resolved."""
    from xeda.utils import _ENV_BLACKLIST, expand_env_vars

    before = set(_ENV_BLACKLIST)
    results = {str(expand_env_vars("$DESIGN_ROOT/a.vhd", {"DESIGN_ROOT": "/r"})) for _ in range(50)}

    assert results == {str(Path("/r/a.vhd"))}
    assert set(_ENV_BLACKLIST) == before and len(_ENV_BLACKLIST) == len(before)


# ---------------------------------------------------------------------------------------------
# A generator has one job, and the design says what it is
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "generator",
    [
        f"{sys.executable} gen.py",
        [sys.executable, "gen.py"],
        {"command": f"{sys.executable} gen.py"},
        {"command": f"{sys.executable} gen.py", "env": {"UNRELATED": "1"}},
    ],
    ids=["shell_string", "argv", "table", "table_with_env"],
)
def test_a_generator_sees_the_design_root_and_its_storage_whatever_the_environment_says(
    root, generator, tmp_path
):
    """`$DESIGN_ROOT` names the design root in a design's paths, whatever the environment says,
    and a generator is told the same, with the storage in `XEDA_GENERATED`. Every form of the
    recipe gets both."""
    from xeda.design import generator_of

    design = Design(
        name="d",
        root=root,
        rtl={"sources": [{"path": "out.vhd"}], "top": "out_", "generator": generator},
    )
    recipe = generator_of(design.rtl, root=root)
    assert recipe is not None
    storage = tmp_path / "storage"
    environment = recipe.process_environment(
        root, storage, {"DESIGN_ROOT": "/another/project", "KEPT": "1"}
    )
    assert environment["DESIGN_ROOT"] == str(root)
    assert environment["XEDA_GENERATED"] == str(storage)
    assert ("KEPT" in environment) == (recipe.env is None), "an `env` is the whole environment"


def test_the_arguments_of_a_generator_expand_the_two_variables_and_no_other(root, tmp_path):
    from xeda.design import generator_of

    design = Design(
        name="d",
        root=root,
        rtl={
            "sources": [{"path": "out.vhd"}],
            "top": "out_",
            "generator": {
                "executable": "tool",
                "args": ["$DESIGN_ROOT/gen.py", "--out=${XEDA_GENERATED}/x", "$HOME", "$OTHER"],
            },
        },
    )
    recipe = generator_of(design.rtl, root=root)
    assert recipe is not None
    storage = tmp_path / "storage"
    assert recipe.execution_command(root, storage) == [
        "tool",
        f"{root}/gen.py",
        f"--out={storage}/x",
        "$HOME",
        "$OTHER",
    ]
    assert recipe.execution_command(root)[1] == "$DESIGN_ROOT/gen.py", "as written, for the key"


def test_an_unwritten_source_says_why_the_design_cannot_be_hashed(root):
    """Nothing promised to write this one, so it survives loading -- but a design with no
    content has no identity, and the error has to say that rather than report an errno."""
    design = Design(
        name="d", root=root, rtl={"sources": ["a.vhd", {"path": "never.vhd"}], "top": "a"}
    )

    with pytest.raises(FileNotFoundError, match="cannot be identified"):
        _ = design.rtl_hash


# ---------------------------------------------------------------------------------------------
# What a source pattern or path may name
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("root_name", ["proj[1]", "proj[v2]", "proj*"])
@pytest.mark.parametrize(
    "pattern",
    ["$DESIGN_ROOT/rtl/*.vhd", "$DESIGN_ROOT/rtl/m*ne.vhd", "$DESIGN_ROOT/rtl/mine.vhd"],
)
def test_the_design_root_is_a_place_not_a_pattern(tmp_path, root_name, pattern):
    """`$DESIGN_ROOT` goes into a glob as the directory it is. Unescaped, a root named
    `proj[1]` matched its sibling `proj1` -- another project's sources -- and `proj[v2]` matched
    nothing at all."""
    root = tmp_path / root_name
    (root / "rtl").mkdir(parents=True)
    (root / "rtl" / "mine.vhd").write_text("-- mine\n")
    (tmp_path / "proj1" / "rtl").mkdir(parents=True)
    (tmp_path / "proj1" / "rtl" / "someone_elses.vhd").write_text("-- not mine\n")

    design = Design(name="d", root=root, rtl={"sources": [pattern], "top": "t"})
    assert [src.file.name for src in design.rtl.sources] == ["mine.vhd"]
    assert design.rtl.sources[0].file.parent.parent == root.resolve()


@pytest.mark.parametrize(
    "name, decoys",
    [("foo[1].v", ["foo1.v"]), ("a?.v", ["ab.v"]), ("[x].vhd", ["x.vhd"]), ("f[!0].v", ["f1.v"])],
)
def test_only_a_star_makes_a_source_a_pattern(tmp_path, name, decoys):
    """`?`, `[` and `]` are ordinary characters of a file name (`foo[1].v`, a bus index), not
    pattern syntax. Read as a pattern, `foo[1].v` named the *other* file `foo1.v` whenever one
    existed -- silently -- and was rejected when none did."""
    for file in (name, *decoys):
        (tmp_path / file).write_text(f"// {file}\n")
    design = Design(name="d", root=tmp_path, rtl={"sources": [name], "top": "t"})
    assert [src.file.name for src in design.rtl.sources] == [name]
    with loading_in_design_root(tmp_path):  # as `Design` builds its generator
        assert Generator(command="true", sources=[name]).sources == [tmp_path / name]


def test_brackets_in_a_star_pattern_are_literal(tmp_path):
    """In a pattern, only `*` matches: the brackets of `foo[1]_*.v` are the file names' own."""
    for file in ("foo[1]_a.v", "foo[1]_b.v", "foo1_a.v"):
        (tmp_path / file).write_text(f"// {file}\n")
    design = Design(name="d", root=tmp_path, rtl={"sources": ["foo[1]_*.v"], "top": "t"})
    assert [src.file.name for src in design.rtl.sources] == ["foo[1]_a.v", "foo[1]_b.v"]


def test_a_source_is_a_file(tmp_path):
    """A directory is not a source: named outright it is a validation error naming it, where it
    used to load and then fail hashing with `IsADirectoryError`; matched by a pattern it is
    passed over, as a shell glob of source files would."""
    (tmp_path / "rtl" / "old.vhd").mkdir(parents=True)  # a directory with a source's name
    (tmp_path / "rtl" / "top.vhd").write_text("-- top\n")

    globbed = Design(name="d", root=tmp_path, rtl={"sources": ["rtl/*.vhd"], "top": "t"})
    assert [src.file.name for src in globbed.rtl.sources] == ["top.vhd"]

    for named in ("rtl", "rtl/old.vhd"):
        with pytest.raises(DesignValidationError, match="directory") as raised:
            Design(name="d", root=tmp_path, rtl={"sources": [named], "top": "t"})
        assert named.split("/")[-1] in str(raised.value)


def test_a_source_not_yet_written_can_be_validated_again(tmp_path):
    """A `{ path = ... }` source need not exist until a generator writes it, so it is one
    source by where it is, not by content it does not have yet: re-validating it (an assignment,
    a reload) used to hash it for de-duplication and raise `FileNotFoundError`."""
    (tmp_path / "top.vhd").write_text("-- top\n")
    design = Design(
        name="d",
        root=tmp_path,
        rtl={"sources": ["top.vhd", {"path": "gen/later.v"}, {"path": "gen/later.v"}], "top": "t"},
    )
    assert [src.file.name for src in design.rtl.sources] == ["top.vhd", "later.v"]

    assert (
        design_from_dump(design.model_dump()).rtl.sources[1].file
        == tmp_path.resolve() / "gen/later.v"
    )
