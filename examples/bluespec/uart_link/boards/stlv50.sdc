# The MMCM uses VCO = 200 MHz * 4 and CLKOUT0 = VCO / 16.
create_clock -name clk50 -period 20.0 [get_nets {board.CLK}]
