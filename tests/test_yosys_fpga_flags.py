"""The `synth_<family>` command `yosys_fpga` generates, for every supported yosys release.

The oracle is read from the synthesis passes' sources of every yosys release from xeda's minimum
(0.63) to 0.69: `PASS_OPTIONS`, the options each pass lists in its help, keyed by the release
that changed them, and `ABC9_CONFLICTS`, the options a pass rejects while ABC9 is on (its
`log_cmd_error` checks). The sweep checks the flag mapping against both for every target,
release and setting: a setting becomes an option that pass has in that release, in a
combination the pass accepts, or it is rejected -- never dropped -- and it is rejected only when
the pass cannot honor it.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, FrozenSet, List

import pytest

from xeda import Design
from xeda.flow import FlowSettingsException
from xeda.flow_runner import DefaultRunner
from xeda.flows import Yosys, YosysFpga, YosysSim
from xeda.flows.yosys.common import (
    MINIMUM_YOSYS,
    NEWEST_CHECKED_YOSYS,
    YosysRelease,
    same_file,
    yosys_release,
)

from .tool_utils import _command_succeeds, require_yosys, require_yosys_config

# `synth_ecp5` runs `synth_lattice -family ecp5`, so the two take the same options.
LATTICE_0_63 = frozenset(
    "-noabc9 -abc2 -retime -dff -nobram -nolutram -nodsp -nowidelut -noflatten -family".split()
)
LATTICE_0_69 = frozenset("-dff -nobram -nolutram -nodsp -nowidelut -noflatten -family".split())

#: pass -> {release that changed its options: the options it has from then on}.
PASS_OPTIONS: Dict[str, Dict[YosysRelease, FrozenSet[str]]] = {
    "synth_xilinx": {
        (0, 63): frozenset(
            "-abc9 -retime -dff -nobram -nolutram -nodsp -nowidelut -flatten -widemux -family".split()
        ),
        (0, 69): frozenset(
            "-dff -nobram -nolutram -nodsp -nowidelut -flatten -widemux -family".split()
        ),
    },
    "synth_ecp5": {(0, 63): LATTICE_0_63, (0, 69): LATTICE_0_69},
    "synth_lattice": {(0, 63): LATTICE_0_63, (0, 69): LATTICE_0_69},
    "synth_ice40": {
        (0, 63): frozenset(
            "-noabc9 -noabc -abc2 -retime -dff -nobram -noflatten -spram -dsp -device".split()
        ),
        (0, 69): frozenset("-noabc -dff -nobram -noflatten -spram -dsp -device".split()),
    },
    "synth_gowin": {
        (0, 63): frozenset(
            "-noabc9 -retime -nobram -nolutram -nodsp -nowidelut -noflatten -family".split()
        ),
        (0, 69): frozenset("-nobram -nolutram -nodsp -nowidelut -noflatten -family".split()),
    },
}

#: Options a pass rejects while ABC9 is on before 0.69: `-retime` everywhere but Gowin,
#: which retimes with a separate classic ABC run, and `synth_ice40 -noabc`. In 0.69,
#: `-noabc` selects the built-in LUT mapper instead of ABC9.
ABC9_CONFLICTS: Dict[str, FrozenSet[str]] = {
    "synth_xilinx": frozenset({"-retime"}),
    "synth_ecp5": frozenset({"-retime"}),
    "synth_lattice": frozenset({"-retime"}),
    "synth_ice40": frozenset({"-retime", "-noabc"}),
}

#: Every yosys release xeda supports.
RELEASES = [(0, minor) for minor in range(MINIMUM_YOSYS[1], NEWEST_CHECKED_YOSYS[1] + 1)]

TARGETS: Dict[str, Dict[str, Any]] = {
    "xilinx": {"part": "xc7a35tcpg236-1"},
    # `synth_xilinx` rejects -widemux for its LUT4-based families.
    "xilinx-lut4": {"vendor": "xilinx", "family": "spartan3"},
    "ecp5": {"part": "LFE5U-25F-6BG256C"},
    "ice40-hx": {"part": "iCE40HX1K-TQ144"},
    "ice40-up": {"part": "iCE40UP5K-SG48I"},
    "crosslink-nx": {"part": "LIFCL-40-9BG400C"},
    "certus-nx": {"part": "LFD2NX-40-7BG256C"},
    "gw1n": {"vendor": "gowin", "family": "gowin", "device": "GW1N-9"},
    "gw2a": {"vendor": "gowin", "family": "gowin", "device": "GW2A-18"},
    "gw5a": {"vendor": "gowin", "family": "gowin", "device": "GW5A-25"},
}

#: Settings that become one option, when the pass has it.
OPTION_OF = {
    "abc_dff": "-dff",
    "nobram": "-nobram",
    "nolutram": "-nolutram",
    "nowidelut": "-nowidelut",
    "noabc": "-noabc",
    "ice40_device": "-device",
}

TOGGLES: List[Dict[str, Any]] = [
    {"abc9": False},
    {"retime": True},
    {"retime": True, "abc9": False},
    {"abc_dff": True},
    {"nobram": True},
    {"nolutram": True},
    {"nodsp": True},
    {"nowidelut": True},
    {"widemux": 5},
    {"noabc": True},
    {"flatten": True},
    {"flatten": False},
    {"ice40_device": "lp"},
    {"ice40_dsp": True},
    {"ice40_spram": True},
]

REJECT = object()


def pass_options(name: str, release: YosysRelease) -> FrozenSet[str]:
    changes = PASS_OPTIONS[name]
    return changes[max(r for r in changes if r <= release)]


def abc9_on(command: List[str], options: FrozenSet[str]) -> bool:
    """Whether the pass maps with ABC9: an opt-in `-abc9`, a default `-noabc9`, or always."""
    if "-noabc" in command:
        return False
    if "-abc9" in options:
        return "-abc9" in command
    if "-noabc9" in options:
        return "-noabc9" not in command
    return True


def expected(target: str, name: str, release: YosysRelease, toggle: Dict[str, Any]) -> Any:
    """What `toggle` must become, from the oracle: an option, None (nothing), or REJECT."""
    options = pass_options(name, release)
    key, value = next(iter(toggle.items()))
    if key == "retime":
        if "-retime" not in options:
            return REJECT
        # ABC9 is on unless `abc9=false`, and every pass but Gowin's rejects -retime with it.
        abc9_off = toggle.get("abc9") is False
        return "-retime" if abc9_off or name not in ABC9_CONFLICTS else REJECT
    if key == "abc9":  # classic ABC: drop an opt-in `-abc9`, or turn the default off
        return "-noabc9" if "-noabc9" in options else None if "-abc9" in options else REJECT
    if key == "flatten":  # each pass does one of the two by default
        option = "-flatten" if value else "-noflatten"
        return option if option in options else None
    if key == "nodsp":  # a pass without -nodsp maps no DSPs unless asked (`synth_ice40 -dsp`)
        return "-nodsp" if "-nodsp" in options else None
    if key == "widemux":
        return "-widemux" if "-widemux" in options and target != "xilinx-lut4" else REJECT
    if key in ("ice40_dsp", "ice40_spram"):  # UltraPlus resources
        return (
            {"ice40_dsp": "-dsp", "ice40_spram": "-spram"}[key] if target == "ice40-up" else REJECT
        )
    option = OPTION_OF[key]
    return option if option in options else REJECT


@pytest.mark.parametrize("target", TARGETS)
def test_every_setting_becomes_an_option_of_that_release_or_is_rejected(target):
    fpga = TARGETS[target]
    for release in RELEASES:
        default = YosysFpga.Settings(fpga=fpga).synth_command(release)
        name = default[0]
        options = pass_options(name, release)
        # ABC9 is the default mapper for every target and release.
        assert abc9_on(default, options), (release, default)
        for toggle in TOGGLES:
            want = expected(target, name, release, toggle)
            case = f"{target} yosys {release} {toggle}"
            try:
                command = YosysFpga.Settings(fpga=fpga, **toggle).synth_command(release)
            except FlowSettingsException:
                assert want is REJECT, f"{case}: rejected, but {name} has {want or 'no need'}"
                continue
            assert want is not REJECT, f"{case}: {name} cannot honor it: {command}"
            unknown = [t for t in command if t.startswith("-") and t not in options]
            assert not unknown, f"{case}: {name} has no {unknown}"
            if abc9_on(command, options):
                clash = ABC9_CONFLICTS.get(name, frozenset()).intersection(command)
                assert not clash, f"{case}: {name} rejects {clash} while ABC9 is on: {command}"
            if want:
                assert want in command, f"{case}: {want} missing from {command}"
            elif toggle == {"abc9": False}:
                assert "-abc9" not in command, case


@pytest.mark.parametrize(
    "target,release,command",
    [
        ("crosslink-nx", (0, 63), ["synth_lattice", "-family", "lifcl"]),
        ("certus-nx", (0, 69), ["synth_lattice", "-family", "lfd2nx"]),
        ("ecp5", (0, 63), ["synth_ecp5"]),
        ("xilinx", (0, 68), ["synth_xilinx", "-family", "xc7", "-abc9", "-flatten"]),
        ("xilinx", (0, 69), ["synth_xilinx", "-family", "xc7", "-flatten"]),
        ("ice40-up", (0, 69), ["synth_ice40", "-device", "u"]),
        ("ice40-hx", (0, 63), ["synth_ice40", "-device", "hx"]),
        ("gw1n", (0, 63), ["synth_gowin", "-family", "gw1n"]),
        ("gw5a", (0, 69), ["synth_gowin", "-family", "gw5a"]),
    ],
)
def test_default_command(target, release, command):
    assert YosysFpga.Settings(fpga=TARGETS[target]).synth_command(release) == command


@pytest.mark.parametrize("target", ["xilinx", "xilinx-lut4"])
def test_xilinx_flattens_by_default_but_the_pass_only_mode_leaves_it_to_the_pass(target):
    """`synth_xilinx` keeps the hierarchy unless told to flatten; xeda's recipe flattens it (the
    measured better default), and `synth_pass_only` keeps the pass's own default."""
    fpga = TARGETS[target]
    for release in RELEASES:
        flattened = YosysFpga.Settings(fpga=fpga).synth_command(release)
        assert "-flatten" in flattened, (release, flattened)
        for settings in (
            {"flatten": False},
            {"synth_pass_only": True},
            {"synth_pass_only": True, "flatten": False},
        ):
            command = YosysFpga.Settings(fpga=fpga, **settings).synth_command(release)
            assert "-flatten" not in command, (release, settings, command)
        explicit = YosysFpga.Settings(fpga=fpga, synth_pass_only=True, flatten=True)
        assert "-flatten" in explicit.synth_command(release)


