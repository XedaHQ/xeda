from .flows.diamond import Diamond
from .sassy_app import SassyApp

def run_sassy(args=None):
    sassy = SassyApp()
    sassy.register_suites(Diamond)

    
    sassy.main()



