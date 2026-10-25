# UART streaming validation

This report records protocol, area, host simulation, FPGA flow, repository test, and physical board results from the revisions named below. The recorded full-suite run passed 23,625 tests and skipped 420. Two tests failed in the installed GHDL/Yosys plugin. A later padding fix passed focused host and application tests and passed an Arty Ascon-128 hardware self-test. The full repository suite was not rerun after that host-only fix. These measurements are historical; they are not fresh builds of the current worktree. All FPGA programming used SRAM.

## Scope and source identity

The stream word is 40 bits: 32 payload bits and 8 metadata bits. Metadata has a 3-bit type, 1-bit last flag, and 4-bit keep mask. There is no message counter. The 500 logic LUT gate applies to the complete LeanLink plus StreamExchange; it excludes application cores, UART PHY, and board clock generation.

The measured transport hashes are LeanLink `de33e8162816107bab065c0fe712ef15b26168bd0728ffafdbb1820e30f7d0f3` and StreamExchange `c8b12623bc3ce00065f173b1bc556061c854f296bd15613a256d8e5864586f04`. The source hash table below covers the recorded 9 Artix/ULX3S images and 3 STLV images. It lists the files used by those bitstreams, including the STLV wrapper, MMCM source, pin constraints, and generated-clock constraint file. The per-run manifests retain the build-time source hashes and flow identities. Strict transport bench hashes are `TbStreamExchange.bsv` `5652ebe0a2449f63565d14c4733940f5aeac18f3b7718a52c92dde4660edded6` and `tb/stream_exchange.yaml` `d7540dcea2e542362923308ca1ad4793c120c7e509b10d0858a41cdab48d16e5`.

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
| `examples/bluespec/uart_link/apps/ascon128a.yaml` (historical STLV build-manifest config) | `37b747c4a81bc89748fe584a9d9e0c3dc7af848443282ecbd7f539d87ba49486` |
| `examples/bluespec/uart_link/apps/gcd.yaml` | `0e1bd1fe020544799a4ee73b5085810bde12d1ea73b85c7719c5d700dfd8accc` |
| `examples/bluespec/uart_link/boards/StlvClock50.v` | `7ee7169436f0040e225394de2c8fb1c2af22a34775775774f061bd57b666ec67` |
| `examples/bluespec/uart_link/boards/StlvLeanLink50.bsv` | `dd7efee6372e89fea6796fe8fb5f661b78cefa765d3c8985f9300135df42edcf` |
| `examples/bluespec/uart_link/boards/basys3.xdc` | `cc188f99d077277f7f2ac7f63fbf303e24a48cebcd47218c6bc6a01b94875e54` |
| `examples/bluespec/uart_link/boards/stlv50.xdc` (historical generated-clock build input) | `84416bb58ff5b698a33ca068d06bfa18756ca4ffdbef242dbc612bffb812be30` |
| `examples/bluespec/uart_link/boards/stlv7325_v2.xdc` (historical board-pin build input) | `fb2f8ffd0492b1af1a06e9a34f798d00b5b6f116fc40c7f808c3b64f1ecab30e` |
| `examples/bluespec/uart_link/rtl/LeanLink.bsv` | `de33e8162816107bab065c0fe712ef15b26168bd0728ffafdbb1820e30f7d0f3` |
| `examples/bluespec/uart_link/rtl/LeanLinkBoard.bsv` | `62f0fde99e1d9f0a5a1223c28db8bf28837c8c5dbac07da6c668eb154d651386` |
| `examples/bluespec/uart_link/rtl/LeanLinkTestTop.bsv` | `7a4684587c91feed1f9728c047fb4cf6bf1e73884ff8d159f29e10cfd25c5d2b` |
| `examples/bluespec/uart_link/rtl/StreamExchange.bsv` | `c8b12623bc3ce00065f173b1bc556061c854f296bd15613a256d8e5864586f04` |
| `examples/bluespec/uart_link/apps/gcd.yaml` (STLV build-manifest config at measurement time) | `736c1568eb30fe910b69f791578e02d928e6331b2f4db58c55dd03ede6178522` |
| `examples/bluespec/uart_link/apps/ascon.yaml` (STLV build-manifest config at measurement time) | `20b5197bcfec9a2cb0e465b930835b29950de1a29ccc805ca665817331444a31` |