@pytest.mark.parametrize("target", [t for t in TARGETS if not t.startswith("xilinx")])
def test_the_other_targets_pass_no_flatten_option_by_default(target):
    """Their passes flatten unless told `-noflatten`, so an unset `flatten` adds no option."""
    for release in RELEASES:
        for pass_only in (False, True):
            settings = YosysFpga.Settings(fpga=TARGETS[target], synth_pass_only=pass_only)
            command = settings.synth_command(release)
            assert not {"-flatten", "-noflatten"} & set(command), (release, pass_only, command)


def test_classic_abc_and_no_abc_are_distinct_on_ice40():
    fpga = TARGETS["ice40-hx"]
    classic = YosysFpga.Settings(fpga=fpga, abc9=False).synth_command((0, 68))
    assert "-noabc9" in classic and "-noabc" not in classic
    # Before 0.69 -noabc needs ABC9 off, which it is not by default.
    assert YosysFpga.Settings(fpga=fpga, noabc=True).synth_command((0, 68))[1:3] == [
        "-noabc9",
        "-noabc",
    ]
    with pytest.raises(FlowSettingsException, match="noabc=true"):
        YosysFpga.Settings(fpga=fpga, abc9=False).synth_command((0, 69))
    no_abc = YosysFpga.Settings(fpga=fpga, noabc=True).synth_command((0, 69))
    assert "-noabc" in no_abc and "-noabc9" not in no_abc
    with pytest.raises(FlowSettingsException, match="abc_dff needs ABC"):
        YosysFpga.Settings(fpga=fpga, noabc=True, abc_dff=True).synth_command((0, 69))


