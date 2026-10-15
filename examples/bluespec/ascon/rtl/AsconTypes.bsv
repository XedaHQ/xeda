// SPDX-License-Identifier: Apache-2.0
// Standalone stream types for the Xeda port of BlueLight Ascon v1.2.
// See ../NOTICE.md for the source revision and changes.
package AsconTypes;

import GetPut :: *;

typedef enum { Encrypt, Decrypt, Hash } AsconOperation deriving (Bits, Eq, FShow);

typedef struct {
    AsconOperation operation;
    Bool newKey;
} AsconCommand deriving (Bits, Eq, FShow);

typedef enum { Nonce, AssociatedData, Text, HashMessage } AsconSegment deriving (Bits, Eq, FShow);

typedef struct {
    Bit#(32) data;
    Bit#(4) keep;
    Bool last;
    AsconSegment segment;
} AsconInput deriving (Bits, Eq, FShow);

typedef enum { TextOutput, TagOutput, DigestOutput } AsconOutputKind deriving (Bits, Eq, FShow);

typedef struct {
    Bit#(32) data;
    Bit#(4) keep;
    Bool last;
    Bool endOfOperation;
    AsconOutputKind kind;
} AsconOutput deriving (Bits, Eq, FShow);

interface AsconIfc;
    method Action start(AsconCommand command);
    interface Put#(Bit#(32)) key;
    interface Put#(AsconInput) inputWord;
    interface Get#(AsconOutput) outputWord;
    method Bool idle;
endinterface

endpackage
