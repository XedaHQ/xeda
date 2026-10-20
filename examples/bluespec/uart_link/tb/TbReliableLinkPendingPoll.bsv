package TbReliableLinkPendingPoll;

import StmtFSM :: *;
import Vector :: *;
import BuildVector :: *;
import GetPut :: *;
import UartLinkTypes :: *;
import ReliableLink :: *;

(* synthesize *)
module mkTbReliableLinkPendingPoll(Empty);
   ReliableLinkIfc link <- mkReliableLink(240, 8, 256);
   Reg#(UInt#(16)) cycle <- mkReg(0);
   Reg#(UInt#(16)) i <- mkReg(0);
   Reg#(UInt#(8)) delimiters <- mkReg(0);
   Reg#(UInt#(16)) txBytes <- mkReg(0);
   Reg#(UInt#(2)) epState <- mkReg(0);
   Reg#(LinkRequestHeader) epRequest <- mkRegU;
   Reg#(Bit#(16)) epPos <- mkReg(0);
   Reg#(Bit#(16)) epLength <- mkReg(0);

   Vector#(19, Bit#(8)) openFrame = vec(
      'h02, 'h62, 'h01, 'h01, 'h07, 'h01, 'h04, 'h03, 'h02, 'h01,
      'hf0, 'h02, 'h08, 'h05, 'h60, 'h84, 'h65, 'h03, 'h00);
   Vector#(16, Bit#(8)) req0 = vec(
      'h02, 'h40, 'h01, 'h0c, 'h13, 'h01, 'h04, 'h03, 'h02, 'h01,
      'h11, 'h81, 'h18, 'h23, 'h29, 'h00);
   Vector#(15, Bit#(8)) caps = vec(
      'h03, 'h61, 'h01, 'h0b, 'h13, 'h01, 'h04, 'h03, 'h02, 'h01,
      'hda, 'h0b, 'hb1, 'hb5, 'h00);
   Vector#(15, Bit#(8)) poll277 = vec(
      'h0e, 'h60, 'h09, 'h01, 'h15, 'h01, 'h04, 'h03, 'h02, 'h01,
      'h34, 'h62, 'hce, 'h02, 'h00);
   Vector#(15, Bit#(8)) poll278 = vec(
      'h0e, 'h60, 'h0a, 'h01, 'h16, 'h01, 'h04, 'h03, 'h02, 'h01,
      'h87, 'h2e, 'h36, 'hed, 'h00);

   rule count;
      cycle <= cycle + 1;
      if (cycle == 3000) $fatal(1, "FAIL: POLL while CAPS pending timed out");
   endrule

   rule drainTx;
      let value <- link.tx.get;
      txBytes <= txBytes + 1;
      if (value == 0) delimiters <= delimiters + 1;
   endrule

   rule epHeader (epState == 0);
      let request <- link.requestHeader.get;
      epRequest <= request;
      epPos <= 0;
      epState <= request.length == 0 ? 2 : 1;
   endrule

   rule epPayload (epState == 1);
      let value <- link.requestPayload.get;
      epPos <= epPos + 1;
      if (epPos + 1 == epRequest.length) epState <= 2;
   endrule

   rule epResponseHeader (epState == 2);
      Bit#(16) length = 180;
      link.responseHeader.put(LinkResponseHeader { seqNo: epRequest.seqNo, length: length });
      epLength <= length;
      epPos <= 0;
      epState <= 3;
   endrule

   rule epResponsePayload (epState == 3);
      link.responsePayload.put('h55);
      epPos <= epPos + 1;
      if (epPos + 1 == epLength) epState <= 0;
   endrule

   Stmt pollTest = seq
      for (i <= 0; i < 19; i <= i + 1) link.rx.put(openFrame[i]);
      await (delimiters == 1);
      for (i <= 0; i < 16; i <= i + 1) link.rx.put(req0[i]);
      await (delimiters == 2);
      for (i <= 0; i < 15; i <= i + 1) link.rx.put(caps[i]);
      repeat (40) noAction;
      if (delimiters != 2 || link.stat(10) != 0)
         $fatal(1, "FAIL: CAPS should be pending at 61 bytes of grant");
      for (i <= 0; i < 15; i <= i + 1) link.rx.put(poll277[i]);
      repeat (40) noAction;
      if (delimiters != 2)
         $fatal(1, "FAIL: CAPS sent with only 63 bytes of remaining grant");
      if (link.stat(10) != 0)
         $fatal(1, "FAIL: valid POLL was rejected while CAPS was pending");
      for (i <= 0; i < 16; i <= i + 1) link.rx.put(req0[i]);
      await (link.stat(10) == 1);
      if (delimiters != 2 || link.stat(14) != 1)
         $fatal(1, "FAIL: stale ACK changed pending control state");
      for (i <= 0; i < 15; i <= i + 1) link.rx.put(poll278[i]);
      await (delimiters == 3);
      if (link.stat(10) != 1 || link.stat(14) != 1)
         $fatal(1, "FAIL: pending CAPS did not resume at 64 bytes of grant");
      $display("PASS: pending CAPS accepts POLL ACK and grant at the credit boundary");
      $finish(0);
   endseq;
   mkAutoFSM(pollTest);
endmodule

endpackage
