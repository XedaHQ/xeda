"""The `synth_<family>` command `yosys_fpga` generates, for every supported yosys release.

The oracle is read from the synthesis passes' sources of every yosys release from xeda's minimum
(0.63) to 0.69: `PASS_OPTIONS`, the options each pass lists in its help, keyed by the release
that changed them, and `ABC9_CONFLICTS`, the options a pass rejects while ABC9 is on (its
`log_cmd_error` checks). The sweep checks the flag mapping against both for every target,
release and setting: a setting becomes an option that pass has in that release, in a
combination the pass accepts, or it is rejected -- never dropped -- and it is rejected only when
the pass cannot honor it.
"""

import re
import subprocess
from types import SimpleNamespace
from typing import Any, Dict, FrozenSet, List

import pytest

from xeda import Design
from xeda.flow import FlowSettingsException
from xeda.flows import Yosys, YosysFpga, YosysSim
from xeda.flows.yosys.common import (
    MINIMUM_YOSYS,
    NEWEST_CHECKED_YOSYS,
    YosysRelease,
    yosys_release,
)

from .tool_utils import require_yosys

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
        ("xilinx", (0, 68), ["synth_xilinx", "-family", "xc7", "-abc9"]),
        ("xilinx", (0, 69), ["synth_xilinx", "-family", "xc7"]),
        ("ice40-up", (0, 69), ["synth_ice40", "-device", "u"]),
        ("ice40-hx", (0, 63), ["synth_ice40", "-device", "hx"]),
        ("gw1n", (0, 63), ["synth_gowin", "-family", "gw1n"]),
        ("gw5a", (0, 69), ["synth_gowin", "-family", "gw5a"]),
    ],
)
def test_default_command(target, release, command):
    assert YosysFpga.Settings(fpga=TARGETS[target]).synth_command(release) == command


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
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "d"})
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
