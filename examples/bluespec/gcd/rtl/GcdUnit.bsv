// GcdUnit: a GCD accelerator with a Get/Put (ClientServer) interface.
//
// Requests are queued in a SizedFIFO, handed round-robin to NumEngines `mkGcdEngine` instances
// (a separately synthesized module from the GcdEngine package), and their results collected in
// the same round-robin order into a response FIFO, so responses come back in request order.
package GcdUnit;

import FIFO :: *;
import GetPut :: *;
import ClientServer :: *;
import Vector :: *;
import GcdEngine :: *;

export Operand;
export GcdRequest(..);
export GcdUnit;
export NumEngines;
export mkGcdUnit;

typedef struct {
   Operand a;
   Operand b;
} GcdRequest deriving (Bits, Eq, FShow);

typedef Server#(GcdRequest, Operand) GcdUnit;

typedef 2 NumEngines;
typedef UInt#(TLog#(NumEngines)) EngineIndex;

function EngineIndex nextIndex(EngineIndex i) =
   i == fromInteger(valueOf(NumEngines) - 1) ? 0 : i + 1;

(* synthesize *)
module mkGcdUnit(GcdUnit);
   FIFO#(GcdRequest) requests <- mkSizedFIFO(4); // bsc library primitive SizedFIFO
   FIFO#(Operand) responses <- mkFIFO;           // bsc library primitive FIFO2
   Vector#(NumEngines, GcdEngine) engines <- replicateM(mkGcdEngine);

   Reg#(EngineIndex) issueIdx <- mkReg(0);
   Reg#(EngineIndex) collectIdx <- mkReg(0);

   rule issue;
      let req = requests.first;
      requests.deq;
      engines[issueIdx].start(req.a, req.b);
      issueIdx <= nextIndex(issueIdx);
   endrule

   rule collect;
      let g <- engines[collectIdx].result;
      responses.enq(g);
      collectIdx <= nextIndex(collectIdx);
   endrule

   interface request = toPut(requests);
   interface response = toGet(responses);
endmodule

endpackage
