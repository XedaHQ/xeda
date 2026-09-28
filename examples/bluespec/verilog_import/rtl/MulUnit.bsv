// MulUnit: a latency-insensitive multiplier around an imported Verilog pipeline.
//
// `import "BVI"` describes the Verilog module pipe_mul (pipe_mul.v, a design source) as a BSV
// module: its clock and reset, a parameter, an Action method driving the inputs with an
// enable, value methods reading the outputs, and how the methods may be scheduled.
// Because the pipeline cannot stall, `mkMulUnit` starts a multiplication only while the
// output FIFO has a slot for its result that no multiplication in flight has claimed.
//
// Bluesim cannot simulate imported Verilog: simulate this design with a Verilog simulator.
package MulUnit;

import FIFOF :: *;
import GetPut :: *;
import ClientServer :: *;

typedef 16 Width;
typedef UInt#(Width) Factor;
typedef UInt#(TMul#(2, Width)) Product;

interface PipeMul;
   method Action put(Factor a, Factor b);
   method Bool valid;
   method Product product;
endinterface

import "BVI" pipe_mul =
module mkPipeMul(PipeMul);
   parameter WIDTH = valueOf(Width);
   default_clock clk(CLK);
   default_reset rst(RST);

   method put(in_a, in_b) enable(in_valid);
   method out_valid valid;
   method out_p product;

   // The outputs are registers, unaffected by `put` in the same cycle.
   schedule put C put;
   schedule (valid, product) CF (valid, product, put);
endmodule

typedef 2 Latency;                         // pipeline depth of pipe_mul
typedef TAdd#(Latency, 2) OutDepth;        // enough slots for one result per cycle

typedef Server#(Tuple2#(Factor, Factor), Product) MulUnit;

(* synthesize *)
module mkMulUnit(MulUnit);
   PipeMul mul <- mkPipeMul;
   FIFOF#(Product) outQ <- mkUGSizedFIFOF(valueOf(OutDepth));

   // Output slots not yet claimed by a started multiplication.
   Reg#(UInt#(TLog#(TAdd#(OutDepth, 1)))) credits <- mkReg(fromInteger(valueOf(OutDepth)));
   PulseWire started <- mkPulseWire;
   PulseWire freed <- mkPulseWire;

   (* fire_when_enabled, no_implicit_conditions *)
   rule updateCredits;
      credits <= credits - (started ? 1 : 0) + (freed ? 1 : 0);
   endrule

   // The credits guarantee a free slot, hence the unguarded FIFO (mkUGSizedFIFOF).
   (* fire_when_enabled, no_implicit_conditions *)
   rule collect (mul.valid);
      outQ.enq(mul.product);
   endrule

   interface Put request;
      method Action put(Tuple2#(Factor, Factor) operands) if (credits > 0);
         match {.a, .b} = operands;
`ifdef XEDA_INJECT_BUG
         mul.put(a, a); // deliberate bug for negative tests: squares the first operand
`else
         mul.put(a, b);
`endif
         started.send;
      endmethod
   endinterface

   interface Get response;
      method ActionValue#(Product) get if (outQ.notEmpty);
         outQ.deq;
         freed.send;
         return outQ.first;
      endmethod
   endinterface
endmodule

endpackage
