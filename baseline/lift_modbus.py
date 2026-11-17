from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass
from typing import Dict, List, Optional

PULSES_PER_REV = 10_000
CM_PER_REV = 0.35

# Ouke servo drive register map (Modbus holding registers).
# Parameter index is decimal in the address low byte: P0D-17=0x0D11, P0B-34=0x0B22.
REG_P02_CONTROL_MODE = 0x0200  # P02-00
REG_P10_MODE = 0x1003
REG_P10_POSITION = 0x100E
REG_P10_PP_SPEED = 0x1019
REG_P10_PV_SPEED = 0x102A
REG_POD_FAULT_RESET = 0x0D01  # P0D-01
REG_POD_DI_FORCE = 0x0D11  # P0D-17
REG_POD_CONTROL = 0x0D08
REG_POD_ENABLE = 0x0D12  # P0D-18
REG_POB_RPM = 0x0B00
REG_POB_LOAD = 0x0B02
REG_POB_DI = 0x0B03  # P0B-03 DI 电平监视，bit0=DI1 … bit5=DI6
REG_POB_STATUS = 0x0B04
REG_POB_POSITION = 0x0B07
REG_POB_ALARM = 0x0B22  # P0B-34 当前故障码
REG_P03_DI4_FUNCTION = 0x0308  # P03-08，随后 P03-09 / P03-10 / P03-11
DI4_BIT = 3
DI5_BIT = 4
FUNIN_POSITIVE_OVERTRAVEL = 14  # FunIN.14 正向超程，DI4
FUNIN_NEGATIVE_OVERTRAVEL = 15  # FunIN.15 反向超程，DI5
DI_LOGIC_ACTIVE_LOW = 0
DI_LOGIC_ACTIVE_HIGH = 1

P02_MODE_MODBUS = 9
P10_MODE_PP = 1
P10_MODE_PV = 3
POD_DI_FORCE_ON = 1
POD_FAULT_RESET_ON = 1
POD_ENABLE = 507
POD_DISABLE = 511
ALARM_FOLLOWING_ERROR = 0x0B00  # Er.B00 位置超差；现场有时带 0x6xxx 前缀
ALARM_MOTOR_OVERLOAD = 0x0620  # Er.620 电机过载；现场有时带 0x6xxx 前缀
ALARM_POSITIVE_OVERTRAVEL = 0x0950  # Er.950 正向超程警告；现场有时带 0xExxx 前缀
ALARM_NEGATIVE_OVERTRAVEL = 0x0952  # Er.952 反向超程警告；现场有时带 0xExxx 前缀
# 断电后编码器计数可能归零，而杆仍停在原处。超过该距离的跳变视为计数失效。
POSITION_REFERENCE_JUMP_CM = 50.0
POD_CONTROL_INVALID = 0
POD_CONTROL_REL_START = 1
POD_CONTROL_ABS_START = 3
POD_CONTROL_REL_IMM = 5
POD_CONTROL_ABS_IMM = 7
POD_CONTROL_VEL = 8
POD_CONTROL_JOG_FWD = 16
POD_CONTROL_JOG_REV = 32
POD_CONTROL_TORQUE = 64
POD_CONTROL_PV_STOP = 256  # PV/JOG 停止；现场验真。PP 模式结束用 0 复位。
POD_CONTROL_ESTOP = 512  # 运动急停（软件急停按钮）
# Backward-compatible aliases used by adapter code.
POD_CONTROL_RESET = POD_CONTROL_INVALID
POD_CONTROL_FORWARD = POD_CONTROL_JOG_FWD
POD_CONTROL_REVERSE = POD_CONTROL_JOG_REV
POD_CONTROL_STOP = POD_CONTROL_PV_STOP

POB04_BIT_LABELS = (
    (0, "位置到达"),
    (1, "速度到达"),
    (2, "转矩到达"),
    (3, "回零完成"),
)


def decode_pob04_status_word(status_word: int) -> Dict[str, bool]:
    return {f"bit{bit}": bool(status_word & (1 << bit)) for bit, _ in POB04_BIT_LABELS}


def format_pob04_status_word(status_word: int) -> str:
    labels = [label for bit, label in POB04_BIT_LABELS if status_word & (1 << bit)]
    # Empty status word means no arrival/complete flags, not a dedicated "standby" mode.
    return " + ".join(labels) if labels else ""


