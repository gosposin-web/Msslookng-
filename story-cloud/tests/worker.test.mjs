import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const source=(await readFile(new URL('../worker/index.js',import.meta.url),'utf8')).replace('__STORY_ASSETS__',JSON.stringify({'/index.html':{content:'<h1>Лупа</h1>',type:'text/html'}}));
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
