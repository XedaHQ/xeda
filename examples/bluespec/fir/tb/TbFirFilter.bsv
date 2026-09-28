// Self-checking testbench for mkFirFilter.
//
// Streams directed stimuli (impulses, whose responses are the coefficients, and a full-scale
// step) followed by LFSR-driven samples through the filter, and compares every output with a
// direct-form golden model: y[t] = sum over k of c[k] * x[t - k].
// The widths and tap count come from the FirFilter package, so the testbench follows the
// TAPS / SAMPLE_WIDTH macros the filter was compiled with.
package TbFirFilter;

import FIFO :: *;
import GetPut :: *;
import ClientServer :: *;
import Vector :: *;
import LFSR :: *;
import FirFilter :: *;

typedef 400 NumSamples;
UInt#(32) maxCycles = 5000;

(* synthesize *)
module mkTbFirFilter(Empty);
   FirFilter dut <- mkFirFilter;

   LFSR#(Bit#(32)) lfsr <- mkLFSR_32;
   // history[k] = x[t - k], the input window of the golden model
   Vector#(NumTaps, Reg#(Sample)) history <- replicateM(mkReg(0));
   FIFO#(Acc) expected <- mkSizedFIFO(4);
   Reg#(UInt#(16)) nSent <- mkReg(0);
   Reg#(UInt#(16)) nChecked <- mkReg(0);
   Reg#(UInt#(32)) cycle <- mkReg(0);

   rule watchdog;
      cycle <= cycle + 1;
      if (cycle == maxCycles)
         $fatal(1, "FAIL: timeout after %0d cycles", cycle);
   endrule

   function Sample stimulus(UInt#(16) i);
      UInt#(16) taps = fromInteger(valueOf(NumTaps));
      if (i == 0) return maxBound;                   // impulse: response = coefficients
      else if (i <= taps) return 0;
      else if (i == taps + 1) return minBound;       // negative full-scale impulse
      else if (i <= 2 * taps + 1) return 0;
      else if (i <= 3 * taps + 1) return maxBound;   // step
      else return unpack(truncate(lfsr.value));       // pseudo-random samples
   endfunction

   rule send (nSent < fromInteger(valueOf(NumSamples)));
      let x = stimulus(nSent);
      dut.request.put(x);
      lfsr.next;
      nSent <= nSent + 1;
      // Golden model: direct-form convolution over the input window.
      Vector#(NumTaps, Sample) window = shiftInAt0(readVReg(history), x);
      writeVReg(history, window);
      Acc y = 0;
      for (Integer k = 0; k < valueOf(NumTaps); k = k + 1)
         y = y + signExtend(window[k]) * signExtend(coefficients[k]);
      expected.enq(y);
   endrule

   rule check;
      let y <- dut.response.get;
      let e = expected.first;
      expected.deq;
      if (y != e)
         $fatal(1, "FAIL: output %0d: expected %0d, got %0d", nChecked, e, y);
      nChecked <= nChecked + 1;
      if (nChecked + 1 == fromInteger(valueOf(NumSamples))) begin
         $display("PASS: %0d-tap FIR with %0d-bit samples, %0d outputs checked in %0d cycles",
                  valueOf(NumTaps), valueOf(SampleWidth), nChecked + 1, cycle);
         $finish(0);
      end
   endrule
endmodule

endpackage