def test_retime_needs_classic_abc():
    xilinx = TARGETS["xilinx"]
    with pytest.raises(FlowSettingsException, match="needs abc9=false"):
        YosysFpga.Settings(fpga=xilinx, retime=True).synth_command((0, 68))
    command = YosysFpga.Settings(fpga=xilinx, retime=True, abc9=False).synth_command((0, 68))
    assert "-retime" in command and "-abc9" not in command
    gowin = YosysFpga.Settings(fpga=TARGETS["gw1n"], retime=True).synth_command((0, 68))
    assert "-retime" in gowin
    with pytest.raises(FlowSettingsException, match="removed"):
        YosysFpga.Settings(fpga=xilinx, retime=True).synth_command((0, 69))


def test_widemux_is_off_or_at_least_two():
    with pytest.raises(ValueError, match="at least 2"):
        YosysFpga.Settings(fpga=TARGETS["xilinx"], widemux=1)


@pytest.mark.parametrize(
    "fpga,message",
    [
        ({"vendor": "gowin", "family": "gowin"}, "requires a device"),
        ({"vendor": "gowin", "family": "gw2a", "device": "GW5A-25"}, "conflicts"),
        ({"vendor": "xilinx", "family": "versal"}, "has no family"),
        ({"vendor": "xilinx", "device": "mystery"}, "Cannot select synth_xilinx family"),
        ({"vendor": "xilinx", "part": "XCMYSTERY"}, "Cannot select synth_xilinx family"),
        ({"vendor": "lattice", "family": "machxo2"}, "no FPGA synthesis"),
        ({"vendor": "lattice", "family": "nexus", "device": "unknown"}, "needs an LIFCL"),
        ({"part": "iCE40HX1K-TQ144", "type": "ul"}, "Unknown iCE40"),
    ],
)
def test_unknown_targets_are_rejected(fpga, message):
    with pytest.raises(FlowSettingsException, match=message):
        YosysFpga.Settings(fpga=fpga).synth_command(NEWEST_CHECKED_YOSYS)


