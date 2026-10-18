"""The escape hatches of the `yosys` flows: lists of flags for one command.

A hatch is a list setting whose items go to one command, such as `synth_flags`. It exists
because the typed settings do not cover every option of the command. It must not repeat an
option that a setting renders or that the script fixes. Then one setting says each thing, and a
hatch never overrides a setting without a message.

`OWNED_FLAGS` holds one table per command: each flag that a hatch refuses, with its owner. The
owner is a setting, or the script when no setting can say it. `HATCHES` lists which setting
feeds which command. `flag_problems` is the one check; it reads every word of every item.
"""

import re
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from .asic_groups import AbcGroup

__all__ = [
    "ABC_NEW_LACKS",
    "HATCHES",
    "LIBERTY_HATCHES",
    "OWNED_FLAGS",
    "Hatch",
    "Owner",
    "SLANG_HATCHES",
    "WRITE_VERILOG_SWITCHES",
    "flag_problems",
    "owned_words",
]


class Owner(NamedTuple):
    """Who sets a flag: the `setting` that renders it, or else the script, with the `reason`.

    A `joined` flag may hold its value in the same word (`-DNAME`, `-Iinclude`)."""

    setting: Optional[str] = None
    reason: str = ""
    joined: bool = False


def _setting(name: str, joined: bool = False) -> Owner:
    return Owner(setting=name, joined=joined)


def _fixed(reason: str, joined: bool = False) -> Owner:
    return Owner(reason=reason, joined=joined)


#: The switches of the netlist: for each, the setting, the flag of `write_verilog`, and the
#: value of the setting that adds the flag. `write_verilog_flags` reads this table too.
WRITE_VERILOG_SWITCHES: Tuple[Tuple[str, str, bool], ...] = (
    ("netlist_dec", "-nodec", False),
    ("netlist_hex", "-nohex", False),
    ("netlist_expr", "-noexpr", False),
    ("netlist_attrs", "-noattr", False),
    ("netlist_blackboxes", "-blackboxes", True),
    ("netlist_simple_lhs", "-simple-lhs", True),
)

_HEADERS = "the script passes the directories of the header sources of the design"

#: For each command, the flags that its hatch refuses.
OWNED_FLAGS: Dict[str, Dict[str, Owner]] = {
    "synth": {
        "-top": _fixed("the script passes the top module of the design (`rtl.top`)"),
        "-flatten": _setting("flatten"),
        "-run": _fixed("the script runs `synth` in two parts"),
        "-noabc": _fixed("the script runs `abc` itself, after the flip-flop mapping"),
        "-extra-map": _setting("arithmetic.adder_architecture"),
        "-booth": _setting("arithmetic.multiplier"),
        "-hieropt": _setting("hierarchy.cross_boundary_opt"),
        "-lut": _fixed("a mapping to a liberty library maps to cells, not to LUTs"),
        "-nofsm": _setting("coarse.fsm"),
        "-noshare": _setting("coarse.share"),
        "-noalumacc": _setting("arithmetic.alumacc"),
        "-arith_tree": _setting("arithmetic.arith_tree"),
        "-nordff": _setting("memory.merge_ff"),
        "-no-rw-check": _setting("memory.rw_check"),
        "-latches": _setting("coarse.latches"),
    },
    "abc": {
        **{flag.flag: _setting(f"abc.{flag.field}") for flag in AbcGroup.FLAGS},
        "-keepff": _setting("abc.keep_ff"),
        "-D": _setting("abc.delay_target"),
        "-script": _setting("abc_script"),
        "-constr": _setting("abc_constr"),
        "-liberty": _setting("liberty"),
        "-dont_use": _setting("dont_use_cells"),
        "-dff": _fixed(
            "a liberty mapping maps the flip-flops with `dfflibmap` before `abc` runs, so `-dff` "
            "has no effect"
        ),
        "-g": _fixed("a mapping to a liberty library maps to cells, not to generic gates"),
        "-lut": _fixed("a mapping to a liberty library maps to cells, not to LUTs"),
        "-luts": _fixed("a mapping to a liberty library maps to cells, not to LUTs"),
    },
    "read_verilog": {
        "-defer": _fixed("the script reads each Verilog source with `-defer`"),
        "-I": _fixed(_HEADERS, joined=True),
        "-D": _setting("defines", joined=True),
    },
    "write_verilog": {flag: _setting(field) for field, flag, _ in WRITE_VERILOG_SWITCHES},
    "read_slang": {
        "--top": _fixed("the script sets the top module with `hierarchy -top`"),
        "-D": _setting("defines", joined=True),
        "--define-macro": _setting("defines"),
        "-I": _fixed(_HEADERS, joined=True),
        "--include-directory": _fixed(_HEADERS),
        "-G": _fixed("the script gives the parameters of the design to `read_slang`", joined=True),
        "--blackboxed-module": _setting("black_box"),
        "--extern-modules": _fixed("the script gives it to every `read_slang` command"),
        "--best-effort-hierarchy": _fixed("the script gives it to every `read_slang` command"),
    },
}


