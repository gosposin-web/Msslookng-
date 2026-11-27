import {test} from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';
import {webcrypto} from 'node:crypto';

const source=await readFile(new URL('../app/static/app.js',import.meta.url),'utf8');
const healthy={ok:true,service:'story-cloud',ready:true};
const emptyState={account:null,proxy:null,accounts:[],active_account:1,max_accounts:15,module_version:2,sources:[],jobs:[],events:[],stats:{people:0,active:0,views_24h:0},view_limit_24h:200};
const json=(data,status=200,headers={})=>Response.json(data,{status,headers});

// Run the actual browser script with a small DOM boundary and a controllable clock.
// No Telegram calls, browser installation, or live credentials are involved.
async function harness(responder,wakeUrl='',returned=false){
 let clock=Date.now(),id=0;
 const timers=new Map(),intervals=new Map(),handlers={},requests=[];
 const frames=[],navigations=[];
 const el=()=>({innerHTML:'',textContent:'',dataset:{},open:false,isConnected:true,contentWindow:{},setAttribute(){},remove(){this.removed=true;},classList:{add(){},remove(){}},replaceChildren(...children){this.innerHTML='';this.textContent=children.map(c=>c.textContent).join('');},append(child){this.textContent+=child.textContent;},addEventListener(){},showModal(){this.open=true;},close(){this.open=false;}});
 const elements={'#app':el(),'#dialog':el(),'#toast':el(),'#people-add-error':el(),'#people-file-error':el(),'#profile-about-error':el(),'#profile-channel-error':el(),'#channel-create-error':el()};
 if(wakeUrl)elements['meta[name="story-backend-wake"]']={content:wakeUrl};
 const document={body:{append(frame){frames.push(frame);}},querySelector:selector=>elements[selector]||null,getElementById:id=>elements['#'+id]||null,createElement:()=>el(),querySelectorAll:()=>[],visibilityState:'visible',activeElement:null,
  addEventListener(type,handler){(handlers[type]??=[]).push(handler);}};
 class ClockDate extends Date{static now(){return clock;}}
 class BrowserFormData extends FormData{constructor(form){super();for(const [name,value] of Object.entries(form?.fields||{}))this.append(name,value);}}
 const window={scrollTo(){},location:{hash:returned?'#server-started':'',pathname:'/',search:'',replace:url=>navigations.push(url)},history:{replaceState(){}},addEventListener(type,handler){(handlers['window:'+type]??=[]).push(handler);}};
 const context=vm.createContext({console,document,window,Date:ClockDate,Intl,Set,URL,URLSearchParams,FormData:BrowserFormData,AbortController,crypto:webcrypto,
  fetch:async(path,options)=>{requests.push({path,options});return responder(path,requests.length,options);},
  setTimeout(fn,delay){const next=++id;timers.set(next,{fn,delay,due:clock+delay});return next;},clearTimeout(key){timers.delete(key);},
  setInterval(fn,delay){const next=++id;intervals.set(next,{fn,delay});return next;},clearInterval(key){intervals.delete(key);}});
 await vm.runInContext(source,context);
 async function settle(){for(let i=0;i<12;i++)await new Promise(resolve=>setImmediate(resolve));}
 await settle();
 return {elements,timers,intervals,requests,context,frames,navigations,settle,
  async submitExisting(form){for(const handler of handlers.submit||[])await handler({target:form,preventDefault(){}});await settle();},
  async retry(){const entry=[...timers.entries()].filter(([,t])=>t.delay!==6500).sort((a,b)=>a[1].due-b[1].due)[0];assert(entry,'Expected a scheduled retry');const [key,timer]=entry;timers.delete(key);clock=Math.max(clock,timer.due);await timer.fn();await settle();},
  async message(event){for(const handler of handlers['window:message']||[])await handler(event);await settle();},
  async visibility(value){document.visibilityState=value;for(const handler of handlers.visibilitychange||[])await handler();await settle();},
  async clickRetry(){const target={dataset:{action:'retry-boot'},disabled:false,isConnected:true};for(const handler of handlers.click||[])await handler({target:{closest:()=>target},preventDefault(){}});await settle();},
  async clickAction(action,data={}){const target={dataset:{action,...data},disabled:false,isConnected:true};for(const handler of handlers.click||[])await handler({target:{closest:()=>target},preventDefault(){}});await settle();},
  async submitForm(id,fields,dataset={}){
   const submit={innerHTML:'Добавить',textContent:'Добавить',isConnected:true,disabled:false},fieldset={disabled:false},saved=el(),error=el();
   const form={id,fields,dataset,isConnected:true,elements:Object.fromEntries(Object.entries(fields).map(([k,v])=>[k,{value:v,checked:v==='on'}])),
    matches:selector=>selector==='.profile-form'?id.startsWith('profile-'):selector==='.channel-username-form'?id==='channel-username-form':false,
    querySelector:selector=>selector==='fieldset'?fieldset:selector==='.profile-saved'?saved:selector==='.inline-error'?error:submit};
   for(const handler of handlers.submit||[])await handler({target:form,preventDefault(){}});await settle();return {submit,fieldset,form,saved,error};
  },
  advance(ms){clock+=ms;},
  async poll(){assert.equal(intervals.size,1);await [...intervals.values()][0].fn();},
 };
}

