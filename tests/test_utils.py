"""`utils.copy_fd`: the descriptor copy `deliver` and `replacing_copy` share. The kernel primitives
of the platform the suite runs on are exercised for real (fcopyfile on macOS; copy_file_range and
sendfile on Linux). The Linux primitives' looping and failure handling is also run, on any
platform, against stand-ins for `os.copy_file_range` / `os.sendfile` built on `pread`/`pwrite`
that copy short and fail on request: that tests xeda's logic, not the Linux kernel's."""

import errno
import os
import stat
import sys
from pathlib import Path
from typing import Callable, List

import pytest

from xeda import utils
from xeda.utils import copy_fd, replacing_copy

CHUNK = utils._LOOP_CHUNK
#: sizes: empty, one byte, around a chunk, and larger than three chunks, none a multiple of one
SIZES = [0, 1, CHUNK - 1, CHUNK, CHUNK + 1, 3 * CHUNK + 12345]


def _content(size: int) -> bytes:
    return (bytes(range(251)) * (size // 251 + 1))[:size]


def _copy(tmp_path: Path, content: bytes, dst_content: bytes = b"") -> bytes:
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.write_bytes(content)
    dst.write_bytes(dst_content)
    with open(src, "rb") as s, open(dst, "r+b") as d:
        copy_fd(s.fileno(), d.fileno())
    return dst.read_bytes()


@pytest.mark.parametrize("size", SIZES)
def test_copy_fd_copies_every_size_byte_for_byte(tmp_path, size):
    assert _copy(tmp_path, _content(size)) == _content(size)


@pytest.mark.skipif(sys.platform != "darwin", reason="fcopyfile is macOS's")
def test_on_macos_fcopyfile_does_the_copy(tmp_path, monkeypatch):
    import posix  # type: ignore[import-not-found]

    assert utils._COPYFILE_DATA == posix._COPYFILE_DATA  # type: ignore[attr-defined]
    assert utils._fast_copies() == [utils._copy_with_fcopyfile]
    done: List[int] = []
    real = utils._copy_with_fcopyfile

    def spy(src_fd, dst_fd, size):
        real(src_fd, dst_fd, size)  # raises if it failed
        done.append(size)

    monkeypatch.setattr(utils, "_copy_with_fcopyfile", spy)
    assert _copy(tmp_path, _content(3 * CHUNK + 12345)) == _content(3 * CHUNK + 12345)
    assert done == [3 * CHUNK + 12345]  # the primitive succeeded: the loop was not needed


def test_the_platform_choice_cannot_fail_and_names_the_right_primitives():
    assert utils._fast_copies("win32") == []
    assert utils._fast_copies("freebsd14") == []
    assert utils._fast_copies("darwin") == [utils._copy_with_fcopyfile]
    linux = utils._fast_copies("linux")
    assert all(f in (utils._copy_with_copy_file_range, utils._copy_with_sendfile) for f in linux)
    assert (utils._copy_with_copy_file_range in linux) == hasattr(os, "copy_file_range")
    assert (utils._copy_with_sendfile in linux) == hasattr(os, "sendfile")


@pytest.mark.parametrize("written", [1, 4096, CHUNK])
@pytest.mark.parametrize("garbage", [b"", b"stale" * 1000_000], ids=["empty", "stale-longer"])
def test_a_fast_path_failing_after_writing_is_followed_by_a_complete_copy(
    tmp_path, monkeypatch, written, garbage
):
    """The fast path writes some bytes, then raises: the loop must start from offset 0 and an empty
    destination. A destination that already held more than the source (garbage) shows the
    truncation too."""
    content = _content(2 * CHUNK + 777)
    wrote: List[int] = []

    def partial_then_fail(src_fd, dst_fd, size):
        wrote.append(os.write(dst_fd, os.pread(src_fd, written, 0)))
        raise OSError(errno.EIO, "failed after writing some bytes")

    monkeypatch.setattr(utils, "_fast_copies", lambda *args: [partial_then_fail])
    assert _copy(tmp_path, content, garbage) == content
    assert wrote == [written]


def test_each_fast_path_is_tried_in_turn_from_scratch(tmp_path, monkeypatch):
    """The first fails after writing; the second sees an empty destination at 0 and fails too; the
    loop still delivers."""
    content = _content(CHUNK + 5)
    seen: List[tuple] = []

    def failing(tag: str) -> Callable[[int, int, int], None]:
        def fast(src_fd, dst_fd, size):
            seen.append((tag, os.fstat(dst_fd).st_size, os.lseek(dst_fd, 0, os.SEEK_CUR)))
            os.write(dst_fd, b"x" * 50)
            raise OSError(errno.EXDEV, "cross-device")

        return fast

    monkeypatch.setattr(utils, "_fast_copies", lambda *args: [failing("a"), failing("b")])
    assert _copy(tmp_path, content) == content
    assert seen == [("a", 0, 0), ("b", 0, 0)]


def test_a_real_error_is_raised_by_the_loop_not_swallowed(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.write_bytes(_content(10))
    monkeypatch.setattr(utils, "_fast_copies", lambda *args: [])

    def full(fd, data):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(utils.os, "write", full)
    with open(src, "rb") as s, open(tmp_path / "dst", "wb") as d:
        with pytest.raises(OSError) as excinfo:
            copy_fd(s.fileno(), d.fileno())
    assert excinfo.value.errno == errno.ENOSPC


def test_a_source_that_cannot_be_rewound_is_copied_by_the_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(utils, "_fast_copies", lambda *args: pytest.fail("a pipe has no fast path"))
    r, w = os.pipe()
    try:
        os.write(w, b"through a pipe")
        os.close(w)
        with open(tmp_path / "dst", "wb") as d:
            copy_fd(r, d.fileno())
    finally:
        os.close(r)
    assert (tmp_path / "dst").read_bytes() == b"through a pipe"


def test_the_source_is_read_from_its_start_whatever_its_offset(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.write_bytes(_content(5000))
    with open(src, "rb") as s, open(dst, "wb") as d:
        s.read(1234)
        os.lseek(s.fileno(), 1234, os.SEEK_SET)
        copy_fd(s.fileno(), d.fileno())
    assert dst.read_bytes() == _content(5000)


# --- the Linux primitives' logic, on stand-ins, on any platform ------------------------------


def _fake_copy_file_range(limit: int, calls: List[int]):
    def fake(src, dst, count, offset_src=None, offset_dst=None):
        data = os.pread(src, min(count, limit), offset_src)
        calls.append(len(data))
        return os.pwrite(dst, data, offset_dst)

    return fake


def _fake_sendfile(limit: int, calls: List[int]):
    def fake(out_fd, in_fd, offset, count):
        data = os.pread(in_fd, min(count, limit), offset)
        calls.append(len(data))
        return os.pwrite(out_fd, data, offset)

    return fake


@pytest.mark.parametrize(
    "primitive, name, fake",
    [
        (utils._copy_with_copy_file_range, "copy_file_range", _fake_copy_file_range),
        (utils._copy_with_sendfile, "sendfile", _fake_sendfile),
    ],
)
def test_a_linux_primitive_is_looped_until_every_byte_is_copied(
    tmp_path, monkeypatch, primitive, name, fake
):
    calls: List[int] = []
    monkeypatch.setattr(os, name, fake(100_000, calls), raising=False)
    monkeypatch.setattr(utils, "_fast_copies", lambda *args: [primitive])
    content = _content(1_000_003)
    assert _copy(tmp_path, content) == content
    assert len(calls) == 11 and sum(calls) == len(content)  # short counts: 10 x 100000, then 3


@pytest.mark.parametrize(
    "primitive, name, fake",
    [
        (utils._copy_with_copy_file_range, "copy_file_range", _fake_copy_file_range),
        (utils._copy_with_sendfile, "sendfile", _fake_sendfile),
    ],
)
def test_a_linux_primitive_that_copies_nothing_early_falls_back(
    tmp_path, monkeypatch, primitive, name, fake
):
    """A return of 0 before the file's size (a file system that cannot, a file that shrank) is not
    'done': it fails, and the loop copies from scratch."""
    calls: List[int] = []
    monkeypatch.setattr(os, name, fake(0, calls), raising=False)
    monkeypatch.setattr(utils, "_fast_copies", lambda *args: [primitive])
    content = _content(5000)
    assert _copy(tmp_path, content) == content
    assert calls == [0]


def test_copy_file_range_failing_midway_leaves_sendfile_and_the_loop_a_clean_start(
    tmp_path, monkeypatch
):
    """copy_file_range copies a few chunks and then fails (EXDEV); sendfile gets the pair as if
    new."""
    chunks: List[int] = []
    good = _fake_copy_file_range(1000, chunks)

    def fails_third_time(src, dst, count, offset_src=None, offset_dst=None):
        if len(chunks) == 2:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return good(src, dst, count, offset_src, offset_dst)

    starts: List[tuple] = []
    send = _fake_sendfile(10**9, [])

    def sendfile(out_fd, in_fd, offset, count):
        starts.append((os.fstat(out_fd).st_size, offset))
        return send(out_fd, in_fd, offset, count)

    monkeypatch.setattr(os, "copy_file_range", fails_third_time, raising=False)
    monkeypatch.setattr(os, "sendfile", sendfile, raising=False)
    monkeypatch.setattr(
        utils,
        "_fast_copies",
        lambda *args: [utils._copy_with_copy_file_range, utils._copy_with_sendfile],
    )
    content = _content(50_000)
    assert _copy(tmp_path, content) == content
    assert starts == [(0, 0)]


# --- replacing_copy shares it ----------------------------------------------------------------


def test_replacing_copy_copies_content_and_mode_and_replaces_a_link(tmp_path):
    src = tmp_path / "src"
    content = _content(2 * CHUNK + 9)
    src.write_bytes(content)
    src.chmod(0o751)
    victim = tmp_path / "victim"
    victim.write_text("not touched\n")
    link = tmp_path / "link"
    link.symlink_to(victim)
    assert replacing_copy(src, link) == link
    assert not link.is_symlink() and link.read_bytes() == content
    assert stat.S_IMODE(link.stat().st_mode) == 0o751
    assert victim.read_text() == "not touched\n"


def test_replacing_copy_falls_back_after_a_partial_fast_copy(tmp_path, monkeypatch):
    content = _content(CHUNK + 3)

    def partial_then_fail(src_fd, dst_fd, size):
        os.write(dst_fd, os.pread(src_fd, 777, 0))
        raise OSError(errno.EINVAL, "invalid")

    monkeypatch.setattr(utils, "_fast_copies", lambda *args: [partial_then_fail])
    src = tmp_path / "src"
    src.write_bytes(content)
    assert replacing_copy(src, tmp_path / "dst").read_bytes() == content


@pytest.mark.skipif(sys.platform != "darwin", reason="fcopyfile is macOS's")
def test_a_missing_fcopyfile_is_an_oserror_so_the_loop_takes_over(tmp_path, monkeypatch):
    import ctypes

    class NoLibc:
        def __init__(self, *args, **kwargs):
            pass

        def __getattr__(self, name):
            raise AttributeError(name)

    monkeypatch.setattr(ctypes, "CDLL", NoLibc)
    with pytest.raises(OSError) as excinfo:
        utils._copy_with_fcopyfile(0, 1, 1)
    assert excinfo.value.errno == errno.ENOSYS
    assert _copy(tmp_path, _content(CHUNK + 1)) == _content(CHUNK + 1)


def test_a_failed_command_says_why_when_it_is_told():
    """The tool's own words follow the exit code: a flow that knows what its tool reported
    (`Nextpnr._failure`) adds them, and a failure without them reads as it always did."""
    from xeda.utils import NonZeroExitCode

    assert str(NonZeroExitCode(["tool", "a b"], 2)) == "Command 'tool a b' exited with code 2!"
    told = NonZeroExitCode(["tool"], 2, "it could not place x", "and y")
    assert str(told) == "Command 'tool' exited with code 2! it could not place x and y"
    assert (told.command_args, told.exit_code) == ("tool", 2)
