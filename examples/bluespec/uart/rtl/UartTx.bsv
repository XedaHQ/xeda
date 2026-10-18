// UartTx: the transmitter.
//
// A frame {stop bits, parity?, data, start} sits in one shift register, shifted right with 0 fill
// at every bit boundary; bit 0 drives the pin directly from a register (glitch free, idle high).
// The topmost 1 is the last stop bit and doubles as the end marker, so no bit counter is needed:
// only that bit is left when everything above bit 0 is 0.
//
// A byte is accepted when the line is idle or in the clock that ends the last stop bit, in which
// case the next start bit follows the stop bit with no gap, 10*P clocks per 8N1 frame. The bit
// timer is restarted only when the frame starts from idle, so the average baud stays exact.
package UartTx;

import UartTypes :: *;
import BitTimer :: *;

export UartTx(..);
export mkUartTx;

interface UartTx#(numeric type nData);
   // Send a byte; ready when the line is idle or a frame is just ending.
   method Action put(Bit#(nData) data);
   (* always_ready *)
   method Bit#(1) txd;
   (* always_ready *)
   method Bool busy;
endinterface

module mkUartTx#(BitTimer timer, UartFormat#(nd, np, ns) fmt)(UartTx#(nd))
   provisos (Add#(1, nd, a),
             Add#(a, np, b),
             Add#(b, ns, w),    // start, data, parity, stop bits
             Add#(np, _p, 1));  // at most one parity bit
   Reg#(Bit#(w)) sr <- mkReg(1);     // idle: only the stop bit's 1, which is the line level
   Reg#(Bool) busyReg <- mkReg(False);
   RWire#(Bit#(nd)) request <- mkRWire;

   Bool last = (sr >> 1) == 0;   // only the last stop bit is left
   Bool bitEnd = busyReg && timer.tick;
   Bool frameEnd = bitEnd && last;
`ifdef XEDA_INJECT_BUG_TX
   // deliberate bug for negative tests: a byte is accepted only from idle, which opens a gap
   Bool ready = !busyReg;
`else
   Bool ready = !busyReg || frameEnd;
`endif

   (* fire_when_enabled, no_implicit_conditions *)
   rule step;
      if (request.wget matches tagged Valid .d) begin
         Bit#(np) parity = truncate(reduceXor(d) ^ ((parityKind(fmt) == OddParity) ? 1 : 0));
         Bit#(ns) stop = '1;
         sr <= {stop, parity, d, 1'b0};
         busyReg <= True;
         if (!busyReg) timer.restartFull;
      end
      else if (bitEnd) begin
         if (last) busyReg <= False;
         else sr <= sr >> 1;
      end
   endrule

   method Action put(Bit#(nd) data) if (ready);
      request.wset(data);
   endmethod

   method Bit#(1) txd = sr[0];
   method Bool busy = busyReg;
endmodule

endpackage
