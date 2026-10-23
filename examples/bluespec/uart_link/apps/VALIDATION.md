# UART streaming validation

This report records focused protocol, area, host simulation, FPGA flow, repository test, and physical board results. The full repository suite completed with seven failures; later focused reruns resolved five of those outcomes. Two unrelated Yosys/GHDL plugin tests remain unresolved. All four boards were connected for final SRAM programming and hardware self-tests.

## Scope and source identity

The stream word is 40 bits: 32 payload bits and 8 metadata bits. Metadata has a 3-bit type, 1-bit last flag, and 4-bit keep mask. There is no message counter. The 500 logic LUT gate applies to the complete LeanLink plus StreamExchange; it excludes application cores, UART PHY, and board clock generation.

The final transport hashes are LeanLink `de33e8162816107bab065c0fe712ef15b26168bd0728ffafdbb1820e30f7d0f3` and StreamExchange `c8b12623bc3ce00065f173b1bc556061c854f296bd15613a256d8e5864586f04`. The full source hash table below covers the 9 Artix/ULX3S images and 3 STLV images. The STLV wrapper, MMCM source, pin constraints, and generated-clock XDC are included. The per-run manifests listed at the end retain all source hashes and flow identities. Strict transport bench hashes are `TbStreamExchange.bsv` `5652ebe0a2449f63565d14c4733940f5aeac18f3b7718a52c92dde4660edded6` and `tb/stream_exchange.yaml` `d7540dcea2e542362923308ca1ad4793c120c7e509b10d0858a41cdab48d16e5`.