function normal(path){if(path.startsWith('/api/proxies?'))return json({proxies:[],total:0,selected:null});if(path==='/health')return json(healthy);if(path==='/api/auth/me')return json({csrf:'test-csrf'});if(path==='/api/state')return json(emptyState);throw new Error('Unexpected request');}
function noPreview(h){assert.doesNotMatch(h.elements['#app'].innerHTML,/Предпросмотр интерфейса|Сервер Telegram ещё не запущен/);assert.equal(h.elements['#dialog'].open,false);}
test('An older backend cannot expose multiaccount controls or modify the wrong account',async()=>{
 const h=await harness(path=>path==='/api/state'?json({...emptyState,max_accounts:undefined,accounts:undefined}):normal(path),'https://our-app.onrender.com/wake');
 assert.match(h.elements['#app'].innerHTML,/Ожидаем обновления сервера/);assert.doesNotMatch(h.elements['#app'].innerHTML,/account-picker/);
 assert.equal(h.intervals.size,0);assert.equal(h.frames[0].removed,true);assert.equal(h.navigations.length,0);
});
test('An explicit monthly quota pause stops the cold-start loop and retries after the pause',async()=>{
 const resumeAfter=Math.floor(Date.now()/1000)+86400;let calls=0;
 const h=await harness(path=>path==='/health'&&calls++===0?json({ok:false,service:'story-cloud',ready:false,reason:'hosting_monthly_quota',resume_after:resumeAfter},503):normal(path),'https://our-app.onrender.com/wake');
 assert.match(h.elements['#app'].innerHTML,/Лимит хостинга исчерпан/);assert.equal(h.frames[0].removed,true);
 assert.equal(h.navigations.length,0);assert.equal(h.requests.length,1);
 assert([...h.timers.values()][0].delay>86300000);
 await h.retry();assert.match(h.elements['#app'].innerHTML,/Всё под контролем/);assert.equal(h.navigations.length,0);
});

test('429 with an HTML response waits for Retry-After without entering preview',async()=>{
 const h=await harness(()=>new Response('<h1>Too many requests</h1>',{status:429,headers:{'Retry-After':'120','Content-Type':'text/html'}}));
 noPreview(h);
 assert.match(h.elements['#app'].innerHTML,/Подключение повторится автоматически/);
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
 noPreview(h);assert.equal(h.requests.length,1);assert.match(h.elements['#app'].innerHTML,/автоматически/);
 await h.retry();assert.equal(h.intervals.size,1);
});

