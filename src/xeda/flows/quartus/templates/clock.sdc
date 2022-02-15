create_clock -period {{ "%.3f"|format(flow.clock_period) }} -name clock [get_ports {{design.rtl.clock_port}}]

derive_pll_clocks
derive_clock_uncertainty