| Source path | Raw SHA-256 |
|---|---|
| `examples/bluespec/ascon/rtl/Ascon.bsv` | `b1b0f6007915eedea689d778d667038f140968fa839e51b71f8bd30328753d19` |
| `examples/bluespec/ascon/rtl/AsconCipher.bsv` | `3b5e94c4dd44140c725188b1709e94a58221a01970ed935a9e8cac64206738dd` |
| `examples/bluespec/ascon/rtl/AsconRound.bsv` | `6c8b1d7909a534c21f44050d5a2c49d1159413d2b55683ff9c2e410fe839ef2e` |
| `examples/bluespec/ascon/rtl/AsconStream.bsv` | `58e14bb281b513c17e2940a5802954879668da3f29a108aa0aa88f5aefb4ec5f` |
| `examples/bluespec/ascon/rtl/AsconTypes.bsv` | `cae3662665cd2ae8ef6f6822ebe20e742d41aa9f18ac236b0214810c18f0034e` |
| `examples/bluespec/gcd/rtl/GcdEngine.bsv` | `5bf10bd94db756a3af7c67de4376e8728fe1fb53d6d118d94c67c0191c33a114` |
| `examples/bluespec/gcd/rtl/GcdStream.bsv` | `e49223f5979c3794041dbdbb0ffb18b22fdf226fb4fbeb661c609840397e8a60` |
| `examples/bluespec/gcd/rtl/GcdUnit.bsv` | `76202c1d28fbc4675e46ebd8bca95be3477a25a03298ba776052c1948294ffac` |
| `examples/bluespec/uart/rtl/BitTimer.bsv` | `98bd22e5420b0d98bcbdc1eba744f0faf8b077565f1eb9b466029d848cbae041` |
| `examples/bluespec/uart/rtl/RingFifo.bsv` | `b88db4b3e1ad1e2818ebb25c7b7f385ac03e3455bdc8885c133cb6eea928236e` |
| `examples/bluespec/uart/rtl/Uart.bsv` | `e1029ab1700cbae2b96f305e641005b339bb0a55770b30ab848dcd2278f5aa22` |
| `examples/bluespec/uart/rtl/UartRx.bsv` | `598129a37abeee28a16bc2e472e3e091a13883da9a6cd11c650f4aa2f7bb31b6` |
| `examples/bluespec/uart/rtl/UartTx.bsv` | `f0ce22b4be4025027a37a5332214d6ac46e1acca9a447360a402092d3736bd04` |
| `examples/bluespec/uart/rtl/UartTypes.bsv` | `87f77697296f0ed1f5b82e947cc697040bb157e81c8c551513fef0a6a818a1c2` |
| `examples/bluespec/uart_echo/boards/arty_a7_100t.xdc` | `9f325a89d74d4d5cebc06be1d5e50973e9a891f198af46146aff1e31c60ccc7d` |
| `examples/bluespec/uart_echo/boards/arty_a7_100t_sync.xdc` | `731e064c431b57133492ce0493998cefb0f0a2b12a028596c23f4257e760c5a1` |
| `examples/bluespec/uart_echo/boards/ulx3s.lpf` | `7cd014b3cb9bd20cf6515d77b602e7a52bbbb76a8e24b4f3588a7eb099f0ba3d` |
| `examples/bluespec/uart_link/apps/ascon.yaml` | `89d7e5f350d1141f3a9bafe40bafc9a1e8e93ce4a030a750b0fc17ed06bf40d8` |
| `examples/bluespec/uart_link/apps/ascon128a.yaml` | `37b747c4a81bc89748fe584a9d9e0c3dc7af848443282ecbd7f539d87ba49486` |
| `examples/bluespec/uart_link/apps/gcd.yaml` | `0e1bd1fe020544799a4ee73b5085810bde12d1ea73b85c7719c5d700dfd8accc` |
| `examples/bluespec/uart_link/boards/StlvClock50.v` | `7ee7169436f0040e225394de2c8fb1c2af22a34775775774f061bd57b666ec67` |
| `examples/bluespec/uart_link/boards/StlvLeanLink50.bsv` | `dd7efee6372e89fea6796fe8fb5f661b78cefa765d3c8985f9300135df42edcf` |
| `examples/bluespec/uart_link/boards/basys3.xdc` | `cc188f99d077277f7f2ac7f63fbf303e24a48cebcd47218c6bc6a01b94875e54` |
| `examples/bluespec/uart_link/boards/stlv50.xdc` | `84416bb58ff5b698a33ca068d06bfa18756ca4ffdbef242dbc612bffb812be30` |
| `examples/bluespec/uart_link/boards/stlv7325_v2.xdc` | `fb2f8ffd0492b1af1a06e9a34f798d00b5b6f116fc40c7f808c3b64f1ecab30e` |
| `examples/bluespec/uart_link/rtl/LeanLink.bsv` | `de33e8162816107bab065c0fe712ef15b26168bd0728ffafdbb1820e30f7d0f3` |
| `examples/bluespec/uart_link/rtl/LeanLinkBoard.bsv` | `62f0fde99e1d9f0a5a1223c28db8bf28837c8c5dbac07da6c668eb154d651386` |
| `examples/bluespec/uart_link/rtl/LeanLinkTestTop.bsv` | `7a4684587c91feed1f9728c047fb4cf6bf1e73884ff8d159f29e10cfd25c5d2b` |
| `examples/bluespec/uart_link/rtl/StreamExchange.bsv` | `c8b12623bc3ce00065f173b1bc556061c854f296bd15613a256d8e5864586f04` |
| `examples/bluespec/uart_link/apps/gcd.yaml` (current STLV config) | `736c1568eb30fe910b69f791578e02d928e6331b2f4db58c55dd03ede6178522` |
| `examples/bluespec/uart_link/apps/ascon.yaml` (current STLV config) | `20b5197bcfec9a2cb0e465b930835b29950de1a29ccc805ca665817331444a31` |

The final STLV manifests used the two current config hashes shown above. During PR preparation, I removed an extra trailing blank line from each checked-in application YAML file. Their normalized current SHA-256 values are Ascon `396da341108579e790a6b67e88a749695e930db53725a00abf451a979a188afa` and GCD `81ad770d5e51bdd95ccec813bbe8ea519109e84c060ae04d2bd2f791d2a21ea2`. The YAML content is otherwise unchanged. The measured hardware results map to the original manifest hashes; the checked-in RTL and host source hashes still match the measured source hashes above.

