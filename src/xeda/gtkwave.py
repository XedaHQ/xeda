import logging
import os
from pathlib import Path
from typing import Union

from vcd import gtkw

log = logging.getLogger(__name__)


def _add_sig(g: gtkw.GTKWSave, root_group, signals):
    i = 0
    while i < len(signals):
        sig = signals[i]
        if sig[: len(root_group)] == root_group:  # in this group
            if len(sig) - len(root_group) > 1:
                new_group = sig[len(root_group)]
                with g.group(new_group, closed=True):
                    i += _add_sig(g, root_group + [new_group], signals[i:])
            else:
                g.trace(".".join(sig))
                i += 1
        else:
            break
    return i


def gen_gtkw(dump_file: Union[str, os.PathLike], signals, root_group):
    root_group = root_group or []
    save_file = Path(dump_file).with_suffix(".gtkw")
    log.info("generated gtkwave save file: %s", save_file.absolute())
    with open(save_file, "w") as f:
        g = gtkw.GTKWSave(f)
        g.dumpfile(str(dump_file), abspath=not os.path.isabs(dump_file))
        _add_sig(g, root_group, signals)
