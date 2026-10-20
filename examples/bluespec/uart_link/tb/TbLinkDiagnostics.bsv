// Self-checking tests for diagnostic payloads, bounds, snapshots and response backpressure.
package TbLinkDiagnostics;

import GetPut :: *;
import RegFile :: *;
import StmtFSM :: *;
import UartLinkTypes :: *;
import LinkDiagnostics :: *;

interface DiagnosticLinkModel;
   interface ReliableLinkIfc link;
   method Action submitHeader(LinkRequestHeader header);
   method Action submitByte(Bit#(8) value);
   method UInt#(4) responseHeaders;
   method UInt#(9) responseBytes;
   method LinkResponseHeader responseHeaderAt(Bit#(4) index);
   method ActionValue#(Bit#(8)) takeResponseByte;
endinterface

module mkDiagnosticLinkModel#(Integer headerDelay, Integer payloadDelay)(DiagnosticLinkModel);
   RegFile#(Bit#(3), LinkRequestHeader) requestHeaders <- mkRegFileFull;
   RegFile#(Bit#(8), Bit#(8)) requestBytes <- mkRegFileFull;
   RegFile#(Bit#(3), LinkResponseHeader) responseHeaderMemory <- mkRegFileFull;
   RegFile#(Bit#(9), Bit#(8)) responseByteMemory <- mkRegFileFull;
   Reg#(UInt#(4)) requestHeaderCount <- mkReg(0);
   Reg#(UInt#(4)) requestHeaderRead <- mkReg(0);
   Reg#(UInt#(8)) requestByteCount <- mkReg(0);
   Reg#(UInt#(8)) requestByteRead <- mkReg(0);
   Reg#(UInt#(4)) responseHeaderCount <- mkReg(0);
   Reg#(UInt#(9)) responseByteCount <- mkReg(0);
   Reg#(UInt#(9)) responseByteRead <- mkReg(0);
   Reg#(Bit#(32)) currentStat <- mkReg(0);
   Reg#(UInt#(16)) linkCycles <- mkReg(0);
   Bool headerReady = linkCycles >= fromInteger(headerDelay);
   Bool payloadReady = linkCycles >= fromInteger(payloadDelay);

   (* fire_when_enabled, no_implicit_conditions *)
   rule tickStat;
      currentStat <= currentStat + 'h01010101;
      linkCycles <= linkCycles + 1;
   endrule

   interface ReliableLinkIfc link;
      interface Put rx;
         method Action put(Bit#(8) value) if (False);
            noAction;
         endmethod
      endinterface
      interface Get tx;
         method ActionValue#(Bit#(8)) get if (False);
            return ?;
         endmethod
      endinterface
      interface Get requestHeader;
         method ActionValue#(LinkRequestHeader) get if (requestHeaderRead != requestHeaderCount);
            let value = requestHeaders.sub(truncate(pack(requestHeaderRead)));
            requestHeaderRead <= requestHeaderRead + 1;
            return value;
         endmethod
      endinterface
      interface Get requestPayload;
         method ActionValue#(Bit#(8)) get if (requestByteRead != requestByteCount);
            let value = requestBytes.sub(pack(requestByteRead));
            requestByteRead <= requestByteRead + 1;
            return value;
         endmethod
      endinterface
      interface Put responseHeader;
         method Action put(LinkResponseHeader value) if (headerReady);
            responseHeaderMemory.upd(truncate(pack(responseHeaderCount)), value);
            responseHeaderCount <= responseHeaderCount + 1;
         endmethod
      endinterface
      interface Put responsePayload;
         method Action put(Bit#(8) value) if (payloadReady);
            responseByteMemory.upd(pack(responseByteCount), value);
            responseByteCount <= responseByteCount + 1;
         endmethod
      endinterface
      method Action abortRx = noAction;
      method Action recordPhyFault(Bit#(2) kind) = noAction;
      method Bit#(32) stat(Bit#(4) index) = currentStat;
   endinterface

   method Action submitHeader(LinkRequestHeader header);
      requestHeaders.upd(truncate(pack(requestHeaderCount)), header);
      requestHeaderCount <= requestHeaderCount + 1;
   endmethod
   method Action submitByte(Bit#(8) value);
      requestBytes.upd(pack(requestByteCount), value);
      requestByteCount <= requestByteCount + 1;
   endmethod
   method UInt#(4) responseHeaders = responseHeaderCount;
   method UInt#(9) responseBytes = responseByteCount;
   method LinkResponseHeader responseHeaderAt(Bit#(4) index) =
      responseHeaderMemory.sub(truncate(index));
   method ActionValue#(Bit#(8)) takeResponseByte if (responseByteRead != responseByteCount);
      let value = responseByteMemory.sub(pack(responseByteRead));
      responseByteRead <= responseByteRead + 1;
      return value;
   endmethod
endmodule

(* synthesize *)
module mkTbLinkDiagnostics(Empty);
   DiagnosticLinkModel peer <- mkDiagnosticLinkModel(20, 25);
   DiagnosticLinkModel blockedPeer <- mkDiagnosticLinkModel(1000, 1005);
   Empty service <- mkLinkDiagnostics(peer.link, 100000000, 12000000, 'h12345678);
   Empty blockedService <- mkLinkDiagnostics(blockedPeer.link, 100000000, 12000000, 'h12345678);
   Reg#(UInt#(16)) cycles <- mkReg(0);
   Reg#(UInt#(4)) baseHeaders <- mkReg(0);
   Reg#(UInt#(9)) baseBytes <- mkReg(0);
   Reg#(Bit#(8)) statsFirstByte <- mkReg(0);

   rule watchdog;
      cycles <= cycles + 1;
      if (cycles == 1200)
         $fatal(1, "FAIL: diagnostics test timed out headers=%0d bytes=%0d blocked=%0d lastLen=%0d",
                peer.responseHeaders, peer.responseBytes, blockedPeer.responseHeaders,
                peer.responseHeaderAt(5).length);
   endrule

   Stmt test = seq
      // A separately stalled header channel must block payload writes and retain the header.
      blockedPeer.submitHeader(LinkRequestHeader { seqNo: 0, length: 1, maxResponse: 1 });
      blockedPeer.submitByte(0);
      repeat (8) noAction;
      if (blockedPeer.responseHeaders != 0 || blockedPeer.responseBytes != 0)
         $fatal(1, "FAIL: response crossed a stalled header channel");

      // Payload readiness follows the first accepted header. The first payload is stalled until
      // after that header, which exposes repeated writes while responseWritePos remains zero.
      peer.submitHeader(LinkRequestHeader { seqNo: 1, length: 6, maxResponse: 4 });
      peer.submitByte('h30); peer.submitByte(3); peer.submitByte(0);
      peer.submitByte('h11); peer.submitByte('h22); peer.submitByte('h33);
      await (peer.responseBytes == 4);
      if (peer.responseHeaders != 1)
         $fatal(1, "FAIL: response header was repeated while payload was stalled");
      if (peer.responseHeaderAt(0).length != 4)
         $fatal(1, "FAIL: ECHO response length was not 4");
      action let value <- peer.takeResponseByte;
         if (value != 'hB0) $fatal(1, "FAIL: ECHO marker was %02x", value);
      endaction
      action let value <- peer.takeResponseByte;
         if (value != 'h11) $fatal(1, "FAIL: ECHO byte 0 was %02x", value);
      endaction
      action let value <- peer.takeResponseByte;
         if (value != 'h22) $fatal(1, "FAIL: ECHO byte 1 was %02x", value);
      endaction
      action let value <- peer.takeResponseByte;
         if (value != 'h33) $fatal(1, "FAIL: ECHO byte 2 was %02x", value);
      endaction

      // An empty request after ECHO must return a deterministic LENGTH error, not the prior opcode.
      peer.submitHeader(LinkRequestHeader { seqNo: 2, length: 0, maxResponse: 3 });
      await (peer.responseHeaders == 2 && peer.responseBytes == 7);
      action let value <- peer.takeResponseByte;
         if (value != 'hFF) $fatal(1, "FAIL: empty request error tag was %02x", value);
      endaction
      action let value <- peer.takeResponseByte;
         if (value != 2) $fatal(1, "FAIL: empty request error code was %02x", value);
      endaction
      action let value <- peer.takeResponseByte;
         if (value != 0) $fatal(1, "FAIL: empty request used stale opcode %02x", value);
      endaction

      // This fixed vector is independently calculated from the documented 31-bit recurrence.
      peer.submitHeader(LinkRequestHeader { seqNo: 3, length: 7, maxResponse: 5 });
      peer.submitByte('h31); peer.submitByte('h78); peer.submitByte('h56);
      peer.submitByte('h34); peer.submitByte('h12); peer.submitByte(4); peer.submitByte(0);
      await (peer.responseHeaders == 3 && peer.responseBytes == 12);
      action let value <- peer.takeResponseByte;
         if (value != 'hB1) $fatal(1, "FAIL: PRBS marker was %02x", value);
      endaction
      action let value <- peer.takeResponseByte;
         if (value != 'h24) $fatal(1, "FAIL: PRBS byte 0 was %02x", value);
      endaction
      action let value <- peer.takeResponseByte;
         if (value != 'h68) $fatal(1, "FAIL: PRBS byte 1 was %02x", value);
      endaction
      action let value <- peer.takeResponseByte;
         if (value != 'hAC) $fatal(1, "FAIL: PRBS byte 2 was %02x", value);
      endaction
      action let value <- peer.takeResponseByte;
         if (value != 'hF0) $fatal(1, "FAIL: PRBS byte 3 was %02x", value);
      endaction

      // A 16-bit count must not wrap its response-length calculation to zero.
      baseHeaders <= peer.responseHeaders;
      baseBytes <= peer.responseBytes;
      peer.submitHeader(LinkRequestHeader { seqNo: 4, length: 7, maxResponse: 256 });
      peer.submitByte('h31); peer.submitByte('h78); peer.submitByte('h56);
      peer.submitByte('h34); peer.submitByte('h12); peer.submitByte('hFF); peer.submitByte('hFF);
      await (peer.responseHeaders == baseHeaders + 1 && peer.responseBytes == baseBytes + 3);
      action let value <- peer.takeResponseByte;
         if (value != 'hFF) $fatal(1, "FAIL: oversized PRBS error tag was %02x", value);
      endaction
      action let value <- peer.takeResponseByte;
         if (value != 2) $fatal(1, "FAIL: oversized PRBS error code was %02x", value);
      endaction
      action let value <- peer.takeResponseByte;
         if (value != 'h31) $fatal(1, "FAIL: oversized PRBS error opcode was %02x", value);
      endaction

      // GET_STATS snapshots one counter at dispatch. A later counter change cannot tear the reply.
      baseHeaders <= peer.responseHeaders;
      baseBytes <= peer.responseBytes;
      peer.submitHeader(LinkRequestHeader { seqNo: 6, length: 2, maxResponse: 6 });
      peer.submitByte('h03); peer.submitByte(0);
      repeat (5) noAction;
      if (peer.responseHeaders != baseHeaders + 1)
         $fatal(1, "FAIL: GET_STATS response header did not arrive once");
      if (peer.responseHeaderAt(truncate(pack(baseHeaders))).length != 6)
         $fatal(1, "FAIL: GET_STATS returned length %0d", peer.responseHeaderAt(truncate(pack(baseHeaders))).length);
      await (peer.responseBytes == baseBytes + 6);
      action let value <- peer.takeResponseByte;
         if (value != 'h83) $fatal(1, "FAIL: GET_STATS marker was %02x", value);
      endaction
      action let value <- peer.takeResponseByte;
         if (value != 0) $fatal(1, "FAIL: GET_STATS index was %02x", value);
      endaction
      action let value <- peer.takeResponseByte;
         statsFirstByte <= value;
      endaction
      action let value <- peer.takeResponseByte;
         if (value != statsFirstByte) $fatal(1, "FAIL: GET_STATS snapshot byte changed");
      endaction
      action let value <- peer.takeResponseByte;
         if (value != statsFirstByte) $fatal(1, "FAIL: GET_STATS snapshot byte changed");
      endaction
      action let value <- peer.takeResponseByte;
         if (value != statsFirstByte) $fatal(1, "FAIL: GET_STATS snapshot byte changed");
      endaction


      // The high count byte remains FF from the prior PRBS request. The declared six-byte
      // payload updates the low byte to FF; a 16-bit count-plus-seven would wrap to six.
      baseHeaders <= peer.responseHeaders;
      baseBytes <= peer.responseBytes;
      peer.submitHeader(LinkRequestHeader { seqNo: 5, length: 6, maxResponse: 3 });
      peer.submitByte('h32); peer.submitByte('h78); peer.submitByte('h56);
      peer.submitByte('h34); peer.submitByte('h12); peer.submitByte('hFF);
      await (peer.responseHeaders == baseHeaders + 1 && peer.responseBytes == baseBytes + 3);
      action let value <- peer.takeResponseByte;
         if (value != 'hFF) $fatal(1, "FAIL: PRBSCHK error tag was %02x", value);
      endaction
      action let value <- peer.takeResponseByte;
         if (value != 2) $fatal(1, "FAIL: PRBSCHK error code was %02x", value);
      endaction
      action let value <- peer.takeResponseByte;
         if (value != 'h32) $fatal(1, "FAIL: PRBSCHK error opcode was %02x", value);
      endaction

      $display("PASS: diagnostics ECHO, PRBS, ERR, bounds, stats snapshot and backpressure");
      $finish(0);
   endseq;
   mkAutoFSM(test);
endmodule

endpackage
