// A full output queue must not stop the failed wrapper from draining later input.
package TbAsconStreamBackpressure;
import GetPut :: *;
import AsconStream :: *;

typedef enum { StartHash, SendEmptyHash, WaitForOutput, SendFailure,
               DrainFailedInput, ReadOutput, Done }
   TestPhase deriving (Bits, Eq, FShow);

(* synthesize *)
module mkTbAsconStreamBackpressure(Empty);
   AsconWordStream dut <- mkAsconWordStream;
   Reg#(TestPhase) phase <- mkReg(StartHash);
   Reg#(UInt#(16)) count <- mkReg(0);
   Reg#(UInt#(4)) outputsRead <- mkReg(0);
   Reg#(UInt#(16)) cycle <- mkReg(0);

   rule watchdog;
      cycle <= cycle + 1;
      if (cycle == 5000) $fatal(1, "FAIL: failed-state input stalled behind full output");
   endrule

   rule startHash (phase == StartHash);
      dut.inputWord.put(40'h0000000210);
      phase <= SendEmptyHash;
   endrule

   rule finishHash (phase == SendEmptyHash);
      dut.inputWord.put(40'h00000000b0);
      phase <= WaitForOutput;
      count <= 300;
   endrule

   rule waitForOutput (phase == WaitForOutput);
      if (count == 0) phase <= SendFailure;
      else count <= count - 1;
   endrule

   rule failOperation (phase == SendFailure);
      dut.inputWord.put(40'h00000000ff);
      phase <= DrainFailedInput;
      count <= 0;
   endrule

   rule drainFailedInput (phase == DrainFailedInput);
      dut.inputWord.put(40'h00000000b0);
      count <= count + 1;
      if (count == 31) phase <= ReadOutput;
   endrule

   rule releaseOutput (phase == ReadOutput);
      let got <- dut.outputWord.get;
      if (got[7:5] == 3) begin
         if (got != 40'h000000017f) $fatal(1, "FAIL: incorrect terminal error %h", got);
         $display("PASS: failed-state input drains while terminal error waits behind full output");
         phase <= Done;
      end else begin
         if (got[7:5] != 2 || got[4] != pack(outputsRead == 7))
            $fatal(1, "FAIL: unexpected Ascon output before error %h", got);
         outputsRead <= outputsRead + 1;
      end
   endrule

   rule finish (phase == Done);
      $finish(0);
   endrule
endmodule
endpackage