## Focused behavior and application simulations

Strict public-pin regressions passed for Ascon40 W8/P256 and W1/P64 (10/10 cases each), and GCD64/32 W8/P256 and W1/P64 (1/1 case each). They observe `coreReset`, data, and handshake pins. Coverage includes status 0 with accepted-prefix reporting under backpressure, host suffix retry, maximum legal packets, empty and malformed requests, a stalled input followed by polling, ordered multiword requests, response rewind without reexecution, CRC recovery, and OPEN while an older response is queued. The Ascon W8 case reaches 48 complete 40-bit words; W1/P64 reaches its maximum legal packet. Generic transport widths 1 through 7, 9, and 13 passed.

The GCD public-pin client polls until all results arrive. A first response may accept a prefix and require a later poll; this is expected backpressure behavior. The byte and UART host simulations passed for GCD, Ascon-128, and Ascon-128a. Ascon-128 and Ascon-128a passed the 61 independent reference vectors and long-stream host cases. These are simulator results, not physical UART tests.

## Logic area

The area matrix uses canonical Yosys 0.70+post and complete observable tops with reset, data, and handshake ports. Logic LUTs and distributed-RAM LUT equivalents are separate.

| Complete top | Width / packet | Logic LUT | RAM LUT | Total LUT | FF | CARRY4 | RAMB18 | Ports / bits |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GCD64/32 | W8/P256 | 498 | 8 | 506 | 679 | 42 | 2 | 15 / 123 |
| GCD64/32 | W1/P64 | 451 | 24 | 475 | 679 | 31 | 0 | 15 / 123 |
| Ascon40 | W8/P256 | 495 | 8 | 503 | 649 | 44 | 2 | 15 / 107 |
| Ascon40 | W1/P64 | 449 | 24 | 473 | 649 | 33 | 0 | 15 / 107 |
| Bare LeanLink | W8/P256 | 384 | 8 | 392 | 460 | 38 | 2 | 20 / 66 |
| Bare LeanLink | W1/P64 | 335 | 24 | 359 | 462 | 28 | 0 | 20 / 66 |

All four complete application profiles meet the 500 logic LUT limit. Peak complete-top logic use is GCD64/32 W8/P256 at 498 LUT; Ascon40 W8/P256 uses 495 LUT.

## Board build timing, resources, and bitstream identity

All 12 final builds completed synthesis and place-and-route and produced a bitstream. The Artix-7 images used Vivado 2025.2. The area gate used Yosys 0.70+post. The STLV7325 flows used BSC 2026.07.1, the openXC7 Yosys 0.69 build, and nextpnr-himbaechel `1.0.0-75-g26f5e17a`; fpga-as completed bitstream packing.

