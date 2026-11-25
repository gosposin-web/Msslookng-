import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const source=(await readFile(new URL('../worker/index.js',import.meta.url),'utf8')).replace('__STORY_ASSETS__',JSON.stringify({'/index.html':{content:'<html><head></head><body><h1>Лупа</h1></body></html>',type:'text/html'}}));
const worker=(await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'))).default;
class Bucket{
 objects=new Map();count=0;
 async get(key){const entry=this.objects.get(key);if(!entry)return null;return {etag:entry.etag,httpEtag:'"'+entry.etag+'"',body:entry.body,json:async()=>JSON.parse(Buffer.from(entry.body).toString())};}
 async put(key,value,options={}){
  const old=this.objects.get(key),c=options.onlyIf;
  const match=c instanceof Headers?c.get('if-match')?.replaceAll('"',''):c?.etagMatches;
  const none=c instanceof Headers?c.get('if-none-match'):c?.etagDoesNotMatch;
  if((match&&old?.etag!==match)||(none==='*'&&old))return null;
  const etag=String(++this.count),body=typeof value==='string'?Buffer.from(value):new Uint8Array(value);
  this.objects.set(key,{etag,body});return {etag,httpEtag:'"'+etag+'"'};
 }
}
const env=()=>({STORY_BRIDGE_SECRET:'a-test-secret',BUCKET:new Bucket()});
test('A known monthly hosting pause is explicit and makes no upstream request',async()=>{
 const e=env();e.BACKEND_URL='https://backend.test';e.BACKEND_PAUSED_UNTIL=String(Math.floor(Date.now()/1000)+86400);
 const previous=globalThis.fetch;let calls=0;
 globalThis.fetch=async()=>{calls++;throw new Error('Must not contact suspended backend');};
 try{
  const r=await worker.fetch(new Request('https://private.test/health'),e);
  assert.equal(r.status,503);assert.equal((await r.json()).reason,'hosting_monthly_quota');assert.equal(calls,0);assert(Number(r.headers.get('retry-after'))>86390);
  const head=await worker.fetch(new Request('https://private.test/health',{method:'HEAD'}),e);assert.equal(head.status,503);assert.equal(await head.text(),'');
 }finally{globalThis.fetch=previous;}
});
test('Proxy forwards the selected account without forwarding visitor credentials',async()=>{
 const e=env();e.BACKEND_URL='https://backend.test';const previous=globalThis.fetch;let observed;
 globalThis.fetch=async(url,options)=>{observed=options;return Response.json({ok:true});};
 try{
  const r=await worker.fetch(request('/api/account/profile/about','POST',{'oai-authenticated-user-id':'site-owner','X-Story-Account':'5','Content-Type':'application/json','Origin':'https://private.test','Cookie':'private=1','Authorization':'Bearer secret'},JSON.stringify({about:'Описание'})),e);
  assert.equal(r.status,200);assert.equal(observed.headers.get('x-story-account'),'5');
  assert.equal(observed.headers.get('cookie'),null);assert.equal(observed.headers.get('authorization'),null);
 }finally{globalThis.fetch=previous;}
});
function request(path,method='GET',headers={},body){return new Request('https://private.test'+path,{method,headers:{'X-Story-Cloud-Key':'a-test-secret','X-Story-Instance':'one',...headers},body});}
test('Private checkpoint requires the backend key',async()=>{
 const r=await worker.fetch(request('/internal/snapshot','GET',{'X-Story-Cloud-Key':'wrong'}),env());assert.equal(r.status,403);
});
test('Lease and conditional checkpoint prevent competing writes',async()=>{
 const e=env();assert.equal((await worker.fetch(request('/internal/lease','POST'),e)).status,200);
 assert.equal((await worker.fetch(request('/internal/lease','POST',{'X-Story-Instance':'two'}),e)).status,409);
 const saved=await worker.fetch(request('/internal/snapshot','PUT',{'If-None-Match':'*'},'encrypted'),e);assert.equal(saved.status,200);
 const read=await worker.fetch(request('/internal/snapshot'),e);assert.equal(await read.text(),'encrypted');assert.equal(read.headers.get('etag'),saved.headers.get('etag'));
 assert.equal((await worker.fetch(request('/internal/snapshot','PUT',{'If-Match':'"old"'},'bad'),e)).status,412);
 assert.equal((await worker.fetch(request('/internal/snapshot','PUT',{'X-Story-Instance':'two','If-Match':saved.headers.get('etag')},'bad'),e)).status,409);
});
test('Proxy does not send sign-in cookies or platform bearer tokens to Render',async()=>{
 const e=env();e.BACKEND_URL='https://backend.test';
 const previous=globalThis.fetch;let observed;
 globalThis.fetch=async(url,options)=>{observed={url:String(url),...options};return Response.json({ok:true},{headers:{'Set-Cookie':'forbidden=1'}});};
 try{
  const r=await worker.fetch(request('/api/state','GET',{'oai-authenticated-user-id':'site-owner','Cookie':'site-sign-in=private','Authorization':'Bearer private','OAI-Sites-Authorization':'Bearer private'}),e);
  assert.equal(r.status,200);assert.equal(observed.url,'https://backend.test/api/state');
  assert.equal(observed.headers.get('cookie'),null);assert.equal(observed.headers.get('authorization'),null);assert.equal(observed.headers.get('oai-sites-authorization'),null);
  assert.equal(observed.headers.get('x-story-user'),'site-owner');assert.equal(observed.headers.get('x-story-bridge-secret'),'a-test-secret');assert.equal(r.headers.get('set-cookie'),null);
 }finally{globalThis.fetch=previous;}
});
test('Cross-origin mutations and identity-less proxy requests are rejected',async()=>{
 const e=env();e.BACKEND_URL='https://backend.test';
 assert.equal((await worker.fetch(request('/api/state'),e)).status,401);
 assert.equal((await worker.fetch(request('/api/sources','POST',{'oai-authenticated-user-id':'owner','Origin':'https://evil.test'},'{}'),e)).status,403);
});
test('Service readiness returns metadata without credentials or account data',async()=>{
 const e=env();e.BACKEND_URL='https://backend.test';
 e.BUCKET.head=async()=>({size:4096});
 const previous=globalThis.fetch;
 globalThis.fetch=async()=>Response.json({ok:true,service:'story-cloud',ready:true,mode:'free'});
 try{
  const r=await worker.fetch(new Request('https://private.test/service-health'),e);
  assert.deepEqual(await r.json(),{ready:true,mode:'free',checkpoint_saved:true,checkpoint_bytes:4096});
 }finally{globalThis.fetch=previous;}
});
test('Avatar cache validation stays private and forwards only the photo ETag',async()=>{
 const e=env();e.BACKEND_URL='https://backend.test';
 const previous=globalThis.fetch;let forwarded;
 globalThis.fetch=async(url,options)=>{forwarded=options.headers;return new Response(null,{status:304,headers:{ETag:'"revision"','Cache-Control':'private, max-age=300'}});};
 try{
  const r=await worker.fetch(request('/api/account/profile/photo?v=revision','GET',{'oai-authenticated-user-id':'owner','If-None-Match':'"revision"','Cookie':'private-session'}),e);
  assert.equal(r.status,304);assert.equal(r.headers.get('cache-control'),'private, max-age=300');
  assert.equal(forwarded.get('if-none-match'),'"revision"');assert.equal(forwarded.get('cookie'),null);
 }finally{globalThis.fetch=previous;}
});
test('Health checks send no visitor or bridge credentials upstream',async()=>{
 const e=env();e.BACKEND_URL='https://health-privacy.test';
 const previous=globalThis.fetch;let observed;
 globalThis.fetch=async(url,options)=>{observed={url,...options};return Response.json({ok:true,service:'story-cloud',ready:true,mode:'free'});};
 try{
  const r=await worker.fetch(request('/health','GET',{'Cookie':'private','Authorization':'Bearer private','OAI-Sites-Authorization':'Bearer private','oai-authenticated-user-id':'owner'}),e);
  assert.equal(r.status,200);
  assert.deepEqual(observed.headers,{Accept:'application/json'});
  assert.equal(observed.redirect,'manual');
  assert.equal(r.headers.get('cache-control'),'no-store');
  assert.equal((await worker.fetch(request('/api/state'),e)).status,401);
 }finally{globalThis.fetch=previous;}
});
test('Concurrent startup probes share one request and briefly reuse a healthy result',async()=>{
 const e=env();e.BACKEND_URL='https://health-coalescing.test';
 const previous=globalThis.fetch;let calls=0,release;
 globalThis.fetch=async()=>{calls++;return new Promise(resolve=>{release=resolve;});};
 try{
  const pending=Array.from({length:3},()=>worker.fetch(request('/health'),e));
  assert.equal(calls,1);
  release(Response.json({ok:true,service:'story-cloud',ready:true}));
  const results=await Promise.all(pending);assert(results.every(r=>r.status===200));
  assert.equal((await worker.fetch(request('/health'),e)).status,200);
  assert.equal(calls,1);
 }finally{globalThis.fetch=previous;}
});
test('Hosting 429 preserves Retry-After and prevents another upstream probe during the pause',async()=>{
 const e=env();e.BACKEND_URL='https://health-rate-limit.test';e.BUCKET.head=async()=>({size:4096});
 const previous=globalThis.fetch;let calls=0;
 globalThis.fetch=async()=>{calls++;return new Response('<html>rate limited</html>',{status:429,headers:{'Retry-After':'120'}});};
 try{
  const r=await worker.fetch(request('/health'),e);assert.equal(r.status,429);
  assert.equal(r.headers.get('retry-after'),'120');
  assert.equal((await r.json()).reason,'hosting_rate_limit');
  assert.equal((await worker.fetch(request('/health'),e)).status,429);
  const diagnostic=await worker.fetch(request('/service-health'),e),data=await diagnostic.json();
  assert.equal(data.upstream_status,429);assert.equal(data.checkpoint_saved,true);assert.equal(data.ready,false);
  assert.equal(calls,1);
 }finally{globalThis.fetch=previous;}
});
test('Unexpected health responses and failures never become preview mode',async()=>{
 const previous=globalThis.fetch;
 try{
  for(const [name,response] of [['html',new Response('<html>startup</html>')],['json',Response.json({ok:true})]]){
   const e=env();e.BACKEND_URL='https://health-'+name+'.test';
   globalThis.fetch=async()=>response;
   const r=await worker.fetch(request('/health'),e);assert.equal(r.status,503);
   const data=await r.json();assert.equal(data.service,'story-cloud');assert.equal(data.ready,false);
  }
 }finally{globalThis.fetch=previous;}
});
test('Read-only health endpoint supports HEAD and rejects mutations',async()=>{
 const e=env();e.BACKEND_URL='https://health-methods.test';
 const previous=globalThis.fetch;
 globalThis.fetch=async()=>Response.json({ok:true,service:'story-cloud',ready:true});
 try{
  const head=await worker.fetch(request('/health','HEAD'),e);assert.equal(head.status,200);assert.equal(await head.text(),'');
  const post=await worker.fetch(request('/health','POST'),e);assert.equal(post.status,405);
 }finally{globalThis.fetch=previous;}
});
test('Frontend receives only a public browser wake URL without credentials',async()=>{
 const e=env();e.BACKEND_URL='https://our-app.onrender.com';
 const r=await worker.fetch(request('/'),e),body=await r.text();
 assert.match(body,/name="story-backend-wake" content="https:\/\/our-app.onrender.com\/wake"/);
 assert.doesNotMatch(body,/a-test-secret|site-owner|credential/);
 for(const bad of ['http://our-app.onrender.com','https://user:password@our-app.onrender.com','https://evil.example']){
  e.BACKEND_URL=bad;const response=await worker.fetch(request('/'),e);
  assert.doesNotMatch(await response.text(),/story-backend-wake/);
 }
});
