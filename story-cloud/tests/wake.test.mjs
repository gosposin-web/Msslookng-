import {test} from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';

const script=await readFile(new URL('../app/static/wake.js',import.meta.url),'utf8');
async function page(responder,{framed=false,origin='https://private-panel.test'}={}){
 let clock=Date.now(),nextId=0;
 const timers=new Map(),requests=[],navigations=[],messages=[],events={};
 const elements={'wake-status':{textContent:''},'wake-return':{href:''}};
 const document={body:{dataset:{panelOrigin:origin}},getElementById:id=>elements[id],visibilityState:'visible',addEventListener:(type,handler)=>events[type]=handler};
 const window={location:{replace:url=>navigations.push(url)},addEventListener:(type,handler)=>events['window:'+type]=handler};
 window.parent=framed?{postMessage:(data,target)=>messages.push({data,target})}:window;
 class ClockDate extends Date{static now(){return clock;}}
 vm.runInNewContext(script,{window,document,URL,Date:ClockDate,AbortController,
  fetch:async(path,options)=>{requests.push({path,options});return responder(requests.length);},
  setTimeout(fn,delay){const id=++nextId;timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id)});
 async function settle(){for(let i=0;i<8;i++)await new Promise(resolve=>setImmediate(resolve));}
 await settle();
 return {requests,navigations,messages,timers,elements,document,async retry(){const [id,t]=[...timers][0];timers.delete(id);clock+=t.delay;await t.fn();await settle();}};
}
const ready=(value=true)=>Response.json({ok:true,service:'story-cloud',ready:value});

test('Direct startup waits for readiness then returns automatically to the configured private panel',async()=>{
 const p=await page(attempt=>ready(attempt>1));
 assert.equal(p.navigations.length,0);assert.equal([...p.timers.values()][0].delay,5000);
 await p.retry();assert.deepEqual(p.navigations,['https://private-panel.test/#server-started']);
 assert.equal(p.timers.size,0);
 assert(p.requests.every(r=>r.path==='/health'&&r.options.credentials==='omit'));
});

test('A framed startup posts only readiness to the fixed panel origin and performs no navigation',async()=>{
 const p=await page(()=>ready(),{framed:true});
 assert.equal(p.navigations.length,0);assert.equal(p.messages.length,1);
 assert.equal(p.messages[0].target,'https://private-panel.test');
 assert.deepEqual(Object.keys(p.messages[0].data),['type']);
 assert.equal(p.messages[0].data.type,'story-backend-ready');
});

test('Startup honors host Retry-After and recovers from a temporary network failure',async()=>{
 const p=await page(attempt=>{
  if(attempt===1)return new Response('Wait',{status:429,headers:{'Retry-After':'90'}});
  if(attempt===2)throw new TypeError('Offline');
  return ready();
 });
 assert.equal([...p.timers.values()][0].delay,90000);
 await p.retry();assert.equal([...p.timers.values()][0].delay,10000);
 await p.retry();assert.equal(p.navigations.length,1);
});

test('No startup redirect occurs for an unconfigured or unsafe panel origin',async()=>{
 for(const origin of ['__STORY_PANEL_ORIGIN__','https://user:secret@panel.test','http://panel.test','https://panel.test/path']){
  const p=await page(()=>ready(),{origin});assert.equal(p.requests.length,0);assert.equal(p.navigations.length,0);
 }
});
