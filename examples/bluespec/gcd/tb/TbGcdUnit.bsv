// Self-checking testbench for mkGcdUnit.
//
// A producer rule sends directed corner cases followed by LFSR-driven requests; a checker
// (StmtFSM) recomputes each GCD with Euclid's algorithm (remainders, unlike the DUT's binary
// algorithm) and compares it with the DUT's response. Any mismatch, or a missing response
// within the cycle budget, ends the simulation with $fatal (non-zero exit status).
package TbGcdUnit;

import FIFO :: *;
import GetPut :: *;
import ClientServer :: *;
import Vector :: *;
import BuildVector :: *;
import LFSR :: *;
import StmtFSM :: *;
import GcdUnit :: *;

typedef 10 NumDirected;
typedef 200 NumRequests;
UInt#(32) maxCycles = 20000;

function GcdRequest req(Operand a, Operand b) = GcdRequest { a: a, b: b };

Vector#(NumDirected, GcdRequest) directed = vec(
   req(0, 0), req(0, 42), req(35, 0), req(1, 1), req(12, 18), req(1071, 462),
   req('hFFFFFFFF, 'hFFFFFFFE), req('h80000000, 'h40000000),
   req(9699690, 223092870), req('hFFFFFFFB, 'hFFFFFFFB));

(* synthesize *)
module mkTbGcdUnit(Empty);
   GcdUnit dut <- mkGcdUnit;

   LFSR#(Bit#(32)) lfsrA <- mkLFSR_32;
   LFSR#(Bit#(32)) lfsrB <- mkFeedLFSR('hA3000000);
   Reg#(Bool) seeded <- mkReg(False);

   // Requests sent to the DUT and not yet checked, oldest first.
   FIFO#(GcdRequest) inFlight <- mkSizedFIFO(8);
   Reg#(UInt#(16)) nIssued <- mkReg(0);
   Reg#(UInt#(16)) nChecked <- mkReg(0);
   Reg#(UInt#(32)) cycle <- mkReg(0);

   rule watchdog;
      cycle <= cycle + 1;
      if (cycle == maxCycles)
         $fatal(1, "FAIL: timeout after %0d cycles, %0d of %0d requests checked",
                cycle, nChecked, valueOf(NumRequests));
   endrule

   rule seed (!seeded);
      lfsrA.seed('h2545F491);
      lfsrB.seed('h4F6CDD1D);
      seeded <= True;
   endrule

   rule produce (seeded && nIssued < fromInteger(valueOf(NumRequests)));
      Bit#(32) ra = lfsrA.value;
      Bit#(32) rb = lfsrB.value;
      lfsrA.next;
      lfsrB.next;
      GcdRequest r;
      if (nIssued < fromInteger(valueOf(NumDirected)))
         r = directed[nIssued];
      else if (nIssued % 4 == 0)
         // full-range operands: mostly small GCDs
         r = req(unpack(ra), unpack(rb));
      else begin
         // 16-bit multiples of a random common factor: GCDs of up to 32 bits
         UInt#(32) k = zeroExtend(unpack(ra[15:0] ^ rb[15:0]));
         r = req(zeroExtend(unpack(ra[31:16])) * k, zeroExtend(unpack(rb[31:16])) * k);
      end
      dut.request.put(r);
      inFlight.enq(r);
      nIssued <= nIssued + 1;
   endrule

   // Reference model state
   Reg#(Operand) refA <- mkRegU;
   Reg#(Operand) refB <- mkRegU;

   Stmt checker = seq
      while (nChecked < fromInteger(valueOf(NumRequests))) seq
         action
            refA <= inFlight.first.a;
            refB <= inFlight.first.b;
         endaction
         while (refB != 0) action
            refA <= refB;
            refB <= refA % refB;
         endaction
         action
            let g <- dut.response.get;
            let r = inFlight.first;
            inFlight.deq;
            if (g != refA)
               $fatal(1, "FAIL: gcd(%0d, %0d): expected %0d, got %0d", r.a, r.b, refA, g);
            nChecked <= nChecked + 1;
         endaction
      endseq
      action
         $display("PASS: %0d GCD requests checked in %0d cycles", nChecked, cycle);
         $finish(0);
      endaction
   endseq;

   // The checker ends the simulation itself, so mkAutoFSM's own $finish is never reached.
   mkAutoFSM(checker);
endmodule

endpackage
