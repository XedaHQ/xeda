"""Run a part of the suite: `pytest --shard K/N` runs the K-th of N disjoint parts.

CI runs the full suite as several parallel jobs. Each job runs one shard. A test belongs to
exactly one shard, chosen by a hash of its node id: the choice needs no list of tests, no timing
data and no state, so every worker of every job (pytest-xdist collects in each worker) makes the
same choice. The hash is `zlib.crc32`; Python's `hash` changes from one process to the next.
"""

import argparse
import re
import zlib

#: The form of the option value: the shard number K, a slash, the shard count N.
SHARD = re.compile(r"([1-9][0-9]*)/([1-9][0-9]*)")


def parse_shard(text: str) -> tuple[int, int]:
    """`"K/N"` as `(K, N)` with 1 <= K <= N, for the type of the `--shard` option."""
    match = SHARD.fullmatch(text)
    if match is None or int(match[1]) > int(match[2]):
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a shard: write K/N, with K from 1 to N (for example 2/4)"
        )
    return int(match[1]), int(match[2])


def shard_of(node_id: str, count: int) -> int:
    """The shard, from 1 to `count`, that the test with this node id belongs to."""
    return zlib.crc32(node_id.encode()) % count + 1
