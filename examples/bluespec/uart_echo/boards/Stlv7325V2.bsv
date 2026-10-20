// Stlv7325V2: the board wrapper of mkUartEcho for the SITLINV STLV7325 v2 (Kintex-7 xc7k325t).
//
// The ports are the names of Xeda's bundled pin file of the board (src/xeda/data/boards/
// stlv7325_v2/board.xdc), so `board: stlv7325_v2` supplies the pins and this wrapper has none of
// its own:
//
//   clk_p, clk_n   the 200 MHz differential clock (DIFF_SSTL15): an IBUFDS and a BUFG
//   uart_rx        input of the FPGA: what the host sends
//   uart_tx        output of the FPGA: the echo
//   btn[1:0]       active low. btn[1] clears the sticky LEDs; btn[0], the reset button, is not
//                  used, since mkUartEcho makes its own power-on reset
//   led[7:0]       active low. led[3:0] are the four LEDs of mkUartEcho, led[7:4] are off
//
// The board has no switches, so `sw` is tied to 0 and the design is built with FIXED_BAUD 1 (the
// target sets it, and it is the default). The wrapper does not read that macro or ECHO_TRANSFORM:
// `mkUartEcho` does.
//
// IBUFDS and BUFG are the Xilinx primitives, imported with `import "BVI"`. bsc has no Verilog for
// them (it warns, and leaves them to the synthesis tool), so the testbench simulates `mkUartEcho`
// and not this wrapper. Both imported modules are in the same clock family as the clock input, so
// the methods of mkUartEcho are the methods of the wrapper and no clock-domain crossing is made.
//
// The generated module is `mkStlv7325V2(clk_n, uart_rx, btn, clk_p, uart_tx, led)`. It has no
// reset port (`no_default_reset`).
package Stlv7325V2;

import UartEcho :: *;

interface ClockOut;
   interface Clock clk;
endinterface

// Differential input buffer: clk_p is the clock input, clk_n goes to the second pad.
import "BVI" IBUFDS =
module vIBUFDS#(Clock p, Bit#(1) n)(ClockOut);
   default_clock no_clock;
   default_reset no_reset;
   input_clock pin(I, (* unused *) I_GATE) = p;
   port IB clocked_by(pin) = n;
   output_clock clk(O);
   same_family(pin, clk);
endmodule

// Global clock buffer.
import "BVI" BUFG =
module vBUFG#(Clock c)(ClockOut);
   default_clock no_clock;
   default_reset no_reset;
   input_clock cin(I, (* unused *) I_GATE) = c;
   output_clock clk(O);
   same_family(cin, clk);
endmodule

interface Stlv7325V2;
   (* always_ready, result = "uart_tx" *)
   method Bit#(1) uart_tx;
   (* always_ready, result = "led" *)
   method Bit#(8) led;
endinterface

// The default clock is the pad clk_p. clk_n, uart_rx and btn are module arguments, which bsc makes
// input ports of that name.
(* synthesize, default_clock_osc = "clk_p", default_gate_inhigh, no_default_reset *)
module mkStlv7325V2#(Bit#(1) clk_n, Bit#(1) uart_rx, Bit#(2) btn)(Stlv7325V2);
   Clock pad <- exposeCurrentClock;
   ClockOut ibufds <- vIBUFDS(pad, clk_n);
   ClockOut bufg <- vBUFG(ibufds.clk);
   UartEcho echo <- mkUartEcho(clocked_by bufg.clk, reset_by noReset);

   (* fire_when_enabled, no_implicit_conditions *)
   rule feed;
      echo.inputs(uart_rx, 0, ~btn[1]);
   endrule

   method Bit#(1) uart_tx = echo.txd;
   method Bit#(8) led = {4'hF, ~echo.led};
endmodule

endpackage