| Application | Board | Clock | Fmax | WNS | Bitstream SHA-256 |
|---|---|---:|---:|---:|---|
| gcd | arty | 100 MHz | 135.520 MHz | +2.621 ns | `590c0d25a7308db13748f0c19ab3286348ea773a247d1fbf6c832d2a99601e33` |
| gcd | basys3 | 100 MHz | 132.556 MHz | +2.456 ns | `77b66b3340d1889443f9dd1fba1c36636b5f6834a2c10d3723ca142b122e3a58` |
| gcd | ulx3s | 25 MHz | 78.815 MHz | +27.312 ns | `092522795b781212b295259c9371a13b695d32539395dae35888562d4653688a` |
| gcd | stlv7325 | 50 MHz core (200 MHz input) | 179.662 MHz | +14.434 ns | `09d4c80a68c599f411ae45fc338e32425959420957c4413dd4b5565d5290bfbe` |
| ascon128 | arty | 100 MHz | 109.842 MHz | +0.896 ns | `86ced51ce8c31aa0e863f17bda0b2cbd80e1887ed43c2d51be9b30005d432100` |
| ascon128 | basys3 | 100 MHz | 106.826 MHz | +0.639 ns | `daa10b55a2a2a6cd032a48e91c56a6ec530a558a76e3ffee3a23eeedf3203e22` |
| ascon128 | ulx3s | 25 MHz | 71.053 MHz | +25.926 ns | `72c8d3f356b4617c0676df8bcf8e0424627c9566055c2fff33c61339ed21489e` |
| ascon128 | stlv7325 | 50 MHz core (200 MHz input) | 156.814 MHz | +13.623 ns | `b95db10734d64aabaa662b0c2fb783c590048113ba187dbbcaf35d6df10d1ddf` |
| ascon128a | arty | 100 MHz | 113.392 MHz | +1.181 ns | `7a2df26392269b2ba9ac847abbf65ce57954ae3b64cbd0c676f82b1f37131bee` |
| ascon128a | basys3 | 100 MHz | 109.902 MHz | +0.901 ns | `ac22f7e6b3d46ef48d286d13312ed21388ae826db40895f912a08c9121c3eb71` |
| ascon128a | ulx3s | 25 MHz | 53.769 MHz | +21.402 ns | `17405159d5ce225e017c4cbd2f5bd00a281c59c1a10388f4315fd1e5ec0d3f8a` |
| ascon128a | stlv7325 | 50 MHz core (200 MHz input) | 134.264 MHz | +12.552 ns | `99cab5ba2d65eefe4e2a9c9b13e26db3067dc5b326d58a7c8d0df4b19d637ccc` |

Flow resource metrics. The STLV mapped LUT estimates and nextpnr occupied-LUT counts are separate metrics. Do not compare them as if they came from one counter.

| Application | Board | Flow resource report |
|---|---|---|
| gcd | arty | LUT logic/memory 1129/64; FF 1010; RAMB18 2 |
| gcd | basys3 | LUT logic/memory 1129/64; FF 1010; RAMB18 2 |
| gcd | ulx3s | TRELLIS_COMB 1882; FF 1090; DP16KD 2 |
| ascon128 | arty | LUT logic/memory 2578/132; FF 1897; RAMB18 2 |
| ascon128 | basys3 | LUT logic/memory 2584/132; FF 1897; RAMB18 2 |
| ascon128 | ulx3s | TRELLIS_COMB 4558; FF 1991; DP16KD 2 |
| ascon128a | arty | LUT logic/memory 2642/132; FF 1895; RAMB18 2 |
| ascon128a | basys3 | LUT logic/memory 2644/132; FF 1895; RAMB18 2 |
| ascon128a | ulx3s | TRELLIS_COMB 5020; FF 1989; DP16KD 2 |
| GCD | STLV7325 | mapped LUT logic/RAM 1152/60; placed LUT 1815; FF 1034; CARRY4 93; RAMB18 2; MMCM 1; BUFGCTRL 2 |
| Ascon128 | STLV7325 | mapped LUT logic/RAM 3139/128; placed LUT 3751; FF 1935; CARRY4 60; RAMB18 2; MMCM 1; BUFGCTRL 2 |
| Ascon128a | STLV7325 | mapped LUT logic/RAM 3243/128; placed LUT 3871; FF 1933; CARRY4 64; RAMB18 2; MMCM 1; BUFGCTRL 2 |

The STLV7325 physical clock is a 200 MHz differential input, constrained to 5 ns. `StlvClock50.v` configures the MMCM for 800 MHz VCO and 50 MHz output. The generated `board.CLK` is constrained to 20 ns. Each STLV run reports the 200 MHz input, 800 MHz VCO, and 50 MHz MMCM output; nextpnr reports the synchronous core path on `board.CLK`. All three STLV routes meet 50 MHz with positive slack.

