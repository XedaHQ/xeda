package TbReliableLinkPinnedReplayAck;

import StmtFSM :: *;
import Vector :: *;
import BuildVector :: *;
import GetPut :: *;
import UartLinkTypes :: *;
import ReliableLink :: *;

(* synthesize *)
module mkTbReliableLinkPinnedReplayAck(Empty);
   ReliableLinkIfc link <- mkReliableLink(240, 8, 256);
   Reg#(UInt#(16)) cycle <- mkReg(0);
   Reg#(UInt#(16)) i <- mkReg(0);
   Reg#(UInt#(8)) delimiters <- mkReg(0);
   Reg#(Bit#(8)) txByte <- mkReg(1);

   Vector#(19, Bit#(8)) openFrame = vec(
      'h03, 'h62, 'h05, 'h01, 'h03, 'h01, 'h01, 'h01, 'h01, 'h02,
      'hf0, 'h02, 'h08, 'h05, 'hed, 'h45, 'h6a, 'hc4, 0);
   Vector#(16, Bit#(8)) nopFrame = vec(
      'h02, 'h40, 'h01, 'h01, 'h03, 'h01, 'h01, 'h01,
      'h01, 'h01, 'h05, 'h4c, 'h41, 'hf3, 'h09, 0);
   // POLL ACK=1, same session and cursor as the fixture frames above.
   Vector#(15, Bit#(8)) ack1 = vec(
      'h04, 'h60, 'h09, 'h01, 'h03, 'h01, 'h01, 'h01,
      'h01, 'h05, 'hb1, 'he6, 'h60, 'h06, 0);
   // Same POLL with ACK=0, used after ACK=1 to prove the old value is rejected.
   Vector#(15, Bit#(8)) ack0 = vec(
      'h03, 'h60, 'h09, 'h01, 'h03, 'h01, 'h01, 'h01,
      'h01, 'h05, 'h79, 'hca, 'h63, 'h6e, 0);

   rule watchdog;
      cycle <= cycle + 1;
      if (cycle == 3000)
         $fatal(1, "FAIL: pinned replay ACK test timeout rx=%0d dup=%0d err=%0d tx=%0d",
                link.stat(0), link.stat(12), link.stat(10), link.stat(14));
   endrule

   Stmt test = seq
      // Open the link and drain its reply.
      for (i <= 0; i < 19; i <= i + 1) link.rx.put(openFrame[i]);
      action txByte <= 1; endaction
      while (txByte != 0) action
         let value <- link.tx.get;
         txByte <= value;
      endaction
      delimiters <= delimiters + 1;

      // Complete and transmit a one-byte application response.
      for (i <= 0; i < 16; i <= i + 1) link.rx.put(nopFrame[i]);
      action
         let request <- link.requestHeader.get;
         if (request.seqNo != 0 || request.length != 1)
            $fatal(1, "FAIL: request metadata mismatch");
      endaction
      action
         let value <- link.requestPayload.get;
         if (value != 0) $fatal(1, "FAIL: request payload mismatch");
      endaction
      link.responseHeader.put(LinkResponseHeader { seqNo: 0, length: 1 });
      link.responsePayload.put('h5a);
      action txByte <= 1; endaction
      while (txByte != 0) action
         let value <- link.tx.get;
         txByte <= value;
      endaction
      delimiters <= delimiters + 1;
      if (link.stat(14) != 1)
         $fatal(1, "FAIL: first application response was not transmitted");

      // Do not read TX while the duplicate starts its replay. Consume one data
      // byte only, which pins the response slot until the replay delimiter.
      for (i <= 0; i < 16; i <= i + 1) link.rx.put(nopFrame[i]);
      await (link.stat(12) == 1);
      action
         let value <- link.tx.get;
         if (value == 0) $fatal(1, "FAIL: replay began with a delimiter");
      endaction
      for (i <= 0; i < 15; i <= i + 1) link.rx.put(ack1[i]);
      await (link.stat(10) == 1);
      if (link.stat(14) != 1)
         $fatal(1, "FAIL: ACK retired a response pinned by replay TX");

      // Resume TX and finish the replay, then ACK it successfully.
      action txByte <= 1; endaction
      while (txByte != 0) action
         let value <- link.tx.get;
         txByte <= value;
      endaction
      delimiters <= delimiters + 1;
      if (link.stat(14) != 2)
         $fatal(1, "FAIL: replay delimiter did not release the response slot");
      for (i <= 0; i < 15; i <= i + 1) link.rx.put(ack1[i]);
      action txByte <= 1; endaction
      while (txByte != 0) action
         let value <- link.tx.get;
         txByte <= value;
      endaction
      if (link.stat(14) != 2)
         $fatal(1, "FAIL: accepted ACK unexpectedly retransmitted response");

      // A stale ACK must fail after ACK=1 has retired sequence zero.
      for (i <= 0; i < 15; i <= i + 1) link.rx.put(ack0[i]);
      await (link.stat(10) == 2);
      $display("PASS: ACK cannot retire a pinned replay and succeeds after its delimiter");
      $finish(0);
   endseq;
   mkAutoFSM(test);
endmodule

endpackage
