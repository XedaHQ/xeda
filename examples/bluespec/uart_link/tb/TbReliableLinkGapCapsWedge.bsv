package TbReliableLinkGapCapsWedge;

import StmtFSM :: *;
import Vector :: *;
import BuildVector :: *;
import GetPut :: *;
import UartLinkTypes :: *;
import ReliableLink :: *;

(* synthesize *)
module mkTbReliableLinkGapCapsWedge(Empty);
   ReliableLinkIfc link <- mkReliableLink(240, 8, 256);
   Reg#(UInt#(16)) cycle <- mkReg(0);
   Reg#(UInt#(16)) i <- mkReg(0);
   Reg#(UInt#(8)) delimiters <- mkReg(0);
   Reg#(UInt#(16)) txBytes <- mkReg(0);
   Reg#(UInt#(8)) req1Fed <- mkReg(0);
   Reg#(UInt#(2)) epState <- mkReg(0);
   Reg#(LinkRequestHeader) epRequest <- mkRegU;
   Reg#(Bit#(16)) epPos <- mkReg(0);
   Reg#(Bit#(16)) epLength <- mkReg(0);

   Vector#(19, Bit#(8)) openFrame = vec('h02, 'h62, 'h01, 'h01, 'h07, 'h01, 'h04, 'h03, 'h02, 'h01, 'hf0, 'h02, 'h08, 'h05, 'h60, 'h84, 'h65, 'h03, 'h00);
   Vector#(16, Bit#(8)) req0 = vec('h02, 'h40, 'h01, 'h0c, 'h13, 'h01, 'h04, 'h03, 'h02, 'h01, 'h11, 'h81, 'h18, 'h23, 'h29, 'h00);
   Vector#(15, Bit#(8)) caps = vec('h03, 'h61, 'h01, 'h0b, 'h13, 'h01, 'h04, 'h03, 'h02, 'h01, 'hda, 'h0b, 'hb1, 'hb5, 'h00);
   Vector#(16, Bit#(8)) req2 = vec('h03, 'h40, 'h02, 'h0c, 'h13, 'h01, 'h04, 'h03, 'h02, 'h01, 'h22, 'ha5, 'h78, 'h01, 'he7, 'h00);
   Vector#(16, Bit#(8)) req1 = vec('h0f, 'h40, 'h01, 'h01, 'hd6, 'h01, 'h04, 'h03, 'h02, 'h01, 'h33, 'h50, 'had, 'h9d, 'h17, 'h00);

   rule count;
      cycle <= cycle + 1;
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
      Bit#(16) length = epRequest.seqNo == 0 ? 180 : 4;
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

   rule watchdog (cycle == 3000);
      $display("delimiters=%0d txBytes=%0d req1Fed=%0d validRequests=%0d gaps=%0d headerErrors=%0d creditBlocked=%0d responseTx=%0d",
                delimiters, txBytes, req1Fed, link.stat(6), link.stat(11), link.stat(10), link.stat(15), link.stat(14));
      $fatal(1, "FAIL: CAPS/GAP receive wedge");
   endrule

   Stmt test = seq
      for (i <= 0; i < 19; i <= i + 1) link.rx.put(openFrame[i]);
      await (delimiters == 1);
      for (i <= 0; i < 16; i <= i + 1) link.rx.put(req0[i]);
      await (delimiters == 2);
      $display("after response 0: txBytes=%0d (OPEN reply 19 + response 195 = 214)", txBytes);
      for (i <= 0; i < 15; i <= i + 1) link.rx.put(caps[i]);
      repeat (100) noAction;
      for (i <= 0; i < 16; i <= i + 1) link.rx.put(req2[i]);
      repeat (100) noAction;
      for (i <= 0; i < 16; i <= i + 1) action
         link.rx.put(req1[i]);
         req1Fed <= req1Fed + 1;
      endaction
      await (delimiters == 5);
      if (link.stat(6) != 2 || link.stat(11) != 1 || link.stat(14) != 2)
         $fatal(1, "FAIL: GAP recovery dispatch=%0d gaps=%0d responses=%0d",
                link.stat(6), link.stat(11), link.stat(14));
      $display("PASS: CAPS/GAP recovery accepts request 1 after grant refresh");
      $finish(0);
   endseq;
   mkAutoFSM(test);
endmodule

endpackage
