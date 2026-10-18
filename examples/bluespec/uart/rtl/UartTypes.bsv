// UartTypes: the types shared by the UART packages: frame format, receive events, status and pins.
package UartTypes;

export Parity(..);
export UartFormat;
export uartFormat;
export parityKind;
export Format8N1;
export RxKind(..);
export RxFrame(..);
export UartFlags(..);
export UartEvents(..);
export UartPins(..);

typedef enum { NoParity, EvenParity, OddParity } Parity deriving (Bits, Eq, FShow);

// A frame format, fixed at elaboration so that what is switched off costs no hardware: the
// numeric types are the number of data bits (5 to 9), of parity bits (0 or 1) and of stop
// bits (1 or 2). The value holds what the types cannot, the kind of parity. `uartFormat` is the
// only way to make one (the constructor is not exported), so every format that exists is in
// range: `UartFormat#(7, 1, 2) fmt = uartFormat(EvenParity);`.
typedef struct {
   Parity parity;
} UartFormat#(numeric type nData, numeric type nParity, numeric type nStop)
   deriving (Eq, FShow);

typedef UartFormat#(8, 0, 1) Format8N1;

// The sizes are bounded by provisos, so a format out of range is a type error here (bsc says that
// an `Add` instance, such as Add#(5, _, 4) for four data bits, does not exist). The parity kind
// is NoParity exactly when there is no parity bit.
function UartFormat#(nd, np, ns) uartFormat(Parity parity)
   provisos (Add#(5, _dataLow, nd), Add#(nd, _dataHigh, 9),
             Add#(np, _parityHigh, 1),
             Add#(1, _stopLow, ns), Add#(ns, _stopHigh, 2));
   if ((valueOf(np) == 0) != (parity == NoParity))
      return error("uartFormat: the parity bit count (0 or 1) and the parity kind disagree");
   else
      return UartFormat { parity: parity };
endfunction

function Parity parityKind(UartFormat#(nd, np, ns) fmt) = fmt.parity;

// What the receiver made of one frame.
typedef enum {
   Data,          // a good frame
   ParityError,   // the stop bit is high but the parity is wrong
   FrameError,    // the stop bit is low
   Break          // the stop bit and every data and parity bit are low
} RxKind deriving (Bits, Eq, FShow);

typedef struct {
   RxKind kind;
   Bit#(nData) data;   // meaningful in any frame, but only a Data frame is delivered
} RxFrame#(numeric type nData) deriving (Bits, Eq, FShow);

// Sticky error flags; `clearFlags` resets them.
typedef struct {
   Bool frameError;
   Bool parityError;
   Bool brk;
   Bool overrun;
} UartFlags deriving (Bits, Eq, FShow);

// One-cycle pulses. Each received frame raises exactly one of rxByte, frameError, parityError,
// brk and overrun, in the cycle after its stop bit was sampled.
typedef struct {
   Bool rxByte;        // a byte went into the receive buffer
   Bool frameError;    // stop bit low (but not a break); the byte is dropped
   Bool parityError;   // parity wrong; the byte is dropped
   Bool brk;           // a break: the line was low for the whole frame (once per break)
   Bool overrun;       // a good byte arrived while the receive buffer was full: dropped
   Bool txByte;        // a byte moved from the transmit buffer into the transmitter
} UartEvents deriving (Bits, Eq, FShow);

// The two pins, as plain Verilog ports without handshake signals. `rxd` is asynchronous to the
// clock: the receiver synchronizes it itself.
interface UartPins;
   (* always_ready, always_enabled, prefix = "" *)
   method Action rxd((* port = "rxd" *) Bit#(1) pin);
   (* always_ready, result = "txd" *)
   method Bit#(1) txd;
endinterface

endpackage
