const assert=require('node:assert/strict');
const fs=require('node:fs');const vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');
const body=source.slice(source.indexOf('const holds=new Set();'),source.indexOf('function releaseAll('));
function setup(control){
 const timers=new Map(),events={},notes=[];let n=0;
 const button={disabled:false,classList:{add(){},remove(){}},setPointerCapture(){},addEventListener(k,f){events[k]=f;}};
 const context=vm.createContext({Set,connection:{connected:true},crypto:{randomUUID:()=>`intent-${++n}`},control,notify:x=>notes.push(x),refreshReadiness(){},setInterval:f=>{timers.set(++n,f);return n;},clearInterval:id=>timers.delete(id)});
 vm.runInContext(body,context);context.button=button;vm.runInContext("bindHold(button,'up')",context);
 return {context,timers,events,notes,down:()=>events.pointerdown({button:0,pointerId:1,preventDefault(){}})};
}
async function run(){
 let accept;const calls=[];
 const a=setup(async c=>{calls.push(c);if(c.action==='up')await new Promise(r=>accept=r);});
 const pending=a.down();assert.equal(a.timers.size,0);accept();await pending;await Promise.resolve();
 assert.equal(a.timers.size,1);assert.equal(calls.filter(c=>c.action==='heartbeat').length,1);
 await a.events.pointerup();assert.equal(a.timers.size,0);assert.equal(calls.at(-1).action,'stop');
 const b=setup(async c=>{if(c.action==='heartbeat')throw Error('点动保活超时');});
 await b.down();await new Promise(r=>setImmediate(r));
 assert.equal(b.timers.size,0);assert.equal(b.notes.length,1);assert.match(b.notes[0],/保活超时/);
 let finish;const cCalls=[];
 const c=setup(async v=>{cCalls.push(v);if(v.action==='up')await new Promise(r=>finish=r);});
 const starting=c.down();await c.events.pointerup();finish();await starting;
 assert.equal(c.timers.size,0);assert.equal(cCalls.filter(v=>v.action==='heartbeat').length,0);
 const globalCalls=[];const g=setup(async v=>globalCalls.push(v));
 await g.down();
 vm.runInContext(source.slice(source.indexOf('function releaseAll('),source.indexOf("click('servoEnable'")),g.context);
 await vm.runInContext('releaseAll(false)',g.context);
 await g.events.pointerup();
 assert.equal(globalCalls.filter(v=>v.action==='stop').length,0);
 assert.equal(g.timers.size,0);
 console.log('Hold lifecycle passed: no premature heartbeat, release cancels, failed heartbeat ends once, late start cannot renew.');
}
run().catch(e=>{console.error(e);process.exitCode=1;});
