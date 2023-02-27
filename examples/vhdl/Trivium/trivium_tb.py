import pathlib
import random

from cocolight import DUT, ValidReadyTb, cocotest
from cocolight.utils import bytes_to_words

# We create this TB object in every test so that all the required functions can be accessed
# from within this class.


class CModel:
    def __init__(self) -> None:
        self.lib = None
        self.ffi = None

    def compile(self):
        from cffi import FFI

        ffibuilder = FFI()
        # name = 'trivium64'
        tb_dir = pathlib.Path(__file__).parent.resolve()
        sources = [tb_dir / "cref" / "trivium64.c"]
        cdefs = """
        uint64_t trivium64_next();
        void trivium64_setseed(uint64_t seed, uint64_t seq);
        void trivium_api(const uint8_t *key, const uint8_t *iv, size_t ks_size, uint8_t *ks);
        """

        ffibuilder.cdef(cdefs)

        ffibuilder.set_source(
            "_" + self.__class__.__name__.lower(),
            cdefs,
            sources=sources,
            library_dirs=[],
            #  libraries = []
        )

        ffibuilder.compile(verbose=True)
        from _cmodel import ffi, lib

        self.ffi = ffi
        self.lib = lib
        assert self.lib is not None
        assert self.ffi is not None

    def seed(self, key, iv):
        assert self.lib is not None
        self.lib.trivium64_setseed(key, iv)

    def next(self):
        assert self.lib is not None
        return self.lib.trivium64_next()

    def trivium_api(self, key: bytes, iv: bytes, ks_size: int) -> bytes:
        ks = bytes(ks_size)
        assert self.lib is not None
        self.lib.trivium_api(key, iv, ks_size, ks)
        return ks


class TriviumTb(ValidReadyTb):
    """trivium testbench"""

    def __init__(self, dut: DUT, debug: bool = False):
        super().__init__(dut, "clk", "rst", debug)
        self.din = self.driver("din", "data")
        self.ks = self.monitor("ks", "data")
        assert self.clock_edge is not None

    async def rekey(self):
        """re-key"""
        assert self.clock_edge is not None
        self.dut.rekey.value = 1
        while True:
            await self.clock_edge
            if self.ks.valid.value == 1 or self.din.ready.value == 1:
                break
        self.dut.rekey.value = 0


@cocotest
async def test_trivium(dut: DUT):
    c_model = CModel()
    c_model.compile()

    tb = TriviumTb(dut)
    # TODO init/set other signals?
    dut.rekey.value = 0

    assert tb.clock_edge is not None

    # Reset
    await tb.reset()

    # get bound parameters/generics from the simulator
    IN_BITS = tb.get_int_value("G_IN_BITS")
    assert IN_BITS
    OUT_BITS = tb.get_int_value("G_OUT_BITS")
    assert OUT_BITS

    print(f"IN_BITS:{IN_BITS}, OUT_BITS:{OUT_BITS}")

    await tb.clock_edge

    max_exp_words = 10
    num_tests = 200

    for t in range(num_tests):
        key_bytes = random.randbytes(64 // 8)
        iv_bytes = random.randbytes(64 // 8)
        # print(f"key_bytes={key_bytes.hex()}")
        # print(f"iv_bytes={iv_bytes.hex()}")
        # print(f"key words={[w.hex() for w in bytes_to_words(key_bytes, IN_BITS)]}")
        golden_bytes = c_model.trivium_api(
            key_bytes, iv_bytes, random.randrange(1, max_exp_words) * OUT_BITS // 8
        )
        # print(f"golden_bytes={golden_bytes}")
        golden_words = bytes_to_words(golden_bytes, OUT_BITS)

        await tb.rekey()
        await tb.din.enqueue_seq(bytes_to_words(key_bytes + iv_bytes, IN_BITS))

        for i, golden in enumerate(golden_words):
            data = await tb.ks.dequeue()
            data = data.data.hex()
            # golden = hex(c_model.next())
            golden = hex(golden)
            # print(f"{i}: received {data} expected: {golden}")
            assert data == golden, f"{t}.{i}: received {data} expected: {golden}"

    # with open("_out.dat", "wb") as out_file:
    # for b in received_outs:
    # out_file.write(b)
