# Zynq-7000S part parser gap

Investigated 2026-10-09 after `test_installed_project_xray_part_mappings` failed in the Python
3.15 full-suite run. This note records the current defect and a narrow follow-up plan. It does not
change the parser or claim support for devices absent from the installed database.

## Reproducer and expected result

The installed Project X-Ray part map contains:

```yaml
xc7z007sclg225-1:
  device: xc7z007s
  package: clg225
  speedgrade: '1'
```

The expected parsed fields are `device=xc7z007s`, `package=clg`, `pins=225`, and `speed=-1`.
`FPGA("xc7z007sclg225-1")` currently returns `device=xc7z007`, `package=sclg`, `pins=225`, and
`speed=-1`. The parser assigns the device suffix `s` to the package. The existing optional
database test compares each parsed device and package to the database entry, so this mapping
fails that check.

The parser in `src/xeda/flow/fpga.py` is identical to `origin/main` at `c5f77ca1` and was not
changed on this branch (the starting branch commit was `ac6299ea`). Running the reproducer with
the local Python 3.14 and 3.15 tox interpreters returns the same incorrect fields. This is a
pre-existing parser defect, not a Python 3.15 regression.

Run this reproducer from the repository root with the project's environment:

```python
from xeda.flow.fpga import FPGA

fpga = FPGA("xc7z007sclg225-1")
print(fpga.device, fpga.package, fpga.pins, fpga.speed)
```

It prints `xc7z007 sclg 225 -1`; the device and package fields should be `xc7z007s` and `clg`.

## Scope and naming boundary

AMD documents Z-7007S, Z-7012S, and Z-7014S as Zynq-7000S products. Their device identifiers use
`XC7Z007S`, `XC7Z012S`, and `XC7Z014S`. The installed Project X-Ray map has four part entries
whose device field is `xc7z007s`:

| Part | Expected device | Package | Speed grade |
| --- | --- | --- | --- |
| `xc7z007sclg225-1` | `xc7z007s` | `clg225` | `1` |
| `xc7z007sclg225-2` | `xc7z007s` | `clg225` | `2` |
| `xc7z007sclg400-1` | `xc7z007s` | `clg400` | `1` |
| `xc7z007sclg400-2` | `xc7z007s` | `clg400` | `2` |

The installed map has no `xc7z012s` or `xc7z014s` entries, so this database check does not test
those parts. AMD's product documentation confirms the family names, but more package-specific
checks need source rows or ordering examples for those devices.

Do not treat every `s` after a Zynq device number as a device suffix. The installed map also has
six `xc7z030s...` parts: three `sbg485` packages and three `sbv485` packages. For these parts,
`device=xc7z030`; the `s` begins the package code. A generic optional `s` in the device regex
would misparse these valid part numbers.

## Mapping provenance

The failing test reads `/opt/openxc7/share/nextpnr/prjxray-db/zynq7/mapping/parts.yaml`. That file
was dated 2026-09-26 on this host and has SHA-256
`8a271f1cc1ad1a291e95ca19588bc4379374f197ba4afe6630aa538f4a92d75a`.
Its rows match the current [`openXC7/prjxray-db` Zynq mapping](https://github.com/openXC7/prjxray-db/blob/master/zynq7/mapping/parts.yaml), checked on 2026-10-09. The
repository listing reported activity through 2026-10-03, but that does not identify the commit
that supplied this installed file.

The installed database's `Info.md` says its Project X-Ray source was generated on 2021-12-14 from
commit `4c157493ec9f13caea4ad3f0c02f8f318f198846`. However, that file records SHA-256
`41e93442c88339163e8d66cb475c7d4009f9653b5c3ad74b720e480867615503` for the Zynq `parts.yaml`,
which differs from the installed file's hash above. The install has no repository commit or
manifest. Therefore the original dataset provenance is recorded locally, but the exact provenance
of this installed mapping revision cannot be established from its metadata.

AMD's [Zynq-7000 product page](https://www.amd.com/en/products/adaptive-socs-and-fpgas/soc/zynq-7000.html)
lists the Zynq-7000S devices as Z-7007S, Z-7012S, and Z-7014S. AMD's [Zynq-7000 package and pinout
specification](https://docs.amd.com/api/khub/documents/kOAtMqkVamwUDaIfCXO91A/content) identifies
device XC7Z007S in package CLG225 and CLG400. The [openXC7 Zynq mapping](https://raw.githubusercontent.com/openXC7/prjxray-db/master/zynq7/mapping/parts.yaml)
records the exact `xc7z007sclg225-1` device, package, and speed-grade fields used by this check.

## Candidate fix and regression plan

Extend the Series-7 parser to recognize the Zynq-7000S device suffix without moving an `s` that
starts the package code into the device field. Resolve the ambiguity with known valid device
identifiers or another data-backed boundary rule; do not add an unconditional optional `s` to all
Zynq numbers.

Add focused parser cases for `xc7z007sclg225-1` and `xc7z007sclg400-2`, and retain an explicit
boundary case for `xc7z030sbg485-1` with `device=xc7z030`, `package=sbg`, and `pins=485`. Keep the
installed-database test as an integration check when the optional mapping is present. If support
for Z-7012S or Z-7014S is added, first add sourced mapping examples for those exact ordering codes;
the current installed database does not provide them.

## Validation and limits

- Reproduced the incorrect fields with `.tox/py314/bin/python` and `.tox/py315/bin/python`.
- Confirmed that `src/xeda/flow/fpga.py` matches `origin/main` and has no working-tree changes.
- Confirmed four `xc7z007s` map entries and six `xc7z030s...` package-prefixed entries in the
  installed Zynq mapping.
- Verified the expected `xc7z007sclg225-1` row in the upstream openXC7 map and the device/package
  names in AMD documents on 2026-10-09.

No parser fix or new regression test was made as part of this investigation. The local installed
mapping's exact source revision remains unverified because its metadata hash does not match the
recorded Project X-Ray mapping hash.