for(const path of ['/api/auth/me','/api/state'])test(`An HTML 429 during ${path} retains its retry status`,async()=>{
 const h=await harness(url=>url===path?new Response('Rate limited',{status:429,headers:{'Retry-After':'70'}}):normal(url));
 noPreview(h);assert.equal([...h.timers.values()][0].delay,70000);assert.equal(h.intervals.size,0);
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

test('Connection keeps retrying automatically on an active page without an arbitrary attempt cutoff',async()=>{
 const h=await harness(()=>{throw new TypeError('Network failure');});
 for(let i=0;i<15;i++){noPreview(h);await h.retry();}
 assert.equal(h.requests.length,16);assert.equal(h.timers.size,1);
 assert.match(h.elements['#app'].innerHTML,/Нажимать «Повторить» не нужно/);
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

test('Readiness navigation wakes once, verifies its sender and recovers through the private proxy',async()=>{
 const wake='https://our-app.onrender.com/wake';let awake=false;
 const h=await harness(path=>path==='/health'&&!awake?new Response('backend asleep',{status:429,headers:{'Retry-After':'30'}}):normal(path),wake);
 assert.equal(h.frames.length,1);assert.equal(h.frames[0].src,wake);
 assert.equal(h.frames[0].referrerPolicy,'no-referrer');
 assert.equal(h.requests.some(r=>r.path.startsWith('http')),false);
 assert.match(h.elements['#app'].innerHTML,/Запускаем облачную панель/);
 const initial=h.requests.length;
 await h.message({source:h.frames[0].contentWindow,origin:'https://evil.example',data:{type:'story-backend-ready'}});
 assert.equal(vm.runInContext('backendWakeConfirmed',h.context),false);
 awake=true;
 await h.message({source:h.frames[0].contentWindow,origin:'https://our-app.onrender.com',data:{type:'story-backend-ready'}});
 assert.equal(h.requests.length,initial,'A frame message cannot override the proxy Retry-After');
 await h.retry();
 assert.match(h.elements['#app'].innerHTML,/Всё под контролем/);
 assert.equal(h.frames.length,1);assert.equal(h.frames[0].removed,true);assert.equal(h.intervals.size,1);
 assert.equal(h.navigations.length,0);
});

test('A failed background frame automatically opens the normal startup page instead of waiting forever',async()=>{
 const wake='https://our-app.onrender.com/wake';
 const h=await harness(()=>new Response('Wait',{status:429,headers:{'Retry-After':'30'}}),wake);
 await h.retry();assert.equal(h.navigations.length,0);
 await h.retry();assert.deepEqual(h.navigations,[wake]);
 assert.equal(h.frames[0].removed,true);
});

test('The startup return marker prevents an automatic navigation loop',async()=>{
 const h=await harness(()=>new Response('Wait',{status:429,headers:{'Retry-After':'30'}}),'https://our-app.onrender.com/wake',true);
 for(let i=0;i<6;i++)await h.retry();
 assert.equal(h.navigations.length,0);assert(h.requests.length>=5);
});

test('Returning from a suspended phone tab reconnects automatically and hidden tabs do not poll',async()=>{
 let attempts=0;
 const h=await harness(path=>path==='/health'&&attempts++===0?new Response('Wait',{status:503,headers:{'Retry-After':'30'}}):normal(path));
 await h.visibility('hidden');const count=h.requests.length;h.advance(60000);
 assert.equal(h.timers.size,0);assert.equal(h.requests.length,count);
 await h.visibility('visible');
 assert.match(h.elements['#app'].innerHTML,/Всё под контролем/);assert.equal(h.intervals.size,1);
});

test('A connection loss after startup restarts the connection flow and installs polling only once',async()=>{
 let reads=0;
 const h=await harness(path=>path==='/api/state'&&reads++===1?json({detail:'Server restoring'},503):normal(path));
 await h.poll();assert.match(h.elements['#app'].innerHTML,/Всё под контролем/);assert.equal(h.intervals.size,1);
 assert.equal(h.requests.filter(r=>r.path==='/health').length,2);
});

test('Browser readiness navigation rejects arbitrary and credential-bearing destinations',async()=>{
 for(const wake of ['https://evil.example/wake','https://user:password@our-app.onrender.com/wake','https://our-app.onrender.com/wake?secret=value','http://our-app.onrender.com/wake']){
  const h=await harness(normal,wake);assert.equal(h.frames.length,0);assert.equal(h.navigations.length,0);
 }
});

test('Audience offers username entry, phone file upload and a manual audience filter',async()=>{
 const h=await harness(normal);
 vm.runInContext("state.account={name:'Test user'};page='audience';render()",h.context);
 const html=h.elements['#app'].innerHTML;
 assert.match(html,/id='people-add-form'/);
 assert.match(html,/name='username' placeholder='@username'/);
 assert.match(html,/data-action='import-people'/);
 assert.match(html,/Добавленные по юзернейму/);
 await h.clickAction('import-people');
 const dialog=h.elements['#dialog'].innerHTML;
 assert.match(dialog,/до 200 уникальных пользователей/);
 assert.match(dialog,/accept='.txt,.csv,text\/plain,text\/csv'/);
 assert.match(dialog,/name='request_id' value='[a-f0-9-]{36}'/);
 assert.match(dialog,/Просмотр историй запускается отдельно/);
});

test('Single username posts with CSRF and opens all audience including users without stories',async()=>{
 const h=await harness(path=>{
  if(path==='/api/people')return json({ok:true,valid:1,job_id:'users-job'});
  if(path.startsWith('/api/people?'))return json({people:[],total:0});
  return normal(path);
 });
 vm.runInContext("state.account={name:'Test user'};activeOnly=true;source='old-chat';search='old search'",h.context);
 await h.submitForm('people-add-form',{username:'@user_one',request_id:'stable-request-id'});
 const request=h.requests.find(r=>r.path==='/api/people');
 assert.equal(request.options.method,'POST');
 assert.equal(request.options.headers['X-CSRF-Token'],'test-csrf');
 assert.deepEqual(JSON.parse(request.options.body),{username:'@user_one',request_id:'stable-request-id'});
 const query=h.requests.find(r=>r.path.startsWith('/api/people?')).path;
 assert.equal(new URLSearchParams(query.split('?')[1]).get('active'),'false');
 assert.equal(new URLSearchParams(query.split('?')[1]).get('source'),'');
 assert.match(h.elements['#app'].innerHTML,/Аудитория/);
});

test('User file submission retains retry identity and shows accepted plus rejected rows',async()=>{
 let attempts=0;
 const h=await harness(path=>{
  if(path==='/api/people/import')return ++attempts===1?json({detail:'Временно недоступно.'},503):json({valid:2,duplicates:1,invalid_count:1,invalid_rows:[{line:4,value:'<svg onload=alert(1)>',message:'Неверный username'}],replayed:true});
  if(path.startsWith('/api/people?'))return json({people:[],total:0});
  return normal(path);
 });
 const fields={request_id:'stable-user-upload-id',file:new Blob(['@user_one\n@user_two'])};
 const first=await h.submitForm('people-file-form',fields);
 assert.equal(first.fieldset.disabled,false);
 assert.match(h.elements['#people-file-error'].innerHTML||h.elements['#people-file-error'].textContent,/Временно недоступно/);
 await h.submitForm('people-file-form',fields);
 const requests=h.requests.filter(r=>r.path==='/api/people/import');
 assert.equal(requests.length,2);
 assert(requests.every(r=>r.options.body.get('request_id')==='stable-user-upload-id'));
 assert(requests.every(r=>!r.options.headers['Content-Type']),'Browser must add the multipart boundary');
 const html=h.elements['#dialog'].innerHTML;
 assert.match(html,/Юзернеймов на проверку: 2/);
 assert.match(html,/Повторное задание не создано/);
 assert.match(html,/&lt;svg/);assert.doesNotMatch(html,/<svg onload/);
});

test('User import queue shows its progress and escaped per-user failures',async()=>{
 const h=await harness(normal);
 const html=vm.runInContext(`jobCard({id:'import-job',kind:'people_import',status:'paused',total:200,processed:40,collected:32,checked:32,found:12,added:20,rejected:7,duplicate_users:1,created_at:1800000000,import_errors:[{ref:'@missing_user',message:'<script>bad</script>'}]})`,h.context);
 assert.match(html,/Добавление пользователей/);assert.match(html,/Обработано 40 из 200/);
 assert.match(html,/С историями: 12/);assert.match(html,/width:20%/);
 assert.match(html,/Причины пропуска \(7\)/);assert.match(html,/&lt;script&gt;/);
 assert.doesNotMatch(html,/<script>/);assert.match(html,/data-action='job-resume'/);
});

const owner=(name,user_id)=>({name,user_id,first_name:name,last_name:'',username:'owner_'+user_id,profile_loaded:true,about:'',is_premium:false,created_at:1800000000});
function scopedState(slot){
 const accounts=[{...owner('Первый','101'),slot:1},{...owner('Второй','202'),slot:2}];
 return {...emptyState,accounts,active_account:slot,max_accounts:15,module_version:2,account:accounts.find(a=>a.slot===slot)||null,channels:[]};
}
function scopedNormal(path,options){
 if(path==='/api/state')return json(scopedState(Number(options.headers['X-Story-Account'])));
 if(path==='/api/account/profile')return json({account:scopedState(Number(options.headers['X-Story-Account'])).account});
 if(path.startsWith('/api/people?'))return json({people:[],total:0});
 return normal(path);
}

test('ZAVOD renders four navigation tabs and fifteen account slots',async()=>{
 const h=await harness((p,_,o)=>scopedNormal(p,o));
 const html=h.elements['#app'].innerHTML;
 assert.match(html,/ZAVOD/);assert.doesNotMatch(html,/лупа|stories workspace|hero-card|Первые шаги/);
 assert.match(html,/id='account-picker'/);
 assert.equal((html.match(/<option value='(?:[1-9]|1[0-5])'/g)||[]).length,15);
 const mobile=html.slice(html.indexOf("class='mobile-nav'"));
 assert.equal((mobile.match(/data-tab=/g)||[]).length,4);
 assert.match(html,/Premium/);
 await h.clickAction('go-account');
 const profile=h.elements['#app'].innerHTML;
 assert.match(profile,/2 из 15 подключено/);
 assert.match(profile,/id='profile-about-form'/);
 assert.match(profile,/id='profile-channel-form'/);
 assert.match(profile,/id='channel-create-form'/);
});

test('Account switching clears selection and filters and scopes subsequent writes',async()=>{
 const h=await harness((p,_,o)=>p==='/api/people'?json({ok:true}):scopedNormal(p,o));
 vm.runInContext("selected.add('101');source='old-chat';search='old';people=[{user_id:'101'}]",h.context);
 await h.clickAction('select-account',{slot:'2'});
 assert.equal(vm.runInContext('accountSlot',h.context),2);
 assert.equal(vm.runInContext('state.account.name',h.context),'Второй');
 assert.equal(vm.runInContext("selected.size+':'+source+':'+search+':'+people.length",h.context),'0:::0');
 await h.submitForm('people-add-form',{username:'@user_two',request_id:'scope-user-request'});
 const post=h.requests.find(r=>r.path==='/api/people');
 assert.equal(post.options.headers['X-Story-Account'],'2');
 assert.equal(post.options.headers['X-CSRF-Token'],'test-csrf');
 const avatar=vm.runInContext("state.account.has_avatar=true;state.account.photo_revision=8;profileAvatar()",h.context);
 assert.match(avatar,/account=2&/);
});

test('A late response from a previous account cannot replace the active state',async()=>{
 let delayed=false,release;
 const h=await harness((p,_,o)=>{
  if(delayed&&p==='/api/state'&&o.headers['X-Story-Account']==='1')return new Promise(resolve=>release=resolve);
  return scopedNormal(p,o);
 });
 delayed=true;const old=vm.runInContext('sync()',h.context);
 const rejected=assert.rejects(old,err=>err.code==='ACCOUNT_CHANGED');
 await h.settle();assert.equal(typeof release,'function');
 await h.clickAction('select-account',{slot:'2'});
 release(json({...scopedState(1),stats:{people:999}}));await rejected;
 assert.equal(vm.runInContext('state.account.name',h.context),'Второй');
 assert.equal(vm.runInContext('state.stats.people',h.context),0);
});

test('An old JSON body arriving after account selection is rejected',async()=>{
 let delayed=false,release;
 const h=await harness((p,_,o)=>{
  if(delayed&&p==='/api/state'&&o.headers['X-Story-Account']==='1')return {ok:true,status:200,json:()=>new Promise(resolve=>release=resolve)};
  return scopedNormal(p,o);
 });
 delayed=true;const old=vm.runInContext('sync()',h.context);
 const rejected=assert.rejects(old,err=>err.code==='ACCOUNT_CHANGED');
 await h.settle();await h.clickAction('select-account',{slot:'2'});
 release(scopedState(1));await rejected;
 assert.equal(vm.runInContext('state.account.name',h.context),'Второй');
});

test('An account mutation holds the selection until its result is received',async()=>{
 let release;
 const h=await harness((p,_,o)=>p==='/api/account/profile/about'?new Promise(resolve=>release=resolve):scopedNormal(p,o));
 const pending=vm.runInContext("api('/api/account/profile/about',{method:'POST',body:{about:'Описание'}})",h.context);
 await h.settle();await h.clickAction('select-account',{slot:'2'});
 assert.equal(vm.runInContext('accountSlot',h.context),1);
 release(json({account:owner('Первый','101')}));await pending;
 await h.clickAction('select-account',{slot:'2'});
 assert.equal(vm.runInContext('accountSlot',h.context),2);
});

test('Fifteen occupied slots prevent opening a sixteenth account',async()=>{
 const h=await harness(normal);
 vm.runInContext("state.accounts=Array.from({length:15},(_,i)=>({slot:i+1,name:'Аккаунт '+(i+1)}));page='account';render()",h.context);
 assert.match(h.elements['#app'].innerHTML,/15 из 15 подключено/);
 assert.match(h.elements['#app'].innerHTML,/data-action='add-account' disabled/);
 const before=h.requests.length;await h.clickAction('add-account');
 assert.equal(h.requests.length,before);
 assert.match(h.elements['#toast'].textContent,/до 15 аккаунтов/);
});

test('Bio and personal channel forms save to the selected account and show canonical values',async()=>{
 const h=await harness((p,_,o)=>{
  if(p==='/api/account/profile/about')return json({account:{...owner('Второй','202'),about:'Каноническое био'}});
  if(p==='/api/account/profile/channel')return json({account:{...owner('Второй','202'),personal_channel:{id:'42',title:'Канал',username:'my_channel'}}});
  return scopedNormal(p,o);
 });
 await h.clickAction('select-account',{slot:'2'});
 const bio=await h.submitForm('profile-about-form',{about:'Новое био'});
 assert.equal(bio.form.elements.about.value,'Каноническое био');assert.match(bio.saved.textContent,/Сохранено/);
 const channel=await h.submitForm('profile-channel-form',{ref:'@my_channel'});
 assert.equal(channel.form.elements.ref.value,'@my_channel');
 for(const p of ['/api/account/profile/about','/api/account/profile/channel']){
  const req=h.requests.find(r=>r.path===p);
  assert.equal(req.options.headers['X-Story-Account'],'2');assert.equal(req.options.headers['X-CSRF-Token'],'test-csrf');
 }
});

test('Channel retry keeps the creation identity and partial success is escaped',async()=>{
 let attempts=0;
 const h=await harness((p,_,o)=>{
  if(p==='/api/account/channels')return ++attempts===1?json({detail:'Занято'},409):json({channel:{id:'42',title:'<script>Канал</script>',username:'my_channel'},pinned:false,warning:'<img src=x> закрепите отдельно',replayed:true});
  return scopedNormal(p,o);
 });
 const fields={request_id:'channel-request-stable-123',title:'Канал',username:'@my_channel',about:'Описание',pin:'on'};
 await h.submitForm('channel-create-form',fields);await h.submitForm('channel-create-form',fields);
 const posts=h.requests.filter(r=>r.path==='/api/account/channels');assert.equal(posts.length,2);
 assert(posts.every(r=>JSON.parse(r.options.body).request_id===fields.request_id));assert.equal(JSON.parse(posts[1].options.body).pin,true);
 const html=h.elements['#dialog'].innerHTML;
 assert.match(html,/&lt;script&gt;/);assert.match(html,/&lt;img/);assert.doesNotMatch(html,/<script>|<img src=x>/);assert.match(html,/Повторный канал не создан/);
});

test('An uncertain channel creation disables resubmitting the same form',async()=>{
 const h=await harness((p,_,o)=>p==='/api/account/channels'?json({detail:'Результат не подтверждён',code:'CHANNEL_CREATE_UNCERTAIN'},409):scopedNormal(p,o));
 const first=await h.submitForm('channel-create-form',{request_id:'channel-uncertain-123',title:'Канал',username:'my_channel',about:''});
 assert.equal(first.form.dataset.uncertain,'true');assert.equal(first.submit.disabled,true);
 await h.submitExisting(first.form);assert.equal(h.requests.filter(r=>r.path==='/api/account/channels').length,1);
});

test('Channel username edits use the selected account and stored channel ID',async()=>{
 const h=await harness((p,_,o)=>p==='/api/account/channels/42/username'?json({account:owner('Второй','202')}):scopedNormal(p,o));
 await h.clickAction('select-account',{slot:'2'});
 await h.submitForm('channel-username-form',{username:'new_channel'},{channel:'42'});
 const req=h.requests.find(r=>r.path==='/api/account/channels/42/username');
 assert.equal(req.options.headers['X-Story-Account'],'2');assert.deepEqual(JSON.parse(req.options.body),{username:'new_channel'});
});

test('View confirmation describes Premium policy and jobs show acknowledged reaction totals',async()=>{
 const h=await harness(normal);vm.runInContext("selected.add('123');viewDialog()",h.context);
 assert.match(h.elements['#dialog'].innerHTML,/Premium/);assert.match(h.elements['#dialog'].innerHTML,/👍/);
 const html=vm.runInContext("jobCard({id:'v',kind:'view',status:'done',viewed:3,reacted:2,checked:3,total:3})",h.context);
 assert.match(html,/Подтверждено просмотров: 3/);assert.match(html,/Реакций: 2/);
});

test('Batch mode uses an explicit view endpoint and file import can keep collection only',async()=>{
 const h=await harness((p,_,o)=>p==='/api/sources/chat-one/masslook'?json({id:'batch-job'}):scopedNormal(p,o));
 await h.clickAction('scan-source',{id:'chat-one'});
 const req=h.requests.find(r=>r.path==='/api/sources/chat-one/masslook');
 assert.equal(req.options.method,'POST');assert.deepEqual(JSON.parse(req.options.body),{delay:15});
 assert.equal(h.requests.some(r=>r.path.endsWith('/scan')),false);
 await h.clickAction('import-chats');const html=h.elements['#dialog'].innerHTML;
 assert.match(html,/name='auto_view' checked/);assert.match(html,/пачками по 5/);
 assert.match(html,/только сбор и проверка историй без просмотра/);
});

test('Legacy scan labels and uncertain statistics never imply successful views',async()=>{
 const h=await harness(normal);
 const old=vm.runInContext("jobCard({id:'old',kind:'scan',status:'done',found:55,checked:4600,total:4600})",h.context);
 assert.match(old,/Только сбор и проверка/);assert.match(old,/Просмотр в нём не запускался/);
 const batch=vm.runInContext("jobCard({id:'batch',kind:'masslook',status:'paused',found:55,viewed:0,viewed_users:0,unconfirmed:2,batch_pending:5})",h.context);
 assert.match(batch,/Подтверждено просмотров: 0/);assert.match(batch,/Попыток без подтверждения: 2/);assert.match(batch,/В пачке: 5 \/ 5/);
});

test('The previous five-account module stays behind the backend update guard',async()=>{
 const h=await harness(p=>p==='/api/state'?json({...emptyState,max_accounts:5,module_version:undefined}):normal(p));
 assert.match(h.elements['#app'].innerHTML,/Ожидаем обновления сервера/);assert.equal(h.intervals.size,0);
});

test('Proxy controls work before tdata import and keep sharing an explicit choice',async()=>{
 const h=await harness(normal);await h.clickAction('go-account');
 const html=h.elements['#app'].innerHTML;
 assert.match(html,/proxy-assignment-form/);assert.match(html,/1 прокси = 1 аккаунт/);
 await h.clickAction('add-proxy');const form=h.elements['#dialog'].innerHTML;
 assert.match(form,/SOCKS5/);assert.match(form,/HTTP CONNECT/);assert.match(form,/type='password'/);
 assert.doesNotMatch(form,/name='allow_shared' checked/);
 assert.match(form,/Добавление в список не назначает прокси аккаунтам/);
});

test('Proxy editing sends new credentials only and account assignment is scoped',async()=>{
 const h=await harness((p,_,o)=>p==='/api/proxies/proxy-one'?json({proxy:{id:'proxy-one'}}):p==='/api/account/proxy'?json({proxy:null}):scopedNormal(p,o));
 await h.clickAction('select-account',{slot:'15'});
 await h.submitForm('proxy-form',{label:'Test',host:'8.8.8.8',port:'1080',protocol:'socks5',username:'',password:''},{proxy:'proxy-one'});
 const patch=h.requests.find(r=>r.path==='/api/proxies/proxy-one'),body=JSON.parse(patch.options.body);
 assert.equal(patch.options.method,'PATCH');assert.equal(body.allow_shared,false);
 assert.equal(Object.hasOwn(body,'password'),false);assert.equal(Object.hasOwn(body,'username'),false);
 await h.submitForm('proxy-assignment-form',{proxy_id:'proxy-one'});
 const assign=h.requests.find(r=>r.path==='/api/account/proxy');
 assert.equal(assign.options.headers['X-Story-Account'],'15');assert.deepEqual(JSON.parse(assign.options.body),{proxy_id:'proxy-one'});
});

test('A late proxy list cannot overwrite another account connection',async()=>{
 let delayed=false,release;
 const h=await harness((p,_,o)=>delayed&&p.startsWith('/api/proxies?')&&o.headers['X-Story-Account']==='1'?new Promise(resolve=>release=resolve):scopedNormal(p,o));
 delayed=true;const old=vm.runInContext('loadProxies()',h.context);const rejected=assert.rejects(old,err=>err.code==='ACCOUNT_CHANGED');
 await h.settle();await h.clickAction('select-account',{slot:'2'});
 release(json({proxies:[{id:'stale',label:'Old'}],total:1,selected:{id:'stale'}}));await rejected;
 assert.equal(vm.runInContext('state.proxy',h.context),null);assert.equal(vm.runInContext('proxyData.rows.length',h.context),0);
});

test('An unchanged panel uses its private ETag and returns an unmodified copy',async()=>{
 const tag='W/"state-test"';let validations=0;
 const h=await harness((p,_,o)=>{
  if(p==='/api/state'){
   if(o.headers['If-None-Match']===tag){validations++;return new Response(null,{status:304,headers:{ETag:tag}});}
   return json(emptyState,200,{ETag:tag});
  }
  return normal(p);
 });
 vm.runInContext('state.stats.people=999',h.context);
 await vm.runInContext('sync()',h.context);
 assert.equal(validations,1);assert.equal(vm.runInContext('state.stats.people',h.context),0);
 const req=h.requests.filter(r=>r.path==='/api/state').at(-1);
 assert.equal(req.options.headers['If-None-Match'],tag);assert.equal(req.options.cache,'no-store');
});

test('Panel cache is cleared by writes and account switches',async()=>{
 const h=await harness((p,_,o)=>{
  if(p==='/api/state')return json(scopedState(Number(o.headers['X-Story-Account'])),200,{ETag:'W/"slot-'+o.headers['X-Story-Account']+'"'});
  if(p==='/api/sources'&&o.method==='POST')return json({id:'test'});
  return scopedNormal(p,o);
 });
 await vm.runInContext("api('/api/sources',{method:'POST',body:{ref:'@new_group'}});",h.context);
 await vm.runInContext('sync()',h.context);
 assert.equal(h.requests.filter(r=>r.path==='/api/state').at(-1).options.headers['If-None-Match'],undefined);
 await h.clickAction('select-account',{slot:'2'});
 assert.equal(h.requests.filter(r=>r.path==='/api/state').at(-1).options.headers['If-None-Match'],undefined);
 await h.clickAction('select-account',{slot:'1'});
 assert.equal(h.requests.filter(r=>r.path==='/api/state').at(-1).options.headers['If-None-Match'],undefined);
});

test('A response started before a write cannot restore obsolete panel data',async()=>{
 let pending=false,release;
 const h=await harness((p,_,o)=>{
  if(p==='/api/state')return pending?new Promise(resolve=>release=resolve):json(emptyState,200,{ETag:'W/"initial"'});
  if(p==='/api/sources')return json({id:'created'});
  return normal(p);
 });
 pending=true;const old=vm.runInContext('sync()',h.context);
 const rejected=assert.rejects(old,err=>err.code==='READ_SUPERSEDED');await h.settle();
 await vm.runInContext("api('/api/sources',{method:'POST',body:{ref:'@new_group'}})",h.context);
 release(json({...emptyState,stats:{people:999}},200,{ETag:'W/"stale"'}));await rejected;
 assert.equal(vm.runInContext('state.stats.people',h.context),0);
 assert.equal(vm.runInContext('readCache.size',h.context),0);
});

test('A cache validation never hides a server error or invents a missing response body',async()=>{
 let failed=false;
 const h=await harness((p,_,o)=>{
  if(p==='/api/state')return failed?json({detail:'Server unavailable'},503):json(emptyState,200,{ETag:'W/"initial"'});
  if(p.startsWith('/api/people?'))return new Response(null,{status:304});
  return normal(p);
 });
 failed=true;await assert.rejects(vm.runInContext('sync()',h.context),err=>err.status===503);
 await assert.rejects(vm.runInContext("api('/api/people?active=false')",h.context),err=>err.status===502);
});

test('Changing audience filters keeps the read cache bounded in memory',async()=>{
 const h=await harness((p,_,o)=>p.startsWith('/api/people?')?json({people:[],total:0},200,{ETag:'W/"'+p.split('=')[1]+'"'}):normal(p));
 for(let i=0;i<20;i++)await vm.runInContext("api('/api/people?search="+i+"')",h.context);
 assert(vm.runInContext('readCache.size',h.context)<=12);
});

test('The blocked chat queue explains its pause and resumes only the selected account',async()=>{
 let resumed=false;
 const h=await harness((p,_,o)=>{
  if(p==='/api/state'){
   const s=scopedState(Number(o.headers['X-Story-Account']));
   return json({...s,queue_pause:s.active_account===2&&!resumed?{id:'paused-head',message:'<script>never run</script>',waiting_count:199}:null});
  }
  if(p==='/api/jobs/paused-head/control'){resumed=true;return json({ok:true});}
  return scopedNormal(p,o);
 });
 await h.clickAction('select-account',{slot:'2'});
 const html=h.elements['#app'].innerHTML;
 assert.match(html,/Очередь чатов на паузе/);assert.match(html,/Следующих чатов в очереди: 199/);
 assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>never/);
 await h.clickAction('resume-queue',{id:'paused-head'});
 const req=h.requests.find(r=>r.path==='/api/jobs/paused-head/control');
 assert.equal(req.options.headers['X-Story-Account'],'2');assert.equal(req.options.headers['X-CSRF-Token'],'test-csrf');
 assert.deepEqual(JSON.parse(req.options.body),{action:'resume'});
 assert.doesNotMatch(h.elements['#app'].innerHTML,/Очередь чатов на паузе/);
});
