"""One conversion boundary. P0B-07 and motion targets use command units.

Encoder-native counts (e.g. P0B-15) must never be passed to this module.
Integer wire values round half away from zero; display rounding is separate.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import math
from fractions import Fraction


def command_units_per_revolution(pulses_setting, encoder_counts, numerator, denominator):
    """Arithmetic only: readback cannot prove power-cycle or firmware effects."""
    for value in (pulses_setting,encoder_counts,numerator,denominator):
        if type(value) is not int:
            raise ValueError('单位参数必须是完整整数读回')
    if not 0<=pulses_setting<=1048576:
        raise ValueError('P05-02 超出手册范围')
    if not 131072<=encoder_counts<=8388608:
        raise ValueError('P00-31/32 编码器线数超出手册范围')
    if not 1<=numerator<=2**30 or not 1<=denominator<=2**30:
        raise ValueError('P05-07/09 电子齿轮参数超出手册范围')
    return Fraction(pulses_setting,1) if pulses_setting else Fraction(encoder_counts*denominator,numerator)


def finite(value):
    if isinstance(value, bool):
        raise ValueError('布尔值不是数值')
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('数值必须有限')
    return value


def integer(value):
    return int(Decimal(str(value)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class Units:
    pulses_per_rev: int
    cm_per_rev: float

    def pulse(self, cm):
        value = Decimal(str(finite(cm))) * self.pulses_per_rev / Decimal(str(self.cm_per_rev))
        return int(value.quantize(Decimal('1'), rounding=ROUND_HALF_UP))

    def cm(self, pulses):
        return finite(pulses) / self.pulses_per_rev * self.cm_per_rev

    def rpm(self, pr_s):
        return finite(pr_s) / self.pulses_per_rev * 60

    def rpm_to_cm_s(self, rpm):
        return finite(rpm) / 60 * self.cm_per_rev

    def percent_to_cm(self, percent, travel):
        value = finite(percent)
        if not 0 <= value <= 100:
            raise ValueError('目标百分比必须为 0~100')
        return value * travel / 100

    def target(self, cm, reference=0):
        result = int(reference) + self.pulse(cm)
        if not -(2**30) <= result <= 2**30:
            raise ValueError('PP 目标超出手册范围')
        return result
