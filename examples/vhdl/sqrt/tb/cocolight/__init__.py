from dataclasses import dataclass
from typing import Tuple, List, Union, Dict
import cocotb
from cocotb.handle import ModifiableObject
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer
from cocotb.binary import BinaryValue
import logging
import random
from pydantic import BaseModel, root_validator
import uuid
from box import Box


class DutReset(BaseModel):
    port: str = "rst"
    active_high: bool = True
    synchronous: bool = True


class DutClock(BaseModel):
    port: str = "clk"
    period: Tuple[float, str] = (10, "ns")


class IoPort(BaseModel):  # data + valid + ready
    name: str
    is_output: bool
    data_signal: Union[List[str], str]
    valid_signal: str
    ready_signal: str

    @root_validator(pre=True)
    def io_port_validator(cls, values):
        def set_if_not_exist(k, v):
            if k not in values:
                values[k] = v

        name = values.get("name")
        data_signal = values.get("data_signal")
        if not name and data_signal:
            name = data_signal if isinstance(data_signal, str) else data_signal[0]
        if name:
            set_if_not_exist("data_signal", name)
            set_if_not_exist("valid_signal", name + "_valid")
            set_if_not_exist("ready_signal", name + "_ready")
        else:
            name = uuid.uuid1()
        values["name"] = name
        return values


class InPort(IoPort):
    def __init__(self, *args, **kwargs):
        kwargs["is_output"] = False
        super().__init__(*args, **kwargs)


class OutPort(IoPort):
    def __init__(self, *args, **kwargs):
        kwargs["is_output"] = True
        super().__init__(*args, **kwargs)


class TB:
    WAIT_MAX = 100

    @dataclass
    class Port:
        data: Union[Dict[str, ModifiableObject], ModifiableObject]  # FIXME
        valid: ModifiableObject
        ready: ModifiableObject
        _is_output: bool
        _name: str

    def __init__(
        self,
        dut,
        *ports,
        clock: DutClock = DutClock(),
        reset: DutReset = DutReset(),
        debug=False,
    ):
        self.dut = dut
        self.log = logging.getLogger("cocotb_tb")
        self.log.setLevel(logging.DEBUG if debug else logging.INFO)
        self.ports = {
            p.name: self.Port(
                data={d: self.dut_attr(d) for d in p.data_signal}
                if isinstance(p.data_signal, list)
                else self.dut_attr(p.data_signal),
                valid=self.dut_attr(p.valid_signal),
                ready=self.dut_attr(p.ready_signal),
                _is_output=p.is_output,
                _name=p.name,
            )
            for p in ports
        }
        for p in self.ports.values():
            if p._is_output:
                p.ready.value = 0
            else:  # input
                p.valid.value = 0

        self.clock_cfg = clock
        self.reset_cfg = reset
        self.clock_port = self.dut_attr(clock.port)
        self.reset_port = self.dut_attr(reset.port)
        self.clock_edge = None
        self.clock_thread = None
        if self.clock_port:
            self.clock_port.setimmediatevalue(0)
            self.clock_edge = RisingEdge(self.clock_port)
            (period, units) = clock.period
            self.clock_thread = cocotb.fork(
                Clock(self.clock_port, period, units=units).start()
            )
        else:
            self.log.critical(f"No clocks found!")
        if self.reset_port:
            self.reset_value = 1 if self.reset_cfg.active_high else 0
            self.reset_port.setimmediatevalue(not self.reset_value)
        else:
            self.log.warning(f"No resets found. Specified reset signal: {reset.port}")

    def dut_attr(self, attr):
        return getattr(self.dut, attr)

    def get_int_value(self, attr, otherwise=None):
        attr = getattr(self.dut, attr, None)
        if attr:
            return int(attr.value)
        return otherwise

    async def _reset_sync(self, cycles=2, delay=None):
        if delay is None:
            delay = self.clock_cfg.period[0] / 2
        units = self.clock_cfg.period[1]
        await Timer(delay, units)
        self.reset_port.value = self.reset_value
        if self.clock_edge:
            for _ in range(cycles):
                await self.clock_edge
        await Timer(delay, units)
        self.reset_port.value = not self.reset_value

    async def _reset_async(self, duration=None):
        ...

    async def reset(self, **kwargs):
        if not self.reset_port:
            return
        if self.reset_cfg.synchronous:
            await self._reset_sync()
        else:
            await self._reset_async()
        self.reset_port._log.debug("Reset complete")

    def _lookup_port(self, p):
        if isinstance(p, IoPort):
            return self._lookup_port(p.name)
        assert isinstance(p, str)
        return self.ports.get(p)

    def put_rand(self, port: Port, f=random.getrandbits):
        if isinstance(port.data, dict):
            for _, sig in port.data.items():
                sig.value = f(len(sig))
        else:
            port.data.value = f(len(port.data))

    def put_data(self, port: Port, data):
        if isinstance(port.data, dict):
            assert isinstance(
                data, dict
            ), f"put data must be a dict for port {port._name}"
            for k, v in data.items():
                port.data[k].value = v
        else:
            port.data.value = data

    async def send_input(self, portname, data):
        assert self.clock_edge, "must have clock"
        port = self._lookup_port(portname)
        assert port
        assert not port._is_output, "port must be input"
        port.valid.value = 1
        self.put_data(port, data)
        await self.clock_edge
        wait_counter = 0
        while not port.ready.value:
            await self.clock_edge
            wait_counter += 1
            assert wait_counter < self.WAIT_MAX, "timed out"
        self.log.debug(f"sent {data:0X} to {port._name}")
        port.valid.value = 0
        self.put_rand(port, f=lambda _: 0)

    async def receive_output_seq(
        self, portname, n
    ) -> List[Union[BinaryValue, Dict[str, BinaryValue]]]:
        o = []
        for i in range(n):
            o.append(await self.receive_output(portname))
        return o

    async def receive_output(self, portname) -> Union[BinaryValue, Box]:
        assert self.clock_edge, "must have clock"
        port = self._lookup_port(portname)
        assert port
        assert port._is_output, "port must be output"
        port.ready.value = 1
        await self.clock_edge
        wait_counter = 0
        while not port.valid.value:
            await self.clock_edge
            wait_counter += 1
            assert wait_counter < self.WAIT_MAX, "timed out"
        port.ready.value = 0
        if isinstance(port.data, dict):
            data_dict = {}
            for k, v in port.data.items():
                data = v.value
                self.log.debug(f"received {data.hex()} from {k}")
                data_dict[k] = data
            return Box(data_dict)
        else:
            data = port.data.value
            self.log.debug(f"received {data.hex()} from {port._name}")
            return data


def to_binstr(v: int, width: int) -> str:
    b = bin(v)[2:]
    return "0" * (width - len(b)) + b


class CModel:
    def __init__(self, sources) -> None:
        self.sources = sources
        self.lib = None
        self.ffi = None
        self.func_prototypes: List[str] = []

    def compile(self):
        from cffi import FFI

        ffibuilder = FFI()
        # name = 'trivium64'
        cdefs = "\n".join(self.func_prototypes)
        ffibuilder.cdef(cdefs)
        ffibuilder.set_source(
            f"_cmodel",
            cdefs,
            sources=self.sources,
            library_dirs=[],
            #  libraries = []
        )

        ffibuilder.compile(verbose=True, tmpdir=".")
        from _cmodel import ffi, lib

        self.ffi = ffi
        self.lib = lib
