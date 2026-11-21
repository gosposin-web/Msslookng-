import {test} from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';
import {webcrypto} from 'node:crypto';

const source=await readFile(new URL('../app/static/app.js',import.meta.url),'utf8');
const healthy={ok:true,service:'story-cloud',ready:true};
const emptyState={account:null,sources:[],jobs:[],events:[],stats:{people:0,active:0,views_24h:0},view_limit_24h:200};
const json=(data,status=200,headers={})=>Response.json(data,{status,headers});

// Run the actual browser script with a small DOM boundary and a controllable clock.
// No Telegram calls, browser installation, or live credentials are involved.
async function harness(responder,wakeUrl=''){
 let clock=Date.now(),id=0;
 const timers=new Map(),intervals=new Map(),handlers={},requests=[];
 const el=()=>({innerHTML:'',textContent:'',dataset:{},open:false,classList:{add(){},remove(){}},addEventListener(){},showModal(){this.open=true;},close(){this.open=false;}});
 const elements={'#app':el(),'#dialog':el(),'#toast':el()};
 if(wakeUrl)elements['meta[name="story-backend-wake"]']={content:wakeUrl};
 const document={querySelector:selector=>elements[selector]||null,querySelectorAll:()=>[],visibilityState:'visible',activeElement:null,
  addEventListener(type,handler){(handlers[type]??=[]).push(handler);}};
 class ClockDate extends Date{static now(){return clock;}}
 const context=vm.createContext({console,document,window:{scrollTo(){}},Date:ClockDate,Intl,Set,URL,URLSearchParams,FormData,AbortController,crypto:webcrypto,
  fetch:async(path,options)=>{requests.push({path,options});return responder(path,requests.length,options);},
  setTimeout(fn,delay){const next=++id;timers.set(next,{fn,delay});return next;},clearTimeout(key){timers.delete(key);},
  setInterval(fn,delay){const next=++id;intervals.set(next,{fn,delay});return next;},clearInterval(key){intervals.delete(key);}});
 await vm.runInContext(source,context);
 async function settle(){for(let i=0;i<12;i++)await new Promise(resolve=>setImmediate(resolve));}
 await settle();
 return {elements,timers,intervals,requests,context,
  async retry(){const entry=[...timers.entries()].find(([,t])=>t.delay!==6500);assert(entry,'Expected a scheduled retry');const [key,timer]=entry;timers.delete(key);clock+=timer.delay;await timer.fn();await settle();},
  async clickRetry(){const target={dataset:{action:'retry-boot'},disabled:false,isConnected:true};for(const handler of handlers.click||[])await handler({target:{closest:()=>target},preventDefault(){}});await settle();},
  async clickAction(action){const target={dataset:{action},disabled:false,isConnected:true};for(const handler of handlers.click||[])await handler({target:{closest:()=>target},preventDefault(){}});await settle();},
  advance(ms){clock+=ms;},
  async poll(){assert.equal(intervals.size,1);await [...intervals.values()][0].fn();},
 };
}

function normal(path){if(path==='/health')return json(healthy);if(path==='/api/auth/me')return json({csrf:'test-csrf'});if(path==='/api/state')return json(emptyState);throw new Error('Unexpected request');}
function noPreview(h){assert.doesNotMatch(h.elements['#app'].innerHTML,/Предпросмотр интерфейса|Сервер Telegram ещё не запущен/);assert.equal(h.elements['#dialog'].open,false);}

test('429 with an HTML response waits for Retry-After without entering preview',async()=>{
 const h=await harness(()=>new Response('<h1>Too many requests</h1>',{status:429,headers:{'Retry-After':'120','Content-Type':'text/html'}}));
 noPreview(h);
 assert.match(h.elements['#app'].innerHTML,/Сервер попросил подождать/);
 assert.match(h.elements['#app'].innerHTML,/120 сек\./);
 assert.equal([...h.timers.values()][0].delay,120000);
 await h.clickRetry();
 assert.equal(h.requests.length,1,'Manual retry must also respect Retry-After');
 assert.equal(h.intervals.size,0);
});

