# Ascon v1.2 in Bluespec

The interface assumes the segment order and byte masks described below. It does not validate
malformed input. A wrong segment or mask can stall the core until reset or produce wrong output.

This directory contains a standalone, 32-bit Ascon core and a vector-driven testbench. A command
has `operation` and `newKey` fields. For AEAD, send an optional 128-bit key as four `Put32` words,
then the 16-byte nonce as four full `Nonce` words. Set `last` to false for the first three nonce
words and true for the fourth. Send the `AssociatedData` words next, then the `Text` words. Set
`last` on the final word of each segment. An empty associated-data, text, or hash-message segment
uses one final input word with `keep=4'b0000`; empty text produces no `TextOutput`. Do not append
an empty marker to a nonempty segment. For Hash, use `operation=Hash` and `newKey=false`, then send
only `HashMessage` words. Hash sends no key, nonce, or associated data, and it does not require a
loaded key.

This fragment shows the call order. It is an API outline, not a complete module. Issue each
`put` from separate sender steps when its method is ready. A separate consumer must call
`outputWord.get` while the sender is still feeding input; the core has bounded output buffering.

```bsv
import GetPut::*;
import Ascon::*;
import AsconTypes::*;

AsconIfc ascon <- mkAscon;
ascon.start(AsconCommand { operation: Encrypt, newKey: True });
// Send four key words through key.put, one per ready cycle; this is the first.
ascon.key.put(32'h03020100);
// Send four full Nonce words, last=False for the first three and True for the fourth.
ascon.inputWord.put(AsconInput {
    data: 32'h03020100, keep: 4'hf, last: False, segment: Nonce
});
ascon.inputWord.put(AsconInput {
    data: 32'h07060504, keep: 4'hf, last: False, segment: Nonce
});
ascon.inputWord.put(AsconInput {
    data: 32'h0b0a0908, keep: 4'hf, last: False, segment: Nonce
});
ascon.inputWord.put(AsconInput {
    data: 32'h0f0e0d0c, keep: 4'hf, last: True, segment: Nonce
});
// Send any AssociatedData words before Text. For final Text bytes 0x11, 0x22, 0x33:
ascon.inputWord.put(AsconInput {
    data: 32'h00332211, keep: 4'b0111, last: True, segment: Text
});
// In a concurrent consumer step, read while the sender is still active:
let output <- ascon.outputWord.get;
```

The fragment omits the other three key words and optional associated-data calls. Each `put` is
made from a separate sender step. Use `Get` when `outputWord` is ready while the sender is active.
The core has bounded output buffering; if it fills, input processing stops.

Each input uses `Put AsconInput` with a 32-bit data word, a byte `keep` mask, a `last` flag, and a
segment type: `Nonce`, `AssociatedData`, `Text`, or `HashMessage`. Bytes arrive low byte first.
`keep` marks contiguous valid bytes from the low end of the word. Every nonfinal word of a
nonempty segment uses a full `keep` mask (`1111`). A word `0x33221100` with `keep=0011` sends
its low bytes `00 11`. A one-byte final word uses `keep=0001`.

The output uses `Get AsconOutput` with data, `keep`, `last`, `endOfOperation`, and a kind:
`TextOutput`, `TagOutput`, or `DigestOutput`. AEAD output contains text, then the tag. Hash output
contains a digest. The final tag or digest word has both `last` and `endOfOperation` set. The `idle`
output becomes true after the final output is consumed. An AEAD command with `newKey=false` requires
a previously loaded key. Hash ignores `newKey` and works without a loaded key.

The default build selects Ascon-128 AEAD. Define `ASCON128A` to select Ascon-128a AEAD. Both
builds use Ascon-Hash v1.2 with rate 8. `UNROLL_FACTOR` defaults to 1. It must divide all round
counts used by the selected build: 12 and 6 for Ascon-128, or 12 and 8 for Ascon-128a. The raw
interface emits plaintext and a computed tag during decryption. The caller must compare the full
tag before accepting the plaintext. This example
implements Ascon v1.2. It does not claim compliance with a newer NIST standard.

