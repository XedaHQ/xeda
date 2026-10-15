#!/usr/bin/env python3
"""Generate the committed Ascon v1.2 test vectors from the BlueLight C reference."""

from __future__ import annotations

import argparse
import ctypes
import subprocess
import sys
import tempfile
from pathlib import Path

REFERENCE_COMMIT = "4b90d3d689969f9fd44919345d2a8a646a8e3639"
LENGTHS = (0, 1, 3, 4, 7, 8, 9, 15, 16, 17, 31, 32, 33)
AD_LENGTHS = (0, 1, 7, 8, 9, 15, 16, 17, 0, 1, 8, 16, 17)
OUTPUT = Path(__file__).with_name("AsconVectors.bsv")


def compile_reference(
    source: Path,
    filename: str,
    temporary: Path,
    name: str,
    fallback_header: Path | None = None,
) -> ctypes.CDLL:
    """Build one reference implementation as a temporary shared library."""
    include_dir = temporary / f"{name}_include"
    include_dir.mkdir()
    if not (source / "crypto_aead.h").is_file() and fallback_header is not None:
        # Some checkouts omit the local Ascon-128a header; use the tracked v1.2 API header.
        (include_dir / "crypto_aead.h").write_bytes(fallback_header.read_bytes())
    suffix = ".dylib" if sys.platform == "darwin" else ".so"
    target = temporary / f"{name}{suffix}"
    shared_flag = "-dynamiclib" if suffix == ".dylib" else "-shared"
    command = [
        "cc",
        shared_flag,
        "-fPIC",
        "-I",
        str(include_dir),
        "-I",
        str(source),
        str(source / filename),
        str(source / "printstate.c"),
        "-o",
        str(target),
    ]
    subprocess.run(command, check=True)
    return ctypes.CDLL(str(target))


def byte_array(data: bytes):
    return (ctypes.c_ubyte * max(1, len(data)))(*(data or b"\0"))


def pattern(length: int, seed: int) -> bytes:
    return bytes((seed + i * 73 + i * i * 19) & 0xFF for i in range(length))


def load_aead(source: Path, temporary: Path, variant_a: bool) -> ctypes.CDLL:
    variant = "ascon128av12" if variant_a else "ascon128v12"
    library = compile_reference(
        source / "crypto_aead" / variant / "ref",
        "aead.c",
        temporary,
        f"aead_{variant}",
        source / "crypto_aead" / "ascon128v12" / "ref" / "crypto_aead.h",
    )
    byte_ptr = ctypes.POINTER(ctypes.c_ubyte)
    ulong = ctypes.c_ulonglong
    library.crypto_aead_encrypt.argtypes = [
        byte_ptr,
        ctypes.POINTER(ulong),
        byte_ptr,
        ulong,
        byte_ptr,
        ulong,
        byte_ptr,
        byte_ptr,
        byte_ptr,
    ]
    library.crypto_aead_encrypt.restype = ctypes.c_int
    library.crypto_aead_decrypt.argtypes = [
        byte_ptr,
        ctypes.POINTER(ulong),
        byte_ptr,
        byte_ptr,
        ulong,
        byte_ptr,
        ulong,
        byte_ptr,
        byte_ptr,
    ]
    library.crypto_aead_decrypt.restype = ctypes.c_int
    return library


def encrypt(library: ctypes.CDLL, message: bytes, ad: bytes, nonce: bytes, key: bytes) -> bytes:
    output = (ctypes.c_ubyte * (len(message) + 16))()
    output_length = ctypes.c_ulonglong()
    result = library.crypto_aead_encrypt(
        output,
        ctypes.byref(output_length),
        byte_array(message),
        len(message),
        byte_array(ad),
        len(ad),
        None,
        byte_array(nonce),
        byte_array(key),
    )
    assert result == 0
    return bytes(output[: output_length.value])


def decrypt(library: ctypes.CDLL, ciphertext: bytes, ad: bytes, nonce: bytes, key: bytes) -> bytes:
    output = (ctypes.c_ubyte * max(1, len(ciphertext)))()
    output_length = ctypes.c_ulonglong()
    result = library.crypto_aead_decrypt(
        output,
        ctypes.byref(output_length),
        None,
        byte_array(ciphertext),
        len(ciphertext),
        byte_array(ad),
        len(ad),
        byte_array(nonce),
        byte_array(key),
    )
    assert result == 0
    return bytes(output[: output_length.value])