for(const status of [404,500,502,503])test(`HTTP ${status} shows connection status instead of preview`,async()=>{
 const h=await harness(()=>new Response('Unavailable',{status}));
 noPreview(h);assert.match(h.elements['#app'].innerHTML,/data-connection-state=/);assert.equal(h.intervals.size,0);
});

for(const status of [401,403])test(`HTTP ${status} offers Site sign-in without a password-panel prompt`,async()=>{
 const h=await harness(()=>json({detail:'Sign in required'},status));
 noPreview(h);assert.match(h.elements['#app'].innerHTML,/signin-with-chatgpt/);assert.equal(h.timers.size,0);
});

test('Unexpected success payload and HTML do not activate preview',async()=>{
 for(const response of [json({status:'ok'}),new Response('<html>Login page</html>',{headers:{'Content-Type':'text/html'}})]){
  const h=await harness(()=>response);noPreview(h);assert.equal(h.intervals.size,0);
 }
});

test('Only an explicit story-preview response activates the demo',async()=>{
 const h=await harness(()=>json({service:'story-preview',ok:false,ready:false}));
 assert.match(h.elements['#app'].innerHTML,/Предпросмотр интерфейса/);
 assert.equal(h.timers.size,0);assert.equal(h.intervals.size,0);
});

test('A retry recovers into the live workspace and installs polling once',async()=>{
 let healthCalls=0;
 const h=await harness(path=>path==='/health'&&healthCalls++===0?new Response('Wait',{status:429,headers:{'Retry-After':'1'}}):normal(path));
 noPreview(h);await h.retry();
 assert.match(h.elements['#app'].innerHTML,/Всё под контролем/);
 assert.equal(h.intervals.size,1);
 assert.equal(h.timers.size,0);
 await vm.runInContext('boot()',h.context);
 assert.equal(h.intervals.size,1);
});

test('Readiness false waits before authenticating and recovers automatically',async()=>{
 let healthCalls=0;
 const h=await harness(path=>path==='/health'&&healthCalls++===0?json({...healthy,ready:false}):normal(path));
 noPreview(h);assert.equal(h.requests.length,1);assert.match(h.elements['#app'].innerHTML,/восстанавливать данные/);
 await h.retry();assert.equal(h.intervals.size,1);
});

for(const path of ['/api/auth/me','/api/state'])test(`An HTML 429 during ${path} retains its retry status`,async()=>{
 const h=await harness(url=>url===path?new Response('Rate limited',{status:429,headers:{'Retry-After':'70'}}):normal(url));
 noPreview(h);assert.match(h.elements['#app'].innerHTML,/70 сек\./);assert.equal(h.intervals.size,0);
});

test('Retry-After HTTP dates are observed',async()=>{
 const date=new Date(Date.now()+120000).toUTCString();
 const h=await harness(()=>new Response('Wait',{status:429,headers:{'Retry-After':date}}));
 const delay=[...h.timers.values()][0].delay;
 assert(delay>118000&&delay<=120000);
});

test('Password-panel login keeps a poller ready for successful authentication',async()=>{
 const h=await harness(path=>path==='/api/auth/me'?json({detail:'Войдите в панель.'},401):normal(path));
 assert.equal(h.elements['#dialog'].open,true);
 assert.match(h.elements['#dialog'].innerHTML,/Пароль панели/);
 assert.equal(h.intervals.size,1);
 const count=h.requests.length;await h.poll();assert.equal(h.requests.length,count);
});

test('Network failures stop automatic attempts after a bounded number',async()=>{
 const h=await harness(()=>{throw new TypeError('Network failure');});
 for(let i=0;i<11;i++){noPreview(h);await h.retry();}
 assert.equal(h.requests.length,12);assert.equal(h.timers.size,0);
 assert.match(h.elements['#app'].innerHTML,/Автоматические попытки приостановлены/);
});

test('A background 429 pauses reads rather than polling every five seconds',async()=>{
 let stateCalls=0;
 const h=await harness(path=>path==='/api/state'&&stateCalls++>0?new Response('Wait',{status:429,headers:{'Retry-After':'90'}}):normal(path));
 await h.poll();const count=h.requests.length;
 h.advance(5000);await h.poll();assert.equal(h.requests.length,count);
 h.advance(85000);await h.poll();assert.equal(h.requests.length,count+1);
});

