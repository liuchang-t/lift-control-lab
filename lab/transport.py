from __future__ import annotations

import asyncio
import struct
import time
from collections import deque
from baseline.lift_modbus import append_crc, validate_crc, int32_to_registers_le_word, registers_to_int32_le_word
from .units import integer
from .registers import RegisterMonitor


class Trace:
    def __init__(self):
        self.items = deque(maxlen=20000)
        self.serial = 0

    def add(self, kind, **fields):
        self.serial += 1
        self.items.append(dict(id=self.serial, at=time.time(), kind=kind, **fields))

    def tail(self, after=0):
        return dict(items=[item for item in self.items if item['id'] > after],
                    cursor=self.serial, capacity=self.items.maxlen,
                    truncated=bool(after and self.items and after < self.items[0]['id']-1))

    def clear(self):
        self.items.clear()
        return dict(cursor=self.serial)


class TcpTransport:
    def __init__(self, config, trace):
        self.config, self.trace = config, trace
        self.lock = asyncio.Lock()
        self.reader = self.writer = None
        self.epoch = 0
        self.registers = RegisterMonitor()
        self.active = True

    async def close(self):
        writer, self.writer, self.reader = self.writer, None, None
        if writer:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), self.config.timeout)
            except Exception:
                pass

    async def shutdown(self):
        self.active = False
        self.epoch += 1
        async with self.lock:
            await self.close()

    async def transaction(self, request, expected_length, before_send=None):
        async with self.lock:
            if not self.active:
                raise ConnectionError("连接已主动断开，请显式连接")
            if before_send:
                before_send()
            try:
                return await asyncio.wait_for(self._exchange(request, expected_length, before_send), self.config.timeout)
            except BaseException as exc:
                self.trace.add("error", error=type(exc).__name__)
                await self.close()
                raise

    async def _exchange(self, request, expected_length, before_send=None):
        if not self.active:
            raise ConnectionError("连接已主动断开，请显式连接")
        if self.writer is None:
            self.reader, self.writer = await asyncio.open_connection(self.config.host, self.config.port)
            self.epoch += 1
        if not self.active:
            raise ConnectionError("连接已主动断开，请显式连接")
        if before_send:
            before_send()
        self.trace.add("tx", frame=request.hex(" "))
        self.writer.write(request)
        await self.writer.drain()
        header = await self.reader.readexactly(2)
        if header[1] & 0x80:
            response = header + await self.reader.readexactly(3)
        elif header[1] == 3:
            size = await self.reader.readexactly(1)
            if size[0] > 250:
                raise ValueError("invalid RTU byte count")
            response = header + size + await self.reader.readexactly(size[0] + 2)
        else:
            response = header + await self.reader.readexactly(expected_length - 2)
        validate_crc(response)
        self.trace.add("rx", frame=response.hex(" "))
        self.registers.observe(request, response, self.epoch)
        # RTU inter-frame silence; no pre/post read that could swallow another response.
        await asyncio.sleep(0.005)
        return response


