// Attach the generic 64-bit request and 32-bit result stream to the GCD server.
package GcdStream;

import ClientServer :: *;
import GetPut :: *;
import GcdUnit :: *;
import StreamExchange :: *;

export mkGcdStream;

module mkGcdStream#(StreamExchange#(Bit#(64), Bit#(32)) stream)(Empty);
   GcdUnit gcd <- mkGcdUnit(reset_by stream.coreReset);

   // The two independent rules let the server and transport apply backpressure separately.
   rule issue;
      let word <- stream.inputWords.get;
      GcdRequest request = GcdRequest {
         a: unpack(word[31:0]),
         b: unpack(word[63:32])
      };
      gcd.request.put(request);
   endrule

   rule collect;
      let result <- gcd.response.get;
      stream.outputWords.put(pack(result));
   endrule
endmodule

endpackage