def digest(library: ctypes.CDLL, message: bytes) -> bytes:
    output = (ctypes.c_ubyte * 32)()
    result = library.crypto_hash(output, byte_array(message), len(message))
    assert result == 0
    return bytes(output)


def generate_rows(source: Path, temporary: Path) -> list[dict]:
    libraries = [load_aead(source, temporary, variant_a) for variant_a in (False, True)]
    hash_source = source / "crypto_hash" / "asconhashv12" / "ref"
    hash_library = compile_reference(hash_source, "hash.c", temporary, "hash_v12")
    hash_library.crypto_hash.argtypes = [
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.c_ulonglong,
    ]
    hash_library.crypto_hash.restype = ctypes.c_int
    rows: list[dict] = []

    for variant_a in (False, True):
        library = libraries[int(variant_a)]
        key = pattern(16, 0x21 + int(variant_a))
        nonce = pattern(16, 0x91 + int(variant_a))
        for index, length in enumerate(LENGTHS):
            message = pattern(length, 0x40 + index + int(variant_a) * 31)
            ad = pattern(AD_LENGTHS[index], 0xA0 + index + int(variant_a) * 17)
            ciphertext = encrypt(library, message, ad, nonce, key)
            assert decrypt(library, ciphertext, ad, nonce, key) == message
            rows.append(
                dict(
                    v=variant_a,
                    op="Encrypt",
                    new=True,
                    key=key,
                    nonce=nonce,
                    ad=ad,
                    inp=message,
                    exp=ciphertext,
                    msglen=len(message),
                )
            )
            rows.append(
                dict(
                    v=variant_a,
                    op="Decrypt",
                    new=False,
                    key=key,
                    nonce=nonce,
                    ad=ad,
                    inp=ciphertext[:-16],
                    exp=message + ciphertext[-16:],
                    msglen=len(message),
                )
            )

    for variant_a in (False, True):
        for index, length in enumerate(LENGTHS):
            message = pattern(length, 0xC0 + index)
            rows.append(
                dict(
                    v=variant_a,
                    op="Hash",
                    new=False,
                    key=bytes(16),
                    nonce=bytes(16),
                    ad=b"",
                    inp=message,
                    exp=digest(hash_library, message),
                    msglen=0,
                )
            )

    for variant_a in (False, True):
        library = libraries[int(variant_a)]
        key = pattern(16, 0x21 + int(variant_a))
        nonce = pattern(16, 0x91 + int(variant_a))
        for index, ad_length in enumerate((0, 1, 3, 7, 8, 9, 16, 17)):
            ad = pattern(ad_length, 0xE0 + index + int(variant_a) * 11)
            ciphertext = encrypt(library, b"", ad, nonce, key)
            rows.append(
                dict(
                    v=variant_a,
                    op="Encrypt",
                    new=True,
                    key=key,
                    nonce=nonce,
                    ad=ad,
                    inp=b"",
                    exp=ciphertext,
                    msglen=0,
                )
            )
            rows.append(
                dict(
                    v=variant_a,
                    op="Decrypt",
                    new=False,
                    key=key,
                    nonce=nonce,
                    ad=ad,
                    inp=ciphertext[:-16],
                    exp=ciphertext,
                    msglen=0,
                )
            )

    for variant_a in (False, True):
        library = libraries[int(variant_a)]
        key = pattern(16, 0x6D + int(variant_a))
        nonce = pattern(16, 0xB3 + int(variant_a))
        message = pattern(9, 0x37 + int(variant_a))
        ad = pattern(8, 0xD2 + int(variant_a))
        ciphertext = encrypt(library, message, ad, nonce, key)
        rows.append(
            dict(
                v=variant_a,
                op="Encrypt",
                new=True,
                key=key,
                nonce=nonce,
                ad=ad,
                inp=message,
                exp=ciphertext,
                msglen=len(message),
            )
        )
        hash_message = pattern(3, 0x5A + int(variant_a))
        rows.append(
            dict(
                v=variant_a,
                op="Hash",
                new=False,
                key=key,
                nonce=bytes(16),
                ad=b"",
                inp=hash_message,
                exp=digest(hash_library, hash_message),
                msglen=0,
            )
        )
        rows.append(
            dict(
                v=variant_a,
                op="Decrypt",
                new=False,
                key=key,
                nonce=nonce,
                ad=ad,
                inp=ciphertext[:-16],
                exp=message + ciphertext[-16:],
                msglen=len(message),
            )
        )

    for variant_a in (False, True):
        library = libraries[int(variant_a)]
        key = pattern(16, 0x53 + int(variant_a))
        nonce = pattern(16, 0xC7 + int(variant_a))
        message = pattern(129, 0x19 + int(variant_a))
        ad = pattern(64, 0xE7 + int(variant_a))
        ciphertext = encrypt(library, message, ad, nonce, key)
        assert decrypt(library, ciphertext, ad, nonce, key) == message
        rows.append(
            dict(
                v=variant_a,
                op="Encrypt",
                new=True,
                key=key,
                nonce=nonce,
                ad=ad,
                inp=message,
                exp=ciphertext,
                msglen=len(message),
            )
        )
        rows.append(
            dict(
                v=variant_a,
                op="Decrypt",
                new=False,
                key=key,
                nonce=nonce,
                ad=ad,
                inp=ciphertext[:-16],
                exp=message + ciphertext[-16:],
                msglen=len(message),
            )
        )
        hash_message = pattern(129, 0x2B + int(variant_a))
        rows.append(
            dict(
                v=variant_a,
                op="Hash",
                new=False,
                key=key,
                nonce=bytes(16),
                ad=b"",
                inp=hash_message,
                exp=digest(hash_library, hash_message),
                msglen=0,
            )
        )

    assert len(rows) == 122
    assert all(len(row["inp"]) == row["msglen"] for row in rows if row["op"] == "Decrypt")
    return rows