@pytest.mark.parametrize(
    "family,flag",
    [("kintex-us", "xcu"), ("virtex-usp", "xcup"), ("zynq-7", "xc7"), ("xc2v", "xc2v")],
)
def test_xilinx_family(family, flag):
    settings = YosysFpga.Settings(fpga={"vendor": "xilinx", "family": family})
    assert settings.synth_command(NEWEST_CHECKED_YOSYS)[1:3] == ["-family", flag]


@pytest.mark.parametrize(
    "fpga,flag",
    [
        ({"vendor": "xilinx", "generation": "usp"}, "xcup"),
        ({"vendor": "xilinx", "generation": "u"}, "xcu"),
        ({"vendor": "xilinx", "generation": 7}, "xc7"),
        ({"part": "XCVU9P-FLGA2104-2"}, "xcup"),
        ({"part": "XCVU095-2FLGA2104"}, "xcu"),
    ],
)
def test_xilinx_generation_selects_synthesis_family(fpga, flag):
    command = YosysFpga.Settings(fpga=fpga).synth_command(NEWEST_CHECKED_YOSYS)
    assert command[1:3] == ["-family", flag]


def test_unspecified_xilinx_target_keeps_series_7_default():
    command = YosysFpga.Settings(fpga={"vendor": "xilinx"}).synth_command(NEWEST_CHECKED_YOSYS)
    assert "-family" not in command


