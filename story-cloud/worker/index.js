const ASSETS=__STORY_ASSETS__;
const MAX_SNAPSHOT=16*1024*1024;
const CHECKPOINT='story-cloud/state-v1.enc';
const LEASE='story-cloud/lease-v1.json';
const json=(body,status=200,headers={})=>Response.json(body,{status,headers:{'Cache-Control':'no-store',...headers}});

async function equalSecret(value,expected){
 if(!value||!expected)return false;
 const enc=new TextEncoder();
 const [a,b]=await Promise.all([crypto.subtle.digest('SHA-256',enc.encode(value)),crypto.subtle.digest('SHA-256',enc.encode(expected))]);
 const aa=new Uint8Array(a),bb=new Uint8Array(b);let diff=0;for(let i=0;i<aa.length;i++)diff|=aa[i]^bb[i];return diff===0;
}
async function lease(bucket){const obj=await bucket.get(LEASE);return obj?{object:obj,data:await obj.json()}:null;}
function condition(obj){return obj?{etagMatches:obj.etag}:{etagDoesNotMatch:'*'};}
async function boundedBody(request,limit){
 if(Number(request.headers.get('content-length')||0)>limit)throw new Error('oversize');
 const reader=request.body?.getReader();if(!reader)return new Uint8Array();
 const chunks=[];let size=0;
 for(;;){const r=await reader.read();if(r.done)break;size+=r.value.byteLength;if(size>limit){await reader.cancel();throw new Error('oversize');}chunks.push(r.value);}
 const data=new Uint8Array(size);let offset=0;for(const chunk of chunks){data.set(chunk,offset);offset+=chunk.byteLength;}return data;
}

export default {
 async fetch(request,env){
  const url=new URL(request.url);
  if(url.pathname==='/service-health'&&request.method==='GET'){
   // Owner-private dispatch also authorizes the existing Site service credential.
   // Return only readiness and encrypted-file metadata, never account data or keys.
   if(!env.BACKEND_URL||!env.BUCKET)return json({ready:false,checkpoint_saved:false},503);
   try{
    const [response,checkpoint]=await Promise.all([
     fetch(new URL('/health',env.BACKEND_URL)),env.BUCKET.head(CHECKPOINT)]);
    const health=await response.json();
    return json({ready:health.ok===true&&health.ready===true,mode:health.mode,
     checkpoint_saved:Boolean(checkpoint),checkpoint_bytes:checkpoint?.size||0});
   }catch{return json({ready:false,checkpoint_saved:false},503);}
  }
  if(url.pathname.startsWith('/internal/')){
   // Dispatch validates the private Site credential. This second key scopes backend calls.
   if(!await equalSecret(request.headers.get('X-Story-Cloud-Key'),env.STORY_BRIDGE_SECRET))return json({error:'Forbidden'},403);
   if(!env.BUCKET)return json({error:'Storage unavailable'},503);
   const owner=request.headers.get('X-Story-Instance')||'';
   const currentTime=Math.floor(Date.now()/1000);
   if(url.pathname==='/internal/lease'){
    if(!owner||owner.length>128)return json({error:'Invalid instance'},400);
    const current=await lease(env.BUCKET);
    if(current&&current.data.expires>currentTime&&current.data.owner!==owner)return json({error:'Busy'},409);
    if(request.method!=='POST'&&request.method!=='DELETE')return json({error:'Method not allowed'},405);
    if(request.method==='DELETE'&&current&&current.data.owner!==owner)return json({error:'Busy'},409);
    const expires=request.method==='DELETE'?0:currentTime+90;
    const written=await env.BUCKET.put(LEASE,JSON.stringify({owner,expires}),{onlyIf:condition(current?.object)});
    return written?json({expires}):json({error:'Busy'},409);
   }
   if(url.pathname==='/internal/snapshot'){
    if(request.method==='GET'){
     const object=await env.BUCKET.get(CHECKPOINT);
     return object?new Response(object.body,{headers:{'Content-Type':'application/octet-stream','Cache-Control':'no-store',ETag:object.httpEtag}}):json({error:'No checkpoint'},404);
    }
    if(request.method==='PUT'){
     const current=await lease(env.BUCKET);
     if(!owner||!current||current.data.owner!==owner||current.data.expires<=currentTime)return json({error:'Lease required'},409);
     if(!request.headers.has('If-Match')&&request.headers.get('If-None-Match')!=='*')return json({error:'Conditional write required'},428);
     let data;try{data=await boundedBody(request,MAX_SNAPSHOT);}catch{return json({error:'Checkpoint too large'},413);}
     const written=await env.BUCKET.put(CHECKPOINT,data,{onlyIf:request.headers,httpMetadata:{contentType:'application/octet-stream'}});
     return written?json({ok:true},200,{ETag:written.httpEtag}):json({error:'Checkpoint changed'},412);
    }
    return json({error:'Method not allowed'},405);
   }
   return json({error:'Not found'},404);
  }
  if(url.pathname==='/health'||url.pathname.startsWith('/api/')){
   if(!env.BACKEND_URL)return json({ok:false,service:'story-preview',ready:false});
   const user=request.headers.get('oai-authenticated-user-id');
   if(!user)return json({detail:'Войдите в личную панель через ChatGPT.'},401);
   const incomingOrigin=request.headers.get('Origin');
   if(!['GET','HEAD','OPTIONS'].includes(request.method)&&incomingOrigin&&incomingOrigin!==url.origin)return json({detail:'Запрос с другого сайта отклонён.'},403);
   // Never forward the Site's sign-in cookies, bearer tokens or other visitor headers.
   const headers=new Headers();
   for(const name of ['accept','content-type','content-length','x-csrf-token'])if(request.headers.has(name))headers.set(name,request.headers.get(name));
   headers.set('X-Story-Bridge-Secret',env.STORY_BRIDGE_SECRET);
   headers.set('X-Story-User',user);
   headers.set('Origin',url.origin);
   let response;
   try{response=await fetch(new URL(url.pathname+url.search,env.BACKEND_URL),{method:request.method,headers,body:['GET','HEAD'].includes(request.method)?undefined:request.body,redirect:'manual'});}
   catch{return json({detail:'Бесплатный сервер просыпается. Повторите через минуту.'},503);}
   const out=new Headers(response.headers);out.delete('set-cookie');out.set('Cache-Control','no-store');
   return new Response(response.body,{status:response.status,headers:out});
  }
  if(request.method!=='GET'&&request.method!=='HEAD')return json({error:'Method not allowed'},405);
  const asset=ASSETS[url.pathname==='/'?'/index.html':url.pathname];
  if(!asset)return new Response('Not found',{status:404});
  return new Response(request.method==='HEAD'?null:asset.content,{headers:{'Content-Type':asset.type,'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer'}});
 }
};