The historical STLV bitstreams used the local pin XDC and generated-clock XDC named in the table. The current targets use the bundled STLV board pin fallback, whose XDC has the same hash as the former local file, and `boards/stlv50.sdc` for the generated 20 ns core clock. The SDC file retains the same create_clock command as the former XDC. At review base commit `285292577a9a9713e69bf8212a21ed03dd5d84ad`, the dependency-enabled YAML SHA-256 values were Ascon `787de50dd8dc739a6e4e83a96f604d642a989dd6cfb3d76dee22f2decc9319d5` and GCD `a5b1e3d2f4649f6161b0df5548707fc1efd7543a20940dec88f3b41904d4e8b0`. These base-snapshot hashes do not identify the files after this constraint-path update. At the review-base snapshot, a pure-Python comparison resolved 62 baseline variants. It found matching flattened RTL and testbench paths, types, order, tops, clocks, defines, parameters, and effective flow settings, with 42 source-content hash differences limited to three Python files. That comparison predates later stream-helper and STLV constraint-source changes; it does not describe the current flattened sources. The `xeda design-schema` schema accepts the existing URI, local-path, and Git dependency forms. The machine-readable comparison records the comparison-time variants and hashes.

## Focused behavior and application simulations

Strict public-pin regressions passed for Ascon40 W8/P256 and W1/P64 (10/10 cases each), and GCD64/32 W8/P256 and W1/P64 (1/1 case each). They observe `coreReset`, data, and handshake pins. Coverage includes status 0 with accepted-prefix reporting under backpressure, host suffix retry, maximum legal packets, empty and malformed requests, a stalled input followed by polling, ordered multiword requests, response rewind without reexecution, CRC recovery, and OPEN while an older response is queued. The Ascon W8 case reaches 48 complete 40-bit words; W1/P64 reaches its maximum legal packet. Generic transport widths 1 through 7, 9, and 13 passed.

The GCD public-pin client polls until all results arrive. A first response may accept a prefix and require a later poll; this is expected backpressure behavior. The final full-suite run passed all six application cosimulations. The independent Ascon vector and long-stream host checks also passed. These results are simulator tests, not physical UART tests.

## Direct reusable-library tests

The seven library YAML defaults have no active testbench. Each library has explicit direct test targets. All 19 target commands below passed and printed `PASS` in BSC Bluesim:

```bash
xeda run bsc_sim examples/bluespec/uart/uart-library.yaml --target sim
xeda run bsc_sim examples/bluespec/uart_link/leanlink-library.yaml --target test
xeda run bsc_sim examples/bluespec/uart_link/stream-exchange-library.yaml --target test
xeda run bsc_sim examples/bluespec/uart_link/stream-exchange-library.yaml --target gcd
xeda run bsc_sim examples/bluespec/uart_link/stream-exchange-library.yaml --target width1
xeda run bsc_sim examples/bluespec/uart_link/stream-exchange-library.yaml --target width2
xeda run bsc_sim examples/bluespec/uart_link/stream-exchange-library.yaml --target width3
xeda run bsc_sim examples/bluespec/uart_link/stream-exchange-library.yaml --target width4
xeda run bsc_sim examples/bluespec/uart_link/stream-exchange-library.yaml --target width5
xeda run bsc_sim examples/bluespec/uart_link/stream-exchange-library.yaml --target width6
xeda run bsc_sim examples/bluespec/uart_link/stream-exchange-library.yaml --target width7
xeda run bsc_sim examples/bluespec/uart_link/stream-exchange-library.yaml --target width9
xeda run bsc_sim examples/bluespec/uart_link/stream-exchange-library.yaml --target width13
xeda run bsc_sim examples/bluespec/gcd/gcd-library.yaml --target unit
xeda run bsc_sim examples/bluespec/gcd/gcd-stream-library.yaml --target stream
xeda run bsc_sim examples/bluespec/ascon/ascon-library.yaml --target core
xeda run bsc_sim examples/bluespec/ascon/ascon-stream-library.yaml --target stream_vectors
xeda run bsc_sim examples/bluespec/ascon/ascon-stream-library.yaml --target stream_validation
xeda run bsc_sim examples/bluespec/ascon/ascon-stream-library.yaml --target stream_backpressure
```

## Logic area

