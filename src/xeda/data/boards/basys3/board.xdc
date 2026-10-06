# Digilent Basys 3: pin-only fallback constraints for the clock and one LED.
# Source: openXC7 demo-projects, blinky-digilent-basys-3/blinky.xdc (repository at commit
# d36fa6e96aa2fac4553083be365a9f042e22be3a of github.com/kammoh/openxc7-demo-projects).
# Copyright (c) 2022 Hans Baier. BSD-3-Clause; see LICENSE.md.
# The same pins and I/O standard are in LiteX-Boards, platforms/digilent_basys3.py (BSD-2-Clause).
# Not checked against a Digilent master XDC or a board revision.
#
# Ports: `clk` (the 100 MHz oscillator) and `led` (LED 0, which LiteX-Boards names `user_led` 0,
# not `user_led_n`: active high). The polarity is not checked on hardware.
# This file has no `create_clock`: the flow's clock settings give the timing.
set_property LOC W5 [get_ports clk]
set_property IOSTANDARD LVCMOS33 [get_ports {clk}]

set_property LOC U16 [get_ports led]
set_property IOSTANDARD LVCMOS33 [get_ports {led}]
