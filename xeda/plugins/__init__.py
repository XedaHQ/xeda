

class Plugin():
    name = None
    def __init__(self, run_dir, logger) -> None:
        self.run_dir = run_dir
        self.logger = logger
    
    def post_results_hook(self):
        pass 
       
    def post_run_hook(self):
        pass