Nextpnr emits a dedicated-clock-routing warning for `clocks.locked`. The mapped netlist shows `MMCME2_ADV.LOCKED` drives only `BUFGCE.CE`; `CLKOUT0` drives `BUFGCE.I`, and the BUFGCE output drives `board.CLK`. `Arch::routeClock` classifies the MMCM net as a global clock from its sole BUFGCTRL user without checking whether the user pin is `CE`. The dedicated-resource pass skips that CE arc; the normal router completes the route. Placement, timing, and bitstream packing succeed. The warning does not describe the generated `board.CLK` path.

Vivado could not provide an independent STLV cross-check because the available v2025.2 device database does not include `xc7k325t`; Vivado returned Device 21-436 for that exact part. No substitute part was used. The STLV target is validated with the openXC7 flow above. The older Arty 48 MHz baseline is not evidence for the final application images.

## Reproduction and board checks

The final application images were programmed to SRAM only. No flash was written. To build and program from the checked-in application designs, use Xeda's `openfpgaloader` flow with the matching target. PR #236 is present in this worktree history. The flow compares FPGA JTAG IDs with each target's part and selects the matching FTDI probe when multiple probes share the cable type.

```bash
xeda run openfpgaloader examples/bluespec/uart_link/apps/gcd.yaml --target arty
xeda run openfpgaloader examples/bluespec/uart_link/apps/gcd.yaml --target basys3
xeda run openfpgaloader examples/bluespec/uart_link/apps/gcd.yaml --target ulx3s
xeda run openfpgaloader examples/bluespec/uart_link/apps/gcd.yaml --target stlv7325
xeda run openfpgaloader examples/bluespec/uart_link/apps/ascon.yaml --target arty
xeda run openfpgaloader examples/bluespec/uart_link/apps/ascon.yaml --target basys3
xeda run openfpgaloader examples/bluespec/uart_link/apps/ascon.yaml --target ulx3s
xeda run openfpgaloader examples/bluespec/uart_link/apps/ascon.yaml --target stlv7325
```

Ascon-128a uses the Ascon design with the `ASCON128A=true` compile define. For example, add `-s rtl.defines.ASCON128A=true` to the Ascon command. The openFPGALoader flow defaults to SRAM (`write_flash: false`). The STLV target uses the openXC7 chain and its 50 MHz core clock generated from the physical 200 MHz input.

If you use the raw loader as a fallback, name the probe explicitly when multiple boards of one cable type are connected. The board-specific JTAG IDs, serial numbers, and UART device paths are retained in the local hardware records. For example, explicit raw SRAM programming uses `openFPGALoader -b arty_a7_100t --usb-serial-num SERIAL --write-sram IMAGE.bit`. Set `SERIAL` to the connected board's JTAG serial. Do not omit the serial when both Artix probes are connected.

The public host self-tests use an explicit UART port because the FT2232 boards expose two serial interfaces with the same USB serial number. Set each port variable to the UART interface for the connected board. The exact device identifiers used during this validation remain in the local hardware records. Each invocation opens a new session and sends RESET. The self-tests run 1,004 GCD cases, or 61 Ascon reference vectors plus long AEAD/hash, key reuse, and bad-tag checks:

```bash
python3 examples/bluespec/uart_link/host/app_suite.py --port "$ARTY_PORT" --board arty selftest gcd
python3 examples/bluespec/uart_link/host/app_suite.py --port "$BASYS3_PORT" --board basys3 selftest ascon --variant 128
python3 examples/bluespec/uart_link/host/app_suite.py --port "$ULX3S_PORT" --board ulx3s selftest gcd
python3 examples/bluespec/uart_link/host/app_suite.py --port "$STLV7325_PORT" --board stlv7325 selftest ascon --variant 128a
```

