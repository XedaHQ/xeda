# STLV7325 v2 (Kintex-7 xc7k325t): pin-only fallback constraints for the clock, the LEDs, the
# buttons and the UART.
# Sources: the openXC7 demo-projects, blinky-stlv7325/blinky.xdc (last changed in commit
# ff06c478b7034a682a8974e9f90a6076528a8f55 of github.com/openXC7/demo-projects;
# Copyright (c) 2022 Hans Baier, BSD-3-Clause), and LiteX-Boards, platforms/sitlinv_stlv7325_v2.py
# (commit 10debf146d433ce8ac8aedcac84e97d252ff4d5b of github.com/litex-hub/litex-boards;
# Copyright (c) 2023 Gabriel Somlo, Copyright (c) 2022 Andrew Gillham, BSD-2-Clause).
# See LICENSE.md.
# The port names are Xeda's: the board has no vendor constraint file here. The pins of every group
# below are checked on hardware (a STLV7325 v2 programmed through its FT4232H); nothing else is.
# This file is for the second ("V2", 2023) revision. The first revision (the Vivado board files of
# github.com/rriggs/kintex-7-stlv7325t-board-files) has other UART pins and a 100 MHz clock on F17.
# This file has no `create_clock`: the flow's clock settings give the timing.

## Clock
# Source: the demo's blinky.xdc and LiteX-Boards `clk200`.
# `clk_p` and `clk_n` are the 200 MHz differential clock. A design needs an IBUFDS and a BUFG.
# Checked: a counter behind an IBUFDS and a BUFG on these pins, read over JTAG, ran at 200 MHz.
set_property LOC AB11 [get_ports clk_p]
set_property IOSTANDARD DIFF_SSTL15 [get_ports {clk_p}]
set_property LOC AC11 [get_ports clk_n]
set_property IOSTANDARD DIFF_SSTL15 [get_ports {clk_n}]

## LEDs
# Source: LiteX-Boards `user_led_n` 0 to 7. `led[0]` to `led[7]` are LED0 to LED7, in order.
# Checked: a static pattern and a walk over all eight LEDs lit LED0 to LED7 in order. The LEDs are
# active low: lit when the pin is 0.
# LED8 to LED11, beside the SFP cages, are not on these pins. They pass through a level shifter and
# follow led[2], led[3], led[1] and led[0].
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
# Checked: holding K2 lit led[0] and holding K3 lit led[1]. The buttons are active low: a pressed
# button reads 0. `btn[0]` is K2 (the CPU reset of LiteX-Boards), `btn[1]` is K3. K1 is Program_B,
# not a user button.
# `btn[1]` is in a bank whose voltage the jumper J4 sets. LVCMOS33 is right with J4 at 3.3 V.
set_property LOC AC16 [get_ports {btn[0]}]
set_property IOSTANDARD LVCMOS15 [get_ports {btn[0]}]
set_property LOC C24 [get_ports {btn[1]}]
set_property IOSTANDARD LVCMOS33 [get_ports {btn[1]}]

## UART
# Source: LiteX-Boards `serial` only. It calls the bridge a CP2102; the unit tested has a WCH
# CH9102, which a host sees as "USB Single Serial" (USB 0x1A86:0x55D4).
# `uart_rx` is an input of the FPGA: it receives what the bridge sends.
# `uart_tx` is an output of the FPGA: it sends to the bridge.
# Checked: text sent on `uart_tx` arrived through the bridge, and an echo design on both pins
# answered at 115200 and at 3 Mbaud. The FT4232H, the JTAG probe that programs the board, is not
# this UART.
# These pins are in the bank that the jumper J4 sets. LVCMOS33 is right with J4 at 3.3 V.
set_property LOC K21 [get_ports uart_rx]
set_property IOSTANDARD LVCMOS33 [get_ports {uart_rx}]
set_property LOC L23 [get_ports uart_tx]
set_property IOSTANDARD LVCMOS33 [get_ports {uart_tx}]
