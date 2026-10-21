// StlvLeanLink: the STLV7325 v2 board wrapper of mkLeanLinkBoard, adapted from the uart_echo
// wrapper (examples/bluespec/uart_echo/boards/Stlv7325V2.bsv). Ports are the names of Xeda's
// bundled pin file (src/xeda/data/boards/stlv7325_v2/board.xdc): clk_p/clk_n (200 MHz, IBUFDS
// and BUFG), uart_rx, uart_tx, btn[1:0] and led[7:0] (both active low). btn[1] clears the sticky
// LEDs. There are no switches, so the endpoint never stalls (sw = 0).
package StlvLeanLink;

import LeanLinkBoard :: *;

interface ClockOut;
   interface Clock clk;
endinterface

import "BVI" IBUFDS =
module vIBUFDS#(Clock p, Bit#(1) n)(ClockOut);
   default_clock no_clock;
   default_reset no_reset;
   input_clock pin(I, (* unused *) I_GATE) = p;
   port IB clocked_by(pin) = n;
   output_clock clk(O);
   same_family(pin, clk);
endmodule

import "BVI" BUFG =
module vBUFG#(Clock c)(ClockOut);
   default_clock no_clock;
   default_reset no_reset;
   input_clock cin(I, (* unused *) I_GATE) = c;
   output_clock clk(O);
   same_family(cin, clk);
endmodule

interface StlvLeanLink;
   (* always_ready, result = "uart_tx" *)
   method Bit#(1) uart_tx;
   (* always_ready, result = "led" *)
   method Bit#(8) led;
endinterface

(* synthesize, default_clock_osc = "clk_p", default_gate_inhigh, no_default_reset *)
module mkStlvLeanLink#(Bit#(1) clk_n, Bit#(1) uart_rx, Bit#(2) btn)(StlvLeanLink);
   Clock pad <- exposeCurrentClock;
   ClockOut ibufds <- vIBUFDS(pad, clk_n);
   ClockOut bufg <- vBUFG(ibufds.clk);
   LeanLinkBoard board <- mkLeanLinkBoard(clocked_by bufg.clk, reset_by noReset);

   (* fire_when_enabled, no_implicit_conditions *)
   rule feed;
      board.inputs(uart_rx, 0, ~btn[1]);
   endrule

   method Bit#(1) uart_tx = board.txd;
   method Bit#(8) led = {4'hF, ~board.led};
endmodule

endpackage
