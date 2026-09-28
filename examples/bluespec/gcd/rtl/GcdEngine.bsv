// GcdEngine: an iterative greatest-common-divisor engine (binary GCD, Stein's algorithm).
//
// Only shifts, comparisons and subtractions: every cycle removes at least one bit from one of
// the two operands, so a result takes at most 64 iterations for 32-bit operands.
//
// `mkGcdEngine` is (* synthesize *)d, so it becomes its own Verilog module (mkGcdEngine.v),
// instantiated by `mkGcdUnit` in the GcdUnit package.
package GcdEngine;

typedef UInt#(32) Operand;

interface GcdEngine;
   // Begin computing gcd(a, b); ready when the engine is idle.
   method Action start(Operand a, Operand b);
   // The result of the last `start`; ready once it has been computed.
   method ActionValue#(Operand) result;
endinterface

(* synthesize *)
module mkGcdEngine(GcdEngine);
   Reg#(Operand) x <- mkRegU;
   Reg#(Operand) y <- mkRegU;
   // Common factors of two divided out of both operands so far.
   Reg#(UInt#(6)) twos <- mkRegU;
   Reg#(Bool) busy <- mkReg(False);

   // The methods only read state and signal through wires; the single `update` rule, which
   // runs every cycle, changes the state. So `start` and `result` never conflict with each
   // other or with the iteration, and callers can use them from independent rules.
   RWire#(Tuple2#(Operand, Operand)) startW <- mkRWire;
   PulseWire resultTakenW <- mkPulseWire;

   function Bool isEven(Operand v) = pack(v)[0] == 0;
   Bool finished = x == 0 || y == 0;

   (* fire_when_enabled, no_implicit_conditions *)
   rule update;
      if (startW.wget matches tagged Valid {.a, .b}) begin
         x <= a;
         y <= b;
         twos <= 0;
         busy <= True;
      end
      else if (resultTakenW)
         busy <= False;
      else if (busy && !finished)
         case (tuple2(isEven(x), isEven(y))) matches
            {True, True}: begin
               x <= x >> 1;
               y <= y >> 1;
               twos <= twos + 1;
            end
            {True, False}: x <= x >> 1;
            {False, True}: y <= y >> 1;
            {False, False}: begin
               // Both odd: their difference is even, so halve it right away.
               if (x >= y) x <= (x - y) >> 1;
               else y <= (y - x) >> 1;
            end
         endcase
   endrule

   method Action start(Operand a, Operand b) if (!busy);
      startW.wset(tuple2(a, b));
   endmethod

   method ActionValue#(Operand) result if (busy && finished);
      resultTakenW.send;
      // One of x, y is zero, the other is the odd part of the GCD.
`ifdef XEDA_INJECT_BUG
      return x | y; // deliberate bug for negative tests: drops the common factors of two
`else
      return (x | y) << twos;
`endif
   endmethod
endmodule

endpackage
