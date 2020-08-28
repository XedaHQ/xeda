from .flows.diamond import Diamond
from .flows.vivado import Vivado

from .sassy_app import SassyApp

def run_sassy(args=None):
    sassy = SassyApp()
    sassy.register_suites(Diamond, Vivado)
    
    sassy.main()