All 12 image/board combinations passed these public hardware checks. The 4 GCD images each returned the exact expected results for all 1,004 pairs. All 8 Ascon images passed the 61 independent vectors, long-message encryption and decryption, long hash, key reuse after hash, and incorrect-tag rejection. The loaded image hashes match the 12 bitstream hashes in the table above. Every operation used SRAM programming; power cycle or reconfiguration removes the image. During the separate response-loss recovery pass below, the GCD image was loaded on all four boards. Arty was restored to the Ascon-128 image for throughput measurement and held that image at the end of validation; Basys-3, ULX3S, and STLV7325 held their GCD images.

The Xeda flow was also exercised with typed prebuilt bitstreams from the final manifest on all four boards. For Arty and Basys-3, both Digilent JTAG probes were connected at once. Xeda read both ID codes and selected the probe that matched each target's FPGA. The ULX3S FT231X cable and STLV FT4232 cable each had one matching probe, so Xeda used their board-specific cable without a serial override. Each Xeda load was followed by a successful 1,004-case GCD self-test. The tested Xeda version includes the probe selector from PR #236.

| Application | Board | Self-test result | Image SHA-256 |
|---|---|---|---|
| GCD | Arty | 1,004 cases passed | `590c0d25a7308db13748f0c19ab3286348ea773a247d1fbf6c832d2a99601e33` |
| GCD | Basys-3 | 1,004 cases passed | `77b66b3340d1889443f9dd1fba1c36636b5f6834a2c10d3723ca142b122e3a58` |
| GCD | ULX3S | 1,004 cases passed | `092522795b781212b295259c9371a13b695d32539395dae35888562d4653688a` |
| GCD | STLV7325 | 1,004 cases passed | `09d4c80a68c599f411ae45fc338e32425959420957c4413dd4b5565d5290bfbe` |
| Ascon-128 | Arty | 61 vectors and long checks passed | `86ced51ce8c31aa0e863f17bda0b2cbd80e1887ed43c2d51be9b30005d432100` |
| Ascon-128 | Basys-3 | 61 vectors and long checks passed | `daa10b55a2a2a6cd032a48e91c56a6ec530a558a76e3ffee3a23eeedf3203e22` |
| Ascon-128 | ULX3S | 61 vectors and long checks passed | `72c8d3f356b4617c0676df8bcf8e0424627c9566055c2fff33c61339ed21489e` |
| Ascon-128 | STLV7325 | 61 vectors and long checks passed | `b95db10734d64aabaa662b0c2fb783c590048113ba187dbbcaf35d6df10d1ddf` |
| Ascon-128a | Arty | 61 vectors and long checks passed | `7a2df26392269b2ba9ac847abbf65ce57954ae3b64cbd0c676f82b1f37131bee` |
| Ascon-128a | Basys-3 | 61 vectors and long checks passed | `ac22f7e6b3d46ef48d286d13312ed21388ae826db40895f912a08c9121c3eb71` |
| Ascon-128a | ULX3S | 61 vectors and long checks passed | `17405159d5ce225e017c4cbd2f5bd00a281c59c1a10388f4315fd1e5ec0d3f8a` |
| Ascon-128a | STLV7325 | 61 vectors and long checks passed | `99cab5ba2d65eefe4e2a9c9b13e26db3067dc5b326d58a7c8d0df4b19d637ccc` |

### Large-batch throughput

These are measured application transfer times on the Arty at 12 Mbaud, with the manifest-matched GCD and Ascon-128 images. Each backend used a fresh session and RESET; UART counters were cleared after RESET before each timed operation. The FTDI backend used channel B with a 1 ms latency timer and event character 0. The application goodput is application input bytes divided by operation time. UART byte counts are the actual bytes written and read by the backend during the timed operation. The 1.2 MB/s figure is the theoretical one-direction 8N1 byte rate at 12 Mbaud; it is not measured payload throughput.

The 10,000 GCD result values were each compared with Python `math.gcd`; both backends produced the same result SHA-256. For the 64 KiB hash, the digest SHA-256 matched between backends. This large digest was not checked against a separate software oracle; the independent Ascon vector and long-hash checks are covered by the 61-vector self-tests. The large AEAD ciphertext and tag SHA-256 matched between backends. Each backend then decrypted that output on the FPGA, reusing the loaded key, and matched all 65,536 plaintext bytes exactly. The independent vectors and long reference cases are covered separately by the self-tests.

