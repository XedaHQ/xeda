// Streaming COBS receive decoding and reverse-COBS transmit encoding.
package Cobs;

import FIFOF :: *;
import GetPut :: *;

typedef union tagged {
   Bit#(8) Byte;
   Bool End;
} CobsToken deriving (Bits, Eq, FShow);

interface CobsDecoder;
   interface Put#(Bit#(8)) in;
   interface Get#(CobsToken) out;
   method Bool outputReady;
   method Action abort;
endinterface

interface RcobsEncoder;
   interface Put#(CobsToken) in;
   interface Get#(Bit#(8)) out;
endinterface

// Data is speculative until End True. End False marks a truncated COBS block.
// Empty delimiter runs produce no tokens. Both ports can apply backpressure.
module mkCobsDecoder(CobsDecoder);
   FIFOF#(Bit#(8)) inputBytes <- mkFIFOF;
   FIFOF#(CobsToken) outputTokens <- mkFIFOF;
   Reg#(Bit#(8)) remaining <- mkReg(0);
   Reg#(Bool) haveBlock <- mkReg(False);
   Reg#(Bool) insertZero <- mkReg(False);
   Reg#(Bool) discarding <- mkReg(False);
   PulseWire aborting <- mkPulseWire;

   rule abortFrame (aborting);
      inputBytes.clear;
      outputTokens.clear;
      remaining <= 0;
      haveBlock <= False;
      insertZero <= False;
      discarding <= True;
   endrule

   rule decode (!aborting);
      let value = inputBytes.first;
      inputBytes.deq;
      if (discarding) begin
         if (value == 0) discarding <= False;
      end
      else if (value == 0) begin
         if (haveBlock) outputTokens.enq(tagged End (remaining == 0));
         remaining <= 0;
         haveBlock <= False;
         insertZero <= False;
      end
      else if (remaining == 0) begin
         if (haveBlock && insertZero) outputTokens.enq(tagged Byte 0);
         remaining <= value - 1;
         haveBlock <= True;
         insertZero <= value != 255;
      end
      else begin
         outputTokens.enq(tagged Byte value);
         remaining <= remaining - 1;
      end
   endrule

   interface in = toPut(inputBytes);
   interface out = toGet(outputTokens);
   method Bool outputReady = outputTokens.notEmpty;
   method Action abort = aborting.send;
endmodule

typedef enum { Running, FullBlock, FinalCode, Delimiter } EncodeState deriving (Bits, Eq);

// Every nonzero byte passes through. A zero emits the trailing block code.
// End True adds the final code and delimiter. End False terminates an aborted frame.
module mkRcobsEncoder(RcobsEncoder);
   FIFOF#(CobsToken) inputTokens <- mkFIFOF;
   FIFOF#(Bit#(8)) outputBytes <- mkFIFOF;
   Reg#(Bit#(8)) count <- mkReg(0);
   Reg#(EncodeState) state <- mkReg(Running);

   rule encode (state == Running);
      let token = inputTokens.first;
      inputTokens.deq;
      case (token) matches
         tagged Byte .value: begin
            if (value == 0) begin
               outputBytes.enq(count + 1);
               count <= 0;
            end
            else begin
               outputBytes.enq(value);
               count <= count + 1;
               if (count == 253) state <= FullBlock;
            end
         end
         tagged End .valid: state <= valid ? FinalCode : Delimiter;
      endcase
   endrule

   rule fullBlock (state == FullBlock);
      outputBytes.enq(255);
      count <= 0;
      state <= Running;
   endrule

   rule finalCode (state == FinalCode);
      outputBytes.enq(count + 1);
      state <= Delimiter;
   endrule

   rule delimiter (state == Delimiter);
      outputBytes.enq(0);
      count <= 0;
      state <= Running;
   endrule

   interface in = toPut(inputTokens);
   interface out = toGet(outputBytes);
endmodule

endpackage