def normalize_rpm_register(raw: int) -> int:
    value = int(raw) & 0xFFFF
    if value >= 0x8000:
        value -= 0x10000
    return value


def normalize_alarm_code(raw: int) -> int:
    """Strip the 0x6xxx / 0xExxx prefix some units report so Er.B00 is 0x0B00 and Er.950 is 0x0950."""
    value = int(raw) & 0xFFFF
    if (value & 0xF000) in (0x6000, 0xE000):
        value &= 0x0FFF
    return value


def di_input_active(monitor: int, bit: int, logic: int) -> bool:
    """True when that DI function is valid.

    P0B-03 stores the pin level. P03 logic 0 is active-low and 1 is active-high.
    """
    level_high = bool(int(monitor) & (1 << int(bit)))
    if int(logic) == DI_LOGIC_ACTIVE_HIGH:
        return level_high
    if int(logic) == DI_LOGIC_ACTIVE_LOW:
        return not level_high
    return False


def jog_direction_blocked(
    direction: str,
    raw_alarm: int,
    di_monitor: Optional[int],
    *,
    di4_function: int = 0,
    di4_logic: int = DI_LOGIC_ACTIVE_LOW,
    di5_function: int = 0,
    di5_logic: int = DI_LOGIC_ACTIVE_LOW,
) -> bool:
    """Block jog from the live limit switch. A latched Er.950/Er.952 does not.

    P0B-34 keeps a single warning, so Er.950 can still be displayed after the
    pole has left the upper switch and is sitting on the lower one.
    Motor overload still blocks both directions.
    """
    if normalize_alarm_code(raw_alarm) == ALARM_MOTOR_OVERLOAD:
        return True
    if di_monitor is None:
        return False
    if (
        direction == "up"
        and int(di4_function) == FUNIN_POSITIVE_OVERTRAVEL
        and di_input_active(di_monitor, DI4_BIT, di4_logic)
    ):
        return True
    if (
        direction == "down"
        and int(di5_function) == FUNIN_NEGATIVE_OVERTRAVEL
        and di_input_active(di_monitor, DI5_BIT, di5_logic)
    ):
        return True
    return False


def position_reference_should_invalidate(previous: Optional[int], current: int) -> bool:
    """True when the encoder count jumped farther than a powered-off drift."""
    if previous is None:
        return False
    return abs(int(current) - int(previous)) > cm_to_pulse(POSITION_REFERENCE_JUMP_CM)


def format_alarm_code(raw: int) -> str:
    """Map P0B-34 to panel-style Er.xxx. 0 means no alarm."""
    value = normalize_alarm_code(raw)
    if value == 0:
        return ""
    return f"Er.{value:X}"


def is_following_error_alarm(raw: int) -> bool:
    return normalize_alarm_code(raw) == ALARM_FOLLOWING_ERROR


def lift_jog_stall_should_stop(
    *,
    pv_active: bool,
    abs_rpm: float,
    started_at: Optional[float],
    stall_since: Optional[float],
    now: float,
    seen_motion: bool = False,
    stall_rpm: float = 8.0,
    hold_s: float = 0.8,
) -> tuple[bool, Optional[float], bool]:
    """Return (should_stop, stall_since, seen_motion).

    Ignore the initial 0 rpm spin-up. Only stop after the motor has actually
    turned, then sat near 0 while the jog is still held.
    """
    if not pv_active or started_at is None:
        return False, None, False
    if abs(abs_rpm) > stall_rpm:
        return False, None, True
    if not seen_motion:
        return False, None, False
    since = now if stall_since is None else stall_since
    if (now - since) >= hold_s:
        return True, since, True
    return False, since, True


class ModbusRtuError(Exception):
    """Base error for Modbus RTU transport or protocol failures."""


class ModbusCrcError(ModbusRtuError):
    pass


class ModbusProtocolError(ModbusRtuError):
    pass


class ModbusExceptionResponse(ModbusRtuError):
    def __init__(self, function: int, code: int) -> None:
        self.function = function
        self.code = code
        super().__init__(f"Modbus exception response: function=0x{function:02X}, code=0x{code:02X}")