The recorded area matrix below ran in the full repository test suite with BSC 2026.07.1 and Homebrew Yosys 0.70+post on `xc7a100tcsg324-1` at 10 ns. The test checks complete observable tops with clock, reset, data, and handshake ports, including `coreReset` for the adapters. Logic LUTs and distributed-RAM LUT equivalents are separate. It checks all six profiles in `tests/test_uart_stream_area.py`.

| Complete top | Width / packet | Logic LUT | RAM LUT | Total LUT | FF | CARRY4 | RAMB18 | Ports / bits |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GCD64/32 | W8/P256 | 498 | 8 | 506 | 679 | 42 | 2 | 15 / 123 |
| GCD64/32 | W1/P64 | 451 | 24 | 475 | 679 | 31 | 0 | 15 / 123 |
| Ascon40 | W8/P256 | 495 | 8 | 503 | 649 | 44 | 2 | 15 / 107 |
| Ascon40 | W1/P64 | 449 | 24 | 473 | 649 | 33 | 0 | 15 / 107 |
| Bare LeanLink | W8/P256 | 384 | 8 | 392 | 460 | 38 | 2 | 20 / 66 |
| Bare LeanLink | W1/P64 | 335 | 24 | 359 | 462 | 28 | 0 | 20 / 66 |

All four transport and adapter profiles meet the 500 logic LUT limit. The highest complete-top logic count is GCD64/32 W8/P256 at 498 LUT. These counts use Yosys 0.70+post. Earlier measurements from the bundled Yosys 0.70+179 build were 489/443 for GCD W8/W1, 491/450 for Ascon40 W8/W1, and 384/340 for bare LeanLink W8/W1. They are historical and are not the final gate results.

## Board build timing, resources, and bitstream identity

The 12 recorded builds completed synthesis and place-and-route and produced bitstreams. The Artix-7 images used Vivado 2025.2. The area gate used Yosys 0.70+post. The STLV7325 flows used BSC 2026.07.1, the openXC7 Yosys 0.69 build, and nextpnr-himbaechel `1.0.0-75-g26f5e17a`; fpga-as completed bitstream packing.

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

The final application images were programmed to SRAM only. No flash was written. The Artix-7 images used Vivado for synthesis and openFPGALoader for programming; the reproduction commands select `vivado_synth+openfpgaloader`. ULX3S and STLV7325 use the configured open-source flow through `openfpgaloader`. PR #236 is present in this worktree history. The flow compares FPGA JTAG IDs with each target's part and selects the matching FTDI probe when multiple probes share the cable type. Earlier Xeda-only programming checks used prebuilt typed bitstreams; they do not replace these source-build commands.

```bash
xeda run vivado_synth+openfpgaloader examples/bluespec/uart_link/apps/gcd.yaml --target arty
xeda run vivado_synth+openfpgaloader examples/bluespec/uart_link/apps/gcd.yaml --target basys3
xeda run openfpgaloader examples/bluespec/uart_link/apps/gcd.yaml --target ulx3s
xeda run openfpgaloader examples/bluespec/uart_link/apps/gcd.yaml --target stlv7325
xeda run vivado_synth+openfpgaloader examples/bluespec/uart_link/apps/ascon.yaml --target arty
xeda run vivado_synth+openfpgaloader examples/bluespec/uart_link/apps/ascon.yaml --target basys3
xeda run openfpgaloader examples/bluespec/uart_link/apps/ascon.yaml --target ulx3s
xeda run openfpgaloader examples/bluespec/uart_link/apps/ascon.yaml --target stlv7325
```

Ascon-128a uses the Ascon design with the `ASCON128A=true` compile define. For example, add `--design-overrides rtl.defines.ASCON128A=true` to the Ascon command. The openFPGALoader flow defaults to SRAM (`write_flash: false`). The STLV target uses the openXC7 chain and its 50 MHz core clock generated from the physical 200 MHz input.

If you use the raw loader as a fallback, name the probe explicitly when multiple boards of one cable type are connected. The board-specific JTAG IDs, serial numbers, and UART device paths are retained in the local hardware records. For example, explicit raw SRAM programming uses `openFPGALoader -b arty_a7_100t --ftdi-serial SERIAL --write-sram IMAGE.bit`. Set `SERIAL` to the connected board's JTAG serial. Do not omit the serial when both Artix probes are connected.

The public host self-tests use an explicit UART port because the FT2232 boards expose two serial interfaces with the same USB serial number. Set each port variable to the UART interface for the connected board. The exact device identifiers used during this validation remain in the local hardware records. Each invocation opens a new session and sends RESET. The self-tests run 1,004 GCD cases, or 61 Ascon reference vectors plus long AEAD/hash, key reuse, and bad-tag checks:

```bash
python3 examples/bluespec/uart_link/host/app_suite.py --port "$ARTY_PORT" --board arty selftest gcd
python3 examples/bluespec/uart_link/host/app_suite.py --port "$BASYS3_PORT" --board basys3 selftest ascon --variant 128
python3 examples/bluespec/uart_link/host/app_suite.py --port "$ULX3S_PORT" --board ulx3s selftest gcd
python3 examples/bluespec/uart_link/host/app_suite.py --port "$STLV7325_PORT" --board stlv7325 selftest ascon --variant 128a
```

The 12 recorded image/board combinations passed these public hardware checks. The 4 GCD images each returned the exact expected results for all 1,004 pairs. All 8 Ascon images passed the 61 independent vectors, long-message encryption and decryption, long hash, key reuse after hash, and incorrect-tag rejection. The loaded image hashes match the 12 bitstream hashes in the table above. Every operation used SRAM programming; power cycle or reconfiguration removes the image. During the separate response-loss recovery pass below, the GCD image was loaded on all four boards. Arty was restored to the Ascon-128 image for throughput measurement and held that image at the end of validation; Basys-3, ULX3S, and STLV7325 held their GCD images.

The 12-image results above were collected before the bounded-input host-client change and the current application source updates. The RTL and programmed images did not change. After that host update, I ran the public self-test against the four then-loaded SRAM images without reprogramming: Arty Ascon-128 passed 61 vectors and long encrypt/decrypt, hash, key-reuse, and bad-tag checks; Basys-3, ULX3S, and STLV7325 each passed 1,004 GCD cases. This validates the host sources identified by the hashes below on four loaded image/board combinations. It does not claim a new host-client run across all 12 combinations. The tested host sources are `stream.py` SHA-256 `8d7d13f8c10e814251ff3a923c6fe09ea1cb3920365191ec5fcfe8e74d8edff0` and `applications.py` SHA-256 `816f8b14cff394f1820a808e119b42659c1ae2b15a48b280edfb0b05bddec5fc`.

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

These application transfer times were measured on the Arty at 12 Mbaud, before the bounded-input host-client update, using the manifest-matched GCD and Ascon-128 images. They are not throughput measurements of the final host client. Each backend used a fresh session and RESET; UART counters were cleared after RESET before each timed operation. The FTDI backend used channel B with a 1 ms latency timer and event character 0. The application goodput is application input bytes divided by operation time. UART byte counts are the actual bytes written and read by the backend during the timed operation. The 1.2 MB/s figure is the theoretical one-direction 8N1 byte rate at 12 Mbaud; it is not measured payload throughput. The final host client passed byte and UART simulations and was smoke-tested on the four previously loaded images; no new throughput benchmark was run after that host update.

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

## Repository tests and final host fix

The recorded full repository run used candidate commit `90e84dccafe81ea55008fe4eb05749d667a1d917` with four pytest workers. It completed in 1,384.31 seconds and reported **23,625 passed, 420 skipped, 2 failed, and 15 warnings**. All six area profiles and all six app byte and UART-pin cosimulations passed. The two failures were `tests/test_yosys.py::test_yosys_synth_py` and `tests/test_yosys.py::test_yosys_fpga_hands_ghdl_each_vhdl_file_once`. Both failed when the installed GHDL/Yosys plugin terminated with `libc++abi: terminating due to uncaught foreign exception` and `raised PROGRAM_ERROR : unhandled signal`. They do not involve the UART application. The full run used Python 3.15.0, Yosys 0.70+post, Verilator 5.052, BSC 2026.07.1, and GHDL 7.0.0-dev. All 1,086 tracked file hashes and the Git state matched before and after the run.

After that full run, a host decoder fix made the Ascon client check padding bytes after the `keep` payload bytes. New tests cover zero padding and nonzero bytes in every unused payload position for the affected keep masks. The focused host/client/bounded/protocol tests passed **178 tests**. The application test module passed **10 tests**, including all six public app cosimulations and four wrapper cases. These checks ran with the sanitized Homebrew tool environment. The final full repository suite was not rerun after this host-only change.

