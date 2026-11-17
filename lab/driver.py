from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from baseline.lift_modbus import OukeServoLiftClient, normalize_alarm_code, format_alarm_code, di_input_active
from .model import LabError
from .alarms import describe, blocked
from baseline.lift_modbus import registers_to_int32_le_word
from .units import command_units_per_revolution
from .registers import MONITOR_GROUPS, MONITOR_INTERVAL, MONITOR_MAX_AGE, REFERENCE_ADDRESSES


class ParameterChanged(LabError):
    def __init__(self,message,reference_lost=False):
        super().__init__(message)
        self.reference_lost=reference_lost


class Driver:
    def __init__(self, config, transport):
        self.config, self.transport = config, transport
        self.wire = OukeServoLiftClient(config.host, config.port, slave_id=config.slave_id, timeout=config.timeout)
        self.wire.transport = transport
        self._eeprom_value = None
        self._eeprom_at = None
        self._eeprom_epoch = None
        self._eeprom_error = ''
        self.parameters = {}
        self.parameter_words = {}
        self.expected_words = {}
        self.unit_error = ''
        self.monitor_index = 0
        self.monitor_last = 0.
        self.monitor_started = 0.
        self.force_requested = {}
        self.force_observed = None
        self.force_error = ''

    async def read_force_inputs(self, di=None):
        try:
            words=await self.wire.read_registers(0x0D11,2)
            if di is None:
                di=(await self.wire.read_registers(0x0B03,1))[0]
            self.force_observed=dict(selection=words[0],given=words[1],di3=(di>>2)&1,
                                     at=time.monotonic(),epoch=self.transport.epoch)
            self.force_error=''
            return words
        except Exception as exc:
            self.force_error=str(exc) or type(exc).__name__
            self.force_observed=None
            raise

    def force_status(self):
        item=self.force_observed
        valid=bool(item and item['epoch']==self.transport.epoch)
        age=time.monotonic()-item['at'] if valid else None
        fresh=valid and age<=max(self.config.control_latency_s,self.config.poll_s*3)
        requested={str(a):v['value'] for a,v in self.force_requested.items() if v['epoch']==self.transport.epoch}
        return dict(selection=item['selection'] if fresh else None,given=item['given'] if fresh else None,
                    di3=item['di3'] if fresh else None,requested=requested,age_s=age,
                    fresh=fresh,error=self.force_error,completion_confirmed=False)

    async def write_force_input(self,address,value):
        self.force_requested[address]=dict(value=value,epoch=self.transport.epoch)
        self.force_observed=None
        try:
            await self.wire.write_register(address,value)
        except Exception as exc:
            self.force_error=str(exc) or type(exc).__name__
            raise
        words=await self.read_force_inputs()
        actual=words[address-0x0D11]
        self.transport.trace.add('force_input_readback',address=address,requested=value,actual=actual,
                                 di3=self.force_observed['di3'],matched=actual==value)
        if actual!=value:
            raise LabError('P0D-%s 给定读回不一致：下发 %s，读回 %s；未确认使能完成'%
                           (17 if address==0x0D11 else 18,value,actual))

    async def _read_words(self,address,count):
        values=await self.wire.read_registers(address,count)
        now=time.monotonic()
        for offset,value in enumerate(values):
            self.parameter_words[address+offset]=dict(value=value,at=now,epoch=self.transport.epoch)
        return values

    def _word(self,address):
        item=self.parameter_words.get(address)
        if not item or item['epoch']!=self.transport.epoch:
            raise ValueError('单位参数尚未完整读取或连接已变化')
        return item['value']

    def _unsigned32(self,address):
        return self._word(address)+(self._word(address+1)<<16)

    def unit_status(self):
        result=dict(status='unknown',path='unknown',consistent=None,physical_verified=False,
                    mechanical_verified=False,mechanical_cm_per_rev=self.config.cm_per_rev,
                    configured_command_per_rev=self.config.pulses_per_rev,encoder_counts_per_rev=None,
                    pulses_setting=None,pulses_setting_words=None,gear_numerator=None,gear_denominator=None,
                    effective_command_per_rev=None,effective_fraction=None,observed_age_s=None,
                    connection_epoch=self.transport.epoch,issues=[])
        evidence=('P05-02双字按参数范围和地址空档推定，案例单字写入存在证据差异；'
                  '直接值再次上电生效，读回不证明已生效。P05-02=0时N=编码器线数×分母/分子为推导，待固件实机核验。'
                  '机械每转位移来自配置，读回一致不等于有效比例已验证。')
        try:
            if self.unit_error:
                raise ValueError(self.unit_error)
            order=self._word(0x0C1A)
            result['word_order']=order
            result['pulses_setting_words']=[self._word(0x0502),self._word(0x0503)]
            if order!=self.config.low_word_setting or order!=1:
                raise ValueError('P0C-26 不符合已核实的低字优先方式，不能解释双字参数')
            setting=self._unsigned32(0x0502)
            encoder=self._unsigned32(0x001F)
            numerator=self._unsigned32(0x0507)
            denominator=self._unsigned32(0x0509)
            result.update(pulses_setting=setting,encoder_counts_per_rev=encoder,gear_numerator=numerator,
                          gear_denominator=denominator,path='direct' if setting else 'ratio')
            ratio=command_units_per_revolution(setting,encoder,numerator,denominator)
            result['effective_fraction']='%s/%s'%(ratio.numerator,ratio.denominator)
            if ratio.denominator!=1:
                raise ValueError('推导每转指令数不是整数，不能取整后当作有效换算')
            result['effective_command_per_rev']=ratio.numerator
            if ratio.numerator!=self.config.pulses_per_rev:
                raise ValueError('驱动读回每转指令数 %s 与配置 %s 不一致'%(ratio.numerator,self.config.pulses_per_rev))
            result['consistent']=True
            result['status']='simulated_consistent' if self.config.mode=='sim' else 'consistent'
        except ValueError as exc:
            result['issues']=[str(exc)]
            missing='尚未完整读取' in str(exc) or bool(self.unit_error)
            result['status']='unknown' if missing else 'mismatch'
            result['consistent']=None if missing else False
        addresses=[0x0C1A,0x001F,0x0020,0x0502,0x0503,0x0507,0x0508,0x0509,0x050A]
        observations=[self.parameter_words.get(a) for a in addresses]
        if all(observations):
            result['observed_age_s']=max(time.monotonic()-x['at'] for x in observations)
        result['reason']='；'.join(result['issues'])
        result['notice']=((result['reason']+'；') if result['reason'] else '')+evidence
        return result

    async def read_unit_parameters(self):
        self.unit_error=''
        try:
            for address,count in [(0x0C1A,1),(0x001F,2),(0x0502,2),(0x0507,4)]:
                await self._read_words(address,count)
        except Exception as exc:
            self.unit_error='单位参数读取失败：'+str(exc)
        return self.unit_status()

    async def require_units(self):
        result=await self.read_unit_parameters()
        if result['consistent'] is not True:
            raise ParameterChanged(result['reason'] or '单位换算依据未知',True)
        return result

    def invalidate_parameters(self):
        self.parameter_words.clear()
        self.expected_words.clear()
        self.unit_error=''

    def _changed(self,address,values):
        for offset,value in enumerate(values):
            current=address+offset
            if current in self.expected_words and self.expected_words[current]!=value:
                raise ParameterChanged('驱动参数 0x%04X 已改变（期望 %s，读回 %s），请检查后重新允许控制'%
                                       (current,self.expected_words[current],value),current in REFERENCE_ADDRESSES)

    async def _check_block(self,address,count):
        values=await self._read_words(address,count)
        self._changed(address,values)
        return values

    async def establish_parameter_baseline(self):
        values={}
        for address,count in MONITOR_GROUPS:
            words=await self._read_words(address,count)
            values.update({address+i:word for i,word in enumerate(words)})
        changed=any(address in self.expected_words and self.expected_words[address]!=value
                    for address,value in values.items() if address in REFERENCE_ADDRESSES)
        self.expected_words=values
        self.monitor_started=time.monotonic()
        return changed

    async def verify_before_motion(self):
        await self.require_units()
        for address,count in MONITOR_GROUPS:
            await self._check_block(address,count)

    async def _write_checked(self,address,values):
        if len(values)==1:
            await self.wire.write_register(address,values[0])
        else:
            await self.wire.write_registers(address,values)
        actual=await self._read_words(address,len(values))
        if actual!=values:
            raise ParameterChanged('自写参数 0x%04X 读回不一致'%address,address in REFERENCE_ADDRESSES)
        # Only a successful explicit write+readback updates the expectation.
        self.expected_words.update({address+i:value for i,value in enumerate(actual)})

    async def _write_register(self,address,value):
        await self._write_checked(address,[value])

    async def _write_int32(self,address,value):
        from baseline.lift_modbus import int32_to_registers_le_word
        await self._write_checked(address,int32_to_registers_le_word(value))

    async def _ensure_bus_mode(self):
        # Some drives reject rewriting P02-00=9 even when already in bus mode.
        current=await self._read_words(0x0200,1)
        if current != [9]:
            await self._write_register(0x0200,9)

    async def check_active_parameters(self,mode,checkpoint):
        blocks=[(0x1003,1)]
        if mode=='pp':
            blocks.extend([(0x100E,2),(0x1019,2)])
        elif mode=='pv':
            blocks.append((0x102A,2))
        elif mode=='home':
            blocks.append((0x1023,7))
        for address,count in blocks:
            checkpoint()
            await self._check_block(address,count)
            checkpoint()

    async def monitor_parameter_turn(self,compare=False):
        now=time.monotonic()
        if now-self.monitor_last<MONITOR_INTERVAL or self.transport.lock.locked():
            return
        address,count=MONITOR_GROUPS[self.monitor_index]
        self.monitor_index=(self.monitor_index+1)%len(MONITOR_GROUPS)
        self.monitor_last=now
        values=await self._read_words(address,count)
        if compare:
            self._changed(address,values)

    def monitor_status(self):
        now=time.monotonic()
        ages=[now-self.parameter_words[address]['at'] for address in self.expected_words
              if address in self.parameter_words]
        return dict(interval_s=MONITOR_INTERVAL,nominal_cycle_s=MONITOR_INTERVAL*len(MONITOR_GROUPS),
                    max_age_s=max(ages) if ages else None,allowed_age_s=MONITOR_MAX_AGE,
                    notice='活动模式/目标/速度每保护周期核对，其余分块轮转；外部并发写仍有检测延迟，非硬实时互锁。')

    def require_monitor_fresh(self):
        status=self.monitor_status()
        if self.expected_words and (status['max_age_s'] is None or status['max_age_s']>MONITOR_MAX_AGE):
            raise ParameterChanged('运行参数监视超过12秒未完整更新，已停止等待重新检查',True)

    async def read_eeprom_setting(self):
        # Read only: never write P0C-13, including when its value is 1.
        try:
            value = (await self.wire.read_registers(0x0C0D, 1))[0]
        except Exception as exc:
            self._eeprom_value = None
            self._eeprom_error = str(exc) or type(exc).__name__
            raise
        self._eeprom_value = value
        self._eeprom_at = time.monotonic()
        self._eeprom_epoch = self.transport.epoch
        self._eeprom_error = ''
        return value

    def eeprom_status(self):
        age = None if self._eeprom_at is None else time.monotonic()-self._eeprom_at
        fresh = (age is not None and age < max(1.0, self.config.poll_s*3)
                 and self._eeprom_epoch == self.transport.epoch and not self._eeprom_error)
        value = self._eeprom_value if fresh else None
        if value == 1:
            status = 'warning'
            message = 'P0C-13 = 1：EEPROM 更新已开启（P0B/P0D 组除外）。频繁写 P10 参数存在 EEPROM 损耗风险；请先人工核实。工具不会自动修改，当前不允许通过运动预检。'
        elif value == 0:
            status = 'ok'
            message = 'P0C-13 = 0：通信写参不更新 EEPROM。此项检查通过，不代表其他运动条件已满足。'
        elif value is not None:
            status = 'warning'
            message = 'P0C-13 = %s：非预期值（手册定义 0/1），请核实；工具不会自动修改。' % value
        else:
            status = 'unknown'
            message = 'P0C-13 未知：' + ('读取失败：'+self._eeprom_error if self._eeprom_error else '等待读取或观测已过期，请等待重新读取。')
        return dict(value=value, status=status, message=message, observed_age_s=age)

    async def sample(self):
        eeprom = await self.read_eeprom_setting()
        # Re-read limit mapping; do not let device resets preserve a stale cache.
        self.wire._limit_switch_config = None
        value = asdict(await self.wire.read_status())
        value['eeprom_write'] = eeprom
        value['alarm_raw'] = value['alarm_code']
        value['alarm'] = describe(value['alarm_raw'])
        value['alarm_code'] = value['alarm']['code']
        value['alarm_text'] = value['alarm']['text']
        value['home_attained'] = bool(value['status_word'] & 8)
        value['upper_limit'] = value['di4_function'] == 14 and di_input_active(value['di_monitor'], 3, value['di4_logic'])
        value['lower_limit'] = value['di5_function'] == 15 and di_input_active(value['di_monitor'], 4, value['di5_logic'])
        await self.read_force_inputs(value['di_monitor'])
        return value

    async def read_parameters(self):
        result={}
        for key,address in [('soft_limit_enable',0x0A28),('home_method',0x1023)]:
            result[key]=(await self.wire.read_registers(address,1))[0]
        for key,address in [('a_soft_upper',0x0A29),('a_soft_lower',0x0A2B),
                            ('p10_soft_lower',0x1012),('p10_soft_upper',0x1014),
                            ('home_offset',0x1010),('max_velocity',0x1017),
                            ('pp_speed',0x1019),('acceleration',0x101B),('deceleration',0x101D),
                            ('emergency_deceleration',0x101F),('home_speed',0x1024),
                            ('home_creep_speed',0x1026),('home_acceleration',0x1028),('pv_speed',0x102A)]:
            result[key]=registers_to_int32_le_word(await self.wire.read_registers(address,2))
        result.update(observed_at=time.time(),connection_epoch=self.transport.epoch,
                      soft_limit_verification='待实机验证：P0A/P10关联、PV/JOG适用性、HOME例外及停止行为未确认；工具只读，不自动写入。')
        self.parameters=result
        await self.read_unit_parameters()
        return result

    async def preflight(self):
        result = {}
        for key, address in [('bus_mode',0x0200), ('word_order',0x0C1A), ('enable_function',0x0306), ('enable_logic',0x0307)]:
            result[key] = (await self.wire.read_registers(address,1))[0]
        result.update(await self.sample())
        result['parameters']=await self.read_parameters()
        result['unit_verification']=self.unit_status()
        result['reference_changed']=await self.establish_parameter_baseline()
        failures = []
        if result['eeprom_write'] != 0:
            failures.append('P0C-13 必须为 0；工具不会自动修改 EEPROM 设置')
        if result['word_order'] != self.config.low_word_setting:
            failures.append('P0C-26 与已核实的低字优先设置不一致')
        if result['unit_verification']['consistent'] is not True:
            failures.append(result['unit_verification']['reason'] or '单位换算依据未知')
        if result['enable_function'] != 1 or result['enable_logic'] != 0:
            failures.append('507/511 使能方式要求 DI3=功能1、低电平有效')
        if result['di4_function'] != 14 or result['di5_function'] != 15 or result['di4_logic'] not in (0,1) or result['di5_logic'] not in (0,1):
            failures.append('DI4/DI5 限位配置不符合当前驱动适配条件')
        direction='down' if result['alarm_code']==0x950 else 'up'
        reason=blocked(result,direction)
        if reason:
            failures.append(reason)
        if result['parameters']['max_velocity'] <= 0:
            failures.append('驱动器最大速度限制非正值，请人工核实')
        result['failures'] = failures
        return result

    async def start(self, mode, speed, target, direction, checkpoint):
        async def step(operation, *args):
            checkpoint()
            value = await operation(*args)
            checkpoint()
            return value
        await step(self.verify_before_motion)
        sample=await step(self.sample)
        if sample['eeprom_write']!=0:
            raise LabError('P0C-13 非0，拒绝写入运动参数')
        reason=blocked(sample,direction)
        if reason:
            raise LabError(reason)
        # Explicitly prepare every new motion. Never infer PV readiness from SON.
        await step(self.wire.write_register, 0x0D08, 0)
        await step(self.write_force_input, 0x0D12, 511)
        await step(asyncio.sleep, 0.05)
        raw = await step(self.wire.read_position_feedback)
        await step(self._write_int32, 0x100E, raw)
        await step(self._ensure_bus_mode)
        await step(self._write_register, 0x1003, 1 if mode=='pp' else 3)
        await step(self.speed, mode, speed)
        await step(self.write_force_input, 0x0D11, 1)
        await step(self.write_force_input, 0x0D12, 507)
        sample=await step(self.sample)
        reason=blocked(sample,direction)
        if reason:
            raise LabError(reason)
        if mode == 'pp':
            await step(self._write_int32,0x100E,target)
        await step(self.wire.write_register,0x0D08,3 if mode=='pp' else (16 if direction=='up' else 32))

    async def home(self, checkpoint, triggered):
        async def step(operation,*args):
            checkpoint()
            result=await operation(*args)
            checkpoint()
            return result
        await step(self.verify_before_motion)
        sample=await step(self.sample)
        if sample['eeprom_write']!=0:
            raise LabError('P0C-13 非0，拒绝写入回零参数')
        await step(self.wire.write_register,0x0D08,0)
        await step(self.disable)
        await step(asyncio.sleep,.05)
        await step(self._ensure_bus_mode)
        await step(self._write_register,0x1003,6)
        await step(self._write_register,0x1023,17)
        # Keep existing, read-back-validated HOME speeds, acceleration and offset.
        await step(self.write_force_input,0x0D11,1)
        await step(self.write_force_input,0x0D12,507)
        reason=blocked(await step(self.sample),homing=True)
        if reason:
            raise LabError(reason)
        triggered()
        await step(self.wire.write_register,0x0D08,128)

    async def reset_fault(self,checkpoint,expected_raw):
        checkpoint()
        await self.disable()
        checkpoint()
        sample=await self.sample()
        checkpoint()
        if sample['alarm_raw']!=expected_raw or not sample['alarm']['reset_allowed']:
            raise LabError('报警已变化，请重新检查并确认，未发送复位')
        if sample['alarm_code'] in (0x950,0x952) and (sample['upper_limit'] or sample['lower_limit']):
            raise LabError('限位仍有效，未发送复位')
        await self.wire.write_register(0x0D01,1)
        checkpoint()

    async def stop(self, emergency=False):
        await self.wire.write_register(0x0D08,512 if emergency else 256)

    async def disable(self):
        await self.write_force_input(0x0D12,511)

    async def speed(self, mode, speed):
        if await self.read_eeprom_setting()!=0:
            raise LabError('P0C-13 非0，拒绝写入速度参数')
        address=0x1019 if mode=='pp' else 0x102A
        await self._write_int32(address,speed)
        actual=registers_to_int32_le_word(await self._read_words(address,2))
        if actual!=speed:
            raise LabError('速度寄存器读回不一致')
