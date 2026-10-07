# STLV7325 v2 (Kintex-7 xc7k325t): pin-only fallback constraints for the clock, the LEDs, the
# buttons and the UART.
# Sources: the openXC7 demo-projects, blinky-stlv7325/blinky.xdc (last changed in commit
# ff06c478b7034a682a8974e9f90a6076528a8f55 of github.com/openXC7/demo-projects;
# Copyright (c) 2022 Hans Baier, BSD-3-Clause), and LiteX-Boards, platforms/sitlinv_stlv7325_v2.py
# (commit 10debf146d433ce8ac8aedcac84e97d252ff4d5b of github.com/litex-hub/litex-boards;
# Copyright (c) 2023 Gabriel Somlo, Copyright (c) 2022 Andrew Gillham, BSD-2-Clause).
# See LICENSE.md.
# The board has no vendor constraint file here, so the port names are xeda's. The pins are not
# checked against a vendor schematic, a board revision or hardware.
# This file has no `create_clock`: the flow's clock settings give the timing.

## Clock
# Source: the demo's blinky.xdc and LiteX-Boards `clk200`: the same pins and I/O standard.
# `clk_p` and `clk_n` are the 200 MHz differential clock. A design needs an IBUFDS and a BUFG.
set_property LOC AB11 [get_ports clk_p]
set_property IOSTANDARD DIFF_SSTL15 [get_ports {clk_p}]
set_property LOC AC11 [get_ports clk_n]
set_property IOSTANDARD DIFF_SSTL15 [get_ports {clk_n}]

## LEDs
# Source: LiteX-Boards `user_led_n` 0 to 7. `led[0]` to `led[7]` are those LEDs, in order.
# LiteX-Boards names them `user_led_n`: they are active low, lit by a low level.
set_property LOC AA2 [get_ports {led[0]}]
set_property IOSTANDARD LVCMOS15 [get_ports {led[0]}]
set_property LOC AD5 [get_ports {led[1]}]
set_property IOSTANDARD LVCMOS15 [get_ports {led[1]}]
set_property LOC W10 [get_ports {led[2]}]
set_property IOSTANDARD LVCMOS15 [get_ports {led[2]}]
set_property LOC Y10 [get_ports {led[3]}]
set_property IOSTANDARD LVCMOS15 [get_ports {led[3]}]
set_property LOC AE10 [get_ports {led[4]}]
set_property IOSTANDARD LVCMOS15 [get_ports {led[4]}]
set_property LOC W11 [get_ports {led[5]}]
set_property IOSTANDARD LVCMOS15 [get_ports {led[5]}]
set_property LOC V11 [get_ports {led[6]}]
set_property IOSTANDARD LVCMOS15 [get_ports {led[6]}]
set_property LOC Y12 [get_ports {led[7]}]
set_property IOSTANDARD LVCMOS15 [get_ports {led[7]}]

## Buttons
# Source: LiteX-Boards `cpu_reset_n` and `user_btn_n` 0 (one pin, AC16), and `user_btn_n` 1.
# The buttons are active low: a pressed button reads 0.
# `btn[0]` is the reset button: LiteX-Boards uses it as the reset of its designs. The demo
# constrains the same pin with the I/O standard SSTL15; LiteX-Boards says LVCMOS15.
# `btn[1]` is in a bank whose voltage the jumper J4 sets, to 2.5 V or 3.3 V. The standard here,
# LVCMOS33 as in LiteX-Boards, is right with J4 at 3.3 V.
set_property LOC AC16 [get_ports {btn[0]}]
set_property IOSTANDARD LVCMOS15 [get_ports {btn[0]}]
set_property LOC C24 [get_ports {btn[1]}]
set_property IOSTANDARD LVCMOS33 [get_ports {btn[1]}]

## UART
# Source: LiteX-Boards `serial`, the CP2102 USB-UART bridge. No other source has these pins.
# `uart_rx` is an input of the FPGA: it receives what the bridge sends.
# `uart_tx` is an output of the FPGA: it sends to the bridge.
# These pins are in the bank the jumper J4 sets. LVCMOS33 is right with J4 at 3.3 V. Set J4 to
# 3.3 V before you use them.
set_property LOC K21 [get_ports uart_rx]
set_property IOSTANDARD LVCMOS33 [get_ports {uart_rx}]
set_property LOC L23 [get_ports uart_tx]
set_property IOSTANDARD LVCMOS33 [get_ports {uart_tx}]