The source hashes recorded for that host validation were `applications.py` `a69de039b25c4d7c2b7f24c7d926bec7df8b3d19541114939b9888f1ae91dd1e`, `stream.py` `8d7d13f8c10e814251ff3a923c6fe09ea1cb3920365191ec5fcfe8e74d8edff0`, and `apps/stream_apps.py` `96f6162d3c91a4e90542083066f38fc76fd1199ed180827d91ba44c95c3eb5e3`. The padding regression file is `tests/test_uart_stream_clients.py` SHA-256 `d2e06106abe86ecf413b3b7fa43da58fc4707932ffce12fa8413a88491510179`.

The 19 direct reusable-library simulation commands passed. Focused schema, dependency, chain-documentation, changelog, and library tests passed 344 tests. The documentation test suite passed 377 tests. Final quality checks passed: mypy on 153 source files, Black on 487 files, the required Ruff check, catalog freshness, docs with warnings as errors, and `git diff --check`. Optional ORFS, PDK, openXC7, Vivado, and remote-host tests were skipped because their external tool or host settings were not enabled.

After the padding fix, I ran the public Ascon-128 self-test on the Arty without reprogramming. The previously loaded SRAM image hash and matching bitstream artifact hash were `86ced51ce8c31aa0e863f17bda0b2cbd80e1887ed43c2d51be9b30005d432100`. The test passed all 61 vectors, long encrypt/decrypt, long hash, key reuse, and bad-tag rejection. It used the final `applications.py` hash above. This is a current host check on one loaded image; the 12-image board matrix above uses the earlier host version.

The serial-versus-FTDI throughput measurements above predate the bounded-input host-client update and the padding fix. No new throughput benchmark ran after those host changes. No nonvolatile flash was written. The two GHDL/Yosys plugin tests remain unresolved in the final full-suite run.


## Reset and FIFO follow-up, October 25, 2026
The final integrated runtime is `StreamExchange.bsv` SHA-256
`e1af2a32fdc3b49443064d8b89859b96ecf8d6c1c6e4d661a2422bcd5e517099`.
Parent reset and software RESET initialize the application and both queues. Admission waits for
the actual application reset to end. Each native one-entry pipeline FIFO accepts a replacement
word during a dequeue, including when full. A cycle check verifies ten consecutive byte-word
admissions, and a separate test verifies output replacement under backpressure.

The byte-aligned adapter holds each output word and selects its bytes in little-endian order.
Its index covers only valid byte positions, including output widths 9, 25, and 57 that previously
failed elaboration. The buffered adapter uses 11-bit byte offsets so words wider than 256 bits
retain their upper bytes. The runnable tests cover output widths 1, 7, 9, 13, 20, 25, 57, and 64
under backpressure, plus a 257-bit buffered round trip, padding rejection, and reset. A final
1,024-bit output case uses the maximum 255-byte budget and checks that a second queued word
stays for the next reply. Its old
two-word budget literal failed elaboration because 258 does not fit in eight bits. The guard now
rejects an impossible two-word budget at compile time and bounds the converted literal. External
mutants that restore either narrow buffered shift compile and then fail the intended assertion.

The integrated RTL, library, application, area, and warning run on runtime hash
`4538de5badf55c9be78a53a96a9ff352c2511572de42fff15f9eaffbb3dac604` passed **177 tests**. It
includes all **101** runnable Bluespec example configurations with every compiler warning promoted
and no warning suppressions. The gate compiles and elaborates each default and target, including
library tests and the documented Ascon-128a variants. It does not simulate the board targets.
Separate Bluesim and Icarus runs cover the generic-width cases, reset interruption, and FIFO
replacement. The final 1,024-bit addition passes on both simulators with all compiler warnings
promoted and no suppressions. A post-rebase integration run passed **383 tests** before this
addition. All six generated area tops remain byte-identical after the budget fix. The raw Ascon
word wrappers passed on the prior source; their RTL is unchanged.

Fresh synthesis of the complete observable transport-plus-adapter tops gave:

| Profile | Logic LUTs, Yosys 0.69 | Logic LUTs, Yosys 0.70+post | RAM LUTs |
|---|---:|---:|---:|
| GCD, W=8, P=256 | 482 | 475 | 8 |
| GCD, W=1, P=64 | 426 | 427 | 24 |
| Ascon40, W=8, P=256 | 497 | 490 | 8 |
| Ascon40, W=1, P=64 | 449 | 445 | 24 |

