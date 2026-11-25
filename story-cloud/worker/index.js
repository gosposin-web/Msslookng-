const ASSETS=__STORY_ASSETS__;
const MAX_SNAPSHOT=16*1024*1024;
const CHECKPOINT='story-cloud/state-v1.enc';
const LEASE='story-cloud/lease-v1.json';
const json=(body,status=200,headers={})=>Response.json(body,{status,headers:{'Cache-Control':'no-store',...headers}});
const healthProbes=new Map();

function retrySeconds(value,fallback=30){
 if(!value?.trim())return fallback;
 const number=Number(value),seconds=Number.isFinite(number)?number:(Date.parse(value)-Date.now())/1000;
 return Number.isFinite(seconds)&&seconds>0?Math.ceil(seconds):fallback;
}
async function probeHealth(env){
 const key=new URL('/health',env.BACKEND_URL).href;
 let entry=healthProbes.get(key);
 if(entry?.pending)return entry.pending;
 if(entry?.result&&entry.until>Date.now())return entry.result;
 entry={};healthProbes.set(key,entry);
 entry.pending=(async()=>{
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),60000);
  let result;
  try{
   // Health is public metadata on Render. Never send visitor identity or bridge
   // credentials for this request. Site access control still guards this origin.
   const response=await fetch(key,{headers:{Accept:'application/json'},redirect:'manual',signal:controller.signal});
   if(!response.ok){
    const wait=retrySeconds(response.headers.get('Retry-After'),response.status===429?30:10);
    result={status:response.status===429?429:503,upstreamStatus:response.status,wait,
     payload:{ok:false,service:'story-cloud',ready:false,reason:response.status===429?'hosting_rate_limit':'hosting_unavailable'}};
    if(response.body)await response.body.cancel().catch(()=>{});
   }else{
    const health=await response.json();
    if(health.service!=='story-cloud'||health.ok!==true||typeof health.ready!=='boolean')throw new Error('Invalid health response');
    result={status:health.ready?200:503,upstreamStatus:response.status,wait:health.ready?10:5,
     payload:{ok:true,service:'story-cloud',ready:health.ready,mode:health.mode,api_mode:health.api_mode}};
   }
  }catch{
   result={status:503,upstreamStatus:0,wait:10,payload:{ok:false,service:'story-cloud',ready:false,reason:'hosting_connection_failed'}};
  }finally{clearTimeout(timeout);}
  entry.result=result;entry.until=Date.now()+result.wait*1000;result.until=entry.until;entry.pending=null;
  if(result.status!==200)console.warn('health_probe_failed upstream_status='+result.upstreamStatus+' reason='+(result.payload.reason||'restoring')+' retry_after='+result.wait);
  return result;
 })();
 return entry.pending;
}
function healthResponse(result,head=false){
 const headers={'Cache-Control':'no-store','Content-Type':'application/json'};
 if(result.status!==200)headers['Retry-After']=String(Math.max(1,Math.ceil((result.until-Date.now())/1000)));
 return head?new Response(null,{status:result.status,headers}):json(result.payload,result.status,headers);
}
function frontendContent(asset,env){
 if(!asset.type.startsWith('text/html')||!env.BACKEND_URL)return asset.content;
 try{
  const wake=new URL('/wake',env.BACKEND_URL);
  if(wake.protocol!=='https:'||!wake.hostname.endsWith('.onrender.com')||wake.username||wake.password||wake.port)return asset.content;
  // Public readiness document only. Normal navigation can complete the hosting
  // platform's cold-start page; a cross-origin opaque fetch cannot confirm it.
  return asset.content.replace('</head>',`<meta name="story-backend-wake" content="${wake.href}"></head>`);
 }catch{return asset.content;}
}

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
  if(url.pathname==='/health'){
   if(!['GET','HEAD'].includes(request.method))return json({detail:'Method not allowed'},405,{Allow:'GET, HEAD'});
   if(!env.BACKEND_URL)return json({ok:false,service:'story-preview',ready:false});
   return healthResponse(await probeHealth(env),request.method==='HEAD');
  }
  if(url.pathname==='/service-health'&&request.method==='GET'){
   // Owner-private dispatch also authorizes the existing Site service credential.
   // Return only readiness and encrypted-file metadata, never account data or keys.
   if(!env.BACKEND_URL||!env.BUCKET)return json({ready:false,checkpoint_saved:false},503);
   try{
    const [probe,checkpoint]=await Promise.all([probeHealth(env),env.BUCKET.head(CHECKPOINT)]);
    return json({ready:probe.payload.ok===true&&probe.payload.ready===true,mode:probe.payload.mode,
     checkpoint_saved:Boolean(checkpoint),checkpoint_bytes:checkpoint?.size||0,
     ...(probe.status!==200?{upstream_status:probe.upstreamStatus,reason:probe.payload.reason||'restoring',retry_after:Math.max(1,Math.ceil((probe.until-Date.now())/1000))}:{})});
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
  if(url.pathname.startsWith('/api/')){
   if(!env.BACKEND_URL)return json({ok:false,service:'story-preview',ready:false});
   const user=request.headers.get('oai-authenticated-user-id');
   if(!user)return json({detail:'Войдите в личную панель через ChatGPT.'},401);
   const incomingOrigin=request.headers.get('Origin');
   if(!['GET','HEAD','OPTIONS'].includes(request.method)&&incomingOrigin&&incomingOrigin!==url.origin)return json({detail:'Запрос с другого сайта отклонён.'},403);
   // Never forward the Site's sign-in cookies, bearer tokens or other visitor headers.
   const headers=new Headers();
   for(const name of ['accept','content-type','content-length','x-csrf-token','x-story-account','if-none-match'])if(request.headers.has(name))headers.set(name,request.headers.get(name));
   headers.set('X-Story-Bridge-Secret',env.STORY_BRIDGE_SECRET);
   headers.set('X-Story-User',user);
   headers.set('Origin',url.origin);
   let response;
   try{response=await fetch(new URL(url.pathname+url.search,env.BACKEND_URL),{method:request.method,headers,body:['GET','HEAD'].includes(request.method)?undefined:request.body,redirect:'manual'});}
   catch{return json({detail:'Бесплатный сервер просыпается. Повторите через минуту.'},503);}
   const out=new Headers(response.headers);out.delete('set-cookie');
   if(!(request.method==='GET'&&url.pathname==='/api/account/profile/photo'&&(response.ok||response.status===304)))out.set('Cache-Control','no-store');
   return new Response(response.body,{status:response.status,headers:out});
  }
  if(request.method!=='GET'&&request.method!=='HEAD')return json({error:'Method not allowed'},405);
  const asset=ASSETS[url.pathname==='/'?'/index.html':url.pathname];
  if(!asset)return new Response('Not found',{status:404});
  return new Response(request.method==='HEAD'?null:frontendContent(asset,env),{headers:{'Content-Type':asset.type,'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer'}});
 }
};