class LiftFollowingError(ModbusRtuError):
    """Er.B00 still latched after one aligned reset; do not start motion."""


READ_HOLDING_REGISTERS = 0x03
WRITE_SINGLE_REGISTER = 0x06
WRITE_MULTIPLE_REGISTERS = 0x10


def cm_to_pulse(distance_cm: float) -> int:
    return int(round((distance_cm / CM_PER_REV) * PULSES_PER_REV))


def pulse_to_cm(pulse: int) -> float:
    return round(pulse / PULSES_PER_REV * CM_PER_REV, 2)


def display_pulse(raw_pulse: int, zero_pulse: int = 0) -> int:
    """Encoder pulse relative to the software retracted-zero offset."""
    return int(raw_pulse) - int(zero_pulse or 0)


def drive_pulse(relative_pulse: int, zero_pulse: int = 0) -> int:
    """Absolute drive pulse for a display/relative pulse."""
    return int(relative_pulse) + int(zero_pulse or 0)


def height_cm_from_raw(raw_pulse: int, zero_pulse: int = 0) -> float:
    return pulse_to_cm(display_pulse(raw_pulse, zero_pulse))


def drive_target_from_cm(target_cm: float, zero_pulse: int = 0) -> int:
    return drive_pulse(cm_to_pulse(target_cm), zero_pulse)


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def append_crc(data: bytes) -> bytes:
    crc = crc16_modbus(data)
    return data + struct.pack("<H", crc)


def validate_crc(frame: bytes) -> None:
    if len(frame) < 4:
        raise ModbusProtocolError("Modbus RTU response is too short")
    expected = struct.unpack("<H", frame[-2:])[0]
    actual = crc16_modbus(frame[:-2])
    if actual != expected:
        raise ModbusCrcError(f"CRC mismatch: expected=0x{expected:04X}, actual=0x{actual:04X}")


def _check_exception_response(frame: bytes, expected_function: int) -> None:
    exception_function = expected_function | 0x80
    if expected_function == READ_HOLDING_REGISTERS and len(frame) >= 5 and frame[1] == 0x81:
        validate_crc(frame[:5])
        raise ModbusExceptionResponse(expected_function, frame[2])
    if len(frame) >= 5 and frame[1] == exception_function:
        validate_crc(frame[:5])
        raise ModbusExceptionResponse(expected_function, frame[2])


def build_read_holding_registers(slave_id: int, address: int, count: int) -> bytes:
    _validate_slave_id(slave_id)
    if not 1 <= count <= 125:
        raise ValueError("Modbus register count must be between 1 and 125")
    return append_crc(struct.pack(">BBHH", slave_id, READ_HOLDING_REGISTERS, address, count))


def parse_read_holding_registers(frame: bytes, slave_id: int, count: int) -> List[int]:
    _check_exception_response(frame, READ_HOLDING_REGISTERS)
    validate_crc(frame)
    expected_length = 5 + count * 2
    if len(frame) != expected_length:
        raise ModbusProtocolError(f"Unexpected read response length: expected={expected_length}, got={len(frame)}")
    if frame[0] != slave_id or frame[1] != READ_HOLDING_REGISTERS:
        raise ModbusProtocolError("Unexpected read response slave id or function code")
    if frame[2] != count * 2:
        raise ModbusProtocolError("Unexpected read response byte count")
    return list(struct.unpack(f">{count}H", frame[3:-2]))


def build_write_single_register(slave_id: int, address: int, value: int) -> bytes:
    _validate_slave_id(slave_id)
    _validate_u16(value, "register value")
    return append_crc(struct.pack(">BBHH", slave_id, WRITE_SINGLE_REGISTER, address, value))


def parse_write_single_register(frame: bytes, slave_id: int, address: int, value: int) -> None:
    _check_exception_response(frame, WRITE_SINGLE_REGISTER)
    validate_crc(frame)
    if len(frame) != 8:
        raise ModbusProtocolError(f"Unexpected write single response length: got={len(frame)}")
    response_slave, function, response_address, response_value = struct.unpack(">BBHH", frame[:-2])
    if response_slave != slave_id or function != WRITE_SINGLE_REGISTER:
        raise ModbusProtocolError("Unexpected write single response slave id or function code")
    if response_address != address or response_value != value:
        raise ModbusProtocolError("Write single echo does not match request")


