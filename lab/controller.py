from __future__ import annotations

import asyncio
import time
from collections import deque
from .model import CalibrationStore, SpeedStore, LabError
from .units import finite
from .alarms import blocked, describe, jog_blocked, home_blocked
from .driver import ParameterChanged, StopNotSettled


class MotionCancelled(LabError):
    pass


DIRECT_ACTIONS = {'reset_fault', 'servo_p03_enable', 'servo_p03_disable'}


class Controller:
    def __init__(self, config, driver, trace):
        self.config, self.driver, self.trace = config, driver, trace
        self.store = CalibrationStore(config.state_file)
        self.speed_store = SpeedStore(config)
        self.storage_error = ''
        try:
            self.speeds = self.speed_store.load(config)
        except Exception as exc:
            self.speeds = {'pp':0., 'pv':0.}
            self.storage_error = '速度设置无法恢复，请检查配置文件：'+str(exc)
        self.zero = config.home_reference_pulse
        self.trusted = False
        self.armed = False
        self.epoch = 0
        driver.transport.write_guard = self._write_guard
        self.transport_epoch = driver.transport.epoch
        self.state, self.fault = 'DISARMED', ''
        self.applied_speed = 0
        self.mode = self.direction = self.target_cm = self.intent = None
        self.closed = deque(maxlen=10000)
        self.last_heartbeat = self.started = self.last_motion = 0.
        self.pending_stop = self.emergency = False
        self._stop_sent_epoch = None
        self._global_stop = False
        self.sample = {}
        self.sample_at = self.last_raw_at = self.stop_at = 0.
        self.last_raw = None
        self.progress_raw = None
        self.home_status = 'not_homed'
        self.home_phase = 'idle'
        self.home_triggered = self.home_seen_incomplete = False
        self.home_seen_lower = self.home_seen_release = False
        self.home_stable = 0
        self.home_last_raw = None
        self.home_connection = None
        self.alarms = deque(maxlen=100)
        self._last_alarm = None
        self._sequence = asyncio.Lock()
        self._sample_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._tasks = []
        self._closed = False
        self._observed_motion = False
        self.stop_reason = ''
        self.stop_notice = ''
        self.stop_confirmation = None
        self.jog_no_motion = False
        self.operator_result = None
        self.position_status = 'idle'
        self.position_target_cm = None
        self.position_detail = ''
        self.position_stable = 0
        self.position_last_raw = None
        self.position_connection = None
        self.pp_no_motion = False

    def _clear_position_result(self):
        self.position_status='idle'
        self.position_target_cm=None
        self.position_detail=''
        self.position_stable=0
        self.position_last_raw=None
        self.pp_no_motion=False

    async def start(self):
        if self.config.release_enable_s:
            self.trace.add('configuration_notice',message='release_enable_s仅保留兼容，候选实现不延时自动去使能，以免干预外部运动')
        self._tasks = [asyncio.create_task(self._poll_loop()),asyncio.create_task(self._watchdog())]

    def _write_guard(self):
        issued = self.epoch
        def check():
            if issued != self.epoch:
                raise MotionCancelled('排队写入已被新手动指令或控制意图取消')
        return check

    def _checkpoint(self, epoch):
        if self.pending_stop:
            raise MotionCancelled('停止命令尚未确认应答，请稍后重新按住')
        if not self.armed:
            raise MotionCancelled(self.fault or '请先点击检查 / 恢复')
        if epoch != self.epoch:
            raise MotionCancelled('启动已被停止或新控制意图取消')

    def _retire_intent(self):
        if self.intent:
            self.closed.append(self.intent)
        self.intent = None

    def _stop_intent(self, emergency=False, reason='', global_stop=False):
        self.jog_no_motion=False
        self.pp_no_motion=False
        if self.mode=='pp':
            self.position_status='settling' if reason=='目标到位' else 'stopping'
            self.position_detail='目标到位，正在停止并撤销使能给定' if reason=='目标到位' else ('等待停止并撤销使能给定'+('：'+reason if reason else ''))
        if reason:
            self.stop_reason=reason
        if self.pending_stop and (not emergency or self.emergency) and (not global_stop or self._global_stop):
            return
        self._global_stop = global_stop or emergency or (self.pending_stop and self._global_stop)
        self.stop_confirmation=None
        self.epoch += 1
        self._retire_intent()
        self.pending_stop = True
        self.emergency = self.emergency or emergency
        self.state = 'STOPPING'
        self.stop_notice = '正在发送停止指令并确认静止'
        if self.mode == 'home' and self.home_status != 'complete':
            self.trusted = False
            self.home_status = 'cancelled'
            self.home_phase = 'cancelled'
        if emergency:
            self.armed = False
            self.fault = '软件急停已锁存；检查后重新允许控制'
        self.trace.add('stop_intent',emergency=emergency,epoch=self.epoch,reason=reason)

    def take_manual_control(self):
        """Retire automatic intent without adding any device command."""
        if self.mode=='pp':
            self.position_status='cancelled'
            self.position_detail='定位任务已取消，设备状态见反馈'
        self.epoch += 1
        self._retire_intent()
        self.pending_stop = False
        self.stop_confirmation = None
        self.stop_reason = ''
        self.stop_notice = ''
        self.mode = self.direction = self.target_cm = None
        self.applied_speed = 0
        self.armed = self.trusted = False
        self.home_status = 'not_homed'
        self.home_triggered = False
        self.jog_no_motion = False
        self.manual_active = True
        self.manual_failed = False
        self.state = 'MANUAL'
        self.fault = ''
        # Keep the normal-control emergency latch; raw writes do not re-arm it.
        self.trace.add('manual_takeover',epoch=self.epoch,automatic_stop=False)

    def _fault(self, message, reference_lost=False):
        if getattr(self, 'manual_active', False):
            self.armed = self.trusted = False
            self.fault = str(message)
            self.trace.add('manual_observation_warning',message=str(message))
            return
        was_home = self.mode == 'home'
        if reference_lost or was_home:
            self.trusted = False
        if not self.mode and not getattr(self,'manual_active',False) and not self.pending_stop:
            self.armed=False
            self.fault=str(message)
            self.state='FAULT'
            self.trace.add('observation_warning',message=str(message),reference_lost=reference_lost)
            return
        if not self.pending_stop:
            self._stop_intent()
        self.armed = False
        self.fault = str(message)
        self.state = 'MOTION_UNKNOWN'
        if was_home:
            self.home_status = 'failed'
            self.home_phase = 'failed'

    async def observe(self):
        async with self._sample_lock:
            data = await self.driver.sample()
            now = time.monotonic()
            connection = self.driver.transport.epoch
            if connection != self.transport_epoch:
                self.transport_epoch = connection
                if self.sample_at:
                    self._fault('驱动连接周期变化，必须重新回原点',True)
            raw = data['position_feedback']
            self._observed_motion=abs(data['speed_rpm'])>1 or (self.last_raw is not None and abs(raw-self.last_raw)>2)
            if self.trusted and self.mode not in (None, 'pv') and self.last_raw is not None:
                elapsed = max(0,now-self.last_raw_at)
                allowed = self.config.pulse(2)+max(self.config.max_pp_speed,self.config.max_pv_speed)*elapsed*1.5
                if abs(raw-self.last_raw) > allowed:
                    self._fault('位置计数跳变超出运动包络',True)
            alarm = data['alarm']
            if alarm['reference_lost']:
                self.trusted = False
            if alarm['raw_code'] != self._last_alarm:
                self._last_alarm = alarm['raw_code']
                event=dict(alarm,observed_at=time.time())
                self.alarms.append(event)
                self.trace.add('alarm_changed',**event)
            if self.mode=='home' and self.home_triggered and not data['home_attained']:
                self.home_seen_incomplete=True
            if self.mode=='home' and self.home_triggered and data['lower_limit'] and not data['upper_limit']:
                self.home_seen_lower=True
                self.home_phase='leaving_lower'
            if self.mode=='home' and self.home_triggered and self.home_seen_lower and not data['lower_limit']:
                self.home_seen_release=True
                self.home_phase='settling'
            self.last_raw,self.last_raw_at=raw,now
            self.sample,self.sample_at=data,now
            return data

    async def arm(self, hardware_confirmed=False):
        async with self._sequence:
            if self.pending_stop or self.mode is not None:
                raise LabError('运动中或停止未确认，不能允许控制')
            self.armed=False
            self.epoch+=1
            issued=self.epoch
            self.state='DISARMED'
            if self.storage_error:
                raise LabError(self.storage_error)
            for mode in ('pp','pv'):
                self.config.speed_pr(self.speeds[mode],mode)
            if self.config.mode=='real':
                if not hardware_confirmed:
                    raise LabError('请先核实独立硬急停、物理限位、机械自锁、单位与方向')
            report=await self.driver.preflight()
            if report.get('reference_changed'):
                self.trusted=False
                self.home_status='not_homed'
            if report['failures']:
                raise LabError('；'.join(report['failures']))
            await self.observe()
            if issued!=self.epoch or self.pending_stop:
                raise LabError('允许控制已被停止/连接变化取消')
            self.armed,self.fault,self.emergency=True,'',False

            self.state='IDLE'
            self.trace.add('armed',position_trusted=self.trusted)
            return report

    async def _confirm_stop(self):
        async with self._stop_lock:
            if not self.pending_stop:
                return True
            issued=self.epoch
            def checkpoint():
                if issued != self.epoch or not self.pending_stop:
                    raise MotionCancelled('旧停止请求已被新手动指令或控制意图取消')
            try:
                if self._stop_sent_epoch!=issued:
                    if self._global_stop:
                        await self.driver.stop(self.emergency, checkpoint, global_stop=True)
                    else:
                        await self.driver.stop(self.emergency, checkpoint)
                    checkpoint()
                    self._stop_sent_epoch=issued
                    self.stop_notice='停止指令已应答，正在等待减速停止及失能收尾'
            except MotionCancelled:
                return not self.pending_stop
            ack_only=self.driver.jog_context and not self._global_stop and self.sample.get('alarm_code')!=0x601
            if not ack_only:
                first=await self.observe()
                await asyncio.sleep(.06)
                second=await self.observe()
                if abs(first['speed_rpm'])>1 or abs(second['speed_rpm'])>1 or abs(first['position_feedback']-second['position_feedback'])>20:
                    return False
            if issued!=self.epoch:
                return False
            defer_home_fault=(not ack_only and (first['alarm_code']==0x601 or second['alarm_code']==0x601))
            if not ack_only and not defer_home_fault:
                try:
                    cleaned=await self.driver.release_bus_enable(checkpoint)
                    checkpoint()
                    defer_home_fault=cleaned is False
                except StopNotSettled:
                    # Fresh feedback says not settled; the STOP ACK remains valid.
                    raise
                except Exception:
                    # Lost cleanup ACK is a stop uncertainty, not just a DI problem.
                    # The next attempt must send STOP again before feedback/cleanup.
                    self._stop_sent_epoch=None
                    raise
            if defer_home_fault:
                self.armed=self.trusted=False
                self.fault='Er.601 回原点失败；已确认静止。先失能，再点击故障复位'+('；软件急停仍锁存' if self.emergency else '')
                self.trace.add('home_fault_cleanup_deferred',code=0x601,automatic_reset=False)
            if self.mode=='pp' and defer_home_fault:
                self.position_status='stopped'
                self.position_detail='已静止；601报警保留，失能收尾未执行'
            if self.mode=='pp' and not defer_home_fault and not self.fault and self.position_status=='settling' and self.stop_reason=='目标到位':
                self.position_status='complete'
                self.position_detail='定位完成，收尾已结束'
            elif self.mode=='pp' and not defer_home_fault:
                self.position_status='stopped'
                self.position_detail='停止已确认，收尾已结束'
            self.stop_confirmation='ack' if ack_only else 'feedback'
            self.pending_stop=False
            self.stop_notice=''
            self.mode=self.direction=self.target_cm=None
            self.applied_speed=0
            self.stop_at=time.monotonic()
            self.state='FAULT' if self.fault else ('IDLE' if self.armed else 'DISARMED')
            self.trace.add('stop_acknowledged' if ack_only else 'stop_confirmed')
            return True

    async def stop(self, emergency=False, global_stop=False):
        self._stop_intent(emergency, global_stop=global_stop)
        return await self._wait_for_stop()

    def _stop_wait_error(self, exc):
        if isinstance(exc, StopNotSettled):
            self.stop_notice=str(exc)
            self.state='STOPPING'
            if self.mode=='pp':
                self.position_detail=str(exc)
            return
        self.fault='停止待确认：'+str(exc)
        self.armed=False
        if not isinstance(exc, ParameterChanged) or exc.reference_lost:
            self.trusted=False
        self.state='MOTION_UNKNOWN'
        self.stop_notice=self.fault

    async def _wait_for_stop(self):
        # HTTP wait budget, not a physical deceleration or reference-loss limit.
        deadline=time.monotonic()+self.config.stop_timeout_s
        while time.monotonic()<deadline:
            issued=self.epoch
            try:
                if await self._confirm_stop():
                    return self.snapshot()
            except Exception as exc:
                if issued!=self.epoch or not self.pending_stop:
                    return self.snapshot()
                self._stop_wait_error(exc)
            await asyncio.sleep(.05)
        if (self.pending_stop and self.state=='STOPPING' and self._stop_sent_epoch==self.epoch
                and self.sample_at and time.monotonic()-self.sample_at<=self.config.control_latency_s):
            self.trace.add('stop_pending',notice=self.stop_notice)
            return self.snapshot()
        raise LabError('停止尚未确认，后台仍重试；请核对现场运动及通信状态',503)

    async def operator_write(self, action, address, value, read_address):
        # A button is an explicit command, not an automatic recovery program.
        # Retire queued starts/heartbeats before waiting for their sequence lock.
        was_armed=self.armed
        was_trusted=self.trusted
        was_home=self.mode=='home'
        was_active=bool(self.mode or self.pending_stop or getattr(self,'manual_active',False))
        previous_home=self.home_status
        previous_fault=self.fault
        self.take_manual_control()
        self.driver.bus_enable_owned=False
        issued=self.epoch
        connection=self.driver.transport.epoch
        result=dict(action=action,address=address,value=value,write_ack=False,
                    read_address=read_address,read_value=None,read_error='',at=time.time())
        self.operator_result=result
        def checkpoint():
            if issued!=self.epoch or connection!=self.driver.transport.epoch:
                raise MotionCancelled('人工命令已被后续操作或连接变化取消')
        try:
            async with self._sequence:
                checkpoint()
                await self.driver.wire.write_register(address,value)
                checkpoint()
                result['write_ack']=True
                try:
                    words=await self.driver._read_words(read_address,1)
                    checkpoint()
                    result['read_value']=words[0]
                    if action=='reset_fault':
                        alarm=describe(words[0])
                        result['alarm']=alarm
                        result['cleared']=words[0]==0
                        self.sample.update(alarm=alarm,alarm_raw=words[0],alarm_code=alarm['code'],alarm_text=alarm['text'])
                        if alarm['reference_lost']:
                            was_trusted=False
                    else:
                        result['matched']=words[0]==value
                        self.sample['di3_logic']=words[0]
                except MotionCancelled:
                    raise
                except Exception as exc:
                    result['read_error']=str(exc) or type(exc).__name__
                self.trace.add('operator_write_result',result=dict(result))
        except MotionCancelled:
            raise
        except Exception as exc:
            result['write_error']=str(exc) or type(exc).__name__
            self.trace.add('operator_write_result',result=dict(result))
            raise LabError('写应答未确认：'+result['write_error'],503)
        finally:
            if issued==self.epoch:
                self.manual_active=False
                self.manual_failed=False
                self.armed=was_armed and not was_active and not self.emergency
                self.trusted=was_trusted and not was_home
                self.home_status=previous_home if not was_home else 'cancelled'
                if was_home:
                    self.home_phase='idle'
                self.fault=previous_fault if self.emergency else ''
                self.state='IDLE' if self.armed else 'DISARMED'
        return self.snapshot()

    async def reset_fault(self, command):
        return await self.operator_write('reset_fault',0x0D01,1,0x0B22)

    def _speed_cap(self, mode):
        if mode == 'pv':
            return self.config.max_pv_speed
        return min(getattr(self.config,'max_'+mode+'_speed'),self.driver.parameters.get('max_velocity',0))

    async def _set_speed(self, command, issued):
        mode=command.get('mode')
        if mode not in ('pp','pv') or 'speed_cm_s' not in command:
            raise LabError('请明确指定 mode=pp/pv 和 speed_cm_s；旧共用比例接口不再接受',400)
        try:
            value=finite(command['speed_cm_s'])
            pr=self.config.speed_pr(value,mode)
        except (ValueError,TypeError) as exc:
            raise LabError(str(exc),400)
        if self.storage_error:
            raise LabError(self.storage_error)
        if mode=='pv' and pr:
            self.config.units.jog_rpm(value)
        if pr>self._speed_cap(mode):
            raise LabError('速度超过驱动器读回的最大速度限制')
        if value==0 and self.mode==mode:
            # A disk error must never defeat the user's zero-speed stop intent.
            await self.stop()
            issued=self.epoch
        desired=dict(self.speeds,**{mode:value})
        await asyncio.get_running_loop().run_in_executor(None,self.speed_store.save,desired)
        self.speeds=desired
        self._checkpoint(issued)
        if self.mode==mode:
            if not pr:
                return await self.stop()
            try:
                data=await self.observe()
                self._checkpoint(issued)
                reason=jog_blocked(data,self.direction) if self.mode=='pv' else blocked(data,self.direction)
                if reason:
                    raise LabError(reason)
                applied=pr
                if not applied:
                    return await self.stop()
                await self.driver.speed(mode,applied)
                self._checkpoint(issued)
                self.applied_speed=applied
            except MotionCancelled:
                raise
            except Exception as exc:
                self._fault('速度下发未确认：'+str(exc))
                raise LabError(self.fault,503)
        self.trace.add('speed_saved',mode=mode,cm_s=value)
        return self.snapshot()

    async def _home(self, command, issued):
        if command.get('confirm_homing') is not True:
            raise LabError('请确认回零路径与限位有效，HOME将搜索负限位',400)
        if self.mode is not None:
            raise LabError('请先停止当前动作再回原点')
        data=await self.observe()
        self._checkpoint(issued)
        reason=home_blocked(data)
        if reason:
            raise LabError(reason)
        params=self.config.home_parameters(await self.driver.read_parameters())
        self._checkpoint(issued)
        if params['home_offset']!=self.config.home_reference_pulse:
            raise LabError('驱动原点偏移与已确认配置不一致，不自动改写或采用')
        if any(params[key]<=0 for key in ('home_speed','home_creep_speed','home_acceleration')):
            raise LabError('HOME速度或加减速参数无效')
        if max(params['home_speed'],params['home_creep_speed'])>params['max_velocity']:
            raise LabError('HOME速度超过驱动最大速度限制')
        self._clear_position_result()
        self.trusted=False
        self.mode,self.direction,self.target_cm='home','down',None
        self.home_status='starting'
        self.home_phase='preparing'
        self.home_triggered=self.home_seen_incomplete=False
        self.home_seen_lower=self.home_seen_release=False
        self.home_stable=0
        self.home_last_raw=None
        self.home_connection=self.driver.transport.epoch
        self.started=time.monotonic()
        self.last_motion=self.started
        self.progress_raw=data['position_feedback']
        self.state='STARTING'
        def triggered():
            self.home_triggered=True
            self.home_seen_lower=self.driver.home_start_sample['lower_limit']
            self.home_phase='leaving_lower' if self.home_seen_lower else 'seeking_lower'
            self.started=self.last_motion=time.monotonic()
        try:
            await self.driver.home(lambda:self._checkpoint(issued),triggered)
            self._checkpoint(issued)
            self.state='HOMING'
            self.home_status='homing'
            self.trace.add('home_started',method=17,parameters=params)
        except MotionCancelled:
            raise
        except Exception as exc:
            self._fault('回零启动未确认：'+str(exc),True)
            raise LabError(self.fault,503)
        return self.snapshot()

    async def set_servo_given(self, enabled):
        # Explicit register controls supersede the automatic cleanup intent.
        # Relinquishing ownership does not assert that the drive is disabled.
        was_trusted=self.trusted
        was_home=self.mode=='home'
        previous_home=self.home_status
        self.take_manual_control()
        self.driver.bus_enable_owned=False
        issued=self.epoch
        connection=self.driver.transport.epoch
        success=False
        def checkpoint():
            if issued!=self.epoch or connection!=self.driver.transport.epoch:
                raise MotionCancelled('使能给定操作已被停止或连接变化取消')
        try:
            async with self._sequence:
                checkpoint()
                # Send only STOP, never the previous PP/HOME disable program.
                await self.driver.stop(False,checkpoint,global_stop=True)
                checkpoint()
                deadline=time.monotonic()+self.config.stop_timeout_s
                first=await self.observe()
                checkpoint()
                while True:
                    remaining=deadline-time.monotonic()
                    if remaining<=0:
                        raise LabError('未确认静止，尚未切换使能给定')
                    await asyncio.sleep(min(.06,remaining))
                    checkpoint()
                    second=await self.observe()
                    checkpoint()
                    if (abs(first['speed_rpm'])<=1 and abs(second['speed_rpm'])<=1 and
                            abs(first['position_feedback']-second['position_feedback'])<=20):
                        break
                    first=second
                self.stop_confirmation='feedback'
                self.trace.add('servo_stationary_confirmed',enabled=enabled)
                await self.driver.set_force_servo_given(enabled,checkpoint)
                checkpoint()
                self.driver.jog_context=False
                self.driver.jog_running=False
                self.trusted=was_trusted and not was_home and not self.sample.get('alarm',{}).get('reference_lost',False)
                self.home_status=previous_home if not was_home else 'cancelled'
                if was_home:
                    self.home_phase='idle'
                success=True
                self.trace.add('servo_given',enabled=enabled,completion_confirmed=False)
        except MotionCancelled:
            raise
        except Exception as exc:
            if issued==self.epoch:
                self.fault='使能给定操作未确认：'+(str(exc) or type(exc).__name__)
                self.trace.add('servo_given_failed',enabled=enabled,error=self.fault)
            raise LabError(str(exc) or '使能给定操作未确认',503)
        finally:
            if issued==self.epoch:
                self.armed=False
                self.pending_stop=False
                self.manual_active=not success
                self.manual_failed=not success
                self.state='DISARMED' if success else 'MANUAL_LOCKED'
                if success:
                    self.fault='软件急停已锁存；检查后重新允许控制' if self.emergency else ''
        return self.snapshot()

    async def command(self, command):
        action=command.get('action')
        if action in ('servo_p03_enable','servo_p03_disable'):
            return await self.operator_write(action,0x0307,1 if action=='servo_p03_enable' else 0,0x0307)
        if action in ('servo_enable','servo_disable'):
            return await self.set_servo_given(action=='servo_enable')
        if action in ('stop','estop'):
            cancelled=command.get('intent_id')
            if isinstance(cancelled,str) and cancelled and action=='stop':
                if cancelled in self.closed or (self.intent and cancelled!=self.intent):
                    if cancelled not in self.closed:
                        self.closed.append(cancelled)
                    self.trace.add('obsolete_stop_ignored',intent=cancelled)
                    return self.snapshot()
                self.closed.append(cancelled)
            return await self.stop(action=='estop',global_stop=action=='estop' or not cancelled)
        if action=='reset_fault':
            return await self.reset_fault(command)
        if action=='heartbeat':
            if not self.armed or self.pending_stop or not self.intent or command.get('intent_id')!=self.intent or self.mode!='pv':
                raise LabError(self.fault or self.stop_reason or '当前点动已结束，保活不会重新启动运动')
            self.last_heartbeat=time.monotonic()
            return self.snapshot()
        issued=self.epoch
        async with self._sequence:
            self._checkpoint(issued)
            if action=='set_speed':
                return await self._set_speed(command,issued)
            if action=='set_retracted_zero':
                raise LabError('普通人工设零已停用；请使用模式17回原点建立参考',400)
            if action=='home':
                return await self._home(command,issued)
            if action not in ('up','down','goto','max','min'):
                raise LabError('未知升降动作',400)
            if self.mode=='home':
                raise LabError('回零进行中，普通运动被禁止；可执行停止')
            if not self.trusted and action not in ('up','down'):
                raise LabError('位置参考未可信，请先完成模式17回原点')
            intent=command.get('intent_id')
            if action in ('up','down'):
                if not isinstance(intent,str) or not 1<=len(intent)<=128:
                    raise LabError('新点动必须带唯一 intent_id',400)
                if intent in self.closed:
                    raise LabError('已结束的运动意图，拒绝重启')
                if self.mode=='pv' and self.intent==intent and self.direction==action:
                    self.last_heartbeat=time.monotonic()
                    return self.snapshot()
            if self.mode:
                self._stop_intent()
                issued=self.epoch
                if not await self._confirm_stop():
                    self._fault('切换动作前未确认停止')
                    raise LabError('切换动作前未确认停止')
                self._checkpoint(issued)
            data=await self.observe()
            self._checkpoint(issued)
            if not self.trusted and action not in ('up','down'):
                raise LabError('新反馈使位置参考失效，请重新回原点')
            current=self.config.cm(data['position_feedback']-self.zero)
            mode='pv' if action in ('up','down') else 'pp'
            cm=target=None
            if mode=='pp':
                if action=='min':
                    cm=self.config.soft_lower_cm
                elif action=='max':
                    cm=self.config.soft_upper_cm
                elif 'target_cm' in command:
                    cm=finite(command['target_cm'])
                else:
                    cm=self.config.units.percent_to_cm(command.get('height',-1),self.config.travel_cm)
                if not self.config.soft_lower_cm<=cm<=self.config.soft_upper_cm:
                    raise LabError('目标超出软件上下限',400)
                target=self.config.units.target(cm,self.zero)
                delta=target-data['position_feedback']
                if abs(delta)<=self.config.pulse(self.config.pp_tolerance_cm) and abs(data['speed_rpm'])<=1:
                    second=await self.observe()
                    self._checkpoint(issued)
                    if not self.trusted:
                        raise LabError('位置参考已失效，请重新回零')
                    if (abs(second['speed_rpm'])<=1 and
                        abs(second['position_feedback']-data['position_feedback'])<=20 and
                        abs(target-second['position_feedback'])<=self.config.pulse(self.config.pp_tolerance_cm)):
                        self.target_cm=self.position_target_cm=cm
                        self.position_status='already_at_target'
                        self.position_detail='当前位置已在目标范围，未启动运动'
                        return self.snapshot()
                    data=second
                    delta=target-data['position_feedback']
                direction='up' if delta>0 else 'down'
            else:
                direction=action
            reason=jog_blocked(data,direction) if mode=='pv' else blocked(data,direction)
            if reason:
                raise LabError(reason)
            # A trusted reference permits PP from outside to any in-range target.
            outside=current < self.config.soft_lower_cm-.001 or current > self.config.soft_upper_cm+.001
            if mode=='pp' and not outside and ((current>=self.config.soft_upper_cm and direction=='up') or (current<=self.config.soft_lower_cm and direction=='down')):
                raise LabError('软件行程边界禁止该方向')
            speed=self.config.speed_pr(self.speeds[mode],mode)
            if speed>self._speed_cap(mode):
                raise LabError('期望速度超过驱动器最大速度限制')
            if mode=='pv' and speed:
                self.config.units.jog_rpm(self.speeds['pv'])
            if not speed:
                raise LabError('速度为0或剩余距离不足以安全点动，不启动运动')
            if mode=='pp':
                self.position_status='preparing'
                self.position_target_cm=cm
                self.position_detail='准备目标、速度及使能给定'
                self.position_stable=0
                self.position_last_raw=None
                self.position_connection=self.driver.transport.epoch
                self.pp_no_motion=False
            else:
                self._clear_position_result()
            self.state='STARTING'
            self.stop_confirmation=None
            self.jog_no_motion=False
            self.stop_reason=''
            self.mode,self.direction,self.target_cm,self.intent=mode,direction,cm,intent
            self.last_heartbeat=self.started=self.last_motion=time.monotonic()
            self.progress_raw=data['position_feedback']
            try:
                await self.driver.start(mode,speed,target,direction,lambda:self._checkpoint(issued))
                self._checkpoint(issued)
                self.applied_speed=speed
                self.state='MOVING'
                if mode=='pp':
                    self.position_status='moving'
                    self.position_detail='绝对定位启动已应答'
                self.started=self.last_motion=time.monotonic()
                if mode=='pv':
                    # Preparation/readback time is not motion time. A fresh
                    # stop epoch was checked above, so a released hold cannot
                    # be revived by granting the first actual-motion window.
                    self.last_heartbeat=max(self.last_heartbeat,self.started)
                self.trace.add('started',mode=mode,direction=direction,target_cm=cm,intent=intent)
            except MotionCancelled:
                raise
            except Exception as exc:
                self._fault('启动未确认：'+str(exc),getattr(exc,'reference_lost',False))
                raise LabError(self.fault,503)
            return self.snapshot()

    async def _monitor_home(self,data):
        if data['alarm_code']==0x601:
            self._fault('回原点失败；保留601，按需人工复位',True)
            return
        reason=home_blocked(data)
        if reason:
            self._fault('回零中断：'+reason,True)
            return
        now=time.monotonic()
        stalled=self._stalled(data,now)
        reversal_budget=self.driver.parameters.get('home_speed',0)/max(1,self.driver.parameters.get('home_acceleration',1))+self.config.stall_s
        if not data['home_attained'] and stalled and now-self.last_motion>max(self.config.stall_s,reversal_budget):
            self._fault('回零启动无运动或堵转，请检查机构与负限位',True)
            return
        if now-self.started>self.config.home_timeout_s:
            self._fault('回零超时；请检查负限位、方向及速度',True)
            return
        if not self.home_seen_incomplete and now-self.started>self.config.home_start_timeout_s:
            self._fault('未观察到本次回零未完成状态，不能采用旧完成位',True)
            return
        if self.driver.force_status()['selection']!=0 or data.get('di3_logic')!=1 or data.get('di3_function')!=1 or not (data['di_monitor']&4):
            self._fault('HOME使能给定或物理DI配置改变',True)
            return
        if not self.home_seen_incomplete or not data['home_attained']:
            self.home_stable=0
            return
        if not self.home_seen_lower or not self.home_seen_release:
            self._fault('未观察到本次负限位触发和释放，不能建立参考',True)
            return
        if data['lower_limit'] or data['upper_limit']:
            self._fault('回零完成但限位仍有效，不能建立参考',True)
            return
        raw=data['position_feedback']
        if abs(raw-self.config.home_reference_pulse)>self.config.pulse(self.config.home_tolerance_cm):
            self._fault('回零完成位置与确认的原点偏移不符；不以最终反馈任意重置零点',True)
            return
        stable=abs(data['speed_rpm'])<=1 and self.home_last_raw is not None and abs(raw-self.home_last_raw)<=20
        self.home_last_raw=raw
        self.home_stable=self.home_stable+1 if stable else 0
        if self.home_stable>=2 and self.home_connection==self.driver.transport.epoch and self.state=='HOMING':
            issued=self.epoch
            await asyncio.get_running_loop().run_in_executor(None,self.store.save,self.config.home_reference_pulse)
            if issued!=self.epoch or self.pending_stop or self.state!='HOMING':
                return
            self.zero=self.config.home_reference_pulse
            self.trusted=True
            self.home_status='complete'
            self.home_phase='complete'
            self.home_triggered=False
            self._stop_intent()
            self.trace.add('home_confirmed',reference_pulse=self.zero,final_raw=raw)

    async def _monitor_motion(self,data):
        reason=jog_blocked(data,self.direction) if self.mode=='pv' else blocked(data,self.direction)
        if reason:
            if self.mode=='pv' and reason=='物理限位禁止该方向':
                self._stop_intent(reason=reason)
            else:
                self._fault(reason,data['alarm']['reference_lost'])
            return
        if self.mode=='pv':
            if self.driver.force_status()['selection'] != 0:
                self._fault('强制DI已改变，物理限位不可依赖')
                return
            no_motion=self._stalled(data,time.monotonic())
            if no_motion!=self.jog_no_motion:
                self.trace.add('jog_motion_observation',motion_detected=not no_motion)
            self.jog_no_motion=no_motion
            return
        height=self.config.cm(data['position_feedback']-self.zero)
        lower,upper=self.config.soft_lower_cm,self.config.soft_upper_cm
        if (self.direction=='up' and height>upper+self.config.home_tolerance_cm) or (self.direction=='down' and height<lower-self.config.home_tolerance_cm):
            self._fault('反馈超出软件行程保护范围',True)
            return
        if self.mode=='pp':
            if not self.trusted:
                self._fault('定位参考已失效',True)
                return
            if self.driver.force_status()['selection']!=0 or data.get('di3_function')!=1 or data.get('di3_logic')!=1 or not (data['di_monitor']&4):
                self._fault('PP使能给定或物理DI配置改变')
                return
            raw=data['position_feedback']
            in_target=abs(height-self.target_cm)<=self.config.pp_tolerance_cm
            stationary=abs(data['speed_rpm'])<=1
            stable=stationary and self.position_last_raw is not None and abs(raw-self.position_last_raw)<=20
            self.position_last_raw=raw
            reached=bool(data['status_word'] & 1)
            self.position_stable=self.position_stable+1 if in_target and stable and reached else 0
            self.pp_no_motion=self._stalled(data,time.monotonic()) and not (in_target and stationary)
            if in_target and stationary:
                self.position_status='settling'
                self.position_detail='核对位置稳定' if reached else '已进入目标范围，等待驱动到位信号'
            else:
                self.position_status='moving'
                self.position_detail='定位指令已应答，未检测到运动' if self.pp_no_motion else '正在定位'
            if self.position_stable>=2 and self.position_connection==self.driver.transport.epoch:
                self._stop_intent(reason='目标到位')
                self.trace.add('position_reached',target_cm=self.position_target_cm,actual_cm=height,tolerance_cm=self.config.pp_tolerance_cm)
            return

    def _stalled(self,data,now):
        raw=data['position_feedback']
        if abs(data['speed_rpm'])>1 or self.progress_raw is None or abs(raw-self.progress_raw)>=2:
            self.progress_raw=raw
            self.last_motion=now
        return now-self.started>self.config.startup_grace_s and now-self.last_motion>self.config.stall_s

    async def _poll_loop(self):
        while not self._closed:
            try:
                data=await self.observe()
                if data['eeprom_write']!=0 and self.armed:
                    if self.mode:
                        self._fault('P0C-13 已变为非0，停止并禁止继续写运动参数')
                    else:
                        self.armed=False
                        self.fault='P0C-13 非0，请人工核实；工具不会自动修改'
                        self.state='FAULT'
                if self.state=='HOMING':
                    await self._monitor_home(data)
                elif self.state=='MOVING':
                    await self._monitor_motion(data)
                if not self.pending_stop and not getattr(self,'manual_active',False) and not self._sequence.locked():
                    async with self._sequence:
                        issued=self.epoch
                        if self.mode and self.state in ('MOVING','HOMING'):
                            await self.driver.check_active_parameters(self.mode,lambda:self._checkpoint(issued))
                        if self.mode=='pv' and self.state=='MOVING':
                            if time.monotonic()-self.last_heartbeat >= self.config.deadman_s:
                                self._stop_intent(reason='点动保活超时')
                            else:
                                await self.driver.renew_jog(self.direction,lambda:self._checkpoint(issued))
                        if issued==self.epoch and not self.pending_stop:
                            await self.driver.monitor_parameter_turn(compare=bool(self.driver.expected_words) and self.mode not in ('pv','home'),mode=self.mode)
                        if self.mode=='pp' and self.state in ('MOVING','HOMING'):
                            self.driver.require_monitor_fresh()
            except asyncio.CancelledError:
                raise
            except MotionCancelled:
                pass
            except ParameterChanged as exc:
                self._fault(str(exc),exc.reference_lost)
            except Exception as exc:
                self._fault('状态采集/保护失败：'+str(exc),True)
            await asyncio.sleep(self.config.poll_s)

    async def _watchdog(self):
        while not self._closed:
            now=time.monotonic()
            if self.mode=='pv' and self.state=='MOVING' and now-self.last_heartbeat>=self.config.deadman_s:
                self._stop_intent(reason='点动保活超时：超过 %.2g 秒未收到心跳，已请求停止'%self.config.deadman_s)
            if self.mode and self.state in ('MOVING','HOMING','STARTING') and self.sample_at and now-self.sample_at>self.config.control_latency_s:
                self._fault('位置反馈超过保护延迟预算，运动状态未知',True)
            if self.mode=='home' and self.state in ('HOMING','STARTING') and now-self.started>self.config.home_timeout_s:
                self._fault('回零超时',True)
            if self.pending_stop:
                issued=self.epoch
                try:
                    await self._confirm_stop()
                except Exception as exc:
                    if issued==self.epoch and self.pending_stop:
                        self._stop_wait_error(exc)
            await asyncio.sleep(.05)

    def snapshot(self):
        data=dict(self.sample)
        data['eeprom_check']=self.driver.eeprom_status()
        data['force_inputs']=self.driver.force_status()
        data['stop_reason']=self.stop_reason
        data['stop_notice']=self.stop_notice if self.pending_stop else ''
        data['stop_confirmation']=self.stop_confirmation
        data['motion_notice']='命令已应答，未检测到运动' if self.mode=='pv' and self.state=='MOVING' and self.jog_no_motion else ''
        if self.mode=='pp' and self.state=='MOVING' and self.pp_no_motion:
            data['motion_notice']='定位指令已应答，未检测到运动'
        data['motion_advice']=''
        if self.mode=='pv' and data['motion_notice'] and data.get('alarm_code') == 0x950:
            data['motion_notice']+='（Er.950）'
            data['motion_advice']='当前存在Er.950，可能是驱动器内部仍在阻止运动。请先松开点动，待停止命令应答后点击“人工故障复位”，再手动点动。工具不会自动复位。'
        data['unit_verification']=self.driver.unit_status()
        data['parameter_monitor']=self.driver.monitor_status()
        data['observed_motion']=self._observed_motion
        data['untracked_motion']=bool(self._observed_motion and not self.mode and not self.pending_stop and not getattr(self,'manual_active',False))
        raw=data.get('position_feedback')
        height=self.config.cm(raw-self.zero) if raw is not None else None
        data['positioning']=dict(status=self.position_status,target_cm=self.position_target_cm,
            error_cm=None if height is None or self.position_target_cm is None else self.position_target_cm-height,
            tolerance_cm=self.config.pp_tolerance_cm,detail=self.position_detail)
        age=None if not self.sample_at else time.monotonic()-self.sample_at
        fresh=age is not None and age<max(1.,self.config.poll_s*3)
        speeds={}
        for mode in ('pp','pv'):
            cm_s=self.speeds[mode]
            pr=self.config.pulse(cm_s)
            speeds[mode]=dict(requested_cm_s=cm_s,min_cm_s=self.config.cm(getattr(self.config,'min_'+mode+'_speed')),
                max_cm_s=self.config.cm(getattr(self.config,'max_'+mode+'_speed')),pr_s=pr,
                rpm=self.config.units.rpm(pr),ratio=pr/getattr(self.config,'max_'+mode+'_speed'))
        params=dict(self.driver.parameters)
        params['stale']=params.get('connection_epoch')!=self.driver.transport.epoch or not fresh
        data.update(state=self.state,motion=('homing' if self.state=='HOMING' else self.direction if self.state=='MOVING' else 'unknown' if self.pending_stop else 'stop_acknowledged' if self.stop_confirmation=='ack' else 'stopped'),
            height_cm=height,total_height_cm=None if height is None else self.config.base_cm+height,
            height=None if height is None else height/self.config.travel_cm*100,target_cm=self.target_cm,
            target_height=None if self.target_cm is None else self.target_cm/self.config.travel_cm*100,
            speeds=speeds,applied_speed_pr_s=self.applied_speed,command_speed_cm_s=self.config.cm(self.applied_speed),
            position_trusted=self.trusted,zero_pulse=self.zero,armed=self.armed,pending_stop=self.pending_stop,
            intent_id=self.intent,epoch=self.epoch,faults=[x for x in (self.fault,self.storage_error) if x],
            travel_height_cm=self.config.travel_cm,base_height_cm=self.config.base_cm,
            soft_lower_cm=self.config.soft_lower_cm,soft_upper_cm=self.config.soft_upper_cm,
            home_status=self.home_status,home_phase=self.home_phase,parameters=params,alarm_history=list(self.alarms),operator_result=self.operator_result,
            limit_protection=dict(host='PP目标校验；基础JOG仅按物理限位方向拦截，不依赖软件高度',
                driver='待实机验证：不假定P0A/P10软限位覆盖PV/JOG或HOME；仅只读观察',
                motion_profile_verified=self.config.motion_profile_verified,mechanical='无电机抱闸；传动自锁，机械端点非日常停止方式'),
            limits={'min':bool(self.trusted and height is not None and height<=self.config.soft_lower_cm),
                    'max':bool(self.trusted and height is not None and height>=self.config.soft_upper_cm)},
            observed_age_s=age)
        # Raw bus takeover does not run the ordinary PP/PV state machine.
        # Never report its untracked motion as a confirmed software idle state.
        data['manual_active'] = bool(getattr(self, 'manual_active', False))
        data['manual_failed'] = bool(getattr(self, 'manual_failed', False))
        data['underlying_control_state'] = self.state
        if data['manual_active']:
            data['state'] = 'MANUAL_LOCKED' if data['manual_failed'] else 'MANUAL'
            data['motion'] = 'unknown' if self.pending_stop else 'manual'
        return dict(online=fresh,control_mode=self.config.mode,implementation='candidate',details=data)

    async def close(self, stop=True):
        try:
            if stop and (self.mode or self.pending_stop or getattr(self,'manual_active',False)):
                await self.stop()
        finally:
            self._closed=True
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks,return_exceptions=True)
            await self.driver.transport.shutdown()