def vector_literal(data: bytes, size: int) -> str:
    padded = data[:size] + bytes(max(0, size - len(data)))
    value = int.from_bytes(padded, "little")
    return f"unpack({size * 8}'h{value:0{size * 2}x})"


def render_row(row: dict) -> str:
    return (
        "AsconTest { variantA: "
        + ("True" if row["v"] else "False")
        + ", operation: "
        + row["op"]
        + ", newKey: "
        + ("True" if row["new"] else "False")
        + f", key: 128'h{int.from_bytes(row['key'], 'little'):032x}"
        + f", nonce: 128'h{int.from_bytes(row['nonce'], 'little'):032x}"
        + f", adLength: 8'd{len(row['ad'])}, dataLength: 8'd{len(row['inp'])}"
        + f", messageLength: 8'd{row['msglen']}"
        + ", ad: "
        + vector_literal(row["ad"], 64)
        + ", inputData: "
        + vector_literal(row["inp"], 160)
        + f", outputLength: 8'd{len(row['exp'])}"
        + ", expected: "
        + vector_literal(row["exp"], 160)
        + " }"
    )


def render(rows: list[dict]) -> str:
    header = f"""// Generated from the local Ascon v1.2 reference checkout at {REFERENCE_COMMIT[:7]}.
// AEAD: crypto_aead/{{ascon128v12,ascon128av12}}/ref; Hash: crypto_hash/asconhashv12/ref.
// The committed vectors are byte values; ordinary simulations do not compile the C sources.
package AsconVectors;
import Vector::*;
import BuildVector::*;
import AsconTypes::*;

typedef struct {{
   Bool variantA;
   AsconOperation operation;
   Bool newKey;
   Bit#(128) key;
   Bit#(128) nonce;
   Bit#(8) adLength;
   Bit#(8) dataLength;
   Bit#(8) messageLength;
   Vector#(64, Bit#(8)) ad;
   Vector#(160, Bit#(8)) inputData;
   Bit#(8) outputLength;
   Vector#(160, Bit#(8)) expected;
}} AsconTest deriving (Bits, Eq, FShow);

Vector#({len(rows)}, AsconTest) asconTests = vec(
"""
    return (
        header
        + ",\n".join("   " + render_row(row) for row in rows)
        + (f"\n);\nInteger asconTestCount = {len(rows)};\nendpackage\n")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("blue_light", type=Path, help="path to the BlueLight checkout root")
    parser.add_argument("--check", action="store_true", help="fail if the committed table is stale")
    args = parser.parse_args()
    source = args.blue_light.resolve() / "Ascon" / "cref"
    if not (source / "crypto_aead/ascon128v12/ref/aead.c").is_file():
        parser.error(f"no Ascon v1.2 reference sources found under {source}")
    with tempfile.TemporaryDirectory(prefix="ascon-vectors-") as temp_name:
        content = render(generate_rows(source, Path(temp_name)))
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_text() != content:
            print(f"{OUTPUT} differs from regenerated vectors")
            return 1
        print(f"{OUTPUT} matches {REFERENCE_COMMIT}")
        return 0
    OUTPUT.write_text(content)
    print(f"wrote 122 vectors to {OUTPUT} from {REFERENCE_COMMIT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