def build_write_multiple_registers(slave_id: int, address: int, values: List[int]) -> bytes:
    _validate_slave_id(slave_id)
    if not 1 <= len(values) <= 123:
        raise ValueError("Modbus multiple-write register count must be between 1 and 123")
    for value in values:
        _validate_u16(value, "register value")
    body = struct.pack(">BBHHB", slave_id, WRITE_MULTIPLE_REGISTERS, address, len(values), len(values) * 2)
    body += struct.pack(f">{len(values)}H", *values)
    return append_crc(body)


def parse_write_multiple_registers(frame: bytes, slave_id: int, address: int, count: int) -> None:
    _check_exception_response(frame, WRITE_MULTIPLE_REGISTERS)
    validate_crc(frame)
    if len(frame) != 8:
        raise ModbusProtocolError(f"Unexpected write multiple response length: got={len(frame)}")
    response_slave, function, response_address, response_count = struct.unpack(">BBHH", frame[:-2])
    if response_slave != slave_id or function != WRITE_MULTIPLE_REGISTERS:
        raise ModbusProtocolError("Unexpected write multiple response slave id or function code")
    if response_address != address or response_count != count:
        raise ModbusProtocolError("Write multiple echo does not match request")


def int32_to_registers_le_word(value: int) -> List[int]:
    if not -(2**31) <= value <= 2**31 - 1:
        raise ValueError("signed 32-bit value out of range")
    packed = struct.pack("<i", value)
    low, high = struct.unpack("<HH", packed)
    return [low, high]


def registers_to_int32_le_word(registers: List[int]) -> int:
    if len(registers) != 2:
        raise ValueError("signed 32-bit values require exactly two registers")
    return struct.unpack("<i", struct.pack("<HH", registers[0], registers[1]))[0]


def _validate_slave_id(slave_id: int) -> None:
    if not 1 <= slave_id <= 247:
        raise ValueError("Modbus slave id must be between 1 and 247")


def _validate_u16(value: int, label: str) -> None:
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"{label} must be between 0 and 65535")


@dataclass
class OukeServoStatus:
    speed_rpm: int
    load_percent: int
    status_word: int
    position_feedback: int
    alarm_code: int = 0
    di_monitor: Optional[int] = None
    di4_function: int = 0
    di4_logic: int = DI_LOGIC_ACTIVE_LOW
    di5_function: int = 0
    di5_logic: int = DI_LOGIC_ACTIVE_LOW


class RtuOverTcpClient:
    def __init__(self, host: str, port: int, *, timeout: float = 1.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._lock = asyncio.Lock()
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None

    async def close(self) -> None:
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is not None:
            writer.close()
            await writer.wait_closed()

    async def transaction(self, request: bytes, expected_length: int) -> bytes:
        async with self._lock:
            try:
                reader, writer = await self._connect()
                await self._drain_reader(reader)
                writer.write(request)
                await asyncio.wait_for(writer.drain(), timeout=self.timeout)
                response = await self._read_response(reader, expected_length)
                await self._drain_reader(reader)
                return response
            except Exception:
                await self.close()
                raise

    async def _drain_reader(self, reader: asyncio.StreamReader) -> None:
        while True:
            try:
                pending = await asyncio.wait_for(reader.read(256), timeout=0.05)
            except asyncio.TimeoutError:
                return
            if not pending:
                return

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._reader is not None and self._writer is not None and not self._writer.is_closing():
            return self._reader, self._writer
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=self.timeout,
        )
        return self._reader, self._writer

    async def _read_response(self, reader: asyncio.StreamReader, expected_length: int) -> bytes:
        header = await asyncio.wait_for(reader.readexactly(2), timeout=self.timeout)
        function = header[1]
        if function & 0x80:
            rest = await asyncio.wait_for(reader.readexactly(3), timeout=self.timeout)
            return header + rest
        if function == READ_HOLDING_REGISTERS:
            byte_count = await asyncio.wait_for(reader.readexactly(1), timeout=self.timeout)
            rest = await asyncio.wait_for(reader.readexactly(byte_count[0] + 2), timeout=self.timeout)
            return header + byte_count + rest
        rest = await asyncio.wait_for(reader.readexactly(expected_length - 2), timeout=self.timeout)
        return header + rest


