create_clock -period {{flow.clock_period}} -name clock [get_ports {{design.clock_port}}]

derive_pll_clocks
derive_clock_uncertainty