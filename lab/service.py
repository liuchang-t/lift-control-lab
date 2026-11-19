from __future__ import annotations

import asyncio
import hmac
import json
import re
import time
from dataclasses import asdict
from pathlib import Path
from .controller import Controller, DIRECT_ACTIONS

EXPLICIT_CONTROL_ACTIONS = DIRECT_ACTIONS | {'servo_enable', 'servo_disable'}
from .driver import Driver
from .legacy import Legacy
from .model import Config, LabError, atomic_json_write, ROOT
from .transport import Simulator, TcpTransport, Trace
from .manual_bus import parse_frame, validate_response
from .registers import CATALOG, REFRESH_GROUPS
from .readiness import build_readiness
from baseline.lift_modbus import build_read_holding_registers, parse_read_holding_registers


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
        self._register_refresh_lock=asyncio.Lock()
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
                return self.config.public(include_display=True)
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
            is_direct=suffix in ('/lift/control','/api/control') and body.get('action') in EXPLICIT_CONTROL_ACTIONS
            self._require_connected(allow_stop=is_stop or is_direct or path in ('/api/manual/transaction','/api/registers/refresh'))
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
                if action in EXPLICIT_CONTROL_ACTIONS and self.config.implementation=='candidate':
                    self._require_manual_connection()
                    # Explicit register buttons replace old raw/automatic intent.
                    self._clear_manual()
                    if self.connection_state=='stop_pending':
                        self.connection_state='connected'
                        self.connection_error=''
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
                    notice='手动写直接接管，仅发送所编辑报文；报警与反馈只观测，不自动插入停止。结束手动后普通控制需重新检查。' if self.manual_active else '手动读不打断运动；确认手动写取消普通任务，不自动停止设备。')

    def _clear_manual(self):
        self.manual_active=None
        self.controller.manual_active=False
        self.controller.manual_failed=False

    def _lock_manual(self,reason,request_stop=False):
        # Called by explicit stop/exit/disconnect. A later new manual request
        # can take over again; ordinary control still requires its own checks.
        if not self.manual_active:
            return
        self.manual_active.update(failed=False,reason=reason)
        self.controller.manual_failed=False
        if request_stop:
            self.controller._stop_intent()

    def _monitor_manual(self):
        if not self.manual_active:
            return
        ctrl=self.controller
        reason=''
        if not ctrl.sample_at or time.monotonic()-ctrl.sample_at>self.config.control_latency_s:
            reason='手动观测反馈过期；不自动发送控制指令，请核对通信和实际设备状态'
        elif ctrl.sample.get('alarm_raw',ctrl.sample.get('alarm_code',0)):
            reason='驱动器报告报警；仅显示，不拦截手动指令或自动复位/停止'
        elif ctrl.pending_stop:
            reason='用户请求的停止仍待确认；新手动写可接管并取消旧停止重试'
        self.manual_active['reason']=reason or self.manual_active.get('last_error','')

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
        self._require_connected(allow_stop=True)
        if self.config.implementation=='baseline':
            raise LabError('冻结基线仅展示已观测报文，不主动刷新候选寄存器',400)
        ctrl,transport=self.controller,self.transport
        def checkpoint():
            self._require_connected(allow_stop=True)
            if self.connection_state not in ('connected','stop_pending') or ctrl is not self.controller or transport is not self.transport:
                raise LabError('连接已变化，寄存器刷新已取消；请在当前连接重新刷新',409)
        errors=[]
        refreshed=0
        async with self._register_refresh_lock:
            checkpoint()
            for address,width in REFRESH_GROUPS:
                checkpoint()
                request=build_read_holding_registers(self.config.slave_id,address,width)
                try:
                    # Use the shared transport directly: observation must not
                    # accept changed parameters as the normal motion baseline.
                    response=await transport.transaction(request,5+2*width,before_send=checkpoint)
                    parse_read_holding_registers(response,self.config.slave_id,width)
                    checkpoint()
                    refreshed+=sum(address<=item[0]<address+width for item in CATALOG)
                except Exception as exc:
                    checkpoint()
                    transport.registers.read_failed(address,width,exc,transport.epoch)
                    errors.append(dict(address=address,address_hex='0x%04X'%address,error=str(exc) or type(exc).__name__))
                # A refresh has no controller-sequence lock. Yield between
                # bounded blocks for protection samples and explicit control.
                await asyncio.sleep(0)
        result=self.register_snapshot()
        result.update(cached=False,refreshed_count=refreshed,requested_count=len(CATALOG),
                      errors=errors,partial=bool(errors),
                      notice=('已实际读取%d/%d项寄存器；%d组读取失败，请查看错误项'%(refreshed,len(CATALOG),len(errors)))
                              if errors else '已实际读取%d项寄存器，主值均为读回；命令读回不代表动作完成'%refreshed)
        return result

    def _require_manual_connection(self):
        self._require_connected(allow_stop=True)
        if self.connection_state not in ('connected','stop_pending'):
            raise LabError('连接正在切换，不能将手动指令发送到不确定的目标',409)

    async def manual_transaction(self,client,body):
        self._require_manual_connection()
        parsed=parse_frame(body.get('frame'),self.config.slave_id)
        if parsed['write']:
            controller=self.controller
            issued=controller.epoch
            async with self._manual_lock:
                self._require_manual_connection()
                if controller is not self.controller or issued!=controller.epoch:
                    raise LabError('排队手动写已被停止或连接变化取消')
                return await self._manual_transaction(client,body)
        return await self._manual_transaction(client,body)

    async def _manual_transaction(self,client,body):
        if self.config.implementation=='baseline':
            raise LabError('冻结基线不提供手动总线接口',400)
        parsed=parse_frame(body.get('frame'),self.config.slave_id)
        ctrl=self.controller
        transport=self.transport
        def same_connection():
            self._require_manual_connection()
            if ctrl is not self.controller or transport is not self.transport:
                raise LabError('连接已变化，旧手动报文已取消')
        if not parsed['write']:
            response=await transport.transaction(parsed['request'],parsed['expected_length'],before_send=same_connection)
            validate_response(parsed,response)
        else:
            if body.get('confirm_write') is not True:
                raise LabError('请确认直接手动写入；它会接管普通任务，不自动执行停止或运动保护',400)
            if (not self.manual_active or self.manual_active['epoch']!=ctrl.epoch or ctrl.pending_stop):
                ctrl.take_manual_control()
                self.manual_active=dict(holder_id=client,failed=False,reason='',last_error='',epoch=ctrl.epoch,
                                        connection_epoch=transport.epoch)
                if self.connection_state=='stop_pending':
                    self.connection_state='connected'
                    self.connection_error=''
            issued=self.manual_active['epoch']
            def checkpoint():
                same_connection()
                if not self.manual_active or ctrl.epoch!=issued or ctrl.pending_stop:
                    raise LabError('排队手动写已被后续停止、断开或新控制意图取消')
            async with ctrl._sequence:
                checkpoint()
                try:
                    # Select the operator-stop channel at actual transmission,
                    # including uncertain ACK outcomes, never on a queued write.
                    def before_send():
                        checkpoint()
                        for offset,value in enumerate(parsed['values']):
                            address=parsed['address']+offset
                            if address==0x0D0B:
                                ctrl.driver.jog_context=True
                                ctrl.driver.jog_running=value in (17,33)
                                ctrl.driver.jog_enable_epoch=transport.epoch if value in (1,17,33) else None
                            elif address==0x0D08 and value in (1,3,5,7,8,16,32,64,128,256,512):
                                ctrl.driver.jog_context=False
                                ctrl.driver.jog_running=False
                    response=await transport.transaction(parsed['request'],parsed['expected_length'],before_send=before_send)
                    validate_response(parsed,response)
                    checkpoint()
                    self.manual_active['last_error']=''
                    self.trace.add('manual_write',frame=parsed['request'].hex(' '),client=client)
                except Exception as exc:
                    message='手动写未确认：'+str(exc)+'；不自动重发或插入停止，实际执行结果请核对反馈'
                    if self.manual_active and self.manual_active['epoch']==issued:
                        self.manual_active['last_error']=message
                        self.manual_active['reason']=message
                    self.last_rejection=dict(message=message,action='manual_write',at=time.time())
                    raise LabError(message,503)
            self._monitor_manual()
        self.last_rejection=None
        return dict(tx=parsed['request'].hex(' '),rx=response.hex(' '),function=parsed['function'],
                    address=parsed['address'],count=parsed['count'],write=parsed['write'],
                    notice='仅发送所编辑报文；应答不代表运动完成，报警/限位仅观测，不自动插入控制指令' if parsed['write'] else '读取完成')

    async def update_config(self,body):
        from .model import HOME_DISPLAY_FIELDS
        body=dict(body)
        for display,stored in HOME_DISPLAY_FIELDS.items():
            if display in body and stored in body:
                raise LabError('同一回零参数不能同时提交厘米单位与指令单位：'+display,400)
        # Keep the persisted schema in command units. Conversion is performed
        # only for explicitly edited display fields, using the candidate scale.
        display_input={key:body.pop(key) for key in HOME_DISPLAY_FIELDS if key in body}
        if display_input:
            conversion=Config(**{**asdict(self.config),**{key:body[key] for key in ('cm_per_rev','pulses_per_rev') if key in body}})
            try:
                conversion.validate()
                for key,value in display_input.items():
                    body[HOME_DISPLAY_FIELDS[key]]=conversion.home_display_input(value)
            except (ValueError,TypeError,ZeroDivisionError) as exc:
                raise LabError(str(exc),400) from exc
        # Connection changes have a separate explicit stop/disconnect/connect lifecycle.
        allowed={'pp_acceleration_cm_s2','pp_deceleration_cm_s2','pp_emergency_deceleration_cm_s2','pp_tolerance_cm','cm_per_rev','travel_cm','base_cm','max_pp_speed','max_pv_speed','min_pp_speed','min_pv_speed','max_move_pulse','deadman_s','poll_s','startup_grace_s','stall_s','release_enable_s','soft_lower_cm','soft_upper_cm','verified_deceleration_cm_s2','control_latency_s','limit_margin_cm','home_timeout_s','home_enable_hold_s','home_disable_hold_s','home_speed','home_creep_speed','home_acceleration'}
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
        changed={key for key in body if getattr(candidate,key)!=getattr(self.config,key)}
        pp_runtime_only=changed <= {'pp_tolerance_cm','pp_acceleration_cm_s2','pp_deceleration_cm_s2','pp_emergency_deceleration_cm_s2'}
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
            await asyncio.get_running_loop().run_in_executor(None,atomic_json_write,ROOT/'.runtime/config.json',asdict(candidate))
            self.controller.epoch+=1
            self.config.__dict__.update(candidate.__dict__)
            if not pp_runtime_only:
                self.controller.armed=False
                self.controller.trusted=False
                self.controller.driver.invalidate_parameters()
                if not self.controller.pending_stop:
                    self.controller.state='DISARMED'
                self.controller.home_status='not_homed'
        # PP runtime settings are applied/read back on the next PP start.
        # Saving them neither changes the coordinate reference nor writes the drive.
        return self.config.public(include_display=True)