def test_ice40_options_are_rejected_for_other_targets():
    with pytest.raises(FlowSettingsException, match="iCE40 targets"):
        YosysFpga.Settings(fpga=TARGETS["ecp5"], ice40_device="hx").synth_command((0, 69))
    with pytest.raises(FlowSettingsException, match="cannot both"):
        YosysFpga.Settings(fpga=TARGETS["ice40-up"], ice40_dsp=True, nodsp=True).synth_command(
            (0, 69)
        )


def test_sta_needs_a_flat_design(tmp_path):
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "d"})
    settings = YosysFpga.Settings(fpga=TARGETS["ecp5"], sta=True, flatten=False)
    with pytest.raises(FlowSettingsException, match="flatten=false"):
        YosysFpga(settings, design, tmp_path).init()
    flow = YosysFpga(YosysFpga.Settings(fpga=TARGETS["ecp5"], sta=True), design, tmp_path)
    flow.init()
    assert flow.settings.flatten is True
    assert "-noflatten" not in flow.settings.synth_command((0, 69))


@pytest.mark.parametrize(
    "version,release",
    [
        (("0", "69+152"), (0, 69)),
        (("0", "66"), (0, 66)),
        (("0", "71"), NEWEST_CHECKED_YOSYS),  # newer than checked: taken as the newest
        ((), NEWEST_CHECKED_YOSYS),  # unreadable
    ],
)
def test_yosys_release(version, release):
    tool = SimpleNamespace(version=version, version_str=".".join(version))
    assert yosys_release(tool) == release  # type: ignore[arg-type]


def test_only_fpga_synthesis_raises_the_yosys_minimum(tmp_path, monkeypatch):
    """The FPGA flag floor must not narrow generic synthesis or CXXRTL compatibility."""
    monkeypatch.setattr("xeda.flows.yosys.common.Tool", lambda **kwargs: SimpleNamespace(**kwargs))
    (tmp_path / "d.v").write_text("module d; endmodule\n")  # `YosysSim` needs a source it reads
    design = Design(name="d", design_root=tmp_path, rtl={"sources": ["d.v"], "top": "d"})
    for flow_class, settings, expected in (
        (Yosys, Yosys.Settings(), (0, 21)),
        (YosysFpga, YosysFpga.Settings(fpga=TARGETS["ecp5"]), MINIMUM_YOSYS),
        (YosysSim, YosysSim.Settings(), None),
    ):
        flow = flow_class(settings, design, tmp_path)
        assert flow.yosys.minimum_version == expected


COUNTER = """
module top(input clk, input [7:0] a, b, output reg [15:0] q);
  always @(posedge clk) q <= q + a * b;
endmodule
"""


@pytest.mark.parametrize("target", TARGETS)
def test_installed_yosys_accepts_the_generated_command(target, tmp_path):
    """Everything the installed yosys's pass takes, all at once, must run."""
    require_yosys()
    version = subprocess.run(["yosys", "-V"], capture_output=True, text=True, check=True).stdout
    match = re.search(r"Yosys (\d+)\.(\d+)", version)
    assert match, version
    release: YosysRelease = min((int(match[1]), int(match[2])), NEWEST_CHECKED_YOSYS)
    fpga = TARGETS[target]
    accepted: Dict[str, Any] = {}
    for toggle in TOGGLES:
        if "flatten" in toggle or ("noabc" in toggle and "abc9" in accepted):
            continue
        try:
            YosysFpga.Settings(fpga=fpga, **{**accepted, **toggle}).synth_command(release)
        except FlowSettingsException:
            continue
        accepted.update(toggle)
    source = tmp_path / "top.v"
    source.write_text(COUNTER)
    for flatten in (True, False, None):
        settings = YosysFpga.Settings(fpga=fpga, flatten=flatten, **accepted)
        script = f"read_verilog {source}; {' '.join(settings.synth_command(release))} -top top"
        result = subprocess.run(["yosys", "-q", "-p", script], capture_output=True, text=True)
        assert result.returncode == 0, f"{script}\n{result.stdout}\n{result.stderr}"


