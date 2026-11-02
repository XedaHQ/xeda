"""The composed ABC mapping strategies `area_0` to `area_2` and `delay_0` to `delay_3`.

The steps and their order come from `construct_abc_script.py` of LibreLane
(`librelane/scripts/pyosys/construct_abc_script.py` at commit
e39720fff016c354d822885b2d1acd5508010653, Copyright 2020-2024 Efabless Corporation), which has
the Apache License 2.0. `data/asic/NOTICE.md` holds the license text. Xeda changed the code: it
builds the commands as a list in Python and returns them as the inline script that `abc -script`
takes, and it names the strategies in lower case. The strategies `AREA 3` and `DELAY 4` of
LibreLane are the scripts `area` and `speed` of OpenROAD-flow-scripts, which `yosys.py` holds.
The strategy `AREA 4` of LibreLane is not offered.
"""

from dataclasses import dataclass
from typing import List, Literal, Optional

from .asic_groups import COMPOSED_STRATEGIES

__all__ = ["Composition", "composed_abc_script"]


@dataclass(frozen=True)
class Composition:
    """The options that change how a composed strategy builds its script.

    * `buffering`: end the script with `buffer`, `upsize` and `dnsize`.
    * `sizing`: end it with `upsize` and `dnsize` only. `buffering` already sizes.
    * `max_fanout`, `max_slew`: the `-N` (fanout) and `-S` (slew, in picoseconds) limits of
      `buffer`. A limit left unset keeps the value of ABC.
    * `mfs3`: add the `mfs3` step after the mapping.
    * `area_mapper`: map for area with `amap` or with `&nf`.
    * `legacy_refactor`, `legacy_rewrite`: use `refactor` and `rewrite` instead of `drf -l` and
      `drw -l`.
    """

    buffering: bool = False
    sizing: bool = False
    max_fanout: Optional[int] = None
    max_slew: Optional[int] = None
    mfs3: bool = False
    area_mapper: Literal["amap", "nf"] = "amap"
    legacy_refactor: bool = False
    legacy_rewrite: bool = False


def composed_abc_script(strategy: str, options: Composition) -> str:
    """Return the script of the composed `strategy`, in the form `abc -script` takes.

    The text starts with `+`. It holds the commands separated by `;`, and each command has its
    words separated by `,`.
    """
    if strategy not in COMPOSED_STRATEGIES:
        raise ValueError(f"unknown abc strategy {strategy!r}")
    area = strategy.startswith("area_")
    rewrite = "rewrite" if options.legacy_rewrite else "drw -l"
    rewrite_z = "rewrite -z" if options.legacy_rewrite else "drw -l -z"
    refactor = "refactor" if options.legacy_refactor else "drf -l"
    refactor_z = "refactor -z" if options.legacy_refactor else "drf -l -z"
    balance = "balance"
    resyn2 = [
        *(balance, rewrite, refactor, balance, rewrite, rewrite_z),
        *(balance, refactor_z, rewrite_z, balance),
    ]
    choice = ["fraig_store", *resyn2, "fraig_store", *resyn2, "fraig_store", "fraig_restore"]
    choice2 = [
        *("fraig_store", balance, "fraig_store", *resyn2),
        *("fraig_store", *resyn2, "fraig_store", *resyn2, "fraig_store", "fraig_restore"),
    ]
    map_area = (
        ["&get -n", "&nf -R 1000", "&put"]
        if options.area_mapper == "nf"
        else ["amap -m -Q 0.1 -F 20 -A 20 -C 5000"]
    )
    map_delay = "map -p -B 0.2 -A 0.9 -M 0"
    # the strategies that map for area, and `delay_3` for its first mapping
    maps_for_area = area or strategy == "delay_3"

    steps: List[str] = ["fx", "mfs", "strash", refactor]
    steps += choice2 if strategy == "area_2" else resyn2
    steps.append("retime -M 5" if maps_for_area else "retime -M 6")
    steps.append("scleanup")
    if strategy == "delay_2":
        steps += choice
    elif strategy != "delay_0":
        steps += choice2
    steps += map_area if maps_for_area else [map_delay]
    # area recovery
    if strategy in ("area_1", "area_2"):
        steps += [*choice2, *map_area]
    elif strategy == "delay_1":
        steps += [*choice2, "map"]
    elif strategy == "delay_2":
        steps += [*choice, "map"]
    elif strategy == "delay_3":
        steps += [*choice2, map_delay]
    if options.mfs3:
        steps.append("mfs3 -aemvz -I 4 -O 2" if area else "mfs3 -emvz -I 4 -O 2")
    steps += ["retime", "&get -n", "&st", "&dch", "&nf", "&put"]
    if options.buffering:
        buffer = "buffer"
        if options.max_fanout is not None:
            buffer += f" -N {options.max_fanout}"
        if options.max_slew is not None:
            buffer += f" -S {options.max_slew}"
        steps += [buffer, "upsize", "dnsize"]
    elif options.sizing:
        steps += ["upsize", "dnsize"]
    steps += ["stime -p", "print_stats -m"]
    return "+" + ";".join(steps).replace(" ", ",")