## Compile and simulate

Install bsc 2026.07.1 or newer. Run from any directory with Xeda installed. Replace the path
below with the location of this repository:

```bash
xeda run bsc /path/to/xeda/examples/bluespec/ascon/ascon.yaml
xeda run bsc_sim /path/to/xeda/examples/bluespec/ascon/ascon.yaml
xeda run bsc_sim /path/to/xeda/examples/bluespec/ascon/ascon.yaml -s simulator=verilator
xeda run bsc_sim /path/to/xeda/examples/bluespec/ascon/ascon.yaml -s simulator=iverilog
python /path/to/xeda/examples/bluespec/ascon/run.py sim --simulator=verilator
```

The default build selects Ascon-128 AEAD. To select Ascon-128a with the helper, add
`--ascon128a` after the `sim`, `fpga`, or `asic` command. For direct Xeda commands, use
`--design-overrides rtl.defines.ASCON128A=true`. To change the unroll factor, add
`--unroll-factor 2` after the helper command; the helper checks the selected AEAD round counts.

## Synthesize

`run.py` compiles the BSV with Xeda's `Bsc` flow, takes the complete `artifacts.verilog` file set,
and passes it to Xeda's regular synthesis or place-and-route flow. This makes the handoff visible
and keeps generated Verilog inside the chosen run root.

```bash
python /path/to/xeda/examples/bluespec/ascon/run.py fpga \
  --run-root ./xeda_run --part xc7a100tftg256-2L --clock-period 10ns
python /path/to/xeda/examples/bluespec/ascon/run.py asic \
  --run-root ./xeda_run --platform nangate45 --pdk-root ./pdks --clock-period 10ns
```

The FPGA command needs Yosys with the Xilinx synthesis pass. ASIC synthesis and place and route
both need the platform's PDK files because Yosys uses its Liberty cell library. Install Nangate45
in a local store with `xeda pdk install nangate45 --root ./pdks --yes`. The command passes that
store with `--pdk-root ./pdks`. Add `--place-route` to run OpenROAD after synthesis; this uses a
10 ns clock by default. Set `--clock-period` to change it. `run.py fpga --help` and
`run.py asic --help` list the options. Every flow writes under `--run-root`, which defaults to
`./xeda_run` from the current directory.

## Generated Verilog interface

Xeda's `Bsc` flow includes `bsv_defines.v` in `artifacts.verilog`; this file defines
`BSV_POSITIVE_RESET`. Keep it in the Verilog file set. The generated `mkAscon` top has `CLK` and
`RST_N` ports. Despite its name, `RST_N` is an active-high synchronous reset in this build.

Bluespec maps the `Put` and `Get` methods to ready/enable signals:

- `start_command` is 3 bits. Assert `EN_start` when the command is valid and `RDY_start` is high.
- `key_put` is 32 bits. Assert `EN_key_put` when key data is valid and `RDY_key_put` is high.
- `inputWord_put` is 39 bits. Assert `EN_inputWord_put` when input data is valid and
  `RDY_inputWord_put` is high.
- `outputWord_get` is 40 bits. `RDY_outputWord_get` marks valid output; assert
  `EN_outputWord_get` when the consumer is ready.
- `idle` is high after the final output word is consumed.

The output side must keep draining while the input side sends a message. If the output FIFO fills,
the core stops accepting input until a consumer reads an output word.

## License and source

The source is adapted from BlueLight's Ascon BSV implementation. See [NOTICE.md](NOTICE.md) for the
source revision and [LICENSE.md](LICENSE.md) for its Apache License 2.0 terms.

To reproduce the committed vectors, run the generator against a BlueLight checkout at the revision
in `NOTICE.md`:

```bash
python tb/generate_vectors.py /path/to/bluelight --check
```

See [VALIDATION.md](VALIDATION.md) for the tools and configurations used to validate this port.
