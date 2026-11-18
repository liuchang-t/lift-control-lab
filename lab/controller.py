from __future__ import annotations

import asyncio
import time
from collections import deque
from .model import CalibrationStore, SpeedStore, LabError
from .units import finite
from .alarms import blocked, describe, jog_blocked
from .driver import ParameterChanged


class MotionCancelled(LabError):
    pass


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
        self.transport_epoch = driver.transport.epoch
        self.state, self.fault = 'DISARMED', ''
        self.applied_speed = 0
        self.mode = self.direction = self.target_cm = self.intent = None
        self.closed = deque(maxlen=10000)
        self.last_heartbeat = self.started = self.last_motion = 0.
        self.pending_stop = self.emergency = False
        self.sample = {}
        self.sample_at = self.last_raw_at = self.stop_at = 0.
        self.last_raw = None
        self.progress_raw = None
        self.home_status = 'not_homed'
        self.home_triggered = self.home_seen_incomplete = False
        self.home_seen_lower = False
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
        self.stop_confirmation = None
        self.jog_no_motion = False

    async def start(self):
        if self.config.release_enable_s:
            self.trace.add('configuration_notice',message='release_enable_s仅保留兼容，候选实现不延时自动去使能，以免干预外部运动')
        self._tasks = [asyncio.create_task(self._poll_loop()),asyncio.create_task(self._watchdog())]

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

    def _stop_intent(self, emergency=False, reason=''):
        self.jog_no_motion=False
        if reason:
            self.stop_reason=reason
        if self.pending_stop and (not emergency or self.emergency):
            return
        self.stop_confirmation=None
        self.epoch += 1
        self._retire_intent()
        self.pending_stop = True
        self.emergency = self.emergency or emergency
        self.state = 'STOPPING'
        if self.mode == 'home' and self.home_status != 'complete':
            self.trusted = False
            self.home_status = 'cancelled'
        if emergency:
            self.armed = False
            self.fault = '软件急停已锁存；检查后重新允许控制'
        self.trace.add('stop_intent',emergency=emergency,epoch=self.epoch,reason=reason)

    def _fault(self, message, reference_lost=False):
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
            await self.driver.stop(self.emergency)
            ack_only=self.driver.jog_context
            if not ack_only:
                first=await self.observe()
                await asyncio.sleep(.06)
                second=await self.observe()
                if abs(first['speed_rpm'])>1 or abs(second['speed_rpm'])>1 or abs(first['position_feedback']-second['position_feedback'])>20:
                    return False
            if issued!=self.epoch:
                return False
            self.stop_confirmation='ack' if ack_only else 'feedback'
            self.pending_stop=False
            self.mode=self.direction=self.target_cm=None
            self.applied_speed=0
            self.stop_at=time.monotonic()
            self.state='FAULT' if self.fault else ('IDLE' if self.armed else 'DISARMED')
            self.trace.add('stop_acknowledged' if ack_only else 'stop_confirmed')
            return True

    async def stop(self, emergency=False):
        self._stop_intent(emergency)
        return await self._wait_for_stop()

    async def _wait_for_stop(self):
        deadline=time.monotonic()+self.config.stop_timeout_s
        while time.monotonic()<deadline:
            try:
                if await self._confirm_stop():
                    return self.snapshot()
            except Exception as exc:
                self.fault='停止待确认：'+str(exc)
                self.armed=self.trusted=False
                self.state='MOTION_UNKNOWN'
            await asyncio.sleep(.05)
        raise LabError('停止尚未确认，后台仍重试；请现场使用独立硬急停',503)

    async def reset_fault(self, command):
        if command.get('confirm_reset') is not True:
            raise LabError('请先确认已按建议排查原因；复位不会恢复运动',400)
        async with self._sequence:
            # Clearing only a limit warning must not add a recheck requirement.
            was_ready = self.armed and not self.emergency and not self.fault
            # Retire all old starts before the only permitted reset write.
            self.armed=False
            await self.stop()
            issued=self.epoch
            data=await self.observe()
            alarm=data['alarm']
            if not alarm['reset_allowed']:
                raise LabError(alarm['text']+' 不允许通用复位；'+alarm['advice'])
            if alarm['code'] in (0x950,0x952) and (data['upper_limit'] or data['lower_limit']):
                self.armed=was_ready
                if was_ready:
                    self.state='IDLE'
                raise LabError('物理限位仍有效，必须先按允许方向脱离，不能用复位绕过限位')
            def checkpoint():
                if issued!=self.epoch or self.pending_stop:
                    raise MotionCancelled('故障复位被停止/连接变化取消')
            try:
                await self.driver.reset_fault(checkpoint,alarm['raw_code'])
                data=await self.observe()
                checkpoint()
                self.trace.add('manual_reset',previous_alarm=alarm['raw_hex'],remaining=data['alarm']['raw_hex'])
                await self.driver.read_parameters()
                checkpoint()
                if data['alarm_code']:
                    raise LabError('复位后报警仍存在：'+data['alarm_text'])
                if alarm['code'] in (0x950,0x952) and was_ready:
                    self.armed=True
                    self.fault=''
                    self.state='IDLE'
                else:
                    self.fault='驱动报警已复位；仍需重新检查并允许控制'+('，必须回原点' if not self.trusted else '')
                    self.state='DISARMED'
                return self.snapshot()
            except MotionCancelled:
                raise
            except Exception as exc:
                self._fault('故障复位未确认：'+str(exc),True)
                raise LabError(self.fault,503)

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
        reason=blocked(data,homing=True)
        if reason:
            raise LabError(reason)
        params=await self.driver.read_parameters()
        self._checkpoint(issued)
        if params['home_method']!=17:
            raise LabError('当前驱动回零方式不是17；请维护人员核实，禁止自动替换未知配置')
        if params['home_offset']!=self.config.home_reference_pulse:
            raise LabError('驱动原点偏移与已确认配置不一致，不自动改写或采用')
        if any(params[key]<=0 for key in ('home_speed','home_creep_speed','home_acceleration')):
            raise LabError('HOME速度或加减速参数无效')
        if max(params['home_speed'],params['home_creep_speed'])>params['max_velocity']:
            raise LabError('HOME速度超过驱动最大速度限制')
        self.trusted=False
        self.mode,self.direction,self.target_cm='home','down',None
        self.home_status='starting'
        self.home_triggered=self.home_seen_incomplete=False
        self.home_seen_lower=False
        self.home_stable=0
        self.home_last_raw=None
        self.home_connection=self.driver.transport.epoch
        self.started=time.monotonic()
        self.last_motion=self.started
        self.progress_raw=data['position_feedback']
        self.state='STARTING'
        def triggered():
            self.home_triggered=True
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
        if enabled and (self.mode or self.pending_stop or self.emergency):
            raise LabError('请先停止当前任务并解除软件急停，再独立使能')
        # Retire queued starts before waiting for a preparation sequence to exit.
        self._stop_intent()
        issued=self.epoch
        connection=self.driver.transport.epoch
        async with self._sequence:
            def checkpoint():
                if issued!=self.epoch or connection!=self.driver.transport.epoch:
                    raise MotionCancelled('使能给定操作已被停止或连接变化取消')
            checkpoint()
            # Verify two stationary samples without replacing this operation's
            # epoch; a concurrent stop must still cancel the pending enable.
            await self._wait_for_stop()
            checkpoint()
            try:
                data=await self.observe()
                checkpoint()
                if enabled and (self.emergency or data['alarm_code'] not in (0,0x950,0x952)):
                    raise LabError('存在报警或软件急停，不能独立使能')
                if enabled:
                    await self.driver.prepare_jog(checkpoint)
                    await self.driver.jog_command(1,checkpoint)
                    checkpoint()
                else:
                    await self.driver.jog_command(65,checkpoint)
                    checkpoint()
                self.driver.jog_context=True
                self.driver.jog_running=False
                self.armed=False
                self.state='FAULT' if self.fault else 'DISARMED'
                self.trace.add('servo_given',enabled=enabled,completion_confirmed=False)
            except MotionCancelled:
                raise
            except Exception as exc:
                self._stop_intent()
                self._fault('使能给定操作未确认：'+str(exc))
                raise LabError(self.fault,503)
            return self.snapshot()

    async def command(self, command):
        action=command.get('action')
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
            return await self.stop(action=='estop')
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
                if abs(delta)>self.config.max_move_pulse:
                    raise LabError('目标超过配置的单次位移上限')
                if abs(delta)<=20:
                    reason=blocked(data)
                    if reason:
                        raise LabError(reason)
                    self.target_cm=cm
                    return self.snapshot()
                direction='up' if delta>0 else 'down'
            else:
                direction=action
            reason=jog_blocked(data,direction) if mode=='pv' else blocked(data,direction)
            if reason:
                raise LabError(reason)
            # A trusted reference permits PP from outside to any in-range target.
            # Explicit endpoint recovery retains its existing creep speed.
            outside=current < self.config.soft_lower_cm-.001 or current > self.config.soft_upper_cm+.001
            recovering=outside and ((action=='min' and current<self.config.soft_lower_cm) or (action=='max' and current>self.config.soft_upper_cm))
            if mode=='pp' and not outside and ((current>=self.config.soft_upper_cm and direction=='up') or (current<=self.config.soft_lower_cm and direction=='down')):
                raise LabError('软件行程边界禁止该方向')
            speed=self.config.speed_pr(self.speeds[mode],mode)
            if speed>self._speed_cap(mode):
                raise LabError('期望速度超过驱动器最大速度限制')
            if mode=='pv' and speed:
                self.config.units.jog_rpm(self.speeds['pv'])
            if not speed:
                raise LabError('速度为0或剩余距离不足以安全点动，不启动运动')
            if recovering:
                speed=min(speed,self.driver.parameters['home_creep_speed'])
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
        reason=blocked(data,homing=True)
        # Only this HOME's observed switch transition can explain a latched 952.
        if data['alarm_code']==0x952 and self.home_seen_lower and not data['upper_limit'] and self.home_connection==self.driver.transport.epoch:
            reason=''
        if reason:
            self._fault('回零中断：'+reason,True)
            return
        now=time.monotonic()
        if not data['home_attained'] and self._stalled(data,now):
            self._fault('回零启动无运动或堵转，请检查机构与负限位',True)
            return
        if now-self.started>self.config.home_timeout_s:
            self._fault('回零超时；请检查负限位、方向及速度',True)
            return
        if not self.home_seen_incomplete and now-self.started>self.config.home_start_timeout_s:
            self._fault('未观察到本次回零未完成状态，不能采用旧完成位',True)
            return
        if not self.home_seen_incomplete or not data['home_attained']:
            self.home_stable=0
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
            if abs(height-self.target_cm)<=self.config.cm(20) and abs(data['speed_rpm'])<=1:
                self._stop_intent()
                return
        if self._stalled(data,time.monotonic()):
            self._fault('启动无运动或堵转，禁止保活重启')

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
                elif self.state=='IDLE' and data['alarm_code'] not in (0,0x950,0x952):
                    self.armed=False
                    self.fault=data['alarm_text']+' '+data['alarm']['name']
                    self.state='FAULT'
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
                            await self.driver.monitor_parameter_turn(compare=bool(self.driver.expected_words) and self.mode!='pv')
                        if self.mode and self.mode!='pv' and self.state in ('MOVING','HOMING'):
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
                try:
                    await self._confirm_stop()
                except Exception as exc:
                    self.state='MOTION_UNKNOWN'
                    self.fault='停止重试中：'+str(exc)
                    self.armed=self.trusted=False
            await asyncio.sleep(.05)

    def snapshot(self):
        data=dict(self.sample)
        data['eeprom_check']=self.driver.eeprom_status()
        data['force_inputs']=self.driver.force_status()
        data['stop_reason']=self.stop_reason
        data['stop_confirmation']=self.stop_confirmation
        data['motion_notice']='命令已应答，未检测到运动' if self.mode=='pv' and self.state=='MOVING' and self.jog_no_motion else ''
        data['motion_advice']=''
        if data['motion_notice'] and data.get('alarm_code') == 0x950:
            data['motion_notice']+='（Er.950）'
            data['motion_advice']='当前存在Er.950，可能是驱动器内部仍在阻止运动。请先松开点动，待停止命令应答后点击“人工故障复位”，再手动点动。工具不会自动复位。'
        data['unit_verification']=self.driver.unit_status()
        data['parameter_monitor']=self.driver.monitor_status()
        data['observed_motion']=self._observed_motion
        data['untracked_motion']=bool(self._observed_motion and not self.mode and not self.pending_stop and not getattr(self,'manual_active',False))
        raw=data.get('position_feedback')
        height=self.config.cm(raw-self.zero) if raw is not None else None
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
            home_status=self.home_status,parameters=params,alarm_history=list(self.alarms),
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
