from .flows.diamond import Diamond
from .flows.vivado import Vivado
from .flows.quartus import Quartus

from .xeda_app import XedaApp

def run_xeda(args=None):
    xeda = XedaApp()
    xeda.register_suites(Diamond, Vivado, Quartus)
    
    xeda.main()