def _begin_reads(pass_name: str) -> list[tuple[frozenset[str], str]]:
    """What the installed yosys's `pass_name` reads in its `begin` step: one `(flags, path)` for
    every file `read_verilog` names, from the pass's own help (`yosys -h`)."""
    text = subprocess.run(
        ["yosys", "-p", f"help {pass_name}"], capture_output=True, text=True, check=True
    ).stdout
    begin = re.search(r"^    begin:\n((?:        .*\n)+)", text, re.MULTILINE)
    if not begin and pass_name in ("synth_ecp5", "synth_nexus"):
        return _begin_reads("synth_lattice")  # they run it with `-family`
    assert begin, f"no `begin` step in the help of {pass_name}"
    reads = []
    for line in begin[1].splitlines():
        words = line.split()
        if words[:1] != ["read_verilog"]:
            continue
        paths = [word for word in words[1:] if word.startswith("+/")]
        flags = frozenset(word for word in words[1:] if word not in paths)
        reads += [(flags, path) for path in paths]
    return reads


_LIB = frozenset({"-lib", "-specify"})
_LATTICE_ECP5 = {(_LIB, "+/lattice/cells_sim_ecp5.v"), (_LIB, "+/lattice/cells_bb_ecp5.v")}
_LATTICE_NEXUS = {(_LIB, "+/lattice/cells_sim_nexus.v"), (_LIB, "+/lattice/cells_bb_nexus.v")}
_XILINX = {(_LIB, "+/xilinx/cells_sim.v"), (frozenset({"-lib"}), "+/xilinx/cells_xtra.v")}


def _gowin(family: str):
    return {(_LIB, "+/gowin/cells_sim.v"), (_LIB, f"+/gowin/cells_xtra_{family}.v")}


#: What each target's pass reads in its `begin` step, as `(flags, path)`. Every supported release
#: (0.63 to 0.69) reads the same files. That was checked by hand, once per release: by running
#: `synth_ecp5`, `synth_lattice`, `synth_nexus`, `synth_gowin`, `synth_xilinx` and `synth_ice40`
#: and listing the files the frontend read, and by reading each pass's help. Only the installed
#: yosys is compared with this table by a test (below). `synth_ecp5` is `synth_lattice -family
#: ecp5` in every supported release, so it reads the Lattice library of its family, never
#: `+/ecp5/cells_sim.v`. The iCE40 define is left out; it follows the device, and
#: `test_the_ice40_library_is_defined_for_the_device` covers it.
PASS_READS: Dict[str, set] = {
    "xilinx": _XILINX,
    "xilinx-lut4": _XILINX,
    "ecp5": _LATTICE_ECP5,
    "ice40-hx": {(_LIB, "+/ice40/cells_sim.v")},
    "ice40-up": {(_LIB, "+/ice40/cells_sim.v")},
    "crosslink-nx": _LATTICE_NEXUS,
    "certus-nx": _LATTICE_NEXUS,
    "gw1n": _gowin("gw1n"),
    "gw2a": _gowin("gw2a"),
    "gw5a": _gowin("gw5a"),
}


def _as_read(library) -> tuple:
    flags = frozenset(w for w in library.flags if w != "-D" and not w.startswith("ICE40_"))
    return flags, library.path


def test_every_target_has_a_pass_read_to_compare_with():
    assert set(PASS_READS) == set(TARGETS)


@pytest.mark.parametrize("target", TARGETS)
def test_primitive_libraries_are_exactly_what_the_pass_reads(target):
    """Equal as sets, so a library the pass reads and xeda does not (Gowin's, once) fails as a
    library xeda reads and the pass does not (ECP5's old `+/ecp5/cells_sim.v`) does. The table
    holds for every supported release, so one list serves them all."""
    libraries = YosysFpga.Settings(fpga=TARGETS[target]).primitive_libraries()
    reads = [_as_read(library) for library in libraries]
    assert len(reads) == len(set(reads)), libraries
    assert set(reads) == PASS_READS[target], (target, libraries)


