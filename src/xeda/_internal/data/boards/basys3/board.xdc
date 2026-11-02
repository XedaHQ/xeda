# Digilent Basys 3: pin-only fallback constraints for the clock, the LEDs, the buttons and the
# USB-UART.
# Pins and I/O standards: Digilent's master constraints, Basys-3-Master.xdc, at commit
# 00a3404901f35aa9567b01ecb3f2c233b6efe9f4 of github.com/Digilent/digilent-xdc (Basys 3, revision B).
# Copyright (c) 2017 Digilent. MIT license; see LICENSE.md.
# The port names are Digilent's, as in the bundled Arty files. The switches, the seven-segment
# display, the Pmod headers and the other peripherals have no constraints here.
# LiteX-Boards, platforms/digilent_basys3.py (BSD-2-Clause), has the same pins and standards.
# The pins are not checked on hardware.
# This file has no `create_clock`: the flow's clock settings give the timing.

## Clock
# Source: Basys-3-Master.xdc, "Clock signal". `clk` is the 100 MHz oscillator.
set_property LOC W5 [get_ports clk]
set_property IOSTANDARD LVCMOS33 [get_ports {clk}]

## LEDs
# Source: Basys-3-Master.xdc, "LEDs". `led[0]` to `led[15]` are LD0 to LD15.
# LiteX-Boards names them `user_led`, not `user_led_n`: they are active high, lit by a high level.
set_property LOC U16 [get_ports {led[0]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[0]}]
set_property LOC E19 [get_ports {led[1]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[1]}]
set_property LOC U19 [get_ports {led[2]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[2]}]
set_property LOC V19 [get_ports {led[3]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[3]}]
set_property LOC W18 [get_ports {led[4]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[4]}]
set_property LOC U15 [get_ports {led[5]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[5]}]
set_property LOC U14 [get_ports {led[6]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[6]}]
set_property LOC V14 [get_ports {led[7]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[7]}]
set_property LOC V13 [get_ports {led[8]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[8]}]
set_property LOC V3 [get_ports {led[9]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[9]}]
set_property LOC W3 [get_ports {led[10]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[10]}]
set_property LOC U3 [get_ports {led[11]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[11]}]
set_property LOC P3 [get_ports {led[12]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[12]}]
set_property LOC N3 [get_ports {led[13]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[13]}]
set_property LOC P1 [get_ports {led[14]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[14]}]
set_property LOC L1 [get_ports {led[15]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[15]}]

## Buttons
# Source: Basys-3-Master.xdc, "Buttons". The buttons are active high: a pressed button reads 1.
# `btnC`, the center button, is the reset button: LiteX-Boards uses it as the reset of its designs.
set_property LOC U18 [get_ports btnC]
set_property IOSTANDARD LVCMOS33 [get_ports {btnC}]
set_property LOC T18 [get_ports btnU]
set_property IOSTANDARD LVCMOS33 [get_ports {btnU}]
set_property LOC W19 [get_ports btnL]
set_property IOSTANDARD LVCMOS33 [get_ports {btnL}]
set_property LOC T17 [get_ports btnR]
set_property IOSTANDARD LVCMOS33 [get_ports {btnR}]
set_property LOC U17 [get_ports btnD]
set_property IOSTANDARD LVCMOS33 [get_ports {btnD}]

## UART
# Source: Basys-3-Master.xdc, "USB-RS232 Interface", the USB-UART bridge of the board.
# `RsRx` is an input of the FPGA: it receives what the bridge sends.
# `RsTx` is an output of the FPGA: it sends to the bridge.
set_property LOC B18 [get_ports RsRx]
set_property IOSTANDARD LVCMOS33 [get_ports {RsRx}]
set_property LOC A18 [get_ports RsTx]
set_property IOSTANDARD LVCMOS33 [get_ports {RsTx}]