Both runs used BSC 2026.07.1. The 0.69 tool is the installed openXC7 Yosys; the 0.70+post tool is
the Homebrew build at git `0c11ea098`. Every profile meets the 500-logic-LUT limit. RAM LUTs are
reported separately. These tops exclude the UART PHY and application core. Synthesis does not
establish a clock frequency.

The final fast-path GCD counter change was built with Vivado 2025.2 and tested on the Arty
A7-100T at 100 MHz and 12 Mbaud. The following whole-top results include the application core,
UART PHY, transport, and adapter:

| Application | Slice LUTs | Slice registers | BRAM tiles | Setup WNS | Hold WHS | Reported Fmax |
|---|---:|---:|---:|---:|---:|---:|
| GCD | 1,187 | 1,008 | 1 | +2.742 ns | +0.087 ns | 137.780 MHz |
| Ascon-128 | 2,750 | 1,896 | 1 | +1.132 ns | +0.058 ns | 112.765 MHz |
| Ascon-128a | 2,860 | 1,894 | 1 | +0.822 ns | +0.045 ns | 108.956 MHz |

All three routed builds have zero setup and hold violations. GCD passed its 1,004-pair self-test
twice in fresh processes. The Ascon images passed repeated 61-vector self-tests, long AEAD and
hash checks, key reuse after hash, and bad-tag rejection. A separate hash after reopening matched
the independent 1,537-byte fixture. The Ascon fast-path RTL from the counter change matches the
previously tested design after three collision-free internal wire renames; all core files match
byte for byte. The buffered wide-word change does not instantiate in these application profiles.
The wide-word budget fix leaves all six area tops and the GCD application Verilog byte-identical
to the measured builds. Both Ascon application tops differ only by three collision-free internal
wire renames; all other modules match byte for byte. The comparison with the preserved Vivado
Ascon image also differs only by three collision-free internal wire renames. These comparisons
establish RTL equivalence. They do not establish identical rebuilt bitstreams or a new routed
timing result after the budget fix.

The Arty Ascon target supplies `nextpnr.tmg_ripup=true` and
`nextpnr.placer_heap_timingweight=40` for explicit openXC7 builds. On runtime hash `4538de5b`
before the final budget fix, both variants built through Xeda at 100 MHz: Ascon-128 reported 100.675 MHz and +0.067 ns slack;
Ascon-128a reported 101.031 MHz and +0.102 ns slack. No clock or timing check was relaxed.
These are nextpnr timing results, not physical frequency-sweep measurements. Bare programming
commands select Vivado; use an explicit `yosys_fpga+nextpnr+fpga_pack+openfpgaloader` chain to
select openXC7. Both openXC7 images were loaded into Arty SRAM and passed the public
61-vector self-test, long encryption/decryption and hash, key reuse, and bad-tag rejection.
Their bitstream SHA-256 values are
`8c13b5ace9e849b8bf95594bb71dd6b548e06560a5e30ce6219bd39fc198a64c` (128) and
`7311fbfd39e828f570fcfdbf9c1fce106f784b8277a9504f2ab75049e79095ca` (128a).
The preserved Vivado Ascon-128a image
`d6aece373e1cb8fa1b6ba04273c5255f76480fe4d20f8a7646027bc23be39edd` was restored last and passed
its full self-test. Only SRAM was programmed. The openXC7 builds each ran once on the final
source; reproducible bitstreams across run roots are not established.

The older four-board tables and throughput measurements above belong to their recorded source
and host versions. They do not establish results for this follow-up. A later physical GCD check
measured 32.60 s over serial before FTDI tuning, 10.47 s over configured FTDI, and 6.67 s over
serial immediately afterward for 10,000 pairs. All results matched. A tuned old/current host
comparison found no slowdown in the current host. The residual cost remains unexplained, so
these tests do not establish peak throughput. No flash was written.

The final budget-fix delta passed **163 tests**, including all four complete adapter area gates,
the serialization library target, its strict warning gate, and changelog validation. The existing
area tops are byte-identical to the source used for the two-tool area table above.
The strengthened maximum-budget case also passes on Icarus with all compiler warnings promoted.
An external mutant that removes only the static budget guard compiles, then fails at the last byte
of the first 1,024-bit word: `last=0`, expected `last=1`. The final 163-test delta was rerun after
this test change and passed. The independent Claude review found no guard defect and prompted
this stronger regression.