class OukeServoLiftClient:
    def __init__(self, host: str, port: int, *, slave_id: int = 1, timeout: float = 1.0) -> None:
        self.slave_id = slave_id
        self.transport = RtuOverTcpClient(host, port, timeout=timeout)
        self._modbus_mode_ready = False
        self._pv_ready = False
        self._limit_switch_config: Optional[tuple[int, int, int, int]] = None

    async def close(self) -> None:
        await self.transport.close()

    async def read_registers(self, address: int, count: int) -> List[int]:
        request = build_read_holding_registers(self.slave_id, address, count)
        response = await self.transport.transaction(request, expected_length=5 + count * 2)
        return parse_read_holding_registers(response, self.slave_id, count)

    async def write_register(self, address: int, value: int) -> None:
        request = build_write_single_register(self.slave_id, address, value)
        response = await self.transport.transaction(request, expected_length=8)
        parse_write_single_register(response, self.slave_id, address, value)

    async def write_int32(self, address: int, value: int) -> None:
        await self.write_registers(address, int32_to_registers_le_word(value))

    async def write_registers(self, address: int, values: List[int]) -> None:
        request = build_write_multiple_registers(self.slave_id, address, values)
        response = await self.transport.transaction(request, expected_length=8)
        parse_write_multiple_registers(response, self.slave_id, address, len(values))

    async def force_di_enable(self) -> None:
        await self.write_register(REG_POD_DI_FORCE, POD_DI_FORCE_ON)

    async def set_pp_mode(self) -> None:
        await self.write_register(REG_P10_MODE, P10_MODE_PP)

    async def set_pv_mode(self) -> None:
        await self.write_register(REG_P10_MODE, P10_MODE_PV)

    async def write_relative_position(self, pulse: int) -> None:
        await self.write_int32(REG_P10_POSITION, pulse)

    async def write_pp_speed(self, speed_pr_s: int) -> None:
        await self.write_int32(REG_P10_PP_SPEED, speed_pr_s)

    async def write_pv_speed(self, speed_pr_s: int) -> None:
        await self.write_int32(REG_P10_PV_SPEED, speed_pr_s)

    async def servo_enable(self) -> None:
        await self.write_register(REG_POD_ENABLE, POD_ENABLE)
        self._pv_ready = True

    async def servo_disable(self) -> None:
        await self.write_register(REG_POD_ENABLE, POD_DISABLE)
        self._pv_ready = False

    async def relative_start(self) -> None:
        await self.write_register(REG_POD_CONTROL, POD_CONTROL_REL_START)

    async def absolute_start(self) -> None:
        await self.write_register(REG_POD_CONTROL, POD_CONTROL_ABS_START)

    async def soft_estop(self) -> None:
        await self.write_register(REG_POD_CONTROL, POD_CONTROL_ESTOP)
        await self.servo_disable()
        self._pv_ready = False

    async def reset_control(self) -> None:
        await self.write_register(REG_POD_CONTROL, POD_CONTROL_RESET)

    async def pv_forward(self) -> None:
        await self.write_register(REG_POD_CONTROL, POD_CONTROL_FORWARD)

    async def pv_reverse(self) -> None:
        await self.write_register(REG_POD_CONTROL, POD_CONTROL_REVERSE)

    async def pv_stop(self) -> None:
        await self.write_register(REG_POD_CONTROL, POD_CONTROL_PV_STOP)

    async def read_position_feedback(self) -> int:
        return registers_to_int32_le_word(await self.read_registers(REG_POB_POSITION, 2))

    async def read_alarm_code(self) -> int:
        return (await self.read_registers(REG_POB_ALARM, 1))[0]

    async def fault_reset(self) -> None:
        await self.write_register(REG_POD_FAULT_RESET, POD_FAULT_RESET_ON)

    async def ensure_modbus_mode(self) -> None:
        if self._modbus_mode_ready:
            return
        # P02-00 is already 9 after a successful bus-mode write. Rewriting it is
        # rejected with exception 04 and aborts jog/goto before the control word.
        current = (await self.read_registers(REG_P02_CONTROL_MODE, 1))[0]
        if current != P02_MODE_MODBUS:
            await self.write_register(REG_P02_CONTROL_MODE, P02_MODE_MODBUS)
        self._modbus_mode_ready = True

    async def _read_limit_switch_config(self) -> tuple[int, int, int, int]:
        if self._limit_switch_config is None:
            regs = await self.read_registers(REG_P03_DI4_FUNCTION, 4)
            self._limit_switch_config = (int(regs[0]), int(regs[1]), int(regs[2]), int(regs[3]))
        return self._limit_switch_config

    async def read_status(self) -> OukeServoStatus:
        speed_rpm = normalize_rpm_register((await self.read_registers(REG_POB_RPM, 1))[0])
        load_percent = (await self.read_registers(REG_POB_LOAD, 1))[0]
        status_word = (await self.read_registers(REG_POB_STATUS, 1))[0]
        di_monitor = (await self.read_registers(REG_POB_DI, 1))[0]
        position_feedback = await self.read_position_feedback()
        alarm_code = await self.read_alarm_code()
        di4_function, di4_logic, di5_function, di5_logic = await self._read_limit_switch_config()
        return OukeServoStatus(
            speed_rpm=speed_rpm,
            load_percent=load_percent,
            status_word=status_word,
            position_feedback=position_feedback,
            alarm_code=alarm_code,
            di_monitor=di_monitor,
            di4_function=di4_function,
            di4_logic=di4_logic,
            di5_function=di5_function,
            di5_logic=di5_logic,
        )

    async def prepare_for_mode_change(self) -> int:
        """Stop-time setup: disable first, then write 停机参数, then return.

        Manufacturer: parameters cannot be written while enabled. Do not force
        DI (P0D-17) here — that can SON the drive before P10-03 / P02-00.
        """
        await self.reset_control()
        await self.servo_disable()
        await asyncio.sleep(0.05)
        current = await self.read_position_feedback()
        await self.write_int32(REG_P10_POSITION, current)
        alarm = await self.read_alarm_code()
        if is_following_error_alarm(alarm):
            await self.fault_reset()
        await self.ensure_modbus_mode()
        return current

    async def _confirm_not_following_error(self) -> None:
        alarm = await self.read_alarm_code()
        if is_following_error_alarm(alarm):
            raise LiftFollowingError(
                f"{format_alarm_code(alarm)} still latched after aligned reset; not starting motion"
            )

    async def run_pp_move(self, delta_pulse: int, speed_pr_s: int) -> None:
        await self.prepare_for_mode_change()
        await self.set_pp_mode()
        await self.write_pp_speed(speed_pr_s)
        await self.force_di_enable()
        await self.servo_enable()
        await self._confirm_not_following_error()
        await self.write_relative_position(delta_pulse)
        await self.relative_start()
        self._pv_ready = False

    async def run_pp_move_absolute(self, target_pulse: int, speed_pr_s: int) -> None:
        await self.prepare_for_mode_change()
        await self.set_pp_mode()
        await self.write_pp_speed(speed_pr_s)
        await self.force_di_enable()
        await self.servo_enable()
        await self._confirm_not_following_error()
        await self.write_int32(REG_P10_POSITION, target_pulse)
        await self.absolute_start()
        self._pv_ready = False

    async def run_pv_start(self, direction: str, speed_pr_s: int) -> None:
        if direction not in {"up", "down"}:
            raise ValueError(f"Unsupported PV direction: {direction}")
        if self._pv_ready:
            # Enable still held (including after a 256 stop). Do not 511 / P02-00 again.
            await self.reset_control()
            await self.write_pv_speed(speed_pr_s)
        else:
            await self.prepare_for_mode_change()
            await self.set_pv_mode()
            await self.write_pv_speed(speed_pr_s)
            await self.force_di_enable()
            await self.servo_enable()
        await self._confirm_not_following_error()
        if direction == "up":
            await self.pv_forward()
        else:
            await self.pv_reverse()

    async def run_pv_stop(self) -> None:
        # Jog stop: halt velocity only. Keep enable so the motor holds the pole.
        await self.pv_stop()

    async def run_disable(self) -> None:
        await self.servo_disable()
        self._pv_ready = False


# Backward-compatible alias for older imports during transition.
TsdaLiftClient = OukeServoLiftClient
