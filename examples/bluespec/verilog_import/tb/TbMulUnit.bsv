// Self-checking testbench for mkMulUnit.
//
// Sends directed and LFSR-driven operand pairs and compares every product with bsc's own
// `unsignedMul`. The consumer takes a response on only about three cycles in eight, so the
// output FIFO fills up and the unit must throttle requests to not lose any of the results
// that the imported pipeline cannot hold back.
package TbMulUnit;

import FIFO :: *;
import GetPut :: *;
import ClientServer :: *;
import Vector :: *;
import BuildVector :: *;
import LFSR :: *;
import MulUnit :: *;

typedef 500 NumProducts;
typedef 4 NumDirected;
UInt#(32) maxCycles = 5000;

Vector#(NumDirected, Tuple2#(Factor, Factor)) directed =
   vec(tuple2(0, 12345), tuple2(1, maxBound), tuple2(maxBound, maxBound), tuple2(256, 255));

(* synthesize *)
module mkTbMulUnit(Empty);
   MulUnit dut <- mkMulUnit;

   LFSR#(Bit#(32)) operands <- mkLFSR_32;
   LFSR#(Bit#(16)) consumerPattern <- mkLFSR_16;
   FIFO#(Product) expected <- mkSizedFIFO(8);
   Reg#(UInt#(16)) nSent <- mkReg(0);
   Reg#(UInt#(16)) nChecked <- mkReg(0);
   Reg#(UInt#(32)) cycle <- mkReg(0);

   rule watchdog;
      cycle <= cycle + 1;
      if (cycle == maxCycles)
         $fatal(1, "FAIL: timeout after %0d cycles", cycle);
   endrule

   rule send (nSent < fromInteger(valueOf(NumProducts)));
      Bit#(32) r = operands.value;
      let ab = nSent < fromInteger(valueOf(NumDirected))
                  ? directed[nSent]
                  : tuple2(unpack(r[31:16]), unpack(r[15:0]));
      dut.request.put(ab);
      expected.enq(unsignedMul(tpl_1(ab), tpl_2(ab)));
      operands.next;
      nSent <= nSent + 1;
   endrule

   rule advancePattern;
      consumerPattern.next;
   endrule

   rule check (consumerPattern.value[2:0] < 3);
      let p <- dut.response.get;
      let e = expected.first;
      expected.deq;
      if (p != e)
         $fatal(1, "FAIL: product %0d: expected %0d, got %0d", nChecked, e, p);
      nChecked <= nChecked + 1;
      if (nChecked + 1 == fromInteger(valueOf(NumProducts))) begin
         $display("PASS: %0d products checked in %0d cycles", nChecked + 1, cycle);
         $finish(0);
      end
   endrule
endmodule

endpackage
