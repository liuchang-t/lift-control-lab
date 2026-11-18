const assert=require('node:assert/strict');
const fs=require('node:fs');const path=require('node:path');const vm=require('node:vm');
const nodes=new Map();
function node(){return {dataset:{},style:{},children:[],value:'',hidden:false,disabled:false,events:{},classList:{add(){},remove(){}},addEventListener(name,fn){this.events[name]=fn;},replaceChildren(){this.children=[];},appendChild(x){this.children.push(x);},append(...xs){this.children.push(...xs);},click(){return this.events.click?.();}};}
const get=id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);};
const context=vm.createContext({sessionStorage:{liftLabClient:'test'},document:{getElementById:get,createElement:node,querySelectorAll:()=>[],addEventListener(){}},window:{addEventListener(){}},fetch:()=>new Promise(()=>{}),EventSource:class{},Set,clearTimeout(){},clearInterval(){},setTimeout(){return 0;},setInterval(){return 0;},Date});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/log-buffer.js'),'utf8'),context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8'),context);
context.connected={connected:true,state:'connected',mode:'sim',current:{mode:'sim',host:'127.0.0.1',port:8899,slave_id:1,timeout:.5},saved:{mode:'sim',host:'127.0.0.1',port:8899,slave_id:1,timeout:.5}};
vm.runInContext('renderConnection(connected)',context);
const html=fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8');
const css=fs.readFileSync(path.join(__dirname,'../web/style.css'),'utf8');
assert.match(html,/id="eeprom"[^>]*aria-live="polite"/);
assert.doesNotMatch(html,/集中平台对照|故障注入|data-inject|centralAcquire|id="acquire"|id="release"|id="lease"|允许控制|控制权/);assert.match(html,/>检查 \/ 恢复<\/button>/);
assert.match(css,/height:100dvh/);assert.match(css,/overflow:hidden/);assert.match(css,/\.table-scroll\{overflow:auto/);
for(const status of ['ok','warning','unknown']){context.event={status:{control_mode:'sim',implementation:'candidate',details:{eeprom_check:{status,value:status==='ok'?0:null,message:'P0C-13 '+status}}},lease:{}};vm.runInContext('render(event)',context);assert.equal(get('eeprom').dataset.status,status);assert.equal(get('eeprom').dataset.tip,'P0C-13 '+status);}
context.event={status:{control_mode:'sim',implementation:'baseline',details:{}},lease:{}};vm.runInContext('render(event)',context);assert.match(get('eeprom').textContent,/冻结基线不提供/);assert.equal(get('home').disabled,true);assert.equal(get('resetFault').disabled,true);assert.equal(get('independentSpeeds').hidden,true);
context.event={status:{online:true,control_mode:'sim',implementation:'candidate',details:{state:'IDLE',armed:true,position_trusted:true,home_status:'complete',height_cm:1,soft_lower_cm:1,soft_upper_cm:180,speeds:{pp:{requested_cm_s:1.0499999999999998,min_cm_s:.105,max_cm_s:3.5,ratio:.3,pr_s:30000,rpm:180},pv:{requested_cm_s:.5,min_cm_s:.2,max_cm_s:2,ratio:.25,pr_s:14286,rpm:85.716}},alarm:{text:'Er.B00',name:'位置偏差过大',raw_hex:'0x0B00',reset_allowed:true,policy:'stop_and_latch',advice:'排查卡滞，不自动复位'}}},lease:{}};
vm.runInContext('render(event)',context);assert.equal(get('height').textContent,'1.00 cm');assert.equal(get('ppSpeed').value,1.05);assert.match(get('ppSpeedInfo').textContent,/0.105/);assert.match(get('ppSpeedInfo').dataset.tip,/30000.00 Pr\/s/);assert.equal(get('homeStatus').textContent,'回零完成');assert.match(get('limitsInfo').textContent,/1.00–180.00/);assert.equal(get('alarmAdvice').textContent,'排查卡滞，不自动复位');assert.equal(get('resetFault').disabled,false);
vm.runInContext('editingSpeed.pp=true',context);get('ppSpeed').value='1.5';vm.runInContext('render(event)',context);assert.equal(get('ppSpeed').value,'1.5');get('target').value='1';assert.equal(JSON.stringify(vm.runInContext('targetCommand()',context)),JSON.stringify({action:'goto',target_cm:1}));
const oldFetch=context.fetch;let observerRequests=0;context.fetch=()=>{observerRequests++;return new Promise(()=>{});};
context.external={status:{online:true,control_mode:'sim',implementation:'candidate',details:{state:'IDLE',observed_motion:true,untracked_motion:true,speed_rpm:120,armed:false,position_trusted:false}},lease:{holder_name:'其他操作者',remaining_s:90}};
vm.runInContext('render(external)',context);assert.match(get('state').textContent,/外部\/未跟踪运动/);assert.doesNotMatch(get('state').textContent,/静止/);assert.match(get('state').dataset.tip,/不推断/);assert.equal(observerRequests,0);
context.external.status.details={state:'MOVING',observed_motion:true,untracked_motion:false,armed:true,position_trusted:true};vm.runInContext('render(external)',context);assert.match(get('state').textContent,/运动中/);assert.doesNotMatch(get('state').textContent,/外部/);assert.equal(observerRequests,0);context.fetch=oldFetch;
context.startupDisconnected={...context.connected,connected:false,state:'disconnected'};
vm.runInContext('renderConnection(startupDisconnected)',context);assert.equal(get('height').textContent,'—');
context.atomicEvent={...context.event,connection:context.connected};vm.runInContext('render(atomicEvent)',context);assert.equal(get('height').textContent,'1.00 cm');assert.doesNotMatch(get('state').textContent,/未连接/);assert.equal(get('connectionState').textContent,'已连接');
context.atomicEvent.connection=context.startupDisconnected;vm.runInContext('render(atomicEvent)',context);assert.equal(get('height').textContent,'—');assert.match(get('state').textContent,/未连接/);
context.atomicEvent.connection=context.connected;vm.runInContext('render(atomicEvent)',context);
context.ready={summary:'需要检查',steps:['检查 / 恢复','回零'],checks:[{id:'armed',label:'检查通过',ok:false,detail:'先检查'}],actions:[{id:'arm',label:'检查 / 恢复',enabled:true}],manual:false,last_rejection:{message:'位置未可信',at:1}};
vm.runInContext('renderReadiness(ready)',context);assert.equal(get('guideSummary').textContent,'需要检查');assert.equal(get('guideSteps').textContent,'检查 / 恢复 → 回零');assert.equal(get('conditions').children[0].textContent,'○ 检查通过');assert.match(get('rejection').textContent,/位置未可信/);assert.equal(get('endManual').disabled,true);
context.ready.manual=true;vm.runInContext('renderReadiness(ready)',context);assert.equal(get('endManual').disabled,false);assert.match(get('manualState').textContent,/手动接管/);
// Manual send is not gated by normal motion or a pending stop, but needs a live connection.
context.manualPending={...context.connected,state:'stop_pending'};
vm.runInContext('renderConnection(manualPending);applyConnectionState()',context);
assert.equal(get('sendFrame').disabled,false);assert.equal(get('confirmFrame').disabled,false);assert.equal(get('goto').disabled,true);
context.manualTransition={...context.connected,state:'disconnecting'};
vm.runInContext('renderConnection(manualTransition)',context);
assert.equal(get('sendFrame').disabled,true);assert.equal(get('confirmFrame').disabled,true);
vm.runInContext('renderConnection(connected)',context);
assert.match(html,/不自动插入停止或复位/);assert.doesNotMatch(html,/先停止普通动作，再发送本帧|停止与故障保护优先/);
get('autoCrc').checked=false;
get('manualFrame').value='01 03 0C 0D 00 01 16 9B';assert.equal(vm.runInContext('inspectFrame().write',context),false);
get('manualFrame').value='01 06 10 03 00 03 00 00';assert.equal(vm.runInContext('inspectFrame().write',context),true);
get('manualFrame').value='01 05 00 00 00 00 00 00';assert.throws(()=>vm.runInContext('inspectFrame()',context),/功能码/);
get('manualFrame').value='01 03 invalid';assert.throws(()=>vm.runInContext('inspectFrame()',context),/十六进制/);
// Published Modbus read vector and manual's corrected DS2 write vectors, low CRC byte first.
get('autoCrc').checked=true;
for(const [body,complete] of [
  ['01 03 00 00 00 0A','01 03 00 00 00 0A C5 CD'],
  ['01 06 02 00 00 09','01 06 02 00 00 09 48 74'],
  ['01 10 10 0E 00 02 04 C3 50 00 00','01 10 10 0E 00 02 04 C3 50 00 00 83 B6'],
  ['01 10 10 0E 00 02 04 B1 E0 FF FF','01 10 10 0E 00 02 04 B1 E0 FF FF 98 99']
]){get('manualFrame').value=body;assert.equal(vm.runInContext('inspectFrame().frame',context),complete);get('manualFrame').events.input();assert.equal(get('manualPreview').textContent,'TX（含CRC）：'+complete);assert.equal(get('manualFrame').value,body);get('manualFrame').value=complete;assert.throws(()=>vm.runInContext('inspectFrame()',context),/已含CRC/);}
get('manualFrame').value='01 10 10 0E 00 02 02 00 00';assert.throws(()=>vm.runInContext('inspectFrame()',context),/数量/);
get('manualFrame').value='01 06 02 00 00';assert.throws(()=>vm.runInContext('inspectFrame()',context),/至少6字节/);
get('manualFrame').value='01 06 02 00 00 09';vm.runInContext("pendingFrame='old'",context);get('writeConfirm').hidden=false;get('autoCrc').checked=false;get('autoCrc').events.change();assert.equal(vm.runInContext('pendingFrame',context),null);assert.equal(get('writeConfirm').hidden,true);assert.equal(get('manualFrameLabel').textContent,'485报文（含CRC）');assert.equal(get('manualFrame').value,'01 06 02 00 00 09');
context.regs={registers:[{parameter:'P10-25',address_hex:'0x1019',name:'PP速度',width:2,value:30000,raw_words:[30000,0],access:'RW',source:'read',last_write_value:32000,last_write_age_s:1,age_s:12.9,stale:false,epoch:1,operations:['PP'],polling_kind:'low_frequency',polling_label:'自动低频',polling_notice:'仅空闲读取'}],notice:'缓存'};
vm.runInContext('renderRegisters(regs)',context);assert.equal(get('registerRows').children.length,1);assert.equal(get('registerRows').children[0].children[4].children[0].textContent,'读回');assert.equal(get('registerRows').children[0].children[4].textContent,'12 秒');assert.equal(get('registerRows').children[0].children[1].children[0].textContent,'自动低频');assert.equal(get('registerRows').children[0].children[1].children[0].dataset.tip,undefined);assert.equal(get('registerRows').children[0].dataset.tip,undefined);assert.equal(get('registerRows').children[0].children[2].textContent,'RW');get('registerFilter').value='missing';vm.runInContext('filterRegisters()',context);assert.equal(get('registerRows').children.length,0);
get('registerFilter').value='';
context.sortedRegs={registers:[{parameter:'later',address_hex:'0x1019',source:'write_ack',value:99,raw_words:[99],age_s:1},{parameter:'earlier',address:0x0307,address_hex:'0x0307',source:'read',value:0,age_s:4,last_write_value:1,last_write_age_s:2,readback_pending:true,read_error:'timeout'}]};
vm.runInContext('renderRegisters(sortedRegs)',context);
assert.equal(get('registerRows').children[0].children[0].textContent,'earlier');
assert.equal(get('registerRows').children[0].children[3].textContent,0);
assert.match(get('registerRows').children[0].children[3].dataset.tip,/最近写应答 1/);
assert.equal(get('registerRows').children[0].children[4].children[0].textContent,'读取失败');
assert.equal(get('registerRows').children[1].children[3].textContent,'—');
assert.equal(get('registerRows').children[1].children[4].textContent,'未读取');
context.units={status:'consistent',path:'direct',encoder_counts_per_rev:8388608,pulses_setting:10000,gear_numerator:8388608,gear_denominator:10000,effective_command_per_rev:10000,effective_fraction:'10000/1',configured_command_per_rev:10000,consistent:true,mechanical_cm_per_rev:.35,mechanical_verified:false,notice:'驱动读回一致，机械位移仍需实测',observed_age_s:12.9};
vm.runInContext('renderConversion(units)',context);assert.equal(get('conversionStatus').textContent,'驱动/配置数值一致');assert.equal(get('conversionPath').textContent,'P05-02 直接设定');assert.equal(get('conversionEncoder').textContent,8388608);assert.equal(get('conversionEffective').textContent,10000);assert.equal(get('mechanicalCm').value,.35);assert.match(get('conversionStatus').dataset.tip,/12 秒前/);assert.doesNotMatch(html,/待实机核实|id="conversionNotice"/);assert.equal(get('registerNotice').hidden,true);assert.match(html,/保存控制台比例/);assert.match(html,/新比例 = 旧比例 × 实测位移 \/ 指令位移/);assert.match(html,/PP\/JOG期望cm\/s不变/);
get('mechanicalCm').value='0.4';get('mechanicalCm').events.input();context.units.mechanical_cm_per_rev=.35;vm.runInContext('renderConversion(units)',context);assert.equal(get('mechanicalCm').value,'0.4');
context.units.status='mismatch';context.units.path='ratio';context.units.effective_command_per_rev=5000;context.units.consistent=false;vm.runInContext('renderConversion(units)',context);assert.equal(get('conversionStatus').textContent,'驱动与配置不一致');assert.equal(get('conversionPath').textContent,'电子齿轮比');assert.equal(get('conversionEffective').textContent,5000);
vm.runInContext('renderConversion(undefined)',context);assert.equal(get('conversionStatus').textContent,'驱动换算未确认');assert.equal(get('conversionEffective').textContent,'—');
async function requests(){const calls=[];context.fetch=async(url,options)=>{calls.push({url,body:options.body?JSON.parse(options.body):undefined});return {ok:true,json:async()=>({})};};await vm.runInContext("saveSpeed('pp')",context);assert.deepEqual(calls[0].body,{action:'set_speed',mode:'pp',speed_cm_s:1.5});assert.equal(calls[0].url,'/api/devices/lift_pole_1/lift/control');get('manualFrame').value='01 06 10 03 00 03 00 00';await get('sendFrame').click();assert.equal(get('writeConfirm').hidden,false);assert.equal(calls.some(x=>x.url==='/api/manual/transaction'),false);get('manualFrame').events.input();assert.equal(get('writeConfirm').hidden,true);assert.equal(vm.runInContext('pendingFrame',context),null);console.log('UI contracts passed: fixed dashboard, independent speeds, readiness, registers, manual write confirmation and removed test panels.');}
async function connectionRequests(){ vm.runInContext('connectionReading=false;readingReadiness=false',context);
  const calls=[];let state=JSON.parse(JSON.stringify(context.connected));state.connected=false;state.state='disconnected';
  context.fetch=async(url,options)=>{const body=options.body?JSON.parse(options.body):undefined;calls.push({url,body});if(url==='/api/connection/connect'){state.connected=true;state.state='connected';state.mode=body.mode;state.current={...body};}if(url==='/api/connection/disconnect'){state.connected=false;state.state='disconnected';}if(url==='/api/connection'&&body)state.saved={...body};const value=url.startsWith('/api/connection')?state:url==='/api/config'?{mode:state.mode,travel_cm:180}:{};return{ok:true,json:async()=>value};};
  context.disconnected=state;vm.runInContext('renderConnection(disconnected,true)',context);
  assert.equal(get('connectDevice').disabled,false);assert.equal(get('disconnectDevice').disabled,true);assert.equal(get('arm').disabled,true);assert.equal(get('height').textContent,'—');assert.equal(get('eeprom').textContent,'P0C-13 未读取');
  get('connectionMode').value='real';get('connectionMode').events.change();assert.equal(calls.length,0);
  const attempt=get('connectDevice').click();await Promise.resolve();assert.equal(get('confirmPanel').hidden,false);assert.match(get('confirmText').textContent,/127.0.0.1:8899/);assert.equal(calls.some(x=>x.url==='/api/connection/connect'),false);vm.runInContext('settleConfirmation(false)',context);await attempt;assert.equal(calls.some(x=>x.url==='/api/connection/connect'),false);
  get('connectionHost').value='192.0.2.15';get('connectionPort').value='9000';get('connectionSlave').value='2';get('connectionTimeout').value='1';get('connectionMode').value='sim';await get('saveConnection').click();assert.equal(state.saved.host,'192.0.2.15');assert.equal(calls.some(x=>x.url==='/api/connection/connect'),false);
  await get('connectDevice').click();const connect=calls.find(x=>x.url==='/api/connection/connect');assert.equal(connect.body.mode,'sim');assert.equal(connect.body.host,'192.0.2.15');assert.equal(connect.body.port,9000);assert.equal(connect.body.confirm_real,false);assert.equal(get('connectDevice').disabled,true);assert.equal(get('disconnectDevice').disabled,false);
  const count=calls.filter(x=>x.url==='/api/connection/connect').length;await get('connectDevice').click();assert.equal(calls.filter(x=>x.url==='/api/connection/connect').length,count);
  await get('disconnectDevice').click();assert.equal(get('height').textContent,'—');assert.equal(get('goto').disabled,true);assert.equal(get('sendFrame').disabled,true);
  console.log('Connection UI passed: explicit connect/disconnect, real-target confirmation, save without connection, disconnected stale-data masking.');
}
async function mechanicalRequest(){
  const calls=[];context.fetch=async(url,options)=>{calls.push({url,body:options.body?JSON.parse(options.body):undefined});return{ok:true,json:async()=>url==='/api/config'?{cm_per_rev:.4,travel_cm:180}:url==='/api/conversion'?{...context.units,mechanical_cm_per_rev:.4}:{} };};
  get('mechanicalCm').value='.4';await get('saveMechanical').click();assert.deepEqual(calls[0],{url:'/api/config',body:{cm_per_rev:.4}});assert.equal(get('mechanicalCm').value,.4);assert.match(get('rejection').textContent,/待实机核实/);
  console.log('Conversion UI passed: backend-derived values, polling labels, whole-second ages and mechanical config save without claiming verification.');
}
async function connectionSnapshotRace(){
  vm.runInContext('connectionReading=false',context);let finish;
  context.fetch=()=>new Promise(resolve=>finish=resolve);
  const oldRequest=vm.runInContext('refreshConnection()',context);
  context.atomicEvent.connection=context.connected;vm.runInContext('render(atomicEvent)',context);
  finish({ok:true,json:async()=>context.startupDisconnected});await oldRequest;
  assert.equal(get('connectionState').textContent,'已连接');assert.equal(get('height').textContent,'1.00 cm');assert.doesNotMatch(get('state').textContent,/未连接/);
  console.log('Connection snapshot race passed: initial SSE renders atomically and stale GET cannot erase current metrics.');
}
requests().then(connectionRequests).then(mechanicalRequest).then(connectionSnapshotRace).then(limitWarningReset).catch(e=>{console.error(e);process.exitCode=1;});


// Force-input readback is distinct from the observed DI level and servo completion.
context.force={selection:1,given:507,di3:1,requested:{3345:1,3346:507},fresh:true,age_s:.8,completion_confirmed:false};
vm.runInContext('connection=connected;renderForceInputs(force)',context);
assert.equal(get('force18Read').textContent,507);
assert.match(get('forceDi3').textContent,/1（高电平）/);
assert.match(get('forceMatch').textContent,/给定与读回一致/);
assert.match(html,/给定读回 ≠ 使能完成/);
context.force.given=511;vm.runInContext('renderForceInputs(force)',context);
assert.match(get('forceMatch').textContent,/不同/);
context.force.fresh=false;vm.runInContext('renderForceInputs(force)',context);
assert.equal(get('force18Read').textContent,'—');
assert.equal(get('forceMatch').textContent,'未核对');

// Large retained history must not become a 20,000-line DOM update on each poll.
assert.match(html,/<small>伸出高度<\/small>/);assert.doesNotMatch(html,/id="total"|对地总高/);
assert.match(html,/<details class="control-details"><summary>使能给定 \/ DI观测/);
assert.match(html,/<details class="control-details"><summary>换算依据 \/ 机械比例/);
assert.ok(html.indexOf('aria-label="换算依据与核验"')<html.indexOf('<section class="panel registers">'));
assert.match(css,/grid-template-rows:30px 20px 18px/);
let formattedDates=0;
context.Date=class extends Date{constructor(...args){super(...args);formattedDates++;}};
context.history=Array.from({length:20000},(_,i)=>({id:i+1,at:1700000000+i/100,kind:'rx',frame:'01 03 02 00 00'}));
vm.runInContext('logBuffer.accept({items:history,cursor:20000});renderLogs()',context);
assert.equal(get('logs').textContent.split('\n').length,300);assert.equal(formattedDates,300);
assert.equal(vm.runInContext('logBuffer.rows.length',context),20000);
const tail=get('logs').textContent;formattedDates=0;vm.runInContext('renderLogs()',context);assert.equal(formattedDates,0);assert.equal(get('logs').textContent,tail);
vm.runInContext('pageLogs(1)',context);assert.equal(formattedDates,300);assert.match(get('logs').textContent,/^19401 /);assert.equal(vm.runInContext('logBuffer.follow',context),false);
const paused=get('logs').textContent;vm.runInContext("logBuffer.accept({items:[{id:20001,at:1700001000,kind:'rx',frame:'new'}],cursor:20001});renderLogs()",context);assert.equal(get('logs').textContent,paused);
vm.runInContext('pageLogs(1000)',context);assert.match(get('logs').textContent,/^1 /);assert.equal(get('olderLogs').disabled,true);
context.Date=Date;
console.log('Log rendering bound: 20,000 retained; 300 DOM lines/date formats per page; 0 formats on unchanged poll; oldest history remains accessible; paused content stays fixed.');

// Candidate real mode must not expose the legacy forced-DI PP/HOME path.
context.jogReal={...context.connected,mode:'real',current:{...context.connected.current,mode:'real'}};
vm.runInContext('renderConnection(jogReal); applyConnectionState()',context);
for(const id of ['goto','min','max'])assert.equal(get(id).disabled,true);
context.homeReady={status:{online:true,implementation:'candidate',details:{state:'IDLE',armed:true}}};vm.runInContext('render(homeReady)',context);assert.equal(get('home').disabled,false);
assert.match(html,/>JOG 速度/);
assert.doesNotMatch(html,/只允许软件保护范围内|回零后先全收到软件下限|靠近边界时保护逻辑可能降低/);
console.log('Basic JOG UI: independent speed label, no homing/software-bound hints; real HOME available; unsafe PP disabled.');

context.ackStop={status:{online:true,implementation:'candidate',details:{state:'IDLE',stop_confirmation:'ack',observed_motion:true,untracked_motion:true,speed_rpm:80,armed:true}}};
vm.runInContext('render(ackStop)',context);
assert.match(get('state').textContent,/停止指令已应答/);
assert.match(get('state').textContent,/反馈仍有运动/);
assert.doesNotMatch(get('state').textContent,/静止/);

context.noMotion={status:{online:true,implementation:'candidate',details:{state:'MOVING',motion_notice:'命令已应答，未检测到运动',speed_rpm:0,armed:true,position_trusted:false}}};
vm.runInContext('render(noMotion)',context);
assert.match(get('state').textContent,/未检测到运动/);
assert.doesNotMatch(get('state').textContent,/故障锁定|待检查/);
assert.doesNotMatch(fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8'),/松开后重新按住可发起新点动/);

// Cleared limit notices must not remain after fresh feedback, or hide other faults.
context.limitEvent={status:{online:true,control_mode:'sim',implementation:'candidate',details:{upper_limit:false,lower_limit:true,di4_function:14,di5_function:15,di4_logic:0,di5_logic:0}},lease:{}};
vm.runInContext("notify('点动已结束：物理限位禁止该方向。请按界面指引处理后再操作。');render(limitEvent)",context);
assert.equal(get('rejection').hidden,false);
context.limitEvent.status.online=false;context.limitEvent.status.details.lower_limit=false;
vm.runInContext('render(limitEvent)',context);assert.equal(get('rejection').hidden,false);
context.limitEvent.status.online=true;
vm.runInContext('render(limitEvent)',context);assert.equal(get('rejection').hidden,true);
vm.runInContext("notify('过载');render(limitEvent)",context);assert.equal(get('rejection').textContent,'过载');
get('registerFilter').value='';
vm.runInContext("renderRegisters({registers:[{parameter:'DI',visible:false},{parameter:'P0D-11',visible:true,value:1,source:'read',raw_words:[1],age_s:0}]})",context);
assert.equal(get('registerRows').children.length,1);
assert.equal(get('registerRows').children[0].children[4].children[0].textContent,'读回');

context.loadEvent={status:{online:true,control_mode:'sim',implementation:'candidate',details:{load_signed:-1,load_raw:65535,load_percent:null}},lease:{}};
vm.runInContext('render(loadEvent)',context);
assert.match(get('sensors').textContent,/负载原码 -1/);
assert.doesNotMatch(get('sensors').textContent,/65535%|-1%/);

async function limitWarningReset(){
  vm.runInContext('ask=async()=>true;refreshReadiness=async()=>{};renderConnection(connected)',context);
  for(const armed of [true,false]){
    context.loadEvent.status.details.alarm={code:0x950,reset_allowed:true};
    vm.runInContext('render(loadEvent)',context);
    context.fetch=async()=>({ok:true,json:async()=>({status:{details:{armed}}})});
    await get('resetFault').events.click();
    assert.match(get('rejection').textContent,armed?/可继续点动/:/检查 \/ 恢复/);
  }
  console.log('Limit warning reset UI: preserved readiness and recovery guidance passed.');
}

context.warningHint={status:{online:true,implementation:'candidate',details:{state:'MOVING',motion_notice:'命令已应答，未检测到运动（Er.950）',motion_advice:'当前存在Er.950，可能是驱动器内部仍在阻止运动。请先松开点动，再点击“人工故障复位”。',armed:true}}};
vm.runInContext('render(warningHint);renderReadiness({summary:warningHint.status.details.motion_notice,steps:[warningHint.status.details.motion_advice],checks:[],actions:[]})',context);
assert.match(get('guideSteps').textContent,/可能是驱动器内部仍在阻止运动/);
assert.match(get('guideSteps').textContent,/人工故障复位/);
assert.match(get('state').dataset.tip,/先松开点动/);
context.warningHint.status.details.motion_notice='命令已应答，未检测到运动';
context.warningHint.status.details.motion_advice='';
vm.runInContext('render(warningHint);renderReadiness({summary:warningHint.status.details.motion_notice,steps:[],checks:[],actions:[]})',context);
assert.doesNotMatch(get('state').dataset.tip,/驱动器内部/);
assert.doesNotMatch(get('guideSteps').textContent,/驱动器内部/);

// HOME intent fields remain separate from actual register monitoring and advanced JSON.
context.homeConfig={home_speed:30000,home_creep_speed:null,home_acceleration:20000};
vm.runInContext('showConfig(homeConfig)',context);
assert.equal(get('home_speed').value,30000);
assert.equal(get('home_creep_speed').value,'');
get('home_speed').value='40000';get('home_creep_speed').value='';get('home_acceleration').value='25000';
assert.equal(JSON.stringify(vm.runInContext('homeParameterValues()',context)),JSON.stringify({home_speed:40000,home_creep_speed:null,home_acceleration:25000}));
for(const id of ['home_speed','home_creep_speed','home_acceleration'])assert.ok(html.includes('id="'+id+'"'));
