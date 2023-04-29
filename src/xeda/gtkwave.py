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


COLORS = [
    "dark blue",
    "deep sky blue",
    "yellow green",
    "orange",
    "firebrick",
    "blue violet",
    "brown",
    "burlywood",
    "cadet blue",
    "medium sea green",
    "cornflower blue",
    "dark cyan",
    "dark green",
    "dark khaki",
    "dark magenta",
    "dark olive green",
    "dark orange",
    "dark orchid",
    "dark red",
    "dark salmon",
    "dark sea green",
    "dark slate blue",
    "dark turquoise",
    "dark violet",
    "deep pink",
    "deep sky blue",
    "dodger blue",
    "firebrick",
    "forest green",
    "gold",
    "hot pink",
    "indian red",
    "khaki",
    "maroon",
    "lavender blush",
    "light goldenrod yellow",
    "magenta",
    "medium aquamarine",
    "medium blue",
    "medium orchid",
    "medium purple",
    "medium sea green",
    "medium slate blue",
    "medium spring green",
    "medium turquoise",
    "medium violet red",
    "midnight blue",
    "navy",
    "navy blue",
    "olive drab",
    "orange",
    "orange red",
    "orchid",
    "pale violet red",
    "peru",
    "pink",
    "plum",
    "purple",
    "red",
    "rosy brown",
    "royal blue",
    "saddle brown",
    "salmon",
    "sandy brown",
    "sienna",
    "sky blue",
    "slate blue",
    "steel blue",
    "tan",
    "thistle",
    "tomato",
    "turquoise",
    "violet",
    "violet red",
    "gainsboro",
    "midnight blue",
    "red",
    "alice blue",
    "aquamarine",
    "azure",
    "beige",
    "bisque",
    "black",
    "blanched almond",
    "chocolate",
    "blue",
    "chartreuse",
    "coral",
    "cornsilk",
    "cyan",
    "dark goldenrod",
    "goldenrod",
    "green",
    "green yellow",
    "honeydew",
    "ivory",
    "lavender",
    "lawn green",
    "lemon chiffon",
    "light blue",
    "light coral",
    "light cyan",
    "light goldenrod",
    "light green",
    "light pink",
    "light salmon",
    "mint cream",
    "misty rose",
    "moccasin",
    "old lace",
    "pale goldenrod",
    "pale green",
    "pale turquoise",
    "papaya whip",
    "peach puff",
    "powder blue",
    "sea green",
    "seashell",
    "snow",
    "spring green",
    "wheat",
    "yellow",
]


def get_color(i):
    return COLORS[i % len(COLORS)]