def _named_by_help(path: str) -> str:
    """A library's path as `yosys -h` writes it: without its family."""
    path = re.sub(r"_(ecp5|nexus)(?=\.v$)", "", path)
    return re.sub(r"(?<=cells_xtra_)gw\w+(?=\.v$)", "<family>", path)


def _yosys_data_dir() -> Path:
    """What `+/` stands for, from the `yosys-config` beside the installed `yosys`: the flow asks
    that one too, never another `yosys-config` that happens to be on `PATH`."""
    config = Path(shutil.which("yosys") or "yosys").with_name("yosys-config")
    assert config.exists(), f"no yosys-config beside {shutil.which('yosys')}"
    return Path(
        subprocess.run(
            [str(config), "--datdir"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )


def _installed_release() -> YosysRelease:
    version = subprocess.run(["yosys", "-V"], capture_output=True, text=True, check=True).stdout
    match = re.search(r"Yosys (\d+)\.(\d+)", version)
    assert match, version
    return min((int(match[1]), int(match[2])), NEWEST_CHECKED_YOSYS)


@pytest.mark.parametrize("target", TARGETS)
def test_primitive_libraries_are_read_the_way_the_installed_pass_reads_them(target, tmp_path):
    """The installed pass, both ways: the files its frontend reads in `begin` are the files xeda
    reads, and with the flags its help gives them (`-lib -specify`). The table the other test
    checks every release against must agree with it too."""
    require_yosys()
    require_yosys_config()
    release = _installed_release()
    settings = YosysFpga.Settings(fpga=TARGETS[target])
    libraries = settings.primitive_libraries()
    assert {_as_read(library) for library in libraries} == PASS_READS[target]
    # the help names a family's file without its family: `cells_sim.v` for `cells_sim_ecp5.v`,
    # `cells_xtra_<family>.v` for `cells_xtra_gw1n.v`
    pass_reads = _begin_reads(settings.synth_command(release)[0])
    named_by_help = {
        (flags, _named_by_help(path)) for flags, path in (_as_read(x) for x in libraries)
    }
    assert named_by_help == {
        (frozenset(f for f in flags if f != "-D" and not f.startswith("ICE40_")), path)
        for flags, path in pass_reads
    }, (libraries, pass_reads)
    # and the files it really reads, from a run: the first command reads the design, the pass
    # then reads its libraries before it checks the hierarchy
    (tmp_path / "top.v").write_text("module top(input a, output b); assign b = a; endmodule\n")
    command = " ".join([*settings.synth_command(release), "-top", "top", "-run", "begin:coarse"])
    log = subprocess.run(
        ["yosys", "-p", f"read_verilog top.v; {command}"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    datdir = _yosys_data_dir()
    files = re.findall(r"^\d+\.\d+\. Executing Verilog-2005 frontend: (.*)$", log, re.MULTILINE)
    read = [Path(f) for f in files if Path(f).name != "top.v"]
    expected = [datdir / library.path.removeprefix("+/") for library in libraries]
    # by file, not by spelling: a Homebrew yosys reports `<prefix>/bin/../share/yosys/...` while
    # `yosys-config` names the same directory through the Cellar
    assert len(read) == len(expected), (read, expected)
    assert all(any(same_file(r, e) for r in read) for e in expected), (read, expected)


def test_the_ice40_library_is_defined_for_the_device():
    for part, define in (("iCE40HX1K-TQ144", "ICE40_HX"), ("iCE40UP5K-SG48I", "ICE40_U")):
        (library,) = YosysFpga.Settings(fpga={"part": part}).primitive_libraries()
        assert library.flags[:2] == ("-D", define), library


@pytest.mark.parametrize(
    "target,pass_only,flatten,expected",
    [
        # Xilinx: an unset `flatten` is `True`, except that `synth_pass_only` leaves it to the
        # pass, which keeps the hierarchy
        ("xilinx", False, None, True),
        ("xilinx", False, True, True),
        ("xilinx", False, False, False),
        ("xilinx", True, None, False),
        ("xilinx", True, True, True),
        ("xilinx", True, False, False),
        ("xilinx-lut4", False, None, True),
        # the others: unset is the pass's own choice, which is to flatten
        ("ecp5", False, None, None),
        ("ecp5", True, None, None),
        ("ecp5", False, True, True),
        ("gw1n", False, False, False),
        ("ice40-hx", True, True, True),
    ],
)
def test_effective_flatten(target, pass_only, flatten, expected):
    settings = YosysFpga.Settings(fpga=TARGETS[target], synth_pass_only=pass_only, flatten=flatten)
    assert settings.effective_flatten() is expected


HIERARCHY = """\
module leaf(input a, output y); assign y = ~a; endmodule
module top(input clk, input a, output q);
  wire n;
  leaf u(.a(a), .y(n));
  reg r;
  always @(posedge clk) r <= n;
  assign q = r;
endmodule
"""

#: Every target with `flatten` unset, and both values of it on a Xilinx and on another target;
#: all in a `.ys` script, and three targets in a `.tcl` one.
RTL_CASES = (
    [(target, None, "ys") for target in TARGETS]
    + [(target, flatten, "ys") for target in ("xilinx", "ecp5") for flatten in (True, False)]
    + [(target, None, "tcl") for target in ("xilinx", "ecp5", "gw1n")]
)


@pytest.mark.parametrize(
    "target,flatten,script_format",
    RTL_CASES,
    ids=[f"{target}-{flatten}-{script_format}" for target, flatten, script_format in RTL_CASES],
)
def test_the_rtl_outputs_hold_only_the_designs_modules(target, flatten, script_format, tmp_path):
    """The RTL outputs are written before synthesis, with every primitive library the target's
    pass reads still in the design as boxes. `write_json` cannot write a box that keeps its
    `always` blocks (`ERROR: Module ALU contains processes`), and `write_verilog` and `show`
    skip boxes; so all three describe the design's own modules, and nothing else. The `.tcl`
    script, which not every yosys build can run, must also keep each command on its own line.

    A flat design is the proof of what `flatten` did before the outputs were written: an unset
    `flatten` is `True` on Xilinx, so there the hierarchy is gone from them too."""
    require_yosys()
    if script_format == "tcl" and not _command_succeeds(["yosys", "-q", "-c", "/dev/null"]):
        pytest.skip("this yosys has no TCL support")  # oss-cad-suite has none
    root = tmp_path / "design"
    root.mkdir()
    (root / "top.v").write_text(HIERARCHY)
    design = Design(
        name="top",
        design_root=root,
        rtl={"sources": ["top.v"], "top": "top", "clock": {"port": "clk"}},
    )
    settings: Dict[str, Any] = {
        "fpga": TARGETS[target],
        "clock": {"period": 5.0},
        "rtl_json": "rtl.json",
        "rtl_verilog": "rtl.v",
        "rtl_graph": "rtl.dot",
        "script_format": script_format,
    }
    if flatten is not None:
        settings["flatten"] = flatten
    flow = DefaultRunner(tmp_path / "run", display_results=False).run(
        YosysFpga, design, flow_settings=settings
    )
    assert flow is not None and flow.succeeded
    run = Path(flow.run_path)
    flat = flatten if flatten is not None else target.startswith("xilinx")
    modules = {"top"} if flat else {"leaf", "top"}
    assert set(json.loads((run / "rtl.json").read_text())["modules"]) == modules
    verilog = (run / "rtl.v").read_text()
    assert set(re.findall(r"^module\s+(\S+?)\s*\(", verilog, re.MULTILINE)) == modules
    graphs = re.findall(r'^digraph "([^"]+)"', (run / "rtl.dot").read_text(), re.MULTILINE)
    assert set(graphs) == modules
