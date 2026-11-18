from __future__ import annotations

import asyncio
import hmac
import json
import re
import time
from dataclasses import asdict
from pathlib import Path
from .controller import Controller
from .driver import Driver
from .legacy import Legacy
from .model import Config, LabError, atomic_json_write, ROOT
from .transport import Simulator, TcpTransport, Trace
from .manual_bus import parse_frame, validate_response
from .registers import CATALOG, REFRESH_EXCLUDED
from .readiness import build_readiness


class Service:
    def __init__(self,config):
        self.config=config.validate()
        self.trace=Trace()
        self.transport=(Simulator if config.mode=='sim' else TcpTransport)(config,self.trace)
        self.controller=Legacy(config,self.transport,self.trace) if config.implementation=='baseline' else Controller(config,Driver(config,self.transport),self.trace)
        self.lease=None
        self._lease_lock=asyncio.Lock()
        self._task=None
        self.mqtt_status='disabled'
        self.last_rejection=None
        self.manual_active=None
        self._manual_lock=asyncio.Lock()
        self._connection_lock=asyncio.Lock()
        self.connected=False
        self.connection_state="disconnected"
        self.connection_error=""

    async def start(self):
        if self.config.implementation=='candidate':
            await self.controller.driver.read_unit_parameters()
        await self.controller.start()
        self.connected=True
        self.connection_state="connected"
        self._task=asyncio.create_task(self._expiry_loop())

    async def close(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task,return_exceptions=True)
        if self.connected:
            await self.controller.close()
        elif self.config.implementation=="candidate":
            await self.controller.close(stop=False)
        else:
            await self.transport.shutdown()
        self.connected=False
        self.connection_state="disconnected"

    def _require_connected(self,allow_stop=False):
        if not self.connected:
            raise LabError('尚未连接；请选择模拟或实机，点击连接',409)
        if self.connection_state!='connected' and not allow_stop:
            raise LabError('连接切换或停止确认尚未完成；等待停止确认后重试连接/断开',409)

    def _connection_values(self,config=None):
        config=config or self.config
        return {key:getattr(config,key) for key in ('mode','host','port','slave_id','timeout')}

    def connection_status(self):
        current=self._connection_values()
        saved_path=ROOT/'.runtime/connection.json'
        saved=dict(current)
        saved_error=''
        if saved_path.exists():
            try:
                saved.update(json.loads(saved_path.read_text(encoding='utf-8')))
            except (ValueError,OSError,TypeError) as exc:
                saved_error='已保存连接配置无法读取：'+str(exc)
        return dict(current=current,saved=saved,mode=self.config.mode,connected=self.connected,
                    state=self.connection_state,error=self.connection_error or saved_error)

    def _connection_config(self,body):
        allowed={'mode','host','port','slave_id','timeout','confirm_real'}
        if set(body)-allowed:
            raise LabError('连接设置仅接受模式、主机、端口、从站、超时和实机确认',400)
        values=asdict(self.config)
        values.update({key:value for key,value in body.items() if key!='confirm_real'})
        if not isinstance(values['host'],str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,253}',values['host']):
            raise LabError('请输入IP地址或主机名，不含协议或路径',400)
        if type(values['port']) is not int or type(values['slave_id']) is not int:
            raise LabError('端口和从站地址必须是整数',400)
        if type(values['timeout']) not in (int,float) or not .05<=values['timeout']<=2:
            raise LabError('通信超时必须在0.05至2秒之间',400)
        try:
            return Config(**values).validate()
        except (ValueError,TypeError) as exc:
            raise LabError(str(exc),400)

    async def save_connection(self,body):
        candidate=self._connection_config(body)
        saved=self._connection_values(candidate)
        await asyncio.get_running_loop().run_in_executor(None,atomic_json_write,ROOT/'.runtime/connection.json',saved)
        return dict(saved=saved,notice='已保存；当前连接不变，下次点击连接时生效。')

    async def _disconnect_locked(self,client=None):
        if not self.connected:
            self.connection_state='disconnected'
            return
        ctrl=self.controller
        owns_motion=bool(getattr(ctrl,'mode',None) or getattr(ctrl,'pending_stop',False) or self.manual_active)
        self.connection_state='disconnecting'
        self.connection_error=''
        if self.config.implementation=='candidate':
            ctrl.armed=ctrl.trusted=False
            ctrl.epoch+=1
            ctrl._retire_intent()
        self._lock_manual('正在断开连接，取消手动写入',request_stop=False)
        try:
            # Invalidate pending starts immediately, without waiting on the
            # ordinary sequence lock. A failed stop retains this entire live
            # controller, its transport and retry watchdog.
            if owns_motion or self.config.implementation=='baseline':
                await ctrl.stop()
            if self.config.implementation=='candidate':
                async with ctrl._sequence:
                    if ctrl.pending_stop:
                        raise LabError('停止仍待确认，未断开连接',503)
                    await ctrl.close(stop=False)
                    ctrl.sample={}
                    ctrl.sample_at=ctrl.last_raw_at=0
                    ctrl.last_raw=None
                    ctrl.armed=ctrl.trusted=False
                    ctrl.mode=ctrl.direction=ctrl.target_cm=None
                    ctrl.home_status='not_homed'
                    ctrl.driver.parameters={}
                    ctrl.driver.invalidate_parameters()
                    ctrl.driver._eeprom_at=None
                    ctrl.state='DISCONNECTED'
                    ctrl.fault='连接已主动断开'
            else:
                await ctrl.close()
                await self.transport.shutdown()
                ctrl.state.update(online=False,motion='stopped')
        except Exception as exc:
            self.connection_state='stop_pending'
            self.connection_error='未确认停止，保留原连接并继续停止重试：'+str(exc)
            self.trace.add('disconnect_blocked',error=str(exc))
            raise LabError(self.connection_error,503)
        self.connected=False
        self.connection_state='disconnected'
        self.lease=None
        self._clear_manual()
        self.last_rejection=None
        self.trace.add('disconnected',**self._connection_values())

    async def disconnect(self,client=None):
        async with self._connection_lock:
            await self._disconnect_locked(client)
            return self.connection_status()

    async def connect(self,body,client=None):
        candidate=self._connection_config(body)
        if candidate.mode=='real' and body.get('confirm_real') is not True:
            raise LabError('连接实机需要明确确认目标IP、端口和从站；当前连接未改变',400)
        if candidate.mode=='real':
            # A simulator's checked settings are never a physical acceptance.
            same_device=(self.config.mode=='real' and self.config.motion_profile_verified and
                         all(getattr(self.config,key)==getattr(candidate,key) for key in ('host','port','slave_id')))
            candidate.motion_profile_verified=bool(same_device)
        async with self._connection_lock:
            await self._disconnect_locked(client)
            self.connection_state='connecting'
            self.connection_error=''
            transport=(Simulator if candidate.mode=='sim' else TcpTransport)(candidate,self.trace)
            controller=(Legacy(candidate,transport,self.trace) if candidate.implementation=='baseline'
                        else Controller(candidate,Driver(candidate,transport),self.trace))
            try:
                # Only observation on connect; never enable, reset or start a
                # drive, even when a previously saved profile was armed.
                if candidate.implementation=='candidate':
                    await controller.observe()
                    await controller.driver.read_unit_parameters()
                await controller.start()
            except Exception as exc:
                if candidate.implementation=='candidate':
                    await controller.close(stop=False)
                else:
                    await transport.shutdown()
                self.connection_state='disconnected'
                self.connection_error='连接失败：'+str(exc)
                self.trace.add('connect_failed',error=str(exc),**self._connection_values(candidate))
                raise LabError(self.connection_error,503)
            self.config,self.transport,self.controller=candidate,transport,controller
            self.lease=None
            self._clear_manual()
            self.last_rejection=None
            self.connected=True
            self.connection_state='connected'
            self.trace.add('connected',**self._connection_values())
            return self.connection_status()

    def lease_view(self,client=''):
        # Compatibility endpoint: this private bench has shared controls, not
        # exclusive ownership or time-based permissions.
        return dict(enabled=False,can_control=True,ttl_seconds=0,remaining_s=0)

    async def _expire(self):
        return

    async def _expiry_loop(self):
        while True:
            try:
                self._monitor_manual()
            except Exception as exc:
                self.trace.add('manual_monitor_error',error=str(exc))
            await asyncio.sleep(.1)

    async def acquire(self,client,role,name):
        return self.lease_view(client)

    def _require_owner(self,client):
        # Retained for old API clients only; never gates commands or renews a
        # lease. Motion state and stop-epoch checks are still authoritative.
        return

    def _bridge_auth(self,headers):
        if not self.config.bridge_enabled:
            raise LabError('Bridge 未启用',404)
        provided=headers.get('x-patrol-bridge-token','')
        auth=headers.get('authorization','')
        if not provided and auth.lower().startswith('bearer '):
            provided=auth[7:]
        if self.config.bridge_token and not hmac.compare_digest(provided,self.config.bridge_token):
            raise LabError('Bridge token 不正确',401)

    async def request(self,method,path,body=None,headers=None):
        body=body or {}
        headers={k.lower():v for k,v in (headers or {}).items()}
        bridge=path.startswith('/api/bridge/v1/')
        if bridge:
            self._bridge_auth(headers)
        client='central-bridge' if bridge else headers.get('x-patrol-client-id','')
        if not client:
            client='local-anonymous'
        role='central' if bridge else 'local'
        suffix=path[len('/api/bridge/v1'):] if bridge else path.replace('/api/devices/lift_pole_1','')
        if method=='GET':
            if path=='/api/connection':
                return self.connection_status()
            if path=='/api/conversion':
                if self.config.implementation=='baseline':
                    raise LabError('冻结基线不提供候选单位核验',400)
                return self.controller.driver.unit_status()
            if path=='/api/readiness':
                return build_readiness(self,client)
            if path=='/api/manual/status':
                return self.manual_status()
            if path=='/api/registers':
                result=self.register_snapshot()
                result['connected']=self.connected
                if not self.connected:
                    for row in result['registers']:
                        row['stale']=True
                    result['notice']='已断开：历史观测仅供参考，不自动连接'
                return result
            if path=='/api/config':
                return self.config.public()
            if path=='/api/logs':
                return list(self.trace.items)
            if path=='/api/parameters':
                if self.config.implementation=='baseline':
                    raise LabError('冻结基线不提供候选参数检查',400)
                ctrl=self.controller
                if not self.connected or self.connection_state!='connected':
                    return dict(ctrl.driver.parameters,cached=True,notice='未连接：仅显示缓存，不自动连接')
                if ctrl.mode or ctrl.pending_stop or ctrl._sequence.locked():
                    return dict(ctrl.driver.parameters,cached=True,notice='运动或控制操作期间只返回缓存，避免参数整组读取占用保护通信')
                async with ctrl._sequence:
                    return await ctrl.driver.read_parameters()
            if suffix in ('/lift/status','/api/status'):
                return self.controller.snapshot()
            if suffix in ('/lease','/lift/lease'):
                return self.lease_view(client)
            if suffix=='/health':
                return dict(enabled=True,robot_control_via_bridge=False,capabilities=['lift_status','lift_control'],mqtt_status=self.mqtt_status)
            if suffix=='/snapshot':
                return dict(lift=self.controller.snapshot(),leases={'lift_pole_1':self.lease_view(client)},robot=None,media={},scope='lift-only laboratory')
        if method=='POST':
            if path=='/api/logs/tail':
                after=body.get('after',0)
                if type(after) is not int or after<0:
                    raise LabError('日志游标无效',400)
                return self.trace.tail(after)
            if path=='/api/logs/clear':
                return self.trace.clear()
            if path in ('/api/connection','/api/connection/save'):
                return await self.save_connection(body)
            if path=='/api/connection/connect':
                return await self.connect(body,client)
            if path=='/api/connection/disconnect':
                return await self.disconnect(client)
            is_stop=suffix in ('/lift/control','/api/control') and body.get('action') in ('stop','estop')
            self._require_connected(allow_stop=is_stop)
            if path=='/api/manual/transaction':
                return await self.manual_transaction(client,body)
            if path=='/api/manual/exit':
                self._require_owner(client)
                return await self.exit_manual()
            if path=='/api/registers/refresh':
                return await self.refresh_registers()
            if path=='/api/arm':
                ctrl=self.controller
                try:
                    if self.manual_active:
                        self._require_owner(client)
                        await self.exit_manual()
                    self._require_connected()
                    if ctrl is not self.controller:
                        raise LabError('连接已变化，请重新允许控制')
                    result=await ctrl.arm(body.get('hardware_confirmed') is True)
                    self.last_rejection=None
                    return result
                except Exception as exc:
                    self.last_rejection=dict(message=str(exc),action='arm',at=time.time())
                    raise
            if path=='/api/inject':
                if not isinstance(self.transport,Simulator):
                    raise LabError('故障注入仅模拟可用',403)
                self.transport.inject(body['name'],body.get('value',True))
                return {'ok':True}
            if path=='/api/config':
                if self.manual_active:
                    raise LabError('手动接管中；请先结束手动并确认停机，再修改调试参数')
                return await self.update_config(body)
            if suffix in ('/lease/acquire','/lift/lease/acquire'):
                return await self.acquire(client,role,body.get('holder_name') or ('central' if bridge else '本地调试员'))
            if suffix in ('/lease/renew','/lift/lease/renew'):
                self._require_owner(client)
                return self.lease_view(client)
            if suffix in ('/lease/release','/lift/lease/release'):
                self.lease=None
                return self.lease_view(client)
            if suffix in ('/lift/control','/api/control'):
                action=body.get('action')
                if bridge and action in ('max','min','set_retracted_zero','home','reset_fault'):
                    raise LabError('该动作不向 Bridge 开放',400)
                if action not in ('stop','estop'):
                    if bridge:
                        await self.acquire(client,role,'central')
                    self._require_owner(client)
                if self.manual_active:
                    if action not in ('stop','estop'):
                        self.last_rejection=dict(message='手动接管中；点击允许控制结束手动并重新检查，然后重新回零',action=action,at=time.time())
                        raise LabError(self.last_rejection['message'])
                    self._lock_manual('停止操作已取消手动写；结束手动后才能重新接管',request_stop=False)
                try:
                    result=await self.controller.command(body)
                except Exception as exc:
                    self.last_rejection=dict(message=str(exc),action=action,at=time.time())
                    self.trace.add('command_failed',action=action,client=client,error=str(exc))
                    raise
                self.last_rejection=None
                self.trace.add('command',action=action,client=client)
                return dict(ok=True,operation='lift_'+str(action),payload={'state':result['details']},status=result)
        raise LabError('接口不存在',404)

    def manual_status(self):
        session=self.manual_active or {}
        return dict(active=bool(self.manual_active),holder_id=session.get('holder_id'),
                    failed=bool(session.get('failed')),reason=session.get('reason',''),
                    notice='手动写优先接管，普通运动已取消；手动报文不执行软件行程保护。结束手动后重新允许控制并回零。' if self.manual_active else '手动读不打断运动；首次确认手动写先停止自动运动并接管。')

    def _clear_manual(self):
        self.manual_active=None
        self.controller.manual_active=False
        self.controller.manual_failed=False

    def _lock_manual(self,reason,request_stop=True):
        if not self.manual_active:
            return
        self.manual_active.update(failed=True,reason=reason)
        self.controller.manual_failed=True
        if request_stop:
            self.controller._fault(reason,True)

    def _monitor_manual(self):
        if not self.manual_active or self.manual_active.get('failed'):
            return
        ctrl=self.controller
        reason=None
        if ctrl.epoch!=self.manual_active['epoch'] or ctrl.pending_stop:
            reason='停止或连接变化已取消手动会话'
        elif self.transport.epoch!=self.manual_active['connection_epoch']:
            reason='驱动连接变化，手动运动状态未知'
        elif not ctrl.sample_at or time.monotonic()-ctrl.sample_at>self.config.control_latency_s:
            reason='手动控制反馈过期，请检查通信'
        elif not self.manual_active.get('reset_pending') and ctrl.sample.get('alarm_raw',ctrl.sample.get('alarm_code',0)):
            reason='手动控制期间驱动器报警，已请求停止；按报警建议排查'
        if reason:
            self._lock_manual(reason)

    async def exit_manual(self):
        if self.config.implementation=='baseline':
            raise LabError('冻结基线不提供手动接管',400)
        ctrl=self.controller
        # Stop is intentionally outside the ordinary sequence lock.
        self._lock_manual('正在结束手动控制',request_stop=False)
        expected_epoch=ctrl.epoch+1
        await ctrl.stop()
        self._require_connected()
        if ctrl is not self.controller or ctrl.epoch!=expected_epoch:
            raise LabError('停止或连接变化已取消结束手动流程，请重新检查')
        ctrl.armed=ctrl.trusted=False
        ctrl.home_status='not_homed'
        ctrl.fault='手动控制已结束；请重新允许控制并回零'
        ctrl.state='DISARMED'
        self._clear_manual()
        return self.manual_status()

    def register_snapshot(self):
        ctrl=self.controller
        mode=getattr(ctrl,'mode',None) if getattr(ctrl,'state',None) in ('MOVING','HOMING') else None
        return self.transport.registers.snapshot(self.transport.epoch,
            stale_after=max(self.config.control_latency_s,self.config.poll_s*3),active_mode=mode)

    async def refresh_registers(self):
        self._require_connected()
        if self.config.implementation=='baseline':
            raise LabError('冻结基线仅展示已观测报文，不主动刷新候选寄存器',400)
        ctrl=self.controller
        if ctrl.mode or ctrl.pending_stop or ctrl._sequence.locked() or self.manual_active:
            result=self.register_snapshot()
            result.update(cached=True,notice='运动、手动接管或控制操作期间返回缓存，不增加保护通信负担')
            return result
        async with ctrl._sequence:
            epoch=ctrl.epoch
            for address,width,_,_,access,_ in CATALOG:
                if epoch!=ctrl.epoch or ctrl.pending_stop:
                    raise LabError('寄存器刷新已被停止或新控制意图取消')
                # Command registers have write-ACK evidence, never pretend that
                # a read of a trigger register reports an active command.
                if address not in REFRESH_EXCLUDED:
                    await ctrl.driver._read_words(address,width)
                    ctrl.driver._limit_config_epoch = None
        return self.register_snapshot()

    async def manual_transaction(self,client,body):
        self._require_connected()
        parsed=parse_frame(body.get("frame"),self.config.slave_id)
        if parsed["write"]:
            controller=self.controller
            issued=controller.epoch
            async with self._manual_lock:
                self._require_connected()
                if controller is not self.controller or issued!=controller.epoch:
                    raise LabError("排队手动写已被停止或连接变化取消")
                return await self._manual_transaction(client,body)
        return await self._manual_transaction(client,body)

    async def _manual_transaction(self,client,body):
        if self.config.implementation=='baseline':
            raise LabError('冻结基线不提供手动总线接口',400)
        parsed=parse_frame(body.get('frame'),self.config.slave_id)
        self._require_owner(client)
        ctrl=self.controller
        transport=self.transport
        def same_connection():
            self._require_connected()
            if ctrl is not self.controller or transport is not self.transport:
                raise LabError('连接已变化，旧手动报文已取消')
            self._require_owner(client)
        if not parsed['write']:
            response=await transport.transaction(parsed['request'],parsed['expected_length'],before_send=same_connection)
            validate_response(parsed,response)
        else:
            if body.get('confirm_write') is not True:
                raise LabError('确认手动写入后才能发送；它会取消自动任务，且不执行软件行程保护',400)
            if self.manual_active and self.manual_active.get('failed'):
                raise LabError('手动会话已锁定；先结束手动并排查原因')
            if not self.manual_active:
                # Cancel immediately, before waiting for an in-flight automatic
                # preparation. Its checkpoint will retire late automatic starts.
                ctrl.armed=ctrl.trusted=False
                expected_epoch=ctrl.epoch+1
                await ctrl.stop()
                if ctrl.epoch!=expected_epoch or ctrl.pending_stop:
                    raise LabError('手动接管期间收到新的停止或控制意图，未发送手动报文')
                ctrl.home_status='not_homed'
                ctrl.fault='手动控制已接管；普通运动需重新允许控制并回零'
                ctrl.state='DISARMED'
                self.manual_active=dict(holder_id=client,failed=False,reason='',epoch=ctrl.epoch,
                                        connection_epoch=self.transport.epoch)
                ctrl.manual_active=True
                ctrl.manual_failed=False
            resetting=(parsed['address']==0x0D01 and parsed['count']==1 and parsed['values']==[1])
            # Only a single explicit reset may resolve an existing alarm. Never
            # suppress alarm protection for a mixed multi-register command.
            self.manual_active['reset_pending']=resetting
            issued=self.manual_active['epoch']
            def checkpoint():
                same_connection()
                if (not self.manual_active or self.manual_active.get('failed') or
                    ctrl.epoch!=issued or ctrl.pending_stop or
                    self.transport.epoch!=self.manual_active['connection_epoch']):
                    raise LabError('手动写入已被停止、失联或新控制意图取消')
                self._require_owner(client)
            async with ctrl._sequence:
                checkpoint()
                try:
                    # Recheck *inside* the shared bus lock, so a queued raw
                    # start cannot be sent after an emergency-stop intent.
                    if parsed['address'] <= 0x0D0B < parsed['address']+parsed['count']:
                        ctrl.driver.jog_context=True
                        ctrl.driver.jog_running=True
                    response=await transport.transaction(parsed['request'],parsed['expected_length'],before_send=checkpoint)
                    validate_response(parsed,response)
                    checkpoint()
                    if resetting:
                        await ctrl.observe()
                        checkpoint()
                    self.trace.add('manual_write',frame=parsed['request'].hex(' '),client=client)
                except Exception as exc:
                    self._lock_manual('手动写结果未确认：'+str(exc))
                    self.last_rejection=dict(message=str(exc),action='manual_write',at=time.time())
                    raise LabError('手动写结果未确认，已请求停止并锁定；请结束手动并检查：'+str(exc),503)
                finally:
                    if self.manual_active:
                        self.manual_active['reset_pending']=False
            self._monitor_manual()
        self.last_rejection=None
        return dict(tx=parsed['request'].hex(' '),rx=response.hex(' '),function=parsed['function'],
                    address=parsed['address'],count=parsed['count'],write=parsed['write'],
                    notice='写回包仅表示驱动器应答，需读回确认参数；手动操作不执行软件行程保护' if parsed['write'] else '读取完成')

    async def update_config(self,body):
        # Connection changes have a separate explicit stop/disconnect/connect lifecycle.
        allowed={'cm_per_rev','travel_cm','base_cm','max_pp_speed','max_pv_speed','min_pp_speed','min_pv_speed','max_move_pulse','deadman_s','poll_s','startup_grace_s','stall_s','release_enable_s','soft_lower_cm','soft_upper_cm','verified_deceleration_cm_s2','control_latency_s','limit_margin_cm','home_timeout_s'}
        if set(body)-allowed:
            raise LabError('此处仅允许调整行程/速度/保护参数；连接参数请在连接设置中保存，再显式连接',400)
        if self.config.implementation=='candidate' and self.controller._sequence.locked():
            raise LabError('控制操作正在执行，不能同时修改配置')
        state=self.controller.snapshot()['details']
        if state.get('motion') not in ('stopped','stop_acknowledged',None) or state.get('pending_stop') or state.get('state') in ('STARTING','MOVING'):
            raise LabError('运动中或停止未确认，不能改配置')
        if self.config.implementation=='baseline':
            raise LabError('冻结基线配置通过启动文件指定；不热更新',400)
        values=asdict(self.config)
        values.update(body)
        candidate=Config(**values).validate()
        for mode in ('pp','pv'):
            candidate.speed_pr(self.controller.speeds[mode],mode)
        if self.config.mode=='real':
            # A changed safety profile must be physically re-validated, never inherit approval.
            candidate.motion_profile_verified=False
        ctrl=self.controller
        async with ctrl._sequence:
            issued=ctrl.epoch
            if self.manual_active or ctrl.mode is not None or ctrl.pending_stop:
                raise LabError('运动中或停止未确认，不能改配置')
            if self.connected:
                # ACK completes a JOG stop command, not physical standstill.
                # Verify only for configuration changes; consecutive JOG stays ACK-only.
                first=await ctrl.observe()
                await asyncio.sleep(.06)
                second=await ctrl.observe()
                if abs(first['speed_rpm'])>1 or abs(second['speed_rpm'])>1 or abs(first['position_feedback']-second['position_feedback'])>20:
                    raise LabError('尚未确认静止，请等待升降杆停止后再保存参数')
            if ctrl is not self.controller or issued!=ctrl.epoch or ctrl.pending_stop:
                raise LabError('控制状态已变化，请重新保存参数')
            self.controller.epoch+=1
            self.controller.armed=False
            self.controller.trusted=False
            await asyncio.get_running_loop().run_in_executor(None,atomic_json_write,ROOT/'.runtime/config.json',asdict(candidate))
            self.config.__dict__.update(candidate.__dict__)
            self.controller.driver.invalidate_parameters()
            if not self.controller.pending_stop:
                self.controller.state='DISARMED'
            self.controller.home_status='not_homed'
        return self.config.public()
