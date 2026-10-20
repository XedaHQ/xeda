package UartLinkTypes;

export LinkRequestHeader(..);
export LinkResponseHeader(..);
export LinkService(..);
export ReliableLinkIfc(..);
export LinkStats(..);
export maxWireBytes;
export crc32cUpdate;
export ctlRequest;
export ctlResponse;
export ctlControl;
export ctlControlResponse;
export ctlVersion;
export controlPoll;
export controlCapabilities;
export controlOpen;
export controlClose;
export flagGap;
export flagReplay;

import GetPut :: *;
import Crc32c :: *;

typedef struct {
   Bit#(8) seqNo;
   Bit#(16) length;
   Bit#(16) maxResponse;
} LinkRequestHeader deriving (Bits, Eq, FShow);

typedef struct {
   Bit#(8) seqNo;
   Bit#(16) length;
} LinkResponseHeader deriving (Bits, Eq, FShow);

interface LinkService;
   interface Get#(LinkRequestHeader) requestHeader;
   interface Get#(Bit#(8)) requestPayload;
   interface Put#(LinkResponseHeader) responseHeader;
   interface Put#(Bit#(8)) responsePayload;
endinterface

interface ReliableLinkIfc;
   interface Put#(Bit#(8)) rx;
   interface Get#(Bit#(8)) tx;
   interface Get#(LinkRequestHeader) requestHeader;
   interface Get#(Bit#(8)) requestPayload;
   interface Put#(LinkResponseHeader) responseHeader;
   interface Put#(Bit#(8)) responsePayload;
   method Action abortRx;
   method Action recordPhyFault(Bit#(2) kind);
   method Bit#(32) stat(Bit#(4) index);
endinterface

typedef struct {
   Bit#(32) transportRxBytes;
   Bit#(32) transportTxBytes;
   Bit#(32) framingErrors;
   Bit#(32) parityErrors;
   Bit#(32) breaks;
   Bit#(32) overruns;
   Bit#(32) validRequests;
   Bit#(32) cobsErrors;
   Bit#(32) crcErrors;
   Bit#(32) lengthErrors;
   Bit#(32) headerErrors;
   Bit#(32) gaps;
   Bit#(32) requestDuplicates;
   Bit#(32) receiveDrops;
   Bit#(32) responseTransmissions;
   Bit#(32) creditBlocked;
} LinkStats deriving (Bits, Eq, FShow);

typedef 240 DefaultFrameLimit;
typedef 8 DefaultWindowLimit;
typedef 256 DefaultSlotBytes;
typedef 45 ControlReserveBytes;
typedef 9 FrameHeaderBytes;
typedef 4 FrameCrcBytes;

function Integer maxWireBytes(Integer decodedBytes);
   return decodedBytes + (decodedBytes / 254) + 2;
endfunction

Bit#(8) ctlVersion = 8'h40;
Bit#(8) ctlRequest = 8'h00;
Bit#(8) ctlResponse = 8'h10;
Bit#(8) ctlControl = 8'h20;
Bit#(8) ctlControlResponse = 8'h30;

Bit#(2) controlPoll = 0;
Bit#(2) controlCapabilities = 1;
Bit#(2) controlOpen = 2;
Bit#(2) controlClose = 3;

Bit#(2) flagGap = 1;
Bit#(2) flagReplay = 2;

endpackage
