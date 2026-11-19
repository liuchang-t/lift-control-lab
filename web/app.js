/* Normal motion uses controller guards; confirmed manual frames use direct RTU transport. */
const clientId=sessionStorage.liftLabClient||(sessionStorage.liftLabClient=crypto.randomUUID());
const $=id=>document.getElementById(id);
let config={},live={},draggingTarget=false,readiness=null,registers=[],lastNotice='',pendingFrame=null;
const editingSpeed={pp:false,pv:false};
let editingMechanical=false,unitVerification=null;
let connection={connected:false,state:'unknown',mode:'real',current:{},saved:{}},connectionBusy=false,connectionFieldsDirty=false,connectionLoaded=false,connectionRevision=0;
const baseline=()=>live.implementation==='baseline'||config.implementation==='baseline';
function notify(message){lastNotice=message;$('rejection').textContent=message;$('rejection').hidden=!message;}
async function api(path,body,keepalive=false){
  const headers={'X-Patrol-Client-Id':clientId};if(body!==undefined)headers['Content-Type']='application/json';
  const response=await fetch(path,{method:body===undefined?'GET':'POST',headers,body:body===undefined?undefined:JSON.stringify(body),keepalive});
  const value=await response.json();if(!response.ok)throw new Error(value.detail||response.status);return value;
}
const control=(body,keepalive=false)=>{if(!connection.connected)throw new Error('尚未连接：选择连接模式后点击顶部“连接”。');return api('/api/devices/lift_pole_1/lift/control',body,keepalive);};
function click(id,fn){$(id).addEventListener('click',async()=>{try{await fn();await refreshReadiness();}catch(e){notify(e.message);await refreshReadiness();}});}
let confirmResolve=null;
function ask(title,text){
  if(confirmResolve)confirmResolve(false);
  $('confirmTitle').textContent=title;$('confirmText').textContent=text;$('confirmPanel').hidden=false;
  return new Promise(resolve=>{confirmResolve=resolve;});
}
function settleConfirmation(value){$('confirmPanel').hidden=true;if(confirmResolve){confirmResolve(value);confirmResolve=null;}}
click('confirmYes',()=>settleConfirmation(true));click('confirmNo',()=>settleConfirmation(false));
const holds=new Set();
function bindHold(button,action){
  let intent=null,timer=null,heartbeatBusy=false;
  const release=async(event,send=true)=>{if(!intent)return;const token=intent;intent=null;clearInterval(timer);button.classList.remove('held');if(send===false)return;try{await control({action:'stop',intent_id:token},true);}catch(e){notify(e.message);}};
  holds.add(release);
  button.addEventListener('pointerdown',async event=>{
    if(event.button!==0||intent||button.disabled||!connection.connected)return;event.preventDefault();button.setPointerCapture(event.pointerId);
    const token=crypto.randomUUID();intent=token;button.classList.add('held');
    const heartbeat=async()=>{
      if(intent!==token||heartbeatBusy)return;
      heartbeatBusy=true;
      try{await control({action:'heartbeat',intent_id:token});}
      catch(e){if(intent===token){await release();notify('点动已结束：'+e.message+'。请按界面指引处理后再操作。');refreshReadiness();}}
      finally{heartbeatBusy=false;}
    };
    try{
      await control({action,intent_id:token});
      if(intent===token){timer=setInterval(heartbeat,300);heartbeat();}
      else control({action:'stop',intent_id:token},true).catch(e=>notify(e.message));
    }catch(e){if(intent===token){await release();notify(e.message);refreshReadiness();}}
  });
  ['pointerup','pointercancel','lostpointercapture'].forEach(name=>button.addEventListener(name,release));
}
function releaseAll(send=true){return Promise.all([...holds].map(fn=>fn(null,send)));}
click('servoEnable',async()=>{await control({action:'servo_enable'});notify('使能命令已应答，未启动运动；继续运动前点击“检查 / 恢复”。');});
click('servoDisable',async()=>{await control({action:'servo_disable'});notify('停止及失能命令已应答；继续运动前点击“检查 / 恢复”。');});
window.addEventListener('blur',releaseAll);window.addEventListener('pagehide',releaseAll);
document.addEventListener('visibilitychange',()=>{if(document.hidden)releaseAll();});
document.querySelectorAll('[data-jog]').forEach(b=>bindHold(b,b.dataset.jog));
click('arm',async()=>{if(connection.mode==='real'&&!await ask('检查 / 恢复前确认','即将检查驱动参数；检查本身不启动运动。请确认现场可随时停止设备，再继续。'))return;await api('/api/arm',{hardware_confirmed:true});notify('检查通过。按上方恢复指引继续。');});
click('stop',async()=>{releaseAll(false);const result=await control({action:'stop'});const d=result.status?.details||result.payload?.state;notify(d?.stop_confirmation==='feedback'?'全局停止已发送并确认静止。':'停止请求已处理，请核对实际反馈。');});
click('estop',async()=>{releaseAll(false);settleConfirmation(false);pendingFrame=null;$('writeConfirm').hidden=true;await control({action:'estop'});notify('软件急停已锁定。排查原因后点“检查 / 恢复”；位置未可信时还需回零。');});
click('home',async()=>{if(baseline())throw new Error('冻结基线不支持回零');if(connection.mode==='real'&&!await ask('模式17回零','将向负限位寻找并反向脱离。已确认方向、限位、HOME速度和行程安全？'))return;await control({action:'home',confirm_homing:true});notify('正在回零。请观察回零阶段、限位和位置反馈。');});
function operatorMessage(result){
  const d=result.status?.details||result.payload?.state||{},r=d.operator_result;
  if(!r)return '命令已处理';
  const name=r.action==='reset_fault'?'P0D-01': 'P03-07';
  if(!r.write_ack)return `${name}=${r.value} 写应答未确认`;
  if(r.read_error)return `${name}=${r.value} 已应答；读回失败：${r.read_error}`;
  if(r.action==='reset_fault')return r.cleared?'P0D-01=1 已应答；报警已清除':`P0D-01=1 已应答；当前报警 ${r.alarm?.text||r.read_value}`;
  return `P03-07=${r.value} 已应答；读回 ${r.read_value}`;
}
click('resetFault',async()=>{releaseAll(false);notify(operatorMessage(await control({action:'reset_fault'})));});
click('servoP03Enable',async()=>{releaseAll(false);notify(operatorMessage(await control({action:'servo_p03_enable'})));});
click('servoP03Disable',async()=>{releaseAll(false);notify(operatorMessage(await control({action:'servo_p03_disable'})));});
function inputNumber(id){const text=String($(id).value).trim();if(!text||!Number.isFinite(Number(text)))throw new Error('请输入有效数值');return Number(text);}
function targetCommand(){const target=inputNumber('target');return baseline()?{action:'goto',height:target/config.travel_cm*100}:{action:'goto',target_cm:target};}
click('goto',()=>control(targetCommand()));
document.querySelectorAll('[data-action]').forEach(b=>b.addEventListener('click',async()=>{try{await control({action:b.dataset.action});await refreshReadiness();}catch(e){notify(e.message);refreshReadiness();}}));
async function saveSpeed(mode){if(baseline())throw new Error('冻结基线不支持独立速度设置');const result=await control({action:'set_speed',mode,speed_cm_s:inputNumber(mode+'Speed')});editingSpeed[mode]=false;notify(mode.toUpperCase()+' 速度已保存。');return result;}
for(const mode of ['pp','pv'])$(mode+'Speed').addEventListener('input',()=>editingSpeed[mode]=true);
click('savePpSpeed',()=>saveSpeed('pp'));click('savePvSpeed',()=>saveSpeed('pv'));
click('saveLegacySpeed',()=>control({action:'set_speed',speed:inputNumber('legacySpeed')}));
click('legacyZero',async()=>{if(await ask('基线设零','仅模拟：将当前反馈记为软件零点，不执行回零。'))await control({action:'set_retracted_zero',confirm_retracted:true});});
$('target').addEventListener('focus',()=>draggingTarget=true);$('target').addEventListener('blur',()=>draggingTarget=false);
const configKeys=['travel_cm','base_cm','soft_lower_cm','soft_upper_cm','min_pp_speed','max_pp_speed','min_pv_speed','max_pv_speed','max_move_pulse','deadman_s','poll_s','startup_grace_s','stall_s','verified_deceleration_cm_s2','control_latency_s','limit_margin_cm','home_timeout_s','home_enable_hold_s','home_disable_hold_s'];
const homeParameterKeys=['home_speed','home_creep_speed','home_acceleration'];
function homeParameterValues(){return Object.fromEntries(homeParameterKeys.map(k=>[k,$(k).value.trim()===''?null:Number($(k).value)]));}
function showConfig(value){config=value;for(const k of homeParameterKeys)$(k).value=config[k]??'';if(!editingMechanical&&config.cm_per_rev!==undefined)$('mechanicalCm').value=config.cm_per_rev;$('target').min=config.soft_lower_cm??0;$('target').max=config.soft_upper_cm??config.travel_cm;$('config').value=JSON.stringify(Object.fromEntries(configKeys.filter(k=>config[k]!==undefined).map(k=>[k,config[k]])),null,2);}
click('openConfig',()=>$('configPanel').hidden=false);click('closeConfig',()=>$('configPanel').hidden=true);
click('saveConfig',async()=>{showConfig(await api('/api/config',{...JSON.parse($('config').value),...homeParameterValues()}));$('configPanel').hidden=true;notify('配置已保存。下一步：检查 / 恢复即可基础点动；定位另需回零。');});
function ageSeconds(value){return value===null||value===undefined?'—':Math.max(0,Math.floor(Number(value)))+' 秒';}
function number(value,unit='',digits=2){return value===null||value===undefined?'—':Number(value).toFixed(digits)+(unit?' '+unit:'');}
function renderSpeeds(d){for(const mode of ['pp','pv']){const speed=d.speeds?.[mode];if(!speed){$(mode+'SpeedInfo').textContent='等待读取';continue;}if(!editingSpeed[mode])$(mode+'Speed').value=Number(Number(speed.requested_cm_s).toPrecision(12));$(mode+'Speed').max=speed.max_cm_s;$(mode+'SpeedInfo').textContent=`非零 ${number(speed.min_cm_s,'',3)}–${number(speed.max_cm_s,'',3)} · 比例 ${number(speed.ratio)}`;$(mode+'SpeedInfo').dataset.tip=`已保存 ${number(speed.requested_cm_s,'cm/s',3)}；${number(speed.pr_s,'Pr/s')}；${number(speed.rpm,'rpm')}。速度以cm/s独立保存，0表示不启动。`;}}
function renderAlarm(d){const alarm=d.alarm;$('alarmTitle').textContent=alarm?`${alarm.text||''} ${alarm.name||''}`:'等待读取';$('alarmCode').textContent=alarm?.raw_hex||'';$('alarmAdvice').textContent=alarm?.advice||'';$('alarmPolicy').textContent=alarm?.policy||'';$('resetFault').disabled=directButtonDisabled();}
function render(value){
  // Device status and connection arrive in the same SSE snapshot; render them together.
  if(value.connection)renderConnection(value.connection);
  live=value.status;const d=live.details||{},check=d.eeprom_check;
  if(live.online&&d.di4_function===14&&d.di5_function===15&&[0,1].includes(d.di4_logic)&&[0,1].includes(d.di5_logic)&&d.upper_limit===false&&d.lower_limit===false&&lastNotice.includes('物理限位禁止该方向'))notify('');
  if(lastNotice.startsWith('正在回零')&&!['starting','homing'].includes(d.home_status))notify('');
  $('eeprom').textContent=check?`P0C-13 ${check.value??'未知'}${check.status==='warning'?' · 待检查':''}`:'冻结基线不提供 P0C-13 检查';$('eeprom').dataset.status=check?.status||'unknown';$('eeprom').dataset.tip=check?.message||'基线仅模拟';
  $('mode').textContent=(live.control_mode==='real'?'真实设备':'寄存器模拟')+' · '+(baseline()?'冻结基线':'候选实现');$('online').textContent=live.online?'在线':'离线 / 过期';
  $('height').textContent=number(d.height_cm,'cm');$('rpm').textContent=number(d.speed_rpm,'rpm');$('speedApplied').textContent=number(d.command_speed_cm_s,'cm/s');
  const states={DISARMED:'待检查',IDLE:'静止',MOVING:'运动中',HOMING:'回零中',STARTING:'准备启动',STOPPING:'停止确认中',FAULT:'故障锁定',MOTION_UNKNOWN:'运动状态未知',MANUAL:'手动接管',MANUAL_LOCKED:'手动通信异常'};
  const stateLabel=d.motion_notice?d.motion_notice:d.stop_confirmation==='ack'&&d.state==='IDLE'?'停止指令已应答'+(d.observed_motion?' · 反馈仍有运动':''):d.untracked_motion?'外部/未跟踪运动':states[d.state]||d.state||d.motion||'未知';
  $('state').dataset.tip=d.motion_advice||(d.stop_confirmation==='ack'&&d.state==='IDLE'?'停止命令已应答；当前转速见状态栏。':d.untracked_motion?'反馈显示设备在运动，当前无控制台运动任务。':'');
  $('state').textContent=`${stateLabel} · ${d.position_trusted?'位置可信':'位置未可信'} · ${d.armed?'已检查':'待检查'}`;
  const homes={not_homed:'未回零',complete:'回零完成',cancelled:'回零已取消',failed:'回零失败',running:'回零中',starting:'回零准备中',homing:'回零中'};$('homeStatus').textContent=baseline()?'基线无回零':({preparing:'准备参数与使能给定',seeking_lower:'寻找下限位',leaving_lower:'脱离下限位',settling:'核对回零完成与静止'})[d.home_phase]||homes[d.home_status]||d.home_status||'未回零';
  $('sensors').dataset.tip='P0B-02按16位有符号原码显示；百分比倍率待核实，原始十六进制见寄存器表。';
  $('sensors').textContent=`上限 ${d.upper_limit===undefined?'未知':d.upper_limit?'触发':'正常'} / 下限 ${d.lower_limit===undefined?'未知':d.lower_limit?'触发':'正常'} · 负载原码 ${d.load_signed??'—'} · ${ageSeconds(d.observed_age_s)}前`;
  $('limitsInfo').textContent=`软件范围 ${number(d.soft_lower_cm??config.soft_lower_cm)}–${number(d.soft_upper_cm??config.soft_upper_cm)} cm`;
  $('fault').textContent=(d.faults||[]).join('；');
  $('home').disabled=baseline()||!live.online||!d.armed||!!d.pending_stop||['MOVING','HOMING','STARTING'].includes(d.state);
  $('independentSpeeds').hidden=baseline();$('legacySpeedSettings').hidden=!baseline();if(!baseline())renderSpeeds(d);renderAlarm(d);
  if(!draggingTarget&&d.target_cm!=null)$('target').value=Number(d.target_cm).toFixed(3);
  renderConversion(d.unit_verification);
  renderForceInputs(d.force_inputs);
  applyConnectionState();
}
function renderForceInputs(value){
  const v=value||{},fresh=connection.connected&&v.fresh,req=v.requested||{};
  for(const [suffix,address,key] of [['17','3345','selection'],['18','3346','given']]){
    $('force'+suffix+'Requested').textContent=connection.connected?(req[address]??'—'):'—';
    $('force'+suffix+'Read').textContent=fresh?(v[key]??'—'):'—';
  }
  $('forceAge').textContent=!connection.connected?'已断开':fresh?ageSeconds(v.age_s)+'前':v.error?'读取失败':'待读取 / 过期';
  $('forceDi3').textContent='P0B-03 · DI3观测：'+(fresh&&v.di3!=null?v.di3+'（'+(v.di3?'高':'低')+'电平）':'—');
  const pairs=[['3345','selection'],['3346','given']].filter(([a])=>req[a]!==undefined);
  $('forceMatch').textContent=!fresh?'未核对':!pairs.length?'仅观测，尚无正常流程给定':pairs.every(([a,k])=>req[a]===v[k])?'最近给定与读回一致':'最近给定与读回不同';
  $('forceAge').dataset.tip=v.error||'自动读取；显示距最近一次观测的整秒时间。';
}
function renderReadiness(value){
  readiness=value;
  $('manualState').textContent=value.manual?'手动接管 · 直接收发':'普通控制';$('endManual').disabled=!value.manual;$('guideSummary').textContent=value.summary||'检查操作条件';$('guideSteps').textContent=(value.steps||[]).join(' → ');$('conditions').replaceChildren();
  for(const check of value.checks||[]){const chip=document.createElement('span');chip.className='condition'+(check.ok?' ok':'');chip.textContent=(check.ok?'✓ ':'○ ')+check.label;chip.dataset.tip=check.detail||'';$('conditions').appendChild(chip);}
  $('guideActions').replaceChildren();for(const action of value.actions||[]){const button=document.createElement('button');button.textContent=action.label;button.disabled=action.enabled===false;button.dataset.tip=action.reason||'';button.addEventListener('click',()=>{const map={reset_fault:'resetFault',readParameters:'refreshRegisters',connect:'connectDevice',connect_device:'connectDevice',disconnect:'disconnectDevice'};if(action.id==='exit_manual'){api('/api/manual/exit',{}).then(()=>{notify('手动接管已结束。下一步：检查 / 恢复即可基础点动；定位另需回零。');return refreshReadiness();}).catch(e=>notify(e.message));}else $(map[action.id]||action.id)?.click();});$('guideActions').appendChild(button);}
  applyConnectionState();
  if(value.last_rejection?.message){const when=value.last_rejection.at||0;if(when>renderReadiness.rejectionAt){renderReadiness.rejectionAt=when;notify('操作被拒绝：'+value.last_rejection.message);}}else if(value.can_move&&lastNotice.startsWith('操作被拒绝：'))notify('');
}
renderReadiness.rejectionAt=0;
let readingReadiness=false;
async function refreshReadiness(){if(readingReadiness)return;readingReadiness=true;try{renderReadiness(await api('/api/readiness'));}catch(e){$('guideSummary').textContent='操作条件暂不可用';$('guideSteps').textContent=e.message;}finally{readingReadiness=false;}}
function renderRegisters(value){registers=(value.registers||[]).slice().sort((a,b)=>Number(a.address??a.address_hex)-Number(b.address??b.address_hex));$('registerNotice').hidden=true;filterRegisters();}
function filterRegisters(){
  const filter=$('registerFilter').value.trim().toLowerCase(),visible=registers.filter(r=>r.visible!==false).filter(r=>[r.parameter,r.name,r.address_hex,r.polling_label].join(' ').toLowerCase().includes(filter));
  $('registerRows').replaceChildren();
  for(const r of visible){
    const row=document.createElement('tr');if(r.stale)row.className='stale';
    const identifier=document.createElement('td');identifier.textContent=r.parameter;const addr=document.createElement('small');addr.textContent=r.address_hex;identifier.appendChild(addr);
    const name=document.createElement('td');name.textContent=r.name;
    const polling=document.createElement('small');polling.className='polling-badge';polling.dataset.kind=r.polling_kind||'unknown';polling.textContent=r.polling_label||'读取机制未标注';name.appendChild(polling);
    const actual=r.source==='read';const val=document.createElement('td');val.className='value-cell';val.textContent=actual?(r.value??'—'):'—';if(r.last_write_value!==undefined&&r.last_write_value!==null)val.dataset.tip=`最近写应答 ${r.last_write_value} · ${ageSeconds(r.last_write_age_s)}前（不是读回）`;const raw=document.createElement('small');raw.textContent=(actual?(r.raw_words||[]):[]).map(x=>Number(x).toString(16).padStart(4,'0').toUpperCase()).join(' ');val.appendChild(raw);
    const observed=document.createElement('td');observed.textContent=!actual||r.value===null||r.value===undefined?'未读取':`${r.stale?'过期 · ':''}${ageSeconds(r.age_s)}`;
    const source=document.createElement('small');source.textContent=r.read_error?'读取失败':r.readback_pending?'写后待读回':actual?'读回':'';if(r.read_error)source.dataset.tip=r.read_error;observed.appendChild(source);
    const access=document.createElement('td');access.className='access-cell';access.textContent=r.access||'—';
    row.append(identifier,name,access,val,observed);$('registerRows').appendChild(row);
  }
  $('registerCount').textContent=`${visible.length} / ${registers.filter(r=>r.visible!==false).length} 项`;
}
function renderConversion(value){
  unitVerification=value||null;
  const v=value||{},status={consistent:'驱动/配置数值一致',simulated_consistent:'模拟数值一致',mismatch:'驱动与配置不一致',unknown:'驱动换算未确认'};
  $('conversionStatus').textContent=status[v.status]||'驱动换算未确认';$('conversionStatus').dataset.status=v.status||'unknown';
  $('conversionEncoder').textContent=v.encoder_counts_per_rev??'—';$('conversionPulses').textContent=v.pulses_setting??'—';
  $('conversionGear').textContent=`${v.gear_numerator??'—'} / ${v.gear_denominator??'—'}`;
  $('conversionPath').textContent=({direct:'P05-02 直接设定',ratio:'电子齿轮比',unknown:'未确认'})[v.path]||'未确认';
  $('conversionEffective').textContent=v.effective_command_per_rev??'—';$('conversionEffective').dataset.tip=v.effective_fraction?'精确值 '+v.effective_fraction:'';
  $('conversionConfigured').textContent=v.configured_command_per_rev??config.pulses_per_rev??'—';
  if(!editingMechanical&&v.mechanical_cm_per_rev!==undefined)$('mechanicalCm').value=v.mechanical_cm_per_rev;
  $('conversionStatus').dataset.tip=`驱动参数与工具配置最近核对 ${ageSeconds(v.observed_age_s)}前。`;
}
$('mechanicalCm').addEventListener('input',()=>editingMechanical=true);
click('saveMechanical',async()=>{
  const value=inputNumber('mechanicalCm');if(value<=0)throw new Error('控制台机械比例必须大于0');
  const saved=await api('/api/config',{cm_per_rev:value});editingMechanical=false;showConfig(saved);renderConversion(await api('/api/conversion'));
  notify('控制台比例已保存，仍待实机核实；驱动器电子齿轮和PP/PV期望cm/s未修改。下一步：检查 / 恢复即可基础点动；定位另需回零。');
});
$('registerFilter').addEventListener('input',filterRegisters);
click('refreshRegisters',async()=>{notify('正在读取寄存器…');const result=await api('/api/registers/refresh',{});renderRegisters(result);const failures=(result.errors||[]).map(e=>`${e.address_hex}: ${e.error}`).join('；');notify(`${result.notice||`已读回 ${result.refreshed_count??0} / ${result.requested_count??0} 项`}${failures?'；'+failures:''}`);});
function modbusCrc(bytes){
  let crc=0xFFFF;
  for(const byte of bytes){crc^=byte;for(let bit=0;bit<8;bit++)crc=(crc&1)?(crc>>>1)^0xA001:crc>>>1;}
  return [crc&0xFF,(crc>>>8)&0xFF];
}
function inspectFrame(){
  const automatic=$('autoCrc').checked,text=$('manualFrame').value.trim();
  if(!text)throw new Error(automatic?'请输入485报文正文（不含CRC）':'请输入完整485报文（含CRC）');
  if(!/^[0-9a-fA-F\s]+$/.test(text))throw new Error('报文只能包含十六进制字符和空格');
  const hex=text.replace(/\s/g,'');if(hex.length%2||hex.length<12)throw new Error('报文须为完整字节，正文至少6字节');
  const bytes=hex.match(/../g).map(x=>parseInt(x,16)),fn=bytes[1];
  if(![3,6,16].includes(fn))throw new Error('支持功能码03、06、10；其它功能码不发送');
  let bodyLength=6;
  if(fn===3){const count=(bytes[4]<<8)|bytes[5];if(count<1||count>125)throw new Error('03读取数量必须为1–125个寄存器');}
  if(fn===16){
    if(bytes.length<7)throw new Error('10指令缺少字节数量');
    const count=(bytes[4]<<8)|bytes[5];
    if(count<1||count>123||bytes[6]!==count*2)throw new Error('10指令寄存器数量或字节数量不一致');
    bodyLength=7+count*2;
  }
  if(automatic&&bytes.length===bodyLength+2)throw new Error('输入似乎已含CRC；请关闭“自动 CRC”或移除末尾2字节，未重复追加');
  if(bytes.length!==bodyLength+(automatic?0:2))throw new Error(`报文长度不符：${automatic?'正文':'含CRC完整帧'}应为${bodyLength+(automatic?0:2)}字节`);
  const complete=automatic?bytes.concat(modbusCrc(bytes)):bytes;
  return {frame:complete.map(x=>x.toString(16).padStart(2,'0').toUpperCase()).join(' '),write:fn!==3};
}
function updateManualPreview(){
  pendingFrame=null;$('writeConfirm').hidden=true;
  const automatic=$('autoCrc').checked;
  $('manualFrameLabel').textContent=automatic?'485报文（不含CRC）':'485报文（含CRC）';
  $('manualFrame').placeholder=automatic?'01 03 0C 0D 00 01':'01 03 00 00 00 0A C5 CD';
  try{const result=inspectFrame();$('manualKind').textContent=result.write?'写入 · 需确认':'读取';$('manualPreview').textContent='TX（含CRC）：'+result.frame;}
  catch(e){$('manualKind').textContent=e.message;$('manualPreview').textContent='TX（含CRC）：尚未形成有效帧';}
}
$('manualFrame').addEventListener('input',updateManualPreview);
$('autoCrc').addEventListener('change',updateManualPreview);
async function sendManual(frame,confirmWrite){try{const result=await api('/api/manual/transaction',{frame,confirm_write:confirmWrite});$('manualResult').textContent=`TX  ${result.tx}\nRX  ${result.rx}\n${result.notice||''}`;notify(result.write?'手动写入已应答；实际效果请观察反馈。':'手动读取完成。');await refreshRegistersCache();}catch(e){$('manualResult').textContent='未确认成功：'+e.message;throw e;}finally{await refreshReadiness();}}
click('endManual',async()=>{await api('/api/manual/exit',{});notify('手动接管已结束。下一步：检查 / 恢复即可基础点动；定位另需回零。');});
click('sendFrame',async()=>{const frame=inspectFrame();$('manualPreview').textContent='TX（含CRC）：'+frame.frame;if(frame.write){pendingFrame=frame.frame;$('writeConfirm').hidden=false;$('manualResult').textContent='等待确认，尚未发送。';}else await sendManual(frame.frame,false);});
click('confirmFrame',async()=>{if(!pendingFrame)throw new Error('报文已改变，请重新检查');const frame=pendingFrame;pendingFrame=null;$('writeConfirm').hidden=true;await sendManual(frame,true);});
click('cancelFrame',()=>{pendingFrame=null;$('writeConfirm').hidden=true;$('manualResult').textContent='已取消，未发送。';});
const logBuffer=new LogBuffer();let logsBusy=false,clearingLogs=false,renderingLogs=false;
const LOG_PAGE_SIZE=300;
let logPageOffset=0,lastLogRenderKey='';
function logWindow(){
  const end=Math.max(0,logBuffer.view.length-logPageOffset),start=Math.max(0,end-LOG_PAGE_SIZE);
  return {start,end,rows:logBuffer.view.slice(start,end)};
}
function logSummary(){
  const {start,end}=logWindow();
  $('followLogs').textContent=logBuffer.follow?'暂停':'跟随最新';
  $('olderLogs').disabled=start===0;$('newerLogs').disabled=logPageOffset===0;
  $('logStatus').textContent=`${logBuffer.follow?'跟随':'已暂停'} · ${end?start+1:0}–${end} / ${logBuffer.view.length.toLocaleString()}${logBuffer.pending?' · +'+logBuffer.pending:''}${logBuffer.gap?' · 历史截断':''}`;
}
function renderLogs(force=false){
  if(!logBuffer.follow&&!force){logSummary();return;}
  if(logBuffer.follow)logPageOffset=0;
  const {start,end,rows}=logWindow();
  const key=[logBuffer.generation,start,end,rows[0]?.id,rows[rows.length-1]?.id].join(':');
  if(key===lastLogRenderKey){logSummary();return;}
  renderingLogs=true;
  try{
    $('logs').textContent=rows.map(x=>{
      const time=new Date(x.at*1000).toLocaleTimeString('zh-CN',{hour12:false})+'.'+String(Math.round(x.at*1000)%1000).padStart(3,'0');
      return `${x.id} ${time} ${x.kind} ${x.frame||x.error||JSON.stringify(Object.fromEntries(Object.entries(x).filter(([k])=>!['id','at','kind'].includes(k))))}`;
    }).join('\n')||'暂无记录';
    $('logs').scrollTop=logBuffer.follow?$('logs').scrollHeight:0;
    lastLogRenderKey=key;
  }finally{renderingLogs=false;logSummary();}
}
function pageLogs(direction){
  if(logBuffer.follow)logBuffer.pause();
  const maximum=Math.max(0,Math.ceil(logBuffer.view.length/LOG_PAGE_SIZE)-1)*LOG_PAGE_SIZE;
  logPageOffset=Math.max(0,Math.min(maximum,logPageOffset+direction*LOG_PAGE_SIZE));
  renderLogs(true);
}
async function refreshLogs(){
  if(logsBusy||clearingLogs)return;logsBusy=true;const generation=logBuffer.generation;
  try{const batch=await api('/api/logs/tail',{after:logBuffer.cursor});if(!clearingLogs&&logBuffer.accept(batch,generation))renderLogs();}finally{logsBusy=false;}
}
click('refreshLogs',refreshLogs);
click('olderLogs',()=>pageLogs(1));click('newerLogs',()=>pageLogs(-1));
click('followLogs',()=>{if(logBuffer.follow)logBuffer.pause();else{logBuffer.resume();logPageOffset=0;}renderLogs();});
$('logs').addEventListener('scroll',()=>{if(!renderingLogs&&logBuffer.follow&&$('logs').scrollHeight-$('logs').clientHeight-$('logs').scrollTop>8){logBuffer.pause();logSummary();}});
click('clearLogs',async()=>{
  clearingLogs=true;logBuffer.generation++;
  try{const result=await api('/api/logs/clear',{});logBuffer.clear(result.cursor);logPageOffset=0;lastLogRenderKey='';renderLogs(true);}
  finally{clearingLogs=false;}
});
function connectionParameters(){
  return {mode:$('connectionMode').value,host:$('connectionHost').value.trim(),port:inputNumber('connectionPort'),slave_id:inputNumber('connectionSlave'),timeout:inputNumber('connectionTimeout')};
}
function directButtonDisabled(){return baseline()||connection.connected!==true||connectionBusy||['connecting','disconnecting'].includes(connection.state);}
function applyConnectionState(){
  const active=connection.connected===true,transition=connectionBusy||['connecting','disconnecting'].includes(connection.state);
  const stateNames={connected:'已连接',disconnected:'已断开',connecting:'连接中',disconnecting:'断开中',error:'连接失败',stop_pending:'停止待确认',unknown:'读取状态中'};
  $('connectionState').textContent=stateNames[connection.state]||(active?'已连接':'未连接');
  $('connectionState').dataset.status=active?'ok':connection.error?'error':'off';
  $('connectionState').dataset.tip=connection.error||((connection.mode==='real'?'真实设备':'寄存器模拟')+(active?'已连接':'未连接'));
  $('connectionMode').disabled=active||transition;
  $('connectDevice').disabled=active||transition||connection.state==='unknown';
  $('disconnectDevice').disabled=!active||transition;
  const disabled=!active||transition||connection.state==='stop_pending';
  $('servoEnable').disabled=disabled||baseline()||!!readiness?.manual||['MOVING','HOMING','STARTING','STOPPING'].includes(live.details?.state)||!!live.details?.pending_stop;
  $('servoDisable').disabled=!active||transition||baseline()||!!readiness?.manual;
  for(const id of ['arm','goto','min','max','savePpSpeed','savePvSpeed','saveLegacySpeed','legacyZero'])$(id).disabled=disabled;
  $('refreshRegisters').disabled=!active||transition;
  for(const id of ['sendFrame','confirmFrame'])$(id).disabled=!active||transition;
  document.querySelectorAll('[data-jog]').forEach(b=>b.disabled=disabled);
  if(connection.mode==='real'&&!baseline()){for(const id of ['goto','min','max'])$(id).disabled=true;}
  // Stops stay available while a disconnect request is waiting for hardware confirmation.
  $('stop').disabled=!active;$('estop').disabled=!active;
  if(disabled){$('home').disabled=true;$('endManual').disabled=true;}
  for(const id of ['resetFault','servoP03Enable','servoP03Disable'])$(id).disabled=directButtonDisabled();
  if(!active){
    renderForceInputs(null);
    $('mode').textContent=(connection.mode==='real'?'真实设备':'模拟')+' · 未连接';
    for(const id of ['height','rpm','speedApplied'])$(id).textContent='—';
    $('online').textContent='未连接';$('state').textContent='未连接 · 待检查';$('homeStatus').textContent='连接后重新建立位置参考';
    $('sensors').dataset.tip='P0B-02按16位有符号原码显示；百分比倍率待核实，原始十六进制见寄存器表。';
  $('sensors').textContent='无实时反馈';$('eeprom').textContent='P0C-13 未读取';$('eeprom').dataset.status='unknown';$('eeprom').dataset.tip='连接后读取当前值。断开期间不显示旧值为实时值。';
    $('conversionStatus').textContent='未连接 · 换算未确认';$('conversionStatus').dataset.status='unknown';
    $('alarmTitle').textContent='未连接';$('alarmCode').textContent='';$('alarmAdvice').textContent='连接后读取驱动器报警。';
  }
}
function renderConnection(value,forceFields=false){
  connectionRevision++;
  connection=value;
  const c=value.current||{},saved=value.saved||{};
  if(value.connected)$('connectionMode').value=value.mode;
  else if(!connectionLoaded)$('connectionMode').value=value.mode||'real';
  $('connectionCurrent').textContent=value.connected?(value.mode==='sim'?'当前：模拟器（未连接真实设备）':`当前：${c.host}:${c.port} · 从站 ${c.slave_id}`):'当前：未连接';
  if(forceFields||!connectionFieldsDirty){$('connectionHost').value=saved.host??'';$('connectionPort').value=saved.port??'';$('connectionSlave').value=saved.slave_id??'';$('connectionTimeout').value=saved.timeout??'';}
  if(value.error)$('connectionNotice').textContent=value.error;
  connectionLoaded=true;applyConnectionState();
}
let connectionReading=false;
async function refreshConnection(){
  if(connectionReading)return;connectionReading=true;const revision=connectionRevision;
  try{
    const value=await api('/api/connection');
    // A later SSE or explicit connect/disconnect response wins over this older GET.
    if(revision!==connectionRevision)for(const key of ['connected','state','error','mode','current'])value[key]=connection[key];
    renderConnection(value);
  }finally{connectionReading=false;}
}
for(const id of ['connectionHost','connectionPort','connectionSlave','connectionTimeout'])$(id).addEventListener('input',()=>connectionFieldsDirty=true);
$('connectionMode').addEventListener('change',()=>{$('connectionNotice').textContent='仅改变待连接模式，尚未建立连接。';});
click('saveConnection',async()=>{
  const value=await api('/api/connection',connectionParameters());connectionFieldsDirty=false;
  if(value.current&&value.saved)renderConnection(value,true);else await refreshConnection();
  $('connectionNotice').textContent=value.notice||'参数已保存；断开后点击“连接”使用新值，不自动连接。';
});
click('connectDevice',async()=>{
  if(connection.connected)throw new Error('请先断开当前连接，再连接其它模式或地址。');
  const desired={...(connection.saved||{}),mode:$('connectionMode').value};
  if(!desired.host||!desired.port)throw new Error('请打开“调试参数”保存连接地址和端口。');
  if(desired.mode==='real'&&!await ask('连接真实设备',`将连接 ${desired.host}:${desired.port}，从站 ${desired.slave_id}。请确认这是要测试的驱动器；连接后先只读检查，不自动启动运动。`))return;
  connectionBusy=true;applyConnectionState();
  try{const value=await api('/api/connection/connect',{...desired,confirm_real:desired.mode==='real'});if(value.current)renderConnection(value);else await refreshConnection();showConfig(await api('/api/config'));pendingFrame=null;$('writeConfirm').hidden=true;notify('连接已建立。先点击“检查 / 恢复”，再按提示回零。');}
  finally{connectionBusy=false;await refreshConnection();}
});
click('disconnectDevice',async()=>{
  connectionBusy=true;applyConnectionState();releaseAll();settleConfirmation(false);pendingFrame=null;$('writeConfirm').hidden=true;
  try{const value=await api('/api/connection/disconnect',{});if(value.current)renderConnection(value);else await refreshConnection();notify('已断开。可修改连接模式或保存地址，再点击“连接”。');}
  finally{connectionBusy=false;await refreshConnection();}
});
refreshConnection().catch(e=>notify(e.message));
let registersBusy=false;
async function refreshRegistersCache(){if(registersBusy)return;registersBusy=true;try{renderRegisters(await api('/api/registers'));}catch(e){$('registerNotice').textContent=e.message;$('registerNotice').hidden=false;}finally{registersBusy=false;}}
function moveTooltip(event){const tip=$('tooltip');tip.style.left=Math.min(event.clientX+14,window.innerWidth-350)+'px';tip.style.top=Math.min(event.clientY+16,window.innerHeight-tip.offsetHeight-10)+'px';}
document.addEventListener('pointerover',event=>{const source=event.target.closest?.('[data-tip]');if(!source?.dataset.tip)return;$('tooltip').textContent=source.dataset.tip;$('tooltip').hidden=false;moveTooltip(event);});
document.addEventListener('pointermove',event=>{if(!$('tooltip').hidden)moveTooltip(event);});
document.addEventListener('pointerout',()=>$('tooltip').hidden=true);
document.addEventListener('focusin',event=>{const source=event.target.closest?.('[data-tip]');if(!source?.dataset.tip)return;$('tooltip').textContent=source.dataset.tip;$('tooltip').hidden=false;const bounds=source.getBoundingClientRect();moveTooltip({clientX:bounds.left,clientY:bounds.bottom});});
document.addEventListener('focusout',()=>$('tooltip').hidden=true);
api('/api/config').then(showConfig).catch(e=>notify(e.message));
const events=new EventSource('/api/events');events.onmessage=event=>render(JSON.parse(event.data));events.onerror=()=>{$('online').textContent='状态断开';refreshReadiness();};
refreshReadiness();refreshRegistersCache();
setInterval(()=>{refreshReadiness();refreshRegistersCache();refreshConnection().catch(()=>{});},1000);
setInterval(()=>{refreshLogs().catch(()=>{});},2000);




