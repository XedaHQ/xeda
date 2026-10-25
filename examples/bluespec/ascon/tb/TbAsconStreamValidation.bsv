package TbAsconStreamValidation;

import Clocks :: *;
import FIFOF :: *;
import SpecialFIFOs :: *;
import GetPut :: *;
import AsconStream :: *;

typedef enum { AssertInitialReset, WaitInitialReset, SendBadType, ReadError,
               DrainFailure, WaitFailure, AssertRecoveryReset, WaitRecoveryReset,
               SendHashStart, SendEmptyHash, ReadDigest, Done }
   TestPhase deriving (Bits, Eq, FShow);

(* synthesize *)
module mkTbAsconStreamValidation40(Empty);
   Clock clk <- exposeCurrentClock;
   MakeResetIfc appReset <- mkReset(2, True, clk);
   AsconWordStream dut <- mkAsconWordStream(reset_by appReset.new_rst);
   FIFOF#(Bit#(40)) inputWords <- mkSizedFIFOF(1, reset_by noReset);
   FIFOF#(Bit#(40)) outputWords <- mkSizedBypassFIFOF(8, reset_by noReset);
   Empty streamResetBridge <- mkAsconValidationBridge(dut, inputWords, outputWords,
                                                       reset_by appReset.new_rst);
   Reg#(TestPhase) phase <- mkReg(AssertInitialReset);
   Reg#(UInt#(8)) delay <- mkReg(0);
   Reg#(UInt#(4)) position <- mkReg(0);
   Reg#(Bit#(32)) cycle <- mkReg(0);

   rule watchdog;
      cycle <= cycle + 1;
      if (cycle == 20000) $fatal(1, "FAIL: Ascon40 validation reset test timed out");
   endrule

   rule resetInitial (phase == AssertInitialReset || phase == AssertRecoveryReset);
      appReset.assertReset;
      inputWords.clear;
      outputWords.clear;
      delay <= 8;
      phase <= phase == AssertInitialReset ? WaitInitialReset : WaitRecoveryReset;
   endrule

   rule waitReset (phase == WaitInitialReset || phase == WaitRecoveryReset);
      if (delay == 0)
         phase <= phase == WaitInitialReset ? SendBadType : SendHashStart;
      else delay <= delay - 1;
   endrule

   rule sendBadType (phase == SendBadType);
      inputWords.enq(40'h0000000810); // START has a reserved data bit set
      phase <= ReadError;
   endrule

   rule readError (phase == ReadError && outputWords.notEmpty);
      let got = outputWords.first;
      outputWords.deq;
      if (got != 40'h000000027f)
         $fatal(1, "FAIL: expected error 2 after malformed START, got %h", got);
      phase <= DrainFailure;
      position <= 0;
   endrule

   rule drainFailure (phase == DrainFailure);
      inputWords.enq((position % 2) == 0 ? 40'h0000000010 : 40'h00000000b0);
      position <= position + 1;
      if (position == 15) begin phase <= WaitFailure; delay <= 40; end
   endrule

   rule noExtraOutput ((phase == DrainFailure || phase == WaitFailure) && outputWords.notEmpty);
      let got = outputWords.first;
      outputWords.deq;
      $fatal(1, "FAIL: failed Ascon40 state emitted extra output %h", got);
   endrule

   rule waitFailure (phase == WaitFailure);
      if (delay == 0) phase <= AssertRecoveryReset;
      else delay <= delay - 1;
   endrule

   rule sendHashStart (phase == SendHashStart);
      inputWords.enq(40'h0000000210);
      phase <= SendEmptyHash;
   endrule

   rule sendEmptyHash (phase == SendEmptyHash);
      inputWords.enq(40'h00000000b0);
      phase <= ReadDigest;
      position <= 0;
   endrule

   rule readDigest (phase == ReadDigest && outputWords.notEmpty);
      let got = outputWords.first;
      outputWords.deq;
      Bit#(256) digest = 256'h911f250da896a74fb5f83c4b431184f6f588309197093de07ae836f014bc4673;
      UInt#(9) shift = zeroExtend(position) * 32;
      Bit#(32) expected = truncate(digest >> shift);
      Bit#(8) metadata = position == 7 ? 8'h5f : 8'h4f;
      if (got != {expected, metadata})
         $fatal(1, "FAIL: reset hash output %0d got %h expected %h", position,
                got, {expected, metadata});
      if (position == 7) begin
         phase <= Done;
      end else position <= position + 1;
   endrule

   rule finish (phase == Done);
      $display("PASS: Ascon40 malformed START drains, emits typed error, and reset restores hashing");
      $finish(0);
   endrule
endmodule

module mkAsconValidationBridge#(AsconWordStream dut, FIFOF#(Bit#(40)) inputWords,
                                FIFOF#(Bit#(40)) outputWords)(Empty);
   rule sendInput (inputWords.notEmpty);
      dut.inputWord.put(inputWords.first);
      inputWords.deq;
   endrule

   rule receiveOutput (outputWords.notFull);
      let word <- dut.outputWord.get;
      outputWords.enq(word);
   endrule
endmodule

endpackage