class Simulator:
    """Register-level test double, not a model of drive safety or stopping distance.

    The synthetic negative-switch release edge is +0.05 cm from physical zero.
    HOME 17 establishes P10-16 there. Neither P0A nor P10 software limits are
    enforced: their real firmware relationship is deliberately unmodelled.
    """
    def __init__(self, config, trace):
        self.config, self.trace = config, trace
        self.epoch = 1
        self.lock = asyncio.Lock()
        self.registers = RegisterMonitor()
        self.active = True
        self.physical = 0.0
        self.counter_offset = 0.0
        self.rpm = 0
        self.enabled = False
        self.jog_active = False
        self.motion = None
        self.home_attained = False
        self.home_started = None
        self.home_phase = None
        self.lower_active = True
        self.last = time.monotonic()
        self.regs = {0x4604:100,0x0D11:0,0x0D12:511,0x0200: 9, 0x1003: 3, 0x0C0D: 0, 0x0C1A: config.low_word_setting,
                     0x0502: config.pulses_per_rev, 0x0306: 1, 0x0307: 0,
                     0x0308: 14, 0x0309: 0, 0x030A: 15, 0x030B: 0, 0x0B22: 0,
                     0x0A28: 2, 0x1023: 17}
        for address,value in {
            0x001F: 131072, 0x0502: config.pulses_per_rev, 0x0507: 131072, 0x0509: 10000,
            0x0A29: 2147483647, 0x0A2B: -2147483648,
            0x1012: -2147483648, 0x1014: 2147483647,
            0x1010: config.home_reference_pulse,
            0x1017: max(config.max_pp_speed,config.max_pv_speed),
            0x1019: 10000, 0x102A: 10000,
            0x101B: 10000, 0x101D: 10000, 0x101F: 10000,
            0x1024: 20000, 0x1026: 10000, 0x1028: 10000,
        }.items():
            low,high=int32_to_registers_le_word(value)
            self.regs[address],self.regs[address+1]=low,high
        self.faults = {"offline": False, "stall": False, "lost_start_ack": False,
                       "lost_stop": 0, "delay_ms": 0, "write_failure": -1,
                       "home_never_complete": False, "home_stale_complete": False,
                       "reset_failure": False, "alarm_persistent": False}

    @property
    def upper_switch(self):
        # Preserve frozen-baseline comparisons; candidate limits lie inside DI.
        extra = 0 if self.config.implementation == 'baseline' else 1.0
        return self.config.pulse(self.config.travel_cm + extra)

    def _i32(self, address):
        return registers_to_int32_le_word([self.regs.get(address, 0), self.regs.get(address + 1, 0)])

    def tick(self):
        now = time.monotonic()
        elapsed, self.last = min(now-self.last, 1.0), now
        speed = 0
        if self.enabled and self.motion and not self.faults["stall"]:
            if self.motion == 'home':
                release=self.config.pulse(.05)
                if self.home_phase == 'seek':
                    speed=-abs(self._i32(0x1024))
                    self.physical=max(0.,self.physical+speed*elapsed)
                    if self.physical <= 0:
                        self.home_phase='release'
                        speed=0
                else:
                    speed=abs(self._i32(0x1026))
                    self.physical=min(float(release),self.physical+speed*elapsed)
                    if self.physical >= release:
                        speed=0
                        if now-self.home_started >= .1 and not self.faults['home_never_complete']:
                            self.counter_offset=self._i32(0x1010)-self.physical
                            self.home_attained=True
                            self.motion=None
                            self.home_phase=None
            elif self.motion in ("up", "down"):
                speed = (self.regs[0x4604]*self.config.pulses_per_rev/60 if self.jog_active else abs(self._i32(0x102A))) * (1 if self.motion == "up" else -1)
            else:
                delta = self._i32(0x100E) - (self.physical + self.counter_offset)
                speed = abs(self._i32(0x1019)) * (1 if delta > 0 else -1)
                if abs(delta) <= abs(speed * elapsed):
                    self.physical += delta
                    self.motion = None
                    speed = 0
            if self.motion != 'home' and self.home_phase is None:
                self.physical += speed * elapsed
            top = self.upper_switch
            if self.physical <= 0 and speed < 0:
                self.physical, speed = 0.0, 0
            if self.physical >= top and speed > 0:
                self.physical, speed = float(top), 0
        self.rpm = integer(self.config.units.rpm(speed))
        raw = integer(self.physical + self.counter_offset)
        low, high = int32_to_registers_le_word(raw)
        self.regs.update({0x0B00: self.rpm & 0xFFFF, 0x0B02: 80 if self.faults["stall"] else 10,
                          0x0B04: (1 if not self.motion else 0) | (8 if self.home_attained else 0),
                          0x0B07: low, 0x0B08: high})
        di = 0x3F
        if self.config.implementation == 'baseline':
            self.lower_active=self.physical <= 1
        elif self.physical <= 1:
            self.lower_active=True
        elif self.physical >= self.config.pulse(.05):
            self.lower_active=False
        if self.lower_active:
            di &= ~(1 << 4)
        if self.physical >= self.upper_switch-1:
            di &= ~(1 << 3)
        self.regs[0x0B03] = di

    def inject(self, name, value=True):
        self.tick()
        if name == "power_cycle":
            self.counter_offset = -self.physical
            self.enabled, self.motion = False, None
            self.home_attained=False
            self.home_phase=None
            self.home_started=None
            self.epoch += 1
        elif name == "alarm":
            self.regs[0x0B22] = int(value)
        elif name == "physical_cm":
            self.physical = self.config.pulse(float(value))
        elif name in self.faults:
            self.faults[name] = value
            if name == 'home_stale_complete':
                self.home_attained=bool(value)
        else:
            raise ValueError("unknown fault")
        self.trace.add("injection", name=name, value=value)

    async def close(self):
        pass

    async def shutdown(self):
        self.active = False
        self.epoch += 1
        async with self.lock:
            await self.close()

    async def transaction(self, request, expected_length, before_send=None):
        async with self.lock:
            if not self.active:
                raise ConnectionError("连接已主动断开，请显式连接")
            if before_send:
                before_send()
            return await self._exchange(request, expected_length)

    async def _exchange(self, request, expected_length):
        self.tick()
        self.trace.add("tx", frame=request.hex(" "))
        if self.faults["delay_ms"]:
            await asyncio.sleep(min(float(self.faults["delay_ms"])/1000, self.config.timeout))
        if self.faults["offline"]:
            raise TimeoutError("simulated offline")
        validate_crc(request)
        slave, fn, address, data = struct.unpack(">BBHH", request[:6])
        if slave != self.config.slave_id:
            raise TimeoutError("wrong slave")
        if fn == 3:
            values = [self.regs.get(address+i, 0) for i in range(data)]
            response = append_crc(bytes([slave, 3, data*2]) + struct.pack(">" + "H"*data, *values))
        elif fn in (6, 16):
            values = [data] if fn == 6 else list(struct.unpack(">" + "H"*data, request[7:-2]))
            if address == int(self.faults["write_failure"]):
                self.faults["write_failure"] = -1
                raise TimeoutError("injected write failure")
            if ((address == 0x0D08 and values[0] in (256,512)) or (address == 0x0D0B and values[0] in (1,65))) and self.faults['lost_stop']:
                self.faults["lost_stop"] -= 1
                raise TimeoutError("stop not delivered")
            if address == 0x0D01 and self.faults['reset_failure']:
                raise TimeoutError('reset not delivered')
            for offset, value in enumerate(values):
                self.regs[address+offset] = value
            if address == 0x0D0B:
                value=values[0]
                if value in (1,65):
                    self.enabled=value==1
                    self.motion=None
                    self.home_phase=None
                    self.jog_active=False
                elif value in (17,33):
                    self.jog_active=True
                    self.enabled=True
                    self.motion='up' if value==17 else 'down'
                    if self.faults['lost_start_ack']:
                        self.faults['lost_start_ack']=False
                        raise TimeoutError('JOG start applied, acknowledgement lost')
            if address == 0x0D12:
                self.enabled = values[0] == 507
                if not self.enabled:
                    self.motion = None
                    self.home_phase=None
            if address == 0x0D01:
                if not self.faults['alarm_persistent']:
                    self.regs[0x0B22] = 0
            if address == 0x0D08:
                self.jog_active=False
                value = values[0]
                if value in (256, 512):
                    self.motion = None
                    self.home_phase=None
                elif value in (16, 32):
                    self.motion = "up" if value == 16 else "down"
                elif value == 3:
                    self.motion = "pp"
                elif value == 128:
                    if self.regs.get(0x1003) != 6 or self.regs.get(0x1023) != 17:
                        self.regs[0x0B22]=0x601
                        self.motion=None
                    else:
                        self.motion='home'
                        self.home_phase='release' if self.lower_active else 'seek'
                        self.home_started=time.monotonic()
                        self.home_attained=bool(self.faults['home_stale_complete'])
                if value in (3, 16, 32, 128) and self.faults["lost_start_ack"]:
                    self.faults["lost_start_ack"] = False
                    raise TimeoutError("start applied, acknowledgement lost")
            response = append_crc(request[:6])
        else:
            response = append_crc(bytes([slave, fn | 128, 1]))
        self.trace.add("rx", frame=response.hex(" "))
        self.registers.observe(request, response, self.epoch)
        await asyncio.sleep(0)
        return response
