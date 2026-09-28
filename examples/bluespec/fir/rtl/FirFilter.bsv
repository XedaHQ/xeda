// FirFilter: a streaming transposed-form FIR filter, sized by preprocessor macros.
//
//   TAPS          number of filter taps (default 8, at most 254)
//   SAMPLE_WIDTH  width of the signed input samples (default 16, at most 32)
//
// Xeda passes the design's `rtl.parameters` / `rtl.defines` to bsc as `-D NAME=value`, so
// `parameters = { TAPS = 11 }` in the design file sets them; without them the defaults apply.
// The filter core `mkFir` is polymorphic in the number of taps and the widths: provisos
// relate the widths, and the accumulator grows by TLog#(taps) bits so no sum can overflow.
package FirFilter;

import FIFO :: *;
import GetPut :: *;
import ClientServer :: *;
import Vector :: *;

`ifndef TAPS
`define TAPS 8
`endif
`ifndef SAMPLE_WIDTH
`define SAMPLE_WIDTH 16
`endif

typedef `TAPS NumTaps;
typedef `SAMPLE_WIDTH SampleWidth;
typedef 8 CoeffWidth;
typedef TAdd#(TAdd#(SampleWidth, CoeffWidth), TLog#(NumTaps)) AccWidth;

typedef Int#(SampleWidth) Sample;
typedef Int#(CoeffWidth) Coeff;
typedef Int#(AccWidth) Acc;

// Coefficients: a triangular window with alternating signs, e.g. 1 -2 3 -4 4 -3 2 -1.
function Coeff coefficient(Integer k);
   Integer mag = min(k + 1, valueOf(NumTaps) - k);
   return fromInteger(k % 2 == 0 ? mag : -mag);
endfunction

Vector#(NumTaps, Coeff) coefficients = genWith(coefficient);

// Transposed form: with partial sums p[k] = c[k] * x[t] + p[k+1] of the previous sample,
// the output is y[t] = c[0] * x[t] + p[1]. One multiply and one add deep per sample.
module mkFir#(Vector#(n, Int#(cw)) coeffs)(Server#(Int#(sw), Int#(aw)))
   provisos (Add#(1, m, n),       // at least one tap; m = n - 1 partial sums
             Add#(sw, cw, pw),    // width of one product
             Add#(pw, _e, aw));   // the accumulator is at least as wide as a product

   FIFO#(Int#(sw)) inQ <- mkFIFO;
   FIFO#(Int#(aw)) outQ <- mkFIFO;
   Vector#(m, Reg#(Int#(aw))) partials <- replicateM(mkReg(0));

   rule filter;
      let x = inQ.first;
      inQ.deq;
      function Int#(aw) tap(Int#(cw) c) = signExtend(signedMul(x, c));
      Vector#(1, Int#(aw)) none = replicate(0);
      Vector#(n, Int#(aw)) sums = zipWith(\+ , map(tap, coeffs), append(readVReg(partials), none));
      outQ.enq(head(sums));
`ifdef XEDA_INJECT_BUG
      writeVReg(partials, take(sums)); // deliberate bug for negative tests: misaligned partials
`else
      writeVReg(partials, tail(sums));
`endif
   endrule

   interface request = toPut(inQ);
   interface response = toGet(outQ);
endmodule

typedef Server#(Sample, Acc) FirFilter;

(* synthesize *)
module mkFirFilter(FirFilter);
   FirFilter fir <- mkFir(coefficients);
   return fir;
endmodule

endpackage