test('File import form supports phone files and preconfiguration before account login',async()=>{
 const h=await harness(normal);
 await h.clickAction('import-chats');
 const html=h.elements['#dialog'].innerHTML;
 assert.match(html,/TXT или CSV, до 200/);
 assert.match(html,/accept='.txt,.csv,text\/plain,text\/csv'/);
 assert.match(html,/name='start_scans' disabled/);
 assert.match(html,/name='request_id' value='[a-f0-9-]{36}'/);
});

test('Import results escape invalid file rows and describe partial success',async()=>{
 const h=await harness(normal);
 vm.runInContext(`importResult({added:2,existing:1,queued:2,busy:1,duplicates:3,invalid_count:1,invalid_rows:[{line:5,value:'<img src=x onerror=alert(1)>',message:'Неверная запись'}],replayed:false})`,h.context);
 const html=h.elements['#dialog'].innerHTML;
 assert.match(html,/Новых чатов: 2/);assert.match(html,/Неверных записей: 1/);
 assert.match(html,/&lt;img/);assert.doesNotMatch(html,/<img src=x/);
 assert.match(html,/Открыть очередь/);
});

test('A 200-chat queue is summarized and additional jobs can be shown',async()=>{
 const jobs=Array.from({length:200},(_,i)=>({id:String(i),kind:'scan',status:'queued',created_at:1800000000,source_id:null,total:0,collected:0,checked:0,found:0,phase:'gather'}));
 const data={...emptyState,jobs,job_counts:{queued:200},source_imports:[{id:'test-batch',created_at:1800000000,total:200,queued:200}]};
 const h=await harness(path=>path==='/api/state'?json(data):normal(path));
 vm.runInContext("page='jobs';render()",h.context);
 let html=h.elements['#app'].innerHTML;
 assert.match(html,/Сбор из файла · 200 чатов/);
 assert.match(html,/Пауза всего импорта/);
 assert.equal((html.match(/class='job-card'/g)||[]).length,40);
 await h.clickAction('more-jobs');html=h.elements['#app'].innerHTML;
 assert.equal((html.match(/class='job-card'/g)||[]).length,80);
});

test('A browser visit wakes the backend once and recovers from the proxy cold-start 429',async()=>{
 const wake='https://our-app.onrender.com/health';let awake=false,wakes=0;
 const h=await harness((path,count,options)=>{
  if(path===wake){wakes++;assert.equal(options.mode,'no-cors');assert.equal(options.credentials,'omit');assert.equal(options.referrerPolicy,'no-referrer');assert.equal(options.redirect,'error');assert.equal(options.headers,undefined);return Promise.resolve().then(()=>{awake=true;return new Response('opaque');});}
  if(path==='/health'&&!awake)return new Response('backend asleep',{status:429,headers:{'Retry-After':'30'}});
  return normal(path);
 },wake);
 assert.match(h.elements['#app'].innerHTML,/Подключаемся к серверу/);
 assert.doesNotMatch(h.elements['#app'].innerHTML,/Сервер попросил подождать/);
 await h.retry();
 assert.match(h.elements['#app'].innerHTML,/Всё под контролем/);
 assert.equal(wakes,1);assert.equal(h.intervals.size,1);
 await vm.runInContext('boot()',h.context);assert.equal(wakes,1);
});

test('A blocked browser wake does not prevent the ordinary authenticated proxy from working',async()=>{
 const wake='https://our-app.onrender.com/health';
 const h=await harness(path=>{if(path===wake)throw new TypeError('browser request blocked');return normal(path);},wake);
 assert.match(h.elements['#app'].innerHTML,/Всё под контролем/);
 assert.equal(h.intervals.size,1);
});

test('Browser wake cannot send requests to arbitrary or credential-bearing URLs',async()=>{
 for(const wake of ['https://evil.example/health','https://user:password@our-app.onrender.com/health','https://our-app.onrender.com/health?secret=value','http://our-app.onrender.com/health']){
  const h=await harness(normal,wake);
  assert.equal(h.requests.some(r=>String(r.path).startsWith('http')),false);
 }
});