| Operation | Backend | Payload input | Time | Application input goodput | UART TX / RX bytes | Result check |
|---|---|---:|---:|---:|---:|---|
| GCD, 10,000 pairs | Serial | 80,000 B | 1.167 s | 68.5 kB/s | 505,160 / 68,000 | Output SHA matches FTDI |
| GCD, 10,000 pairs | FTDI channel B | 80,000 B | 0.980 s | 81.7 kB/s | 505,160 / 68,000 | Output SHA matches Serial |
| Ascon hash | Serial | 65,536 B | 0.186 s | 352 kB/s | 86,384 / 4,842 | Digest SHA matches FTDI |
| Ascon hash | FTDI channel B | 65,536 B | 0.164 s | 401 kB/s | 86,384 / 4,842 | Digest SHA matches Serial |
| Ascon AEAD encrypt | Serial | 69,632 B | 0.534 s | 130 kB/s | 193,578 / 92,664 | Ciphertext SHA matches FTDI; large decrypt matched input |
| Ascon AEAD encrypt | FTDI channel B | 69,632 B | 0.483 s | 144 kB/s | 193,578 / 92,664 | Ciphertext SHA matches Serial; large decrypt matched input |

### Repeat-session and response-loss recovery

For this separate recovery pass, I loaded the final GCD image on each board. I ran two host processes per board. Each process opened a new UART and LeanLink session, sent application RESET, and passed the 1,004-case GCD self-test.

I then used the existing `FaultBackend` from `host/hw_suite.py` on each board. After a successful OPEN and RESET, the wrapper dropped all FPGA-to-host response bytes for five seconds and passed host-to-FPGA bytes unchanged. Each GCD request had been sent and ended as `UNKNOWN_OUTCOME` (`was_sent=true`) after protocol retries expired. I closed the lost session, opened a new one, issued RESET, and verified a new known GCD result. This checks recovery after an uncertain result. The application does not expose an execution counter, so this test does not claim an execution count for the request whose response was lost.

Per-image JTAG, programming, self-test, repeated-session, recovery, and Xeda flow logs and manifests were retained in the local validation evidence set. The evidence includes nine Artix/ULX3S images, three STLV images, strict pin tests, and six-top area runs. These local records are not part of this repository.

## Repository suite and remaining checks

The final full repository suite completed with **22,732 passed, 405 skipped, 7 failed, and 15 warnings** in 1,298.90 seconds. After that run, a focused rerun passed 158 tests, including all six area tests, the changelog test, and two make-flag tests. Four owned failures were fixed: one changelog failure and three W1 area-test RAM mapping assumptions. The FST test failure came from missing Homebrew `CPATH` and `LIBRARY_PATH`; after setting those paths, its targeted rerun passed.

Two Yosys/GHDL tests remain unresolved. The installed plugin crashes at `netlists.adb:978`. Direct reproduction on unchanged upstream HEAD produced the same crash with both the Homebrew Yosys 0.70+post/GHDL 7 development toolchain and the OSS toolchain. Standalone GHDL synthesis works. No coherent installed plugin was found, and the UART implementation does not depend on these plugin tests. The full suite was not rerun after the focused fixes, so its result remains the seven-failure result above. Quality checks and catalog validation completed with exit code 0.

The raw full-suite log, status record, and preflight record were retained with the local validation evidence. They are not part of this repository.

The host test environment used `pyserial==3.5`, `pyftdi==0.57.2`, and `pyusb==1.3.1`. The tool help and board listing were checked without opening a USB device; that check confirms software readiness only.

Physical SRAM self-tests and serial-versus-FTDI throughput measurements are complete for the final 12 images. No nonvolatile flash was written. The two unresolved repository-suite failures are the Yosys/GHDL plugin tests described above.
