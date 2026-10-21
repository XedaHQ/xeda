// Test top: LeanLink with a non-idempotent endpoint. Each response is the 32-bit count of
// requests executed so far (little-endian, before this one), followed by the request payload.
// A repeated execution shows up as a skipped count. `stall` pauses the endpoint.
package LeanLinkTestTop;

import GetPut :: *;
import LeanLink :: *;

interface LeanLinkTestTop;
   interface Put#(Bit#(8)) rx;
   interface Get#(Bit#(8)) tx;
   (* always_ready, always_enabled *) method Action stall(Bool s);
   (* always_ready *) method Bit#(32) executed;
   (* always_ready *) method Bool responseReady;
endinterface

module mkLeanLinkTestTopOf#(LeanLink#(logW, logP) link)(LeanLinkTestTop);
`ifdef XEDA_INJECT_BUG
   // A planted bug for the negative test: each request counts twice, as if it ran twice.
   Bit#(32) countStep = 2;
`else
   Bit#(32) countStep = 1;
`endif
   Wire#(Bool) stallW <- mkBypassWire;
   Reg#(Bit#(32)) count <- mkReg(0);
   Reg#(UInt#(2)) phase <- mkReg(0);
   Reg#(UInt#(2)) k <- mkReg(0);
   Reg#(LinkTok) held <- mkRegU;

   rule take (phase == 0 && !stallW);
      let t <- link.request.get;
      held <= t; k <= 0; phase <= 1;
   endrule

   rule countBytes (phase == 1 && !stallW);
      Bit#(8) b = case (k) 0: count[7:0]; 1: count[15:8]; 2: count[23:16]; default: count[31:24]; endcase;
      link.response.put(LinkTok { data: b, last: False, empty: False });
      k <= k + 1;
      if (k == 3) phase <= 2;
   endrule

   rule first (phase == 2 && !stallW);
      link.response.put(held);
      if (held.last) begin count <= count + countStep; phase <= 0; end
      else phase <= 3;
   endrule

   rule echo (phase == 3 && !stallW);
      let t <- link.request.get;
      link.response.put(t);
      if (t.last) begin count <= count + countStep; phase <= 0; end
   endrule

   interface rx = link.rx;
   interface tx = link.tx;
   method Action stall(Bool s); stallW <= s; endmethod
   method Bit#(32) executed = count;
   method Bool responseReady = link.responseReady;
endmodule

(* synthesize *)
module mkLeanLinkTestW8(LeanLinkTestTop);
   LeanLink#(3, 8) link <- mkLeanLink;
   let m <- mkLeanLinkTestTopOf(link);
   return m;
endmodule

(* synthesize *)
module mkLeanLinkTestW1(LeanLinkTestTop);
   LeanLink#(0, 6) link <- mkLeanLink;
   let m <- mkLeanLinkTestTopOf(link);
   return m;
endmodule

endpackage