#: The flags of the table of `abc` that `abc_new` has no option for. `abc_new` takes the others.
ABC_NEW_LACKS = frozenset({"-dff", "-keepff", "-dress", "-liberty_args", "-g", "-lut", "-luts"})


class Hatch(NamedTuple):
    """A hatch: the setting that holds the flags, and the command that gets them. `applies`
    tells whether the command gets the flags for these settings."""

    setting: str
    command: str
    applies: Callable[[Any], bool] = lambda settings: True


#: The hatches of a mapping to a liberty library, in the order of the script.
LIBERTY_HATCHES: Tuple[Hatch, ...] = (
    Hatch("synth_flags", "synth"),
    Hatch("abc_flags", "abc"),
    Hatch("read_verilog_flags", "read_verilog"),
    Hatch("netlist_verilog_flags", "write_verilog"),
)

#: The hatch of the SystemVerilog front end, which the flags reach only with `slang`.
SLANG_HATCHES: Tuple[Hatch, ...] = (
    Hatch(
        "read_systemverilog_flags",
        "read_slang",
        lambda settings: settings.systemverilog == "slang",
    ),
)

HATCHES: Tuple[Hatch, ...] = (*LIBERTY_HATCHES, *SLANG_HATCHES)

_FLAG = re.compile(r"-{1,2}[A-Za-z_]")


def owned_words(command: str, items: Sequence[str]) -> List[Tuple[str, str, Owner]]:
    """Return each word of `items` that the table of `command` refuses, as `(word, flag, owner)`.

    The check reads every word of every item, since an item may hold several. A word is a flag
    when it starts with a dash and a letter, so the value of a flag is never one. It is a refused
    flag when it is that flag, when it is `flag=value`, or, for a joined flag, when the flag
    begins the word.
    """
    table = OWNED_FLAGS[command]
    found: List[Tuple[str, str, Owner]] = []
    for item in items:
        for word in item.split():
            if not _FLAG.match(word):
                continue
            for flag, owner in table.items():
                if (
                    word == flag
                    or word.startswith(f"{flag}=")
                    or (owner.joined and word.startswith(flag))
                ):
                    found.append((word, flag, owner))
                    break
    return found


def _runs_abc_new(settings: Any) -> bool:
    return getattr(getattr(settings, "abc", None), "mapper", None) == "abc_new"


def flag_problems(settings: Any, hatches: Sequence[Hatch]) -> List[Tuple[str, str]]:
    """Return the refused flags in the hatches of `settings`, as `(setting, message)` pairs.

    Each message names the setting that owns the flag, says that the script fixes it, or says that
    `abc_new`, which gets the flags of `abc_flags` with that mapper, has no such option."""
    problems: List[Tuple[str, str]] = []
    for hatch in hatches:
        if not hatch.applies(settings):
            continue
        for word, flag, owner in owned_words(hatch.command, getattr(settings, hatch.setting)):
            if hatch.command == "abc" and flag in ABC_NEW_LACKS and _runs_abc_new(settings):
                message = (
                    f"`{word}` in `{hatch.setting}` is an option of `abc`, and `abc.mapper: "
                    "abc_new` runs `abc_new`, which has no such option. Delete it"
                )
            elif owner.setting is not None:
                message = (
                    f"`{word}` in `{hatch.setting}` is the setting `{owner.setting}`: set that "
                    "instead"
                )
            else:
                message = f"`{word}` in `{hatch.setting}` is fixed: {owner.reason}. Delete it"
            problems.append((hatch.setting, message))
    return problems
