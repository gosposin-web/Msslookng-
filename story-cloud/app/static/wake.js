'use strict';
// This public page handles readiness only. It never reads account data, asks
// for credentials, or accepts an arbitrary return destination from a query.
(()=>{
 const raw=document.body.dataset.panelOrigin;
 let panel;
 try{panel=new URL(raw);}catch{return;}
 if(panel.protocol!=='https:'||panel.username||panel.password||panel.port||panel.search||panel.hash||panel.pathname!=='/')return;
 const returnUrl=new URL('/#server-started',panel.origin).href;
 const status=document.getElementById('wake-status');
 const link=document.getElementById('wake-return');if(link)link.href=returnUrl;
 let pending=false,done=false,timer=null,retryAt=0;
 function schedule(delay){retryAt=Date.now()+delay;clearTimeout(timer);if(document.visibilityState!=='hidden')timer=setTimeout(check,delay);}
 async function check(){
  if(done||pending)return;
  const remaining=retryAt-Date.now();if(remaining>0){schedule(remaining);return;}
  pending=true;
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),15000);
  try{
   const response=await fetch('/health',{cache:'no-store',credentials:'omit',headers:{Accept:'application/json'},signal:controller.signal});
   if(!response.ok){
    const value=response.headers.get('Retry-After');
    const seconds=value?.trim()?Number(value):NaN;
    const delay=Number.isFinite(seconds)?seconds*1000:Date.parse(value)-Date.now();
    schedule(Number.isFinite(delay)&&delay>0?Math.max(1000,delay):10000);return;
   }
   const data=await response.json();
   if(data.service!=='story-cloud'||data.ok!==true||data.ready!==true){schedule(5000);return;}
   done=true;clearTimeout(timer);
   if(status)status.textContent='Сервер готов. Открываем панель…';
   if(window.parent!==window)window.parent.postMessage({type:'story-backend-ready'},panel.origin);
   else window.location.replace(returnUrl);
  }catch{if(status)status.textContent='Соединение пока не установлено. Проверяем автоматически…';schedule(10000);}
  finally{clearTimeout(timeout);pending=false;}
 }
 function resume(){if(!done&&document.visibilityState!=='hidden'){clearTimeout(timer);void check();}}
 document.addEventListener('visibilitychange',resume);
 window.addEventListener('online',resume);
 void check();
})();
