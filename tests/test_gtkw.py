from pathlib import Path
from xeda.flows.ghdl import gen_gtkw, _get_wave_opt_signals, common_root


TESTS_DIR = Path(__file__).parent.absolute()
RESOURCES_DIR = TESTS_DIR / "resources"


def test_gen_gtkw():
    opt_file = RESOURCES_DIR / "wave.opt"
    dump_file = "debug.ghw"
    extra_top = "top"

    signals, root_group = _get_wave_opt_signals(opt_file, extra_top)
    assert len(signals) > 1
    assert len(root_group) == 2
    gen_gtkw(dump_file, signals, root_group)
    gtkw = Path("debug.gtkw")
    assert gtkw.exists()
    with open(gtkw) as f:
        lines = f.read().splitlines()
    for s in signals:
        sig_name = ".".join(s)
        assert lines.count(sig_name) > 0, f"{sig_name} not in lines"


def test_common_root():
    cr = common_root([[1, 2, 3, 4, 5], [1, 2, 3, 7], [1, 2, 3], [1, 2, 3, 9]])
    assert cr == [1, 2, 3]


if __name__ == "__main__":
    test_gen_gtkw()
