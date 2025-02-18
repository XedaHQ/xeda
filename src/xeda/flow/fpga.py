import logging
import re
from typing import Any, Optional

from ..dataclass import Field, XedaBaseModel, root_validator
from ..utils import try_convert

log = logging.getLogger(__name__)


__all__ = ["FPGA"]


class FPGA(XedaBaseModel):
    """FPGA target device"""

    # definition order: part > device > vendor > {family, speed, package, etc}
    part: Optional[str] = Field(None, description="full device part identifier")
    device: Optional[str]
    vendor: Optional[str]
    family: Optional[str]
    generation: Optional[str]
    type: Optional[str]
    speed: Optional[str] = Field(None, description="speed-grade")
    package: Optional[str]
    capacity: Optional[str]
    pins: Optional[int]
    grade: Optional[str]

    def __init__(self, *args: str, **data: Any) -> None:
        if args:
            if len(args) != 1 or not args[0]:
                raise ValueError("Only a single 'part' non-keyword argument is valid.")
            a = args[0]
            if isinstance(a, str):
                if "part" in data:
                    raise ValueError("'part' field already given in keyword arguments.")
                data["part"] = a
            elif isinstance(a, dict):
                if data:
                    raise ValueError("Both dictionary and keyword arguments preset")
                data = a
            else:
                raise ValueError(f"Argument of type {type(a)} is not supported!")
        log.debug("fpga init! data=%s", data)
        super().__init__(**data)

    # this is called before all field validators!
    @root_validator(pre=True)
    def _fpga_root_validator(cls, values):  # pylint: disable=no-self-argument
        if not values:
            return values
        # Intel: https://www.intel.com/content/dam/www/central-libraries/us/en/documents/product-catalog.pdf
        # Lattice: https://www.latticesemi.com/Support/PartNumberReferenceGuide
        # Xilinx: https://www.xilinx.com/support/documents/selection-guides/7-series-product-selection-guide.pdf
        #         https://docs.xilinx.com/v/u/en-US/ds890-ultrascale-overview
        #         https://www.xilinx.com/support/documents/selection-guides/ultrascale-fpga-product-selection-guide.pdf
        #         https://www.xilinx.com/support/documents/selection-guides/ultrascale-plus-fpga-product-selection-guide.pdf
        part = values.get("part")

        def set_if_not_exist(attr: str, v: Any) -> None:
            if attr not in values:
                values[attr] = v

        def set_xc_family(s: str, gen=None):
            d = dict(s="spartan", a="artix", k="kintex", v="virtex", z="zynq")
            s = s.lower()
            if s in d:
                family = d[s]
                if gen:
                    family += str(gen)
                set_if_not_exist("family", family)
                return family
            return None

        if part:
            part = part.strip()
            values["part"] = part
            # speed: 6 = slowest, 8 = fastest
            match_ecp5 = re.match(
                r"^LFE5(U|UM|UM5G)-(\d+)F-(?P<sp>\d)(?P<pkg>[A-Z]+)(?P<pins>\d+)(?P<gr>[A-Z]?)$",
                part,
                flags=re.IGNORECASE,
            )
            if match_ecp5:
                log.debug(
                    "Lattice ECP5 attributes extracted from part# (%s): %s",
                    part,
                    match_ecp5.groupdict(),
                )
                set_if_not_exist("vendor", "lattice")
                set_if_not_exist("family", "ecp5")
                set_if_not_exist("type", match_ecp5.group(1).lower())
                set_if_not_exist("device", "LFE5" + match_ecp5.group(1).upper())
                set_if_not_exist("capacity", match_ecp5.group(2) + "k")
                set_if_not_exist("speed", match_ecp5.group("sp"))
                set_if_not_exist("package", match_ecp5.group("pkg"))
                set_if_not_exist("pins", try_convert(match_ecp5.group("pins"), int))
                set_if_not_exist("grade", match_ecp5.group("gr"))
                return values
            # Commercial Xilinx # Generation # Family # Logic Cells in 1K units # Speed Grade (-1 slowest, L: low-power) # Package Type
            match_xc6 = re.match(
                r"^(XC)(6)(?P<f>[A-Z]+)(?P<lc>\d+)(-(?P<speed_grade>\d))?(-(?P<pkg>[A-Z]+)(?P<pins>\d+))?$",
                part,
                flags=re.IGNORECASE,
            )
            if match_xc6:
                log.debug(
                    "Xilinx/AMD Spartan-6 attributes extracted from part# (%s): %s",
                    part,
                    match_xc6.groupdict(),
                )
                gen = "6"
                set_if_not_exist("vendor", "xilinx")
                set_if_not_exist("generation", gen)
                fam = match_xc6.group("f")
                if fam:
                    set_xc_family(fam[0], gen)
                lc = match_xc6.group("lc")
                set_if_not_exist(
                    "device",
                    "xc" + gen + fam + lc,
                )
                set_if_not_exist("capacity", lc + "K")
                pins = match_xc6.group("pins")
                if pins:
                    set_if_not_exist("package", match_xc6.group("pkg") + pins)
                    set_if_not_exist("pins", try_convert(pins, int))
                set_if_not_exist("speed", match_xc6.group("speed_grade"))
                return values
            match_xc7 = re.match(
                r"^(XC)(?P<g>\d)(?P<f>[A-Z]+)(?P<lc>\d+)-?(?P<s>L?\d)?(?P<pkg>[A-Z]+)(?P<pins>\d+)(?P<gr>-\d)?$",
                part,
                flags=re.IGNORECASE,
            )
            if match_xc7:
                log.debug(
                    "Xilinx/AMD series-7 attributes extracted from part# (%s): %s",
                    part,
                    match_xc7.groupdict(),
                )
                set_if_not_exist("vendor", "xilinx")
                set_if_not_exist("generation", match_xc7.group("g"))
                fam = match_xc7.group("f")
                family = set_xc_family(fam, "-7")
                if family:
                    log.info("Detected FPGA family: %s", family)
                else:
                    log.warning("Could not determine the FPGA family for device %s", part)
                lc = match_xc7.group("lc")
                set_if_not_exist(
                    "device",
                    match_xc7.group(1) + match_xc7.group("g") + match_xc7.group("f") + lc,
                )
                set_if_not_exist("capacity", lc + "K")
                set_if_not_exist("package", match_xc7.group("pkg"))
                set_if_not_exist("pins", try_convert(match_xc7.group("pins"), int))
                set_if_not_exist("grade", match_xc7.group("gr"))
                return values
            match_us = re.match(
                r"^(XC)(?P<f>[A-Z])(?P<g>[A-Z]+)(?P<lc>\d+)-?(?P<s>L?\d)?(?P<pkg>[A-Z][A-Z][A-Z]+)(?P<pins>\d+)(?P<gr>-\d)?$",
                part,
                flags=re.IGNORECASE,
            )
            if match_us:
                log.debug(
                    "Xilinx/AMD Ultrascale attributes extracted from part# (%s): %s",
                    part,
                    match_us.groupdict(),
                )
                set_if_not_exist("vendor", "xilinx")
                set_if_not_exist("generation", match_us.group("g"))
                set_xc_family(match_us.group("f") + "-us")
                lc = match_us.group("lc")
                set_if_not_exist(
                    "device",
                    match_us.group(1) + match_us.group("g") + match_us.group("f") + lc,
                )
                set_if_not_exist("capacity", lc + "K")
                set_if_not_exist("package", try_convert(match_us.group("pkg"), int))
                set_if_not_exist("pins", try_convert(match_us.group("pins"), int))
                set_if_not_exist("grade", match_us.group("gr"))
                return values
            # UltraSCALE+
            # capacity is index to table, roughly x100K LCs
            match_usp = re.match(
                r"^(XC)(?P<f>[A-Z])U(?P<lc>\d+)P-?(?P<s>L?\d)?(?P<pkg>[A-Z]+)(?P<pins>\d+)(?P<gr>-\d)?$",
                part,
                flags=re.IGNORECASE,
            )
            if match_usp:
                log.debug(
                    "Xilinx/AMD Ultrascale+ attributes extracted from part# (%s): %s",
                    part,
                    match_usp.groupdict(),
                )
                set_if_not_exist("vendor", "xilinx")
                set_if_not_exist("generation", "usp")
                set_xc_family(match_usp.group("f") + "-usp")
                lc = match_usp.group("lc")
                set_if_not_exist("capacity", lc)
                set_if_not_exist(
                    "device", match_usp.group(1) + match_usp.group("f") + "U" + lc + "P"
                )
                set_if_not_exist("package", try_convert(match_usp.group("pkg"), int))
                set_if_not_exist("pins", try_convert(match_usp.group("pins"), int))
                set_if_not_exist("grade", match_usp.group("gr"))
                return values
        elif not values.get("device") and not values.get("vendor"):
            raise ValueError(
                "Missing enough information about the FPGA device. Please set the 'part' number and/or device, vendor, family, etc."
            )
        return values
