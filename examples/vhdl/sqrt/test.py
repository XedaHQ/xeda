from pathlib import Path
from xeda import load_design_from_toml, Design, Flow
from xeda.flow_runner import DefaultRunner
from xeda.flows import GhdlSim, YosysSynth

SCRIPT_DIR = Path(__file__).parent.resolve()

design: Design = load_design_from_toml(SCRIPT_DIR / 'sqrt.toml')
xeda_runner = DefaultRunner()


def test_sqrt():
    for w in [8, 9, 16, 17, 18, 20, 21, 31, 32, 63, 64, 66]:
        design.tb.parameters = {
            **design.tb.parameters,
            'G_IN_WIDTH': w
        }
        f: Flow = xeda_runner.run_flow(
            GhdlSim, design
        )
        assert f.results['success'], f"test failed for w={w}"
