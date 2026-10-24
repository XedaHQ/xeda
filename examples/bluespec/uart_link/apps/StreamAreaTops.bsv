// Synthesis tops for the UART link area regression.
package StreamAreaTops;

import GetPut :: *;
import LeanLink :: *;
import StreamExchange :: *;

interface StreamAdapterArea#(type in_t, type out_t);
   interface Put#(Bit#(8)) rx;
   interface Get#(Bit#(8)) tx;
   interface Get#(in_t) inputWords;
   interface Put#(out_t) outputWords;
   interface Reset coreReset;
endinterface

interface LeanLinkArea;
   interface Put#(Bit#(8)) rx;
   interface Get#(Bit#(8)) tx;
   interface Get#(LinkTok) request;
   interface Put#(LinkTok) response;
   method UInt#(8) requestPayloadLength;
   method UInt#(8) responseMax;
   method Bool responseReady;
endinterface

(* synthesize *)
module mkAreaGcdW8P256(StreamAdapterArea#(Bit#(64), Bit#(32)));
   LeanLink#(3, 8) link <- mkLeanLink;
   StreamExchange#(Bit#(64), Bit#(32)) stream <- mkStreamExchange(link);
   interface rx = link.rx;
   interface tx = link.tx;
   interface inputWords = stream.inputWords;
   interface outputWords = stream.outputWords;
   interface coreReset = stream.coreReset;
endmodule

(* synthesize *)
module mkAreaGcdW1P64(StreamAdapterArea#(Bit#(64), Bit#(32)));
   LeanLink#(0, 6) link <- mkLeanLink;
   StreamExchange#(Bit#(64), Bit#(32)) stream <- mkStreamExchange(link);
   interface rx = link.rx;
   interface tx = link.tx;
   interface inputWords = stream.inputWords;
   interface outputWords = stream.outputWords;
   interface coreReset = stream.coreReset;
endmodule

(* synthesize *)
module mkAreaAscon40W8P256(StreamAdapterArea#(Bit#(40), Bit#(40)));
   LeanLink#(3, 8) link <- mkLeanLink;
   StreamExchange#(Bit#(40), Bit#(40)) stream <- mkStreamExchange(link);
   interface rx = link.rx;
   interface tx = link.tx;
   interface inputWords = stream.inputWords;
   interface outputWords = stream.outputWords;
   interface coreReset = stream.coreReset;
endmodule

(* synthesize *)
module mkAreaAscon40W1P64(StreamAdapterArea#(Bit#(40), Bit#(40)));
   LeanLink#(0, 6) link <- mkLeanLink;
   StreamExchange#(Bit#(40), Bit#(40)) stream <- mkStreamExchange(link);
   interface rx = link.rx;
   interface tx = link.tx;
   interface inputWords = stream.inputWords;
   interface outputWords = stream.outputWords;
   interface coreReset = stream.coreReset;
endmodule

(* synthesize *)
module mkAreaBareW8P256(LeanLinkArea);
   LeanLink#(3, 8) link <- mkLeanLink;
   interface rx = link.rx;
   interface tx = link.tx;
   interface request = link.request;
   interface response = link.response;
   method UInt#(8) requestPayloadLength = link.requestPayloadLength;
   method UInt#(8) responseMax = link.responseMax;
   method Bool responseReady = link.responseReady;
endmodule

(* synthesize *)
module mkAreaBareW1P64(LeanLinkArea);
   LeanLink#(0, 6) link <- mkLeanLink;
   interface rx = link.rx;
   interface tx = link.tx;
   interface request = link.request;
   interface response = link.response;
   method UInt#(8) requestPayloadLength = link.requestPayloadLength;
   method UInt#(8) responseMax = link.responseMax;
   method Bool responseReady = link.responseReady;
endmodule

endpackage
