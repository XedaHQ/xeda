// Test endpoint: return the dispatch number followed by the original request.
package LinkPacketTest;

import GetPut :: *;
import RegFile :: *;
import ReliableLink :: *;
import UartLinkTypes :: *;

interface LinkPacketTestIfc;
   method Action rxData(Bit#(8) value);
   method ActionValue#(Bit#(8)) txData;
   method Action abortRx;
   method Action setResponseStall(Bool value);
   method Bit#(32) dispatchCount;
   method Bit#(32) linkStat(Bit#(4) index);
endinterface

typedef enum { Idle, Receiving, Header, Sending } EndpointState deriving (Bits, Eq);

(* synthesize *)
module mkLinkPacketTest(LinkPacketTestIfc);
   ReliableLinkIfc link <- mkReliableLink(240, 8, 256);
   RegFile#(Bit#(8), Bit#(8)) memory <- mkRegFileFull;
   Reg#(EndpointState) state <- mkReg(Idle);
   Reg#(LinkRequestHeader) request <- mkRegU;
   Reg#(Bit#(16)) position <- mkReg(0);
   Reg#(Bit#(16)) responseLength <- mkReg(0);
   Reg#(Bit#(32)) executions <- mkReg(0);
   Reg#(Bool) responseStalled <- mkReg(False);

   rule beginRequest (state == Idle);
      let header <- link.requestHeader.get;
      request <= header;
      position <= 0;
      executions <= executions + 1;
      state <= header.length == 0 ? Header : Receiving;
   endrule

   rule readRequest (state == Receiving);
      let value <- link.requestPayload.get;
      memory.upd(truncate(position), value);
      position <= position + 1;
      if (position + 1 == request.length) state <= Header;
   endrule

   rule sendHeader (state == Header);
      Bit#(16) desiredLength = request.length + 4;
      Bit#(16) boundedLength = desiredLength < request.maxResponse ?
         desiredLength : request.maxResponse;
      link.responseHeader.put(LinkResponseHeader {
         seqNo: request.seqNo, length: boundedLength
      });
      responseLength <= boundedLength;
      position <= 0;
      state <= boundedLength == 0 ? Idle : Sending;
   endrule

   rule sendPayload (state == Sending && !responseStalled);
      Bit#(8) value = memory.sub(truncate(position - 4));
      case (position)
         0: value = executions[7:0];
         1: value = executions[15:8];
         2: value = executions[23:16];
         3: value = executions[31:24];
      endcase
      link.responsePayload.put(value);
      position <= position + 1;
      if (position + 1 == responseLength) state <= Idle;
   endrule

   method Action rxData(Bit#(8) value);
      link.rx.put(value);
   endmethod
   method ActionValue#(Bit#(8)) txData;
      let value <- link.tx.get;
      return value;
   endmethod
   method Action abortRx = link.abortRx;
   method Action setResponseStall(Bool value);
      responseStalled <= value;
   endmethod
   method Bit#(32) dispatchCount = executions;
   method Bit#(32) linkStat(Bit#(4) index) = link.stat(index);
endmodule

endpackage
