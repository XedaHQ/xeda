# STLV7325 v2 (Kintex-7 xc7k325t): pin-only fallback constraints for the 200 MHz differential
# clock and one LED.
# Source: openXC7 demo-projects, blinky-stlv7325/blinky.xdc (repository at commit
# d36fa6e96aa2fac4553083be365a9f042e22be3a of github.com/kammoh/openxc7-demo-projects).
# Copyright (c) 2022 Hans Baier. BSD-3-Clause; see LICENSE.md.
# The same pins and I/O standards are in LiteX-Boards, platforms/sitlinv_stlv7325_v2.py
# (BSD-2-Clause). Not checked against a vendor schematic, a board revision or hardware.
#
# Ports: `clk_p` and `clk_n` (the differential clock; the design needs an IBUFDS and a BUFG)
# and `led`. LiteX-Boards names this LED `user_led_n`, which means active low: the LED is on
# when `led` is 0. That polarity is not checked on hardware.
# Only pins that do not depend on a jumper-selected I/O bank voltage are listed.
# This file has no `create_clock`: the flow's clock settings give the timing.
set_property LOC AB11 [get_ports clk_p]
set_property IOSTANDARD DIFF_SSTL15 [get_ports {clk_p}]

set_property LOC AC11 [get_ports clk_n]
set_property IOSTANDARD DIFF_SSTL15 [get_ports {clk_n}]

set_property LOC AA2 [get_ports led]
set_property IOSTANDARD LVCMOS15 [get_ports {led}]
