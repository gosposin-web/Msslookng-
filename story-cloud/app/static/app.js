'use strict';
const paths={
 dashboard:`<rect x='3' y='3' width='7' height='7' rx='1.5'/><rect x='14' y='3' width='7' height='7' rx='1.5'/><rect x='3' y='14' width='7' height='7' rx='1.5'/><rect x='14' y='14' width='7' height='7' rx='1.5'/>`,
 chat:`<path d='M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8z'/><path d='M8 11h8'/>`,
 people:`<path d='M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2m20 0v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8'/><circle cx='9' cy='7' r='4'/>`,
 jobs:`<rect x='5' y='5' width='14' height='16' rx='2'/><path d='M9 3h6v4H9zm0 8h6m-6 4h6m-6 3h3'/>`,
 account:`<circle cx='12' cy='8' r='4'/><path d='M4 21v-2a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v2'/>`,
 plus:`<path d='M12 5v14M5 12h14'/>`,
 cloud:`<path d='M20 16.5a4.5 4.5 0 0 0-1-8.9A7 7 0 0 0 5.1 8.8a4 4 0 0 0 .9 7.7h14z'/>`,
 eye:`<path d='M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z'/><circle cx='12' cy='12' r='3'/>`,
 story:`<path d='M8 3.8a9 9 0 0 1 8 0M20.3 8a9 9 0 0 1 0 8M16 20.3a9 9 0 0 1-8 0M3.7 16a9 9 0 0 1 0-8'/><path d='m10 8 6 4-6 4z'/>`,
 clock:`<circle cx='12' cy='12' r='9'/><path d='M12 7v5l3 2'/>`,
 lock:`<rect x='5' y='10' width='14' height='11' rx='2'/><path d='M8 10V7a4 4 0 0 1 8 0v3m-4 5v2'/>`,
 check:`<path d='m5 12 4 4L19 6'/>`,
 info:`<circle cx='12' cy='12' r='9'/><path d='M12 11v6m0-10h.01'/>`,
 search:`<circle cx='10.5' cy='10.5' r='6.5'/><path d='m16 16 4.5 4.5'/>`,
 trash:`<path d='M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7'/>`,
 play:`<path d='m8 5 11 7-11 7z'/>`,
 pause:`<path d='M8 5v14m8-14v14'/>`,
 stop:`<rect x='6' y='6' width='12' height='12' rx='1'/>`,
 download:`<path d='M12 3v12m-5-5 5 5 5-5M4 15v6h16v-6'/>`,
 close:`<path d='m6 6 12 12M6 18 18 6'/>`,
 logout:`<path d='M9 5H4v14h5m4-13 6 6-6 6m-5-6h11'/>`,
 refresh:`<path d='M20 7v5h-5M4 17v-5h5m10.5-4.5a8 8 0 0 0-13.2-3M4.5 16.5a8 8 0 0 0 13.2 3'/>`
};
const icon=n=>`<svg viewBox='0 0 24 24' aria-hidden='true'>${paths[n]||paths.info}</svg>`;
const esc=v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll("'",'&#39;').replaceAll(String.fromCharCode(34),'&quot;');
const number=v=>new Intl.NumberFormat('ru-RU').format(v||0);
const date=v=>v?new Intl.DateTimeFormat('ru-RU',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}).format(v*1000):'—';
const initials=n=>String(n||'?').split(' ').filter(Boolean).slice(0,2).map(x=>x[0]).join('').toUpperCase();
const labels={overview:'Главная',sources:'Чаты',audience:'Люди',jobs:'Задания',account:'Аккаунты'};
const navIcons={overview:'dashboard',sources:'chat',audience:'people',jobs:'jobs',account:'account'};
const statuses={queued:'В очереди',running:'Выполняется',waiting:'Ожидание',paused:'На паузе',done:'Завершено',failed:'Ошибка',cancelled:'Остановлено'};
let state={account:null,sources:[],jobs:[],events:[],stats:{people:0,active:0,views_24h:0},view_limit_24h:200,cooldown_until:0};
let server=false,authenticated=false,csrf='',page='overview',people=[],totalPeople=0,offset=0,activeOnly=true,search='',source='',selected=new Set(),polling=false,searchTimer;
const app=document.querySelector('#app');
const dialog=document.querySelector('#dialog');
let toastTimer;
let profileLoading=false,profileBusy=false,avatarPreview='';
let jobsShown=40;
let accountSlot=1,accountEpoch=0,accountWrites=0,peopleRequest=0;
try{const saved=window.localStorage?.getItem('zavod.active_account');if(/^[1-5]$/.test(saved||''))accountSlot=Number(saved);}catch{}
function connectedAccounts(){return Array.isArray(state.accounts)?state.accounts:(state.account?[{...state.account,slot:accountSlot}]:[]);}
function scopeChanged(){return Object.assign(new Error(''),{status:409,code:'ACCOUNT_CHANGED'});}
function lockAccountPicker(){document.querySelectorAll('[data-action="select-account"],[data-action="add-account"],#account-picker').forEach(el=>el.disabled=accountWrites>0||profileBusy);}
function accountPicker(){const accounts=connectedAccounts();return `<label class='account-picker'><span>Аккаунт</span><select id='account-picker' aria-label='Активный аккаунт' ${accountWrites||profileBusy?'disabled':''}>${Array.from({length:5},(_,i)=>{const slot=i+1,a=accounts.find(a=>Number(a.slot)===slot);return `<option value='${slot}' ${slot===accountSlot?'selected':''}>${slot}. ${a?esc(a.name):'Добавить аккаунт'}</option>`;}).join('')}</select></label>`;}
async function selectAccount(value){
 const next=Number(value);if(!Number.isInteger(next)||next<1||next>5)return;
 if(accountWrites||profileBusy){toast('Дождитесь завершения сохранения.');return;}
 if(next===accountSlot){await go('account');return;}
 const accounts=connectedAccounts();if(!accounts.some(a=>Number(a.slot)===next))page='account';
 accountSlot=next;accountEpoch++;peopleRequest++;clearTimeout(searchTimer);
 try{window.localStorage?.setItem('zavod.active_account',String(next));}catch{}
 if(avatarPreview){URL.revokeObjectURL(avatarPreview);avatarPreview='';}
 state={...state,account:null,accounts,sources:[],jobs:[],events:[],channels:[],source_imports:[],job_counts:{},stats:{people:0,active:0,views_24h:0},cooldown_until:0};
 selected.clear();people=[];totalPeople=0;offset=0;source='';search='';activeOnly=true;jobsShown=40;profileLoading=false;render();
 try{await sync();if(page==='account'&&state.account)await refreshProfile();}
 catch(err){if(err.code!=='ACCOUNT_CHANGED')toast(err.message||'Не удалось загрузить аккаунт. Нажмите «Обновить».');}
}

function toast(message){if(!message)return;const el=document.querySelector('#toast');el.textContent=message;el.classList.add('visible');clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove('visible'),6500);}
function online(){if(server&&authenticated)return true;toast(server?'Войдите в панель.':'Сервер Telegram ещё не запущен. Сейчас доступен просмотр интерфейса.');return false;}
async function api(path,options={}){
 const slot=accountSlot,epoch=accountEpoch,writing=!!options.method&&!['GET','HEAD','OPTIONS'].includes(options.method);
 const headers={Accept:'application/json',...options.headers,'X-Story-Account':String(slot)};
 if(writing)headers['X-CSRF-Token']=csrf;
 if(options.body&&!(options.body instanceof FormData)){headers['Content-Type']='application/json';options.body=JSON.stringify(options.body);}
 const {authPrompt=true,...requestOptions}=options;
 if(writing){accountWrites++;lockAccountPicker();}
 try{
  const response=await fetch(path,{...requestOptions,headers,credentials:'same-origin'});
  if(epoch!==accountEpoch)throw scopeChanged();
  let data;try{data=await response.json();}catch{if(epoch!==accountEpoch)throw scopeChanged();const error=new Error(response.status===429?'Сервер попросил подождать.':'Сервер не вернул ожидаемый ответ. Повторите позже.');Object.assign(error,{status:response.status,retryAfter:retryDelay(response,30000)});throw error;}
  if(epoch!==accountEpoch)throw scopeChanged();
  if(!data||typeof data!=='object')throw new Error('Сервер не вернул ожидаемый ответ. Повторите позже.');
  if(!response.ok){if(response.status===401&&server&&path!=='/api/auth/login'&&authPrompt){authenticated=false;showLogin();}const error=new Error(Array.isArray(data.detail)?'Проверьте значения полей.':data.detail||'Не удалось выполнить действие.');Object.assign(error,{status:response.status,code:data.code,hint:data.hint,appealUrl:data.appeal_url,freezeUntil:data.freeze_until,retryAfter:retryDelay(response,30000),diagnosticId:data.diagnostic_id,stageLabel:data.stage_label});throw error;}
  return data;
 }finally{if(writing){accountWrites--;lockAccountPicker();}}
}
function nav(mobile=false){return Object.entries(labels).filter(([key])=>key!=='sources').map(([key,label])=>`<button type='button' class='${mobile?'':'nav-btn '}${page===key?'active':''}' data-tab='${key}' ${page===key?"aria-current='page'":''}>${icon(navIcons[key])}<span>${label}</span>${!mobile&&key==='audience'&&state.stats.people?`<span class='nav-count'>${number(state.stats.people)}</span>`:''}</button>`).join('');}
function title(eyebrow,heading,sub,button=''){return `<div class='page-header'><div><div class='eyebrow'>${eyebrow}</div><h1>${heading}</h1><p class='subtitle'>${sub}</p></div>${button}</div>`;}
function empty(type,heading,body,button=''){return `<div class='empty-state'><div class='empty-icon'>${icon(type)}</div><h3>${heading}</h3><p>${body}</p>${button}</div>`;}
const button=(action,label,type='primary',extra='',symbol='')=>`<button type='button' class='btn ${type}' data-action='${action}' ${extra}>${symbol?icon(symbol):''}${label}</button>`;
function render(){
 app.innerHTML=`<div class='app-shell'><aside class='sidebar'><a class='brand' href='#' data-tab='overview'><img src='/assets/icon.svg' alt=''><div class='brand-name'>ZAVOD <small>1.0</small></div></a><nav class='nav-list' aria-label='Основная навигация'>${nav()}</nav><div class='account-mini'><div class='avatar'>${state.account?esc(initials(state.account.name)):icon('account')}</div><div><strong>${state.account?esc(state.account.name):'Аккаунт '+accountSlot}</strong><small>${state.account?'Подключён':'Не подключён'}</small></div></div></aside><div class='workspace'><header class='topbar'><a class='mobile-brand' href='#' data-tab='overview'><img src='/assets/icon.svg' alt=''>ZAVOD <span>1.0</span></a><div class='breadcrumb'>${labels[page]}</div><div class='topbar-actions'>${accountPicker()}<button type='button' class='icon-btn' data-action='help' aria-label='Помощь'>${icon('info')}</button></div></header><main class='content'>${!server?`<div class='banner'>${icon('info')}<span><b>Предпросмотр интерфейса.</b> Сервер Telegram ещё не запущен. Импорт и задания станут доступны после запуска.</span></div>`:''}${state.storage_error?`<div class='banner error-banner'>${icon('info')}<span>${esc(state.storage_error)}</span></div>`:''}${state.cooldown_until>Date.now()/1000?`<div class='banner'>${icon('clock')}<span>Для аккаунта ${accountSlot} Telegram запросил паузу до ${esc(date(state.cooldown_until))}. Продолжим автоматически.</span></div>`:''}<div id='page-content'>${({overview:overview,sources:sourcesPage,audience:audiencePage,jobs:jobsPage,account:accountPage}[page])()}</div></main></div><nav class='mobile-nav' aria-label='Навигация с телефона'>${nav(true)}</nav></div>`;
 if(page==='audience')renderPeople();lockAccountPicker();
}
function overview(){
 return title('ZAVOD 1.0','Всё под контролем',state.account?esc(state.account.name)+' · аккаунт '+accountSlot:'Подключите аккаунт, чтобы начать.',button('refresh','Обновить','light','','refresh'))+`<div class='metrics'><section class='metric'><span>Пользователей</span><strong>${number(state.stats.people)}</strong></section><section class='metric'><span>С историями</span><strong>${number(state.stats.active)}</strong></section><section class='metric'><span>Просмотров за 24 ч</span><strong>${number(state.stats.views_24h)}<small> / ${state.view_limit_24h}</small></strong></section></div><div class='quick-actions'>${button('go-sources','Чаты','light','','chat')}${button('go-audience','Пользователи','light','','people')}${button('go-account',state.account?'Настройки аккаунта':'Подключить аккаунт','primary','','account')}</div><p class='policy-note'>Автор с Premium — просмотр. Без Premium — просмотр и 👍. Если статус определить не удалось, только просмотр.</p><section class='card'><div class='section-top'><h2>Последние задания</h2><button type='button' class='text-btn' data-tab='jobs'>Все задания</button></div>${state.jobs.length?state.jobs.slice(0,3).map(jobCard).join(''):empty('jobs','Заданий пока нет','Добавьте чаты или пользователей и найдите активные истории.')}</section>`;
}
function sourceCard(s){
 const busy=['queued','running','waiting','paused'].includes(s.scan_status);
 return `<article class='source-card'><div class='source-icon'>${icon('chat')}</div><div class='source-info'><h3>${esc(s.title)}</h3><p>${esc(s.ref)}</p><div class='source-meta'><span>${s.mode==='authors'?'Авторы сообщений':'Участники группы'}</span><span>До ${number(s.max_users)} человек</span><span>Собрано: ${number(s.collected)}</span>${s.scan_status?`<span class='tag ${esc(s.scan_status)}'>${statuses[s.scan_status]||esc(s.scan_status)}</span>`:''}</div>${s.scan_message?`<p class='source-note'>${esc(s.scan_message)}</p>`:''}</div><div class='source-actions'>${button('scan-source',busy?'Сбор уже добавлен':'Собрать','primary small',`data-id='${esc(s.id)}' ${busy?'disabled':''}`,'search')}${button('source-people','Аудитория','light small',`data-id='${esc(s.id)}'`)}<button type='button' class='round-btn' data-action='delete-source' data-id='${esc(s.id)}' aria-label='Удалить чат'>${icon('trash')}</button></div></article>`;
}
function sourcesPage(){return title('ИСТОЧНИКИ АУДИТОРИИ','Ваши чаты','Добавьте группу вручную или загрузите до 200 чатов из файла.',`<div class='page-actions'>${button('import-chats','Загрузить файл','primary','','cloud')}${button('add-source','Добавить чат','light','','plus')}</div>`)+(state.sources.length?`<p class='list-count'>Чатов: ${number(state.sources.length)}</p>`+state.sources.map(sourceCard).join(''):`<section class='card'>${empty('chat','Добавьте чаты для сбора','Загрузите TXT/CSV со списком групп или укажите один чат вручную.',button('import-chats','Загрузить файл с чатами','light','','cloud'))}</section>`)+`<div class='notice'>${icon('info')}<span>Если список участников скрыт, можно собрать авторов доступных сообщений. Аккаунт должен иметь доступ к переписке. Приватность историй учитывается при проверке.</span></div>`;}
function audiencePage(){
 const pending=peopleImportStatus();
 return title('ЛЮДИ И ИХ ИСТОРИИ','Аудитория','Добавьте людей по юзернейму или выберите собранных из чатов.',`<div class='page-actions'>${button('go-sources','Чаты','light','','chat')}${button('import-people','Загрузить список','primary','','cloud')}${button('export','Скачать CSV','light','','download')}</div>`)+
 `<section class='card people-add-card'><h2>Добавить пользователя</h2><p>Укажите @username или t.me/username. Проверим пользователя и его доступные истории.</p><form id='people-add-form'><input type='hidden' name='request_id' value='${crypto.randomUUID()}'><div class='people-add-row'><label class='field'><span>Юзернейм пользователя</span><input name='username' placeholder='@username' required maxlength='200' autocomplete='off' autocapitalize='off' spellcheck='false' ${state.account?'':'disabled'}></label><button class='btn primary' type='submit' ${state.account?'':'disabled'}>${icon('plus')}Добавить</button></div><div id='people-add-error' class='inline-error' role='alert'></div></form>${state.account?`<p class='file-import-note'>Для нескольких пользователей нажмите «Загрузить список». Проверка идёт в очереди, просмотр историй запускается отдельно.</p>`:`<p class='file-import-note'>Сначала подключите Telegram-аккаунт во вкладке «Аккаунт».</p>`}</section>`+
 `<div id='people-import-status'>${pending}</div>`+
 `<div class='toolbar'><div class='search-box'>${icon('search')}<input id='people-search' aria-label='Поиск по аудитории' placeholder='Имя, username или ID' value='${esc(search)}' autocomplete='off'></div><select id='source-filter' class='filter-select' aria-label='Источник пользователей'><option value=''>Все источники</option><option value='manual' ${source==='manual'?'selected':''}>Добавленные по юзернейму</option>${state.sources.map(s=>`<option value='${esc(s.id)}' ${source===s.id?'selected':''}>${esc(s.title)}</option>`).join('')}</select><div class='segmented' aria-label='Фильтр историй'><button class='segment ${activeOnly?'active':''}' data-action='filter-active'>С историями</button><button class='segment ${!activeOnly?'active':''}' data-action='filter-all'>Все</button></div></div><div id='audience-data'></div><div id='selection'></div>`;
}
function importPeople(){showDialog(dialogTitle('Пользователи из файла')+`<p>TXT или CSV, до 200 уникальных пользователей и 512 КБ. В TXT укажите один юзернейм на строку. В CSV можно использовать столбец username.</p><pre class='chat-file-example'>@first_user\nsecond_user\nhttps://t.me/third_user</pre><form id='people-file-form'><fieldset><input type='hidden' name='request_id' value='${crypto.randomUUID()}'><label class='field'><span>Файл со списком юзернеймов</span><input type='file' name='file' accept='.txt,.csv,text/plain,text/csv' required></label><p class='file-import-note'>${state.account?'Проверим каждого пользователя и его доступные истории. Повторы объединятся в общей аудитории. Несуществующие имена, боты и чаты будут пропущены с пояснениями в задании.':'Для проверки пользователей сначала подключите Telegram-аккаунт во вкладке «Аккаунт».'} Просмотр историй запускается отдельно.</p><div id='people-file-error' class='inline-error' role='alert'></div></fieldset><div class='dialog-actions'>${button('close-dialog','Отмена','light')}<button class='btn primary' type='submit' ${state.account?'':'disabled'}>Добавить и проверить</button></div></form>`);}
function peopleImportResult(r){showDialog(dialogTitle('Пользователи в очереди')+`<div class='import-result' role='status'><p><strong>Юзернеймов на проверку: ${number(r.valid)}.</strong></p><p>Повторов в файле: ${number(r.duplicates)}. Неверных записей: ${number(r.invalid_count)}.</p><p>Пользователи появятся в аудитории по мере проверки. Ход работы, пауза и причины пропуска доступны в разделе «Задания».</p>${r.replayed?'<p>Этот запрос уже принят. Повторное задание не создано.</p>':''}</div>${r.invalid_count?`<details class='import-errors' open><summary>Неверные записи в файле</summary><ul>${r.invalid_rows.map(row=>`<li>Строка ${row.line}: <code>${esc(row.value||'пустое поле')}</code><br>${esc(row.message)}</li>`).join('')}</ul>${r.invalid_count>r.invalid_rows.length?'<p>Показаны первые 20 ошибок.</p>':''}</details>`:''}<div class='dialog-actions'>${button('close-dialog','К аудитории','light')}${button('go-jobs','Открыть задания','primary')}</div>`);}
async function refreshImportedPeople(){
 page='audience';source='';search='';activeOnly=false;offset=0;selected.clear();
 try{await sync(false);}catch{toast('Список принят. Данные обновятся после восстановления связи.');}
 render();
}
function peopleImportStatus(){const pending=state.jobs.filter(j=>j.kind==='people_import'&&['queued','running','waiting','paused'].includes(j.status));return pending.length?`<div class='notice'>${icon('clock')}<span>Проверка юзернеймов: ${number(pending.length)} списков. Пользователи появятся здесь по мере обработки.</span>${button('go-jobs','Открыть задания','light small')}</div>`:'';}
function renderPeople(){
 const status=document.querySelector('#people-import-status');if(status)status.innerHTML=peopleImportStatus();
 const el=document.querySelector('#audience-data');if(!el)return;
 el.innerHTML=people.length?`<div class='table-wrap'><table class='people-table'><thead><tr><th><input type='checkbox' id='select-page' aria-label='Выбрать доступных на странице' ${people.filter(p=>p.stories_count).every(p=>selected.has(p.user_id))?'checked':''}></th><th>Пользователь</th><th>Истории</th><th class='desktop-only'>Проверено</th><th>Просмотров</th></tr></thead><tbody>${people.map(p=>`<tr><td><input type='checkbox' data-person='${esc(p.user_id)}' aria-label='Выбрать ${esc(p.name)}' ${selected.has(p.user_id)?'checked':''} ${!p.stories_count?'disabled':''}></td><td><div class='person-name'><span class='avatar'>${esc(initials(p.name))}</span><div><strong>${esc(p.name)}</strong><small>${p.username?'@'+esc(p.username):'ID '+esc(p.user_id)}</small></div></div></td><td>${p.stories_count?`<span class='tag ready'>${p.stories_count} активн.</span>`:`<span class='muted'>Нет</span>`}</td><td class='desktop-only muted'>${esc(date(p.checked_at))}</td><td class='muted'>${p.viewed_count||0}</td></tr>`).join('')}</tbody></table></div><div class='pagination'><span>${offset+1}–${offset+people.length} из ${number(totalPeople)}</span><div>${button('prev-page','Назад','light small',offset===0?'disabled':'')}${button('next-page','Далее','light small',offset+people.length>=totalPeople?'disabled':'')}</div></div>`:`<section class='card'>${empty('people',search?'Никого не нашли':activeOnly?'Активные истории ещё не найдены':'Аудитория пока пуста',search?'Попробуйте другое имя или username.':'Добавьте пользователя по юзернейму, загрузите список или запустите сбор из чата.',!search?button('go-sources','Перейти к чатам','light','','chat'):'')}</section>`;
 renderSelection();
}
function renderSelection(){const el=document.querySelector('#selection');if(el)el.innerHTML=selected.size?`<div class='selection-bar'><span>Выбрано: <strong>${number(selected.size)}</strong></span><div>${button('clear-selection','Снять','small')}${button('confirm-view','Смотреть истории','lime small','','play')}</div></div>`:'';}
function jobCard(j){
 const sourceRow=state.sources.find(s=>s.id===j.source_id);
 const scanning=j.kind==='scan',importing=j.kind==='people_import';
 const progress=j.status==='done'?100:j.total?Math.min(100,Math.round((importing?j.processed:j.checked)/j.total*100)):0;
 return `<article class='job-card'><div class='job-head'><div class='job-title'><div class='source-icon'>${icon(importing?'people':scanning?'search':'eye')}</div><div><h3>${importing?'Добавление пользователей':scanning?'Сбор аудитории':'Просмотр историй'}</h3><small>${scanning?esc(sourceRow?.title||'Чат'):number(j.total)+' пользователей'} · ${esc(date(j.created_at))}</small></div></div><span class='tag ${esc(j.status)}'>${statuses[j.status]||esc(j.status)}</span></div><div class='progress-track'><div class='progress-fill' style='width:${progress}%'></div></div><div class='job-summary'><span>${importing?'Обработано '+number(j.processed)+' из '+number(j.total):scanning&&j.phase==='gather'?'Собрано '+number(j.collected)+' человек':'Проверено '+number(j.checked)+' из '+number(j.total)}</span><span>${scanning||importing?'С историями: '+number(j.found):'Просмотров: '+number(j.viewed)+' · Реакций: '+number(j.reacted)}</span></div>${importing?`<div class='batch-counts'><span>Новых: ${number(j.added)}</span><span>В аудитории: ${number(j.collected)}</span><span>Повторов: ${number(j.duplicate_users)}</span><span>Пропущено: ${number(j.rejected)}</span></div>`:''}<p class='job-note'>${esc(j.message||'Ожидает выполнения.')}${j.status==='waiting'&&j.next_run>Date.now()/1000?' До '+esc(date(j.next_run))+'.':''}</p>${importing&&j.import_errors?.length?`<details class='import-errors'><summary>Причины пропуска (${number(j.rejected)})</summary><ul>${j.import_errors.map(row=>`<li><code>${esc(row.ref)}</code>: ${esc(row.message)}</li>`).join('')}</ul>${j.rejected>j.import_errors.length?'<p>Показаны первые 20 ошибок.</p>':''}</details>`:''}${['queued','running','waiting','paused','failed'].includes(j.status)?`<div class='job-controls'>${['paused','failed'].includes(j.status)?button('job-resume','Продолжить','light small',`data-id='${esc(j.id)}'`,'play'):button('job-pause','Пауза','light small',`data-id='${esc(j.id)}'`,'pause')}${button('job-cancel','Остановить','light small',`data-id='${esc(j.id)}'`,'stop')}</div>`:''}</article>`;
}
function eventsView(max=20){return state.events.length?state.events.slice(0,max).map(e=>`<div class='event'><i class='event-mark ${esc(e.level)}'></i><div><p>${esc(e.message)}</p><time>${esc(date(e.created_at))}</time></div></div>`).join(''):empty('clock','Ничего не пропущено','Подключения, результаты и паузы Telegram будут записаны здесь.');}
function importBatchCard(b){
 const active=(b.queued||0)+(b.running||0)+(b.waiting||0),resumable=(b.paused||0)+(b.failed||0),finished=(b.done||0)+(b.failed||0)+(b.cancelled||0);
 return `<section class='card import-batch'><div class='section-top'><h2>Сбор из файла · ${number(b.total)} чатов</h2><small>${esc(date(b.created_at))}</small></div><div class='progress-track'><div class='progress-fill' style='width:${b.total?Math.round(finished/b.total*100):0}%'></div></div><div class='batch-counts'><span>Готово: ${number(b.done)}</span><span>В очереди: ${number(b.queued)}</span><span>В работе: ${number(b.running)}</span><span>Ожидание: ${number(b.waiting)}</span><span>Пауза: ${number(b.paused)}</span><span>Ошибки: ${number(b.failed)}</span></div>${active||resumable?`<div class='job-controls'>${active?button('import-pause','Пауза всего импорта','light small',`data-id='${esc(b.id)}'`,'pause'):''}${resumable?button('import-resume','Продолжить импорт','light small',`data-id='${esc(b.id)}'`,'play'):''}${button('import-cancel','Остановить импорт','light small',`data-id='${esc(b.id)}'`,'stop')}</div>`:''}</section>`;
}
function jobsPage(){const counts=state.job_counts||{};return title('ФОНОВАЯ РАБОТА','Задания',state.free_mode?'После сна сервера очередь восстановится. Пока сервер активен, чаты обрабатываются через общую очередь.':'После запуска можно закрыть страницу — сервер продолжит работу.',button('refresh','Обновить','light','','refresh'))+`<div class='queue-summary'>В очереди: ${number(counts.queued)} · В работе: ${number(counts.running)} · Готово: ${number(counts.done)} · Ошибки: ${number(counts.failed)}</div>${(state.source_imports||[]).map(importBatchCard).join('')}<div class='bottom-grid'><section>${state.jobs.length?state.jobs.slice(0,jobsShown).map(jobCard).join('')+`<div class='pagination'><span>Показано ${Math.min(jobsShown,state.jobs.length)} из ${state.jobs.length}</span>${jobsShown<state.jobs.length?button('more-jobs','Показать ещё','light'):''}</div>`:`<div class='card'>${empty('jobs','Очередь пуста','Начните со сбора аудитории из одного чата.',button('go-sources','Выбрать чат','light','','chat'))}</div>`}</section><section class='card'><div class='section-top'><h2>Журнал</h2><small>Последние события</small></div>${eventsView()}</section></div>`;}
function profileAvatar(){return state.account?.has_avatar?`<img src='/api/account/profile/photo?account=${accountSlot}&v=${esc(state.account.photo_revision)}' alt='Текущая аватарка'>`:esc(initials(state.account?.name));}
function profileForms(){
 const a=state.account,disabled=profileLoading||profileBusy||!a.profile_loaded;
 return `<div class='profile-forms'>
 <form id='profile-name-form' class='profile-form'><fieldset ${disabled?'disabled':''}><h3>Имя и фамилия</h3><div class='field-row'><label class='field'><span>Имя</span><input name='first_name' value='${esc(a.first_name)}' maxlength='64' required autocomplete='given-name'></label><label class='field'><span>Фамилия</span><input name='last_name' value='${esc(a.last_name)}' maxlength='64' autocomplete='family-name'><small>Можно оставить пустой.</small></label></div><div id='profile-name-error' class='inline-error' role='alert'></div><button type='submit' class='btn primary'>Сохранить имя</button><span class='profile-saved' role='status'></span></fieldset></form>
 <form id='profile-username-form' class='profile-form'><fieldset ${disabled?'disabled':''}><h3>Username</h3><label class='field'><span>Имя пользователя</span><input name='username' value='${esc(a.username)}' maxlength='33' placeholder='@username' autocapitalize='off' autocomplete='off' spellcheck='false'><small>5–32 латинские буквы, цифры или _. Пустое поле удалит username.</small></label><div id='profile-username-error' class='inline-error' role='alert'></div><button type='submit' class='btn primary'>Сохранить username</button><span class='profile-saved' role='status'></span></fieldset></form>
 <form id='profile-about-form' class='profile-form'><fieldset ${disabled?'disabled':''}><h3>Описание аккаунта</h3><label class='field'><span>Био</span><textarea name='about' maxlength='${a.is_premium?140:70}' rows='3' placeholder='Описание профиля'>${esc(a.about)}</textarea><small>До ${a.is_premium?140:70} символов. Пустое поле удалит описание.</small></label><div id='profile-about-error' class='inline-error' role='alert'></div><button type='submit' class='btn primary'>Сохранить описание</button><span class='profile-saved' role='status'></span></fieldset></form>
 <form id='profile-channel-form' class='profile-form'><fieldset ${disabled?'disabled':''}><h3>Канал в профиле</h3><p>${a.personal_channel?'Сейчас: '+esc(a.personal_channel.title||a.personal_channel.username):'Канал не закреплён'}</p><label class='field'><span>Свой публичный канал</span><input name='ref' value='${esc(a.personal_channel?.username?'@'+a.personal_channel.username:'')}' placeholder='@my_channel' autocapitalize='off' autocomplete='off' spellcheck='false'><small>Укажите канал, которым владеет этот аккаунт. Пустое поле открепит его.</small></label><div id='profile-channel-error' class='inline-error' role='alert'></div><button type='submit' class='btn primary'>Сохранить канал</button><span class='profile-saved' role='status'></span></fieldset></form>
 <form id='profile-photo-form'  class='profile-form'><fieldset ${disabled?'disabled':''}><h3>Аватарка</h3><div class='avatar-upload'><span id='avatar-preview' class='avatar'>${profileAvatar()}</span><label class='field'><span>Выберите фото из галереи</span><input name='file' type='file' accept='image/jpeg,image/png,image/webp' required><small>JPG, PNG или WebP до 5 МБ, от 160 × 160 пикселей. Сохраняется квадрат по центру фотографии.</small></label></div><div id='profile-photo-error' class='inline-error' role='alert'></div><button type='submit' class='btn primary'>Сохранить аватарку</button><span class='profile-saved' role='status'></span></fieldset></form></div>`;
}
function showError(el,err){
 if(!el||err.code==='ACCOUNT_CHANGED')return;
 el.replaceChildren();
 const message=document.createElement('strong');message.textContent=err.message;el.append(message);
 if(err.stageLabel){const stage=document.createElement('p');stage.textContent='Этап: '+err.stageLabel;el.append(stage);}
 if(err.hint){const hint=document.createElement('p');hint.textContent=err.hint;el.append(hint);}
 if(err.freezeUntil){const deadline=document.createElement('p');deadline.textContent='Telegram указал срок до возможного удаления аккаунта: '+new Intl.DateTimeFormat('ru-RU',{dateStyle:'medium',timeStyle:'short',timeZone:'Europe/Moscow'}).format(new Date(err.freezeUntil*1000))+' (МСК). Это не дата разморозки.';el.append(deadline);}
 if(err.appealUrl){try{const url=new URL(err.appealUrl);if(url.protocol==='https:'&&['telegram.org','www.telegram.org','t.me'].includes(url.hostname)){const link=document.createElement('a');link.href=url.href;link.textContent='Открыть апелляцию Telegram';link.target='_blank';link.rel='noopener noreferrer';el.append(link);}}catch{}}
 if(err.code){const code=document.createElement('small');code.textContent='Код: '+err.code;el.append(code);}
 if(err.diagnosticId){const diagnostic=document.createElement('small');diagnostic.textContent='Диагностика: '+err.diagnosticId;el.append(diagnostic);}
}
function updateProfileHeader(){
 const a=state.account;if(!a)return;
 const name=document.getElementById('profile-current-name'),username=document.getElementById('profile-current-username');
 if(name)name.textContent=a.name;if(username)username.textContent=a.username?'@'+a.username:'ID '+a.user_id;
 document.querySelectorAll('[data-profile-avatar]').forEach(el=>el.innerHTML=profileAvatar());
 const mini=document.querySelector('.account-mini strong');if(mini)mini.textContent=a.name;
 const miniAvatar=document.querySelector('.account-mini .avatar');if(miniAvatar)miniAvatar.innerHTML=profileAvatar();
}
function lockProfileForms(value){document.querySelectorAll('.profile-form fieldset').forEach(el=>el.disabled=value);const refresh=document.querySelector('[data-action="refresh-profile"]');if(refresh)refresh.disabled=value;document.querySelectorAll('#channel-create-form fieldset,.channel-username-form fieldset').forEach(el=>el.disabled=value);lockAccountPicker();}
async function refreshProfile(){
 if(!state.account||profileBusy||profileLoading)return;
 const epoch=accountEpoch;profileLoading=true;lockProfileForms(true);
 const loading=document.getElementById('profile-loading');if(loading)loading.textContent='Проверяем профиль…';
 const error=document.getElementById('profile-error');if(error)error.textContent='';
 try{const d=await api('/api/account/profile');state.account=d.account;if(page==='account'){render();}}
 catch(err){if(page==='account')showError(document.getElementById('profile-error'),err);else toast(err.message);}
 finally{if(epoch===accountEpoch){profileLoading=false;if(page==='account'){lockProfileForms(!state.account?.profile_loaded);const label=document.getElementById('profile-loading');if(label)label.textContent='';}}}
}
function accountCards(){
 const accounts=connectedAccounts();return "<div class='account-cards'>"+accounts.map(a=>"<button type='button' class='account-card "+(Number(a.slot)===accountSlot?'active':'')+"' data-action='select-account' data-slot='"+Number(a.slot)+"' "+(accountWrites||profileBusy?'disabled':'')+"><span class='avatar'>"+esc(initials(a.name))+"</span><span><strong>"+esc(a.name)+"</strong><small>Аккаунт "+Number(a.slot)+(a.username?' · @'+esc(a.username):'')+"</small></span>"+(Number(a.slot)===accountSlot?icon('check'):'')+"</button>").join('')+"</div>";
}
function channelForms(){
 const disabled=profileLoading||profileBusy||!state.account?.profile_loaded;
 return "<section class='channel-section'><h2>Каналы</h2><p class='form-intro'>Создайте публичный канал и при желании закрепите его в профиле выбранного аккаунта.</p><form id='channel-create-form'><fieldset "+(disabled?'disabled':'')+"><input type='hidden' name='request_id' value='"+crypto.randomUUID()+"'><label class='field'><span>Название канала</span><input name='title' maxlength='128' required placeholder='Мой канал'></label><label class='field'><span>Публичный username</span><input name='username' minlength='5' maxlength='33' required placeholder='@my_channel' autocapitalize='off' autocomplete='off' spellcheck='false'><small>От 5 до 32 латинских букв, цифр или _. Username должен быть свободен.</small></label><label class='field'><span>Описание канала</span><textarea name='about' maxlength='255' rows='3'></textarea></label><label class='check-field'><input name='pin' type='checkbox' checked><span>Закрепить в профиле аккаунта "+accountSlot+"</span></label><div id='channel-create-error' class='inline-error' role='alert'></div><button class='btn primary' type='submit'>Создать канал</button></fieldset></form>"+(state.channels||[]).map(c=>"<article class='owned-channel'><h3>"+esc(c.title)+"</h3><p>"+(c.username?'@'+esc(c.username):'Публичный username не установлен')+"</p><form class='channel-username-form' data-channel='"+esc(c.id)+"'><fieldset "+(disabled?'disabled':'')+"><label class='field'><span>Username канала</span><input name='username' value='"+esc(c.username||'')+"' minlength='5' maxlength='33' required autocapitalize='off' autocomplete='off'></label><div class='inline-error' role='alert'></div><button type='submit' class='btn light small'>Сохранить username</button>"+button('pin-created-channel','Закрепить в профиле','light small',"data-id='"+esc(c.id)+"' "+(!c.username?'disabled':''))+"</fieldset></form></article>").join('')+"</section>";
}
function channelResult(result){
 const c=result.channel||{};showDialog(dialogTitle('Канал создан')+"<div class='import-result' role='status'><p><strong>"+esc(c.title)+"</strong></p>"+(c.username?"<p>@"+esc(c.username)+"</p>":'')+"<p>"+(result.pinned?'Закреплён в профиле.':'Канал не закреплён в профиле.')+"</p>"+(result.warning?"<p class='warning'>"+esc(result.warning)+"</p>":'')+(result.replayed?"<p>Повторный канал не создан.</p>":'')+"</div><div class='dialog-actions'>"+button('close-dialog','Понятно','primary')+"</div>");
}
function accountPage(){
 let body;
 if(state.account){body=`<h2>Аккаунт ${accountSlot} · Профиль Telegram</h2><p class='form-intro'>Изменения сохраняются в вашем аккаунте Telegram.</p><div class='connected-account'><span class='avatar' data-profile-avatar>${profileAvatar()}</span><div><h3 id='profile-current-name'>${esc(state.account.name)}</h3><p id='profile-current-username'>${state.account.username?'@'+esc(state.account.username):'ID '+esc(state.account.user_id)}</p></div></div><div class='profile-toolbar'>${button('refresh-profile','Обновить из Telegram','light','','refresh')}<span id='profile-loading' role='status'>${profileLoading?'Проверяем профиль…':''}</span></div><div id='profile-error' class='inline-error' role='alert'></div>${profileForms()}${channelForms()}<div class='notice'>${icon('lock')}<span>Создана отдельная сессия ZAVOD 1.0. Её можно завершить здесь или в разделе «Устройства» в Telegram.</span></div><p class='form-intro'>Подключён ${esc(date(state.account.created_at))}. Архив tdata после импорта удалён.</p>${button('confirm-disconnect','Отключить аккаунт','danger','','logout')}`;}
 else body=`<h2>Аккаунт ${accountSlot} · Подключение через tdata</h2><p class='form-intro'>Загрузите ZIP с папкой tdata. Из архива будет выбран активный аккаунт Telegram Desktop.</p>${!server?`<div class='notice warning'>${icon('cloud')}<span>Импорт откроется после запуска сервера. В предпросмотре файлы не загружаются и данные аккаунта не запрашиваются.</span></div>`:''}<form id='tdata-form'><fieldset ${!server?'disabled':''}><label class='field'><span>Архив tdata</span><input name='file' type='file' accept='.zip,application/zip' required><small>ZIP до 20 МБ. Файлы авторизации без кэша и медиа.</small></label>${!state.telegram_api_configured?`<div class='field-row'><label class='field'><span>API ID</span><input name='api_id' type='text' inputmode='numeric' pattern='[0-9]+' placeholder='12345678' required autocomplete='off'></label><label class='field'><span>API Hash</span><input name='api_hash' type='password' placeholder='32 символа' minlength='32' maxlength='32' required autocomplete='off'></label></div><p class='links-inline'>Параметры своего приложения: <a href='https://my.telegram.org/apps' target='_blank' rel='noopener noreferrer'>my.telegram.org</a>. Их достаточно ввести при подключении.</p>`:''}${state.telegram_api_mode==='public'?`<div class='notice'>${icon('info')}<span>Для пробного входа подключены опубликованные параметры Telegram Desktop. Можно загрузить tdata без заполнения API ID и API Hash.</span></div>`:''}${state.telegram_api_configured?`<details><summary>Свои API ID и API Hash — необязательно</summary><div class='field-row'><label class='field'><span>API ID</span><input name='api_id' type='text' inputmode='numeric' pattern='[0-9]+' placeholder='Оставить по умолчанию' autocomplete='off'></label><label class='field'><span>API Hash</span><input name='api_hash' type='password' placeholder='Оставить по умолчанию' minlength='32' maxlength='32' autocomplete='off'></label></div><p class='links-inline'>Если Telegram отклонит публичные параметры, укажите оба своих параметра с <a href='https://my.telegram.org/apps' target='_blank' rel='noopener noreferrer'>my.telegram.org</a>.</p></details>`:''}<details><summary>Пароли, если они установлены</summary><label class='field'><span>Код-пароль Telegram Desktop</span><input name='passcode' type='password' autocomplete='off' maxlength='512' placeholder='Если tdata зашифрована'><small>Локальный код-пароль исходного Telegram Desktop.</small></label><label class='field'><span>Пароль двухэтапной проверки</span><input name='password' type='password' autocomplete='off' maxlength='512' placeholder='Если включена в Telegram'><small>Нужен Telegram для создания отдельной облачной сессии.</small></label></details><div id='import-error' class='inline-error' role='alert'></div><div class='form-bottom'><small>Пароли и исходный архив не сохраняются после импорта.</small><button type='submit' class='btn primary'>${icon('lock')}Подключить аккаунт</button></div></fieldset></form>`;
 return title('УПРАВЛЕНИЕ TELEGRAM','Аккаунты',`${connectedAccounts().length} из 5 подключено. Сейчас выбран аккаунт ${accountSlot}.`,button('add-account','Добавить аккаунт','primary',`${connectedAccounts().length>=5?'disabled':''}`,'plus'))+accountCards()+`<div class='account-grid'><section class='card'>${body}</section><aside class='card'><h2>Перед подключением</h2><div class='help-item'><h3>tdata — формат Desktop</h3><p>Нужен уже имеющийся архив вашего Telegram Desktop. Сам мобильный Telegram такой архив не создаёт.</p></div><div class='help-item'><h3>Доступ к аккаунту</h3><p>tdata содержит ключи входа. Загружайте только свой архив в свою панель. Подключение доступно после входа по паролю панели.</p></div><div class='help-item'><h3>Постоянное хранение</h3><p>Сессия хранится на сервере в зашифрованном виде. При отключении данные сессии и собранная аудитория удаляются из приложения.</p></div><div class='help-item'><h3>Ограничения Telegram</h3><p>Запросы ожидания соблюдаются автоматически. Внутренние лимиты приложения не гарантируют отсутствие ограничений аккаунта.</p></div></aside></div>${server?`<div class='footer-logout'>${state.auth_mode==='chatgpt'?`<a class='btn light' href='/signout-with-chatgpt?return_to=%2F' target='_top'>${icon('logout')}Выйти из панели</a>`:button('logout','Выйти из панели','light','','logout')}</div>`:''}`;
}
function showDialog(content,login=false){dialog.innerHTML=`<div class='dialog-inner'>${content}</div>`;dialog.dataset.login=login?'true':'false';if(!dialog.open)dialog.showModal();}
function dialogTitle(t){return `<div class='dialog-title'><h2>${t}</h2><button type='button' class='round-btn' data-action='close-dialog' aria-label='Закрыть'>${icon('close')}</button></div>`;}
function showLogin(){showDialog(`<div class='dialog-title'><h2>Вход в личную панель</h2></div><p>Введите пароль, заданный при запуске сервера.</p><form id='login-form'><label class='field'><span>Пароль панели</span><input name='password' type='password' autocomplete='current-password' required autofocus></label><div id='login-error' class='inline-error' role='alert'></div><button class='btn primary' type='submit'>Войти в панель</button></form>`,true);}
function addSource(){showDialog(dialogTitle('Добавить чат')+`<p>Укажите группу, к которой есть доступ у подключённого аккаунта.</p><form id='source-form'><label class='field'><span>Ссылка или username группы</span><input name='ref' placeholder='t.me/название_группы' required maxlength='200' autocomplete='off' autocapitalize='off'></label><label class='mode-choice'><input type='radio' name='mode' value='members' checked><span><strong>Участники группы</strong><small>Люди из доступного аккаунту списка участников.</small></span></label><label class='mode-choice'><input type='radio' name='mode' value='authors'><span><strong>Авторы сообщений</strong><small>Авторы из доступной переписки, до 5 000 последних сообщений.</small></span></label><label class='field' style='margin-top:19px'><span>Сколько людей собрать</span><input name='max_users' type='number' inputmode='numeric' min='1' max='5000' value='300' required><small>Начните с небольшой аудитории. Проверка историй запускается в этом же задании.</small></label><div id='source-error' class='inline-error' role='alert'></div><div class='dialog-actions'>${button('close-dialog','Отмена','light')}<button class='btn primary' type='submit'>Добавить чат</button></div></form>`);}
function importChats(){showDialog(dialogTitle('Чаты из файла')+`<p>TXT или CSV, до 200 уникальных чатов и 512 КБ. В TXT укажите один чат на строку. В CSV можно использовать столбец «Ссылка», username или chat_id.</p><pre class='chat-file-example'>https://t.me/my_group_01\n@my_group_02\n-1001234567890</pre><form id='chat-file-form'><fieldset><input type='hidden' name='request_id' value='${crypto.randomUUID()}'><label class='field'><span>Файл со списком чатов</span><input type='file' name='file' accept='.txt,.csv,text/plain,text/csv' required></label><label class='field'><span>Кого собирать в каждом чате</span><select name='mode'><option value='members'>Участники группы</option><option value='authors'>Авторы сообщений</option></select></label><label class='field'><span>Лимит людей на каждый чат</span><input type='number' name='max_users' value='300' inputmode='numeric' min='1' max='5000' required></label><label class='mode-choice'><input type='checkbox' name='start_scans' ${state.account?'checked':'disabled'}><span><strong>Запустить сбор после добавления</strong><small>${state.account?'Для каждого доступного чата создаётся отдельное задание.':'Для запуска сначала подключите Telegram-аккаунт. Чаты можно добавить заранее.'}</small></span></label><p class='file-import-note'>Повторы не создают новые чаты. Для чата с незавершённым сбором второе задание не запускается. Ошибки отдельных строк покажем в отчёте; доступность групп проверяется при сборе. Настройки применяются к чатам без действующего сбора.</p><div id='chat-file-error' class='inline-error' role='alert'></div><div class='dialog-actions'>${button('close-dialog','Отмена','light')}<button class='btn primary' type='submit'>${state.account?'Добавить и запустить сбор':'Добавить чаты'}</button></div></fieldset></form>`);}
function importResult(r){showDialog(dialogTitle('Файл обработан')+`<div class='import-result' role='status'><p><strong>Новых чатов: ${number(r.added)}.</strong> Уже были в панели: ${number(r.existing)}.</p><p>Поставлено заданий: <strong>${number(r.queued)}</strong>. Уже есть незавершённый сбор: ${number(r.busy)}.</p><p>Повторов в файле: ${number(r.duplicates)}. Неверных записей: ${number(r.invalid_count)}.</p>${r.replayed?'<p>Этот запрос уже был выполнен. Повторные задания не созданы.</p>':''}</div>${r.invalid_count?`<details class='import-errors' open><summary>Пропущенные записи</summary><ul>${r.invalid_rows.map(row=>`<li>Строка ${row.line}: <code>${esc(row.value||'пустое поле')}</code><br>${esc(row.message)}</li>`).join('')}</ul>${r.invalid_count>r.invalid_rows.length?`<p>Показаны первые ${r.invalid_rows.length} ошибок.</p>`:''}</details>`:''}<div class='dialog-actions'>${button('close-dialog','Закрыть','light')}${button(r.queued?'go-jobs':'go-sources',r.queued?'Открыть очередь':'Открыть чаты','primary')}</div>`);}
function viewDialog(){if(!selected.size)return;showDialog(dialogTitle('Смотреть выбранные истории')+`<p>Выбрано пользователей: <strong>${number(selected.size)}</strong>. Перед просмотром доступность историй будет проверена повторно. Уже просмотренные истории пропускаются. Если у автора Premium, только просмотр; без Premium — просмотр и 👍. Неизвестный статус — без реакции.</p><form id='view-form'><label class='field'><span>Интервал между пользователями, сек.</span><input name='delay' type='number' inputmode='numeric' min='5' max='300' value='15' required><small>Внутренний предел приложения — ${state.view_limit_24h} историй за 24 часа. Запросы ожидания Telegram имеют приоритет.</small></label><div class='inline-error' id='view-error' role='alert'></div><div class='dialog-actions'>${button('close-dialog','Отмена','light')}<button class='btn primary' type='submit'>${icon('play')}Запустить</button></div></form>`);}
async function sync(renderAfter=true){if(!server||!authenticated)return;state=await api('/api/state');if(page==='audience')await loadPeople();else if(renderAfter)render();}
async function loadPeople(){if(!server||!authenticated){people=[];totalPeople=0;renderPeople();return;}const request=++peopleRequest;const q=new URLSearchParams({search,source,active:String(activeOnly),offset:String(offset)});const d=await api('/api/people?'+q);if(request!==peopleRequest)return;people=d.people;totalPeople=d.total;renderPeople();}
async function go(next){if(avatarPreview){URL.revokeObjectURL(avatarPreview);avatarPreview='';}page=next;if(next==='audience'){render();await loadPeople();}else render();window.scrollTo({top:0,behavior:'instant'});if(next==='account'&&state.account)await refreshProfile();}
dialog.addEventListener('cancel',e=>{if(dialog.dataset.login==='true')e.preventDefault();});
document.addEventListener('click',async e=>{
 const target=e.target.closest('[data-action],[data-tab]');if(!target)return;
 e.preventDefault();if(target.disabled)return;
 if(target.dataset.tab){try{await go(target.dataset.tab);}catch(err){toast(err.message);}return;}
 const action=target.dataset.action,id=target.dataset.id;
 try{
  if(action==='select-account'){await selectAccount(target.dataset.slot||id);return;}
  if(action==='add-account'){if(accountWrites||profileBusy){toast('Дождитесь завершения сохранения.');return;}const occupied=new Set(connectedAccounts().map(a=>Number(a.slot)));const free=[1,2,3,4,5].find(slot=>!occupied.has(slot));if(!free){toast('Можно подключить до пяти аккаунтов.');return;}page='account';await selectAccount(free);return;}
  if(action==='close-dialog'){if(dialog.dataset.login!=='true')dialog.close();return;}
  if(action==='retry-boot'){if(Date.now()<bootRetryAt){scheduleBoot();return;}await boot();return;}
  if(action==='wake-server'){if(backendWakeUrl)window.location.replace(backendWakeUrl.href);return;}
  if(action==='add-source'){addSource();return;}
  if(action==='import-chats'){if(online())importChats();return;}
  if(action==='import-people'){if(online())importPeople();return;}
  if(action.startsWith('go-')){if(dialog.open&&dialog.dataset.login!=='true')dialog.close();await go(action.slice(3));return;}
  if(action==='help'||action==='launch-info'){showDialog(dialogTitle(server?'Как это работает':'Запуск облачной панели')+(server?`<ol class='help-list'><li>Подключите свой аккаунт через ZIP с tdata.</li><li>Добавьте чат и нажмите «Собрать».</li><li>Во вкладке «Люди» выберите найденных людей с историями.</li><li>Запустите просмотр и проверяйте задания. Страницу можно закрыть.</li></ol>`:`<p>Интерфейс уже доступен с телефона. Для подключения tdata и работы заданий нужен постоянно работающий сервер с сохранением данных.</p><div class='notice'>${icon('cloud')}<span>Сервер ещё не подключён. Поэтому импорт и запуск заданий сейчас недоступны. После размещения сервер выдаст отдельную ссылку на рабочую панель.</span></div>`)+`<div class='dialog-actions'>${button('close-dialog','Понятно','primary')}</div>`);return;}
  if(action==='clear-selection'){selected.clear();renderPeople();return;}
  if(action==='confirm-view'){if(online())viewDialog();return;}
  if(action==='filter-active'||action==='filter-all'){activeOnly=action==='filter-active';offset=0;selected.clear();render();await loadPeople();return;}
  if(action==='source-people'){source=id;offset=0;selected.clear();await go('audience');return;}
  if(!online())return;
  target.disabled=true;
  if(action==='scan-source'){await api(`/api/sources/${id}/scan`,{method:'POST'});toast('Сбор поставлен в очередь. Результаты появятся в аудитории.');await go('jobs');await sync();}
  if(action==='delete-source'){showDialog(dialogTitle('Удалить чат?')+`<p>Чат будет удалён из источников. История завершённых заданий сохранится.</p><div class='dialog-actions'>${button('close-dialog','Отмена','light')}${button('delete-source-confirm','Удалить','danger',`data-id='${esc(id)}'`)}</div>`);}
  if(action==='delete-source-confirm'){await api(`/api/sources/${id}`,{method:'DELETE'});dialog.close();await sync();toast('Чат удалён.');}
  if(action.startsWith('job-')){await api(`/api/jobs/${id}/control`,{method:'POST',body:{action:action.slice(4)}});await sync();}
  if(['import-pause','import-resume','import-cancel'].includes(action)){await api(`/api/source-imports/${id}/control`,{method:'POST',body:{action:action.slice(7)}});await sync();}
  if(action==='more-jobs'){jobsShown+=40;render();}
  if(action==='refresh'){await sync();toast('Данные обновлены.');}
  if(action==='refresh-profile'){await refreshProfile();}
  if(action==='pin-created-channel'){await api('/api/account/profile/channel',{method:'POST',body:{ref:id}});await sync();toast('Канал закреплён в профиле.');}
  if(action==='prev-page'||action==='next-page'){offset=Math.max(0,offset+(action==='next-page'?100:-100));selected.clear();await loadPeople();}
  if(action==='export'){const exportSlot=accountSlot,exportEpoch=accountEpoch;const q=new URLSearchParams({search,source,active:String(activeOnly)});const r=await fetch('/api/people/export?'+q,{credentials:'same-origin',headers:{'X-Story-Account':String(exportSlot)}});if(exportEpoch!==accountEpoch)throw scopeChanged();if(!r.ok)throw new Error('Не удалось скачать аудиторию. Обновите страницу.');const blob=await r.blob();if(exportEpoch!==accountEpoch)throw scopeChanged();const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download='zavod-account-'+exportSlot+'-audience.csv';link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);}
  if(action==='confirm-disconnect'){showDialog(dialogTitle('Отключить аккаунт?')+`<p>Все его задания будут остановлены. Подключение, сессия и собранная аудитория будут удалены из приложения.</p><div class='dialog-actions'>${button('close-dialog','Отмена','light')}${button('disconnect','Отключить','danger')}</div>`);}
  if(action==='disconnect'){target.textContent='Отключаем…';const result=await api('/api/account',{method:'DELETE'});selected.clear();dialog.close();await sync();toast(result.message);}
  if(action==='logout'){await api('/api/auth/logout',{method:'POST'});authenticated=false;csrf='';state={account:null,sources:[],jobs:[],events:[],stats:{people:0,active:0,views_24h:0},view_limit_24h:200};page='overview';render();showLogin();}
 }catch(err){toast(err.message);}finally{if(target.isConnected)target.disabled=false;}
});
document.addEventListener('change',async e=>{
 try{
 if(e.target.id==='account-picker'){await selectAccount(e.target.value);return;}
 if(e.target.closest('#people-file-form'))e.target.closest('form').elements.request_id.value=crypto.randomUUID();
 if(e.target.closest('#chat-file-form')){const form=e.target.closest('form');form.elements.request_id.value=crypto.randomUUID();form.querySelector('[type=submit]').textContent=form.elements.start_scans.checked?'Добавить и запустить сбор':'Добавить чаты';}
 if(e.target.matches('#profile-photo-form input[type=file]')){const file=e.target.files[0];const error=document.getElementById('profile-photo-error');error.textContent='';if(avatarPreview){URL.revokeObjectURL(avatarPreview);avatarPreview='';}const preview=document.getElementById('avatar-preview');preview.innerHTML=profileAvatar();if(file){if(file.size>5*1024*1024){e.target.value='';throw new Error('Выберите фото размером до 5 МБ.');}avatarPreview=URL.createObjectURL(file);const image=document.createElement('img');image.src=avatarPreview;image.alt='Предпросмотр новой аватарки';preview.replaceChildren(image);}}
 if(e.target.dataset.person){e.target.checked?selected.add(e.target.dataset.person):selected.delete(e.target.dataset.person);renderSelection();}
 if(e.target.id==='select-page'){for(const p of people.filter(p=>p.stories_count)){e.target.checked?selected.add(p.user_id):selected.delete(p.user_id);}renderPeople();}
 if(e.target.id==='source-filter'){source=e.target.value;offset=0;selected.clear();await loadPeople();}
 }catch(err){toast(err.message);}
});
document.addEventListener('input',e=>{if(e.target.closest('#people-add-form'))e.target.closest('form').elements.request_id.value=crypto.randomUUID();if(e.target.id==='people-search'){search=e.target.value;offset=0;clearTimeout(searchTimer);searchTimer=setTimeout(()=>loadPeople().catch(err=>toast(err.message)),300);}if(e.target.closest('.profile-form')){const saved=e.target.closest('form').querySelector('.profile-saved');if(saved)saved.textContent='';}});
document.addEventListener('submit',async e=>{
 const form=e.target;if(!form.matches('.channel-username-form'))return;e.preventDefault();
 if(!online()||profileBusy||profileLoading)return;
 const data=new FormData(form),error=form.querySelector('.inline-error');profileBusy=true;lockProfileForms(true);error.textContent='';
 try{await api('/api/account/channels/'+encodeURIComponent(form.dataset.channel)+'/username',{method:'POST',body:{username:data.get('username')}});await sync(false);render();toast('Username канала сохранён.');}
 catch(err){if(error.isConnected)showError(error,err);else toast(err.message);}
 finally{profileBusy=false;if(page==='account')lockProfileForms(!state.account?.profile_loaded);}
});
document.addEventListener('submit',async e=>{
 const form=e.target;if(!form.matches('.profile-form'))return;e.preventDefault();
 if(!online()||profileBusy||profileLoading)return;
 const data=new FormData(form),kind=form.id.split('-')[1],submit=form.querySelector('[type=submit]'),original=submit.textContent,error=document.getElementById(`profile-${kind}-error`);
 error.textContent='';form.querySelector('.profile-saved').textContent='';
 profileBusy=true;lockProfileForms(true);submit.textContent='Сохраняем в Telegram…';
 try{
  let body,path;
  if(kind==='name'){body={first_name:data.get('first_name'),last_name:data.get('last_name')};path='name';}
  if(kind==='username'){body={username:data.get('username')};path='username';}
  if(kind==='about'){body={about:data.get('about')};path='about';}
  if(kind==='channel'){body={ref:data.get('ref')};path='channel';}
  if(kind==='photo'){const file=data.get('file');if(!file||!file.size||file.size>5*1024*1024)throw new Error('Выберите фото размером до 5 МБ.');body=data;path='photo';}
  const result=await api('/api/account/profile/'+path,{method:'POST',body});state.account=result.account;
  if(form.isConnected){updateProfileHeader();if(kind==='name'){form.elements.first_name.value=state.account.first_name;form.elements.last_name.value=state.account.last_name;}if(kind==='username')form.elements.username.value=state.account.username||'';if(kind==='about')form.elements.about.value=state.account.about||'';if(kind==='channel')form.elements.ref.value=state.account.personal_channel?.username?'@'+state.account.personal_channel.username:'';if(kind==='photo'){form.reset();if(avatarPreview){URL.revokeObjectURL(avatarPreview);avatarPreview='';}document.getElementById('avatar-preview').innerHTML=profileAvatar();}form.querySelector('.profile-saved').textContent='Сохранено в Telegram';}
  toast('Профиль обновлён в Telegram.');
 }catch(err){if(error.isConnected)showError(error,err);else toast(err.message);}
 finally{profileBusy=false;if(page==='account')lockProfileForms(!state.account?.profile_loaded);if(submit.isConnected)submit.textContent=original;}
});
document.addEventListener('submit',async e=>{
 const form=e.target;if(!['login-form','source-form','tdata-form','view-form','chat-file-form','people-add-form','people-file-form','channel-create-form'].includes(form.id))return;e.preventDefault();
 if(form.id!=='login-form'&&!online())return;
 if(form.id==='channel-create-form'&&form.dataset?.uncertain)return;
 const submit=form.querySelector('[type=submit]');const original=submit.innerHTML;submit.disabled=true;
 const errors={ 'login-form':'login-error','source-form':'source-error','tdata-form':'import-error','view-form':'view-error','chat-file-form':'chat-file-error','people-add-form':'people-add-error','people-file-form':'people-file-error','channel-create-form':'channel-create-error'};
 const errEl=document.getElementById(errors[form.id]);if(errEl)errEl.textContent='';
 try{
  const data=new FormData(form);
  if(form.id==='channel-create-form'){form.querySelector('fieldset').disabled=true;submit.textContent='Создаём канал…';const result=await api('/api/account/channels',{method:'POST',body:{request_id:data.get('request_id'),title:data.get('title'),username:data.get('username'),about:data.get('about'),pin:data.get('pin')==='on'}});try{await sync(false);}catch{toast('Канал создан. Список обновится после восстановления связи.');}render();channelResult(result);}
  if(form.id==='people-add-form'){submit.textContent='Добавляем…';const result=await api('/api/people',{method:'POST',body:{username:data.get('username'),request_id:data.get('request_id')}});await refreshImportedPeople();toast(result.replayed?'Пользователь уже в очереди.':'Пользователь в очереди. Проверим профиль и истории.');}
  if(form.id==='people-file-form'){const file=data.get('file');if(!file||!file.size||file.size>512*1024)throw new Error('Выберите TXT или CSV размером до 512 КБ.');form.querySelector('fieldset').disabled=true;submit.textContent='Добавляем пользователей…';const result=await api('/api/people/import',{method:'POST',body:data});await refreshImportedPeople();peopleImportResult(result);}
  if(form.id==='chat-file-form'){const file=data.get('file');if(!file||!file.size||file.size>512*1024)throw new Error('Выберите TXT или CSV размером до 512 КБ.');data.set('start_scans',form.elements.start_scans.checked?'true':'false');form.querySelector('fieldset').disabled=true;submit.textContent='Добавляем чаты…';const result=await api('/api/sources/import',{method:'POST',body:data});page='sources';try{await sync(false);}catch{toast('Файл обработан. Список чатов обновится после восстановления связи.');}render();importResult(result);}
  if(form.id==='login-form'){const d=await api('/api/auth/login',{method:'POST',body:{password:data.get('password')}});csrf=d.csrf;authenticated=true;form.reset();dialog.dataset.login='false';dialog.close();await sync();}
  if(form.id==='source-form'){await api('/api/sources',{method:'POST',body:{ref:data.get('ref'),mode:data.get('mode'),max_users:Number(data.get('max_users'))}});dialog.close();page='sources';await sync();toast('Чат добавлен. Теперь можно запустить сбор.');}
  if(form.id==='tdata-form'){const file=data.get('file');if(!file||file.size>20*1024*1024)throw new Error('Выберите ZIP размером до 20 МБ.');submit.textContent='Подключаем… до 90 сек.';await api('/api/account/tdata',{method:'POST',body:data});form.reset();await sync();await refreshProfile();toast('Аккаунт подключён. Добавьте первый чат.');}
  if(form.id==='view-form'){await api('/api/jobs/view',{method:'POST',body:{user_ids:[...selected],delay:Number(data.get('delay'))}});selected.clear();dialog.close();page='jobs';await sync();toast('Просмотр поставлен в очередь.');}
 }catch(err){if(form.id==='channel-create-form'&&err.code==='CHANNEL_CREATE_UNCERTAIN'){form.dataset.uncertain='true';}if(errEl&&errEl.isConnected)showError(errEl,err);else toast(err.message);}
 finally{if(submit.isConnected){submit.disabled=false;submit.innerHTML=original;if(['chat-file-form','people-file-form','channel-create-form'].includes(form.id))form.querySelector('fieldset').disabled=false;if(form.dataset?.uncertain){submit.disabled=true;submit.textContent='Проверьте канал в Telegram';}}}
});
let bootRetries=0,bootTimer=null,booting=false,bootRetryAt=0,pollTimer=null,pollRetryAt=0,connectionPending=false;
let backendWakeAttempted=false,backendWakeStartedAt=0,backendWakeFrame=null,backendWakeUrl=null,backendWakeTimer=null,backendWakeConfirmed=false;
let returnedFromWake=window.location?.hash==='#server-started';
if(returnedFromWake)window.history?.replaceState(null,'',window.location.pathname+window.location.search);
function stopBackendWake(){
 clearTimeout(backendWakeTimer);backendWakeTimer=null;
 backendWakeFrame?.remove();backendWakeFrame=null;
}
function openBackendWake(){
 if(!backendWakeUrl||backendWakeConfirmed||!connectionPending||returnedFromWake||document.visibilityState==='hidden')return;
 stopBackendWake();
 window.location.replace(backendWakeUrl.href);
}
function wakeBackendFromBrowser(){
 if(backendWakeAttempted)return;
 backendWakeAttempted=true;
 const raw=document.querySelector('meta[name="story-backend-wake"]')?.content;
 if(!raw)return;
 let url;
 try{url=new URL(raw);}catch{return;}
 if(url.protocol!=='https:'||!url.hostname.endsWith('.onrender.com')||url.port||url.username||url.password||url.pathname!=='/wake'||url.search||url.hash)return;
 backendWakeUrl=url;backendWakeStartedAt=Date.now();backendWakeConfirmed=false;
 // Navigation follows the host's cold-start document. No account data or
 // credentials are included in the URL, frame, message, or public health call.
 const frame=document.createElement('iframe');
 frame.src=url.href;frame.title='Запуск облачного сервера';frame.hidden=true;
 frame.referrerPolicy='no-referrer';frame.setAttribute('aria-hidden','true');frame.setAttribute('tabindex','-1');
 backendWakeFrame=frame;document.body.append(frame);
 backendWakeTimer=setTimeout(openBackendWake,45000);
}
window.addEventListener('message',event=>{
 if(!backendWakeFrame||event.source!==backendWakeFrame.contentWindow||event.origin!==backendWakeUrl?.origin||event.data?.type!=='story-backend-ready')return;
 backendWakeConfirmed=true;stopBackendWake();
 if(connectionPending)resumeBoot();
});
function scheduleBoot(){
 clearTimeout(bootTimer);bootTimer=null;
 if(!connectionPending||document.visibilityState==='hidden')return;
 const remaining=bootRetryAt-Date.now();
 if(remaining<=0){void boot();return;}
 bootTimer=setTimeout(resumeBoot,Math.min(remaining,2147483647));
}
function resumeBoot(){
 if(!connectionPending||document.visibilityState==='hidden')return;
 if(backendWakeStartedAt&&Date.now()-backendWakeStartedAt>=45000&&!backendWakeConfirmed&&!returnedFromWake){openBackendWake();return;}
 scheduleBoot();
}
async function reconnect(){
 if(connectionPending||booting)return;
 clearInterval(pollTimer);pollTimer=null;
 server=false;authenticated=false;connectionPending=true;bootRetryAt=0;bootRetries=0;
 stopBackendWake();backendWakeAttempted=false;backendWakeStartedAt=0;backendWakeConfirmed=false;
 await boot();
}
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='hidden'){clearTimeout(bootTimer);bootTimer=null;}else resumeBoot();});
window.addEventListener('online',resumeBoot);
window.addEventListener('pageshow',event=>{if(event.persisted&&server&&authenticated)void reconnect();else resumeBoot();});
function retryDelay(response,fallback){
 const value=response.headers.get('Retry-After');
 if(value===null||!value.trim())return fallback;
 const seconds=Number(value);
 const delay=Number.isFinite(seconds)?Math.ceil(seconds*1000):Date.parse(value)-Date.now();
 return Number.isFinite(delay)&&delay>0?Math.max(1000,delay):fallback;
}
function connectionIssue(status,delay){
 clearTimeout(bootTimer);bootTimer=null;
 const signIn=status===401||status===403;
 connectionPending=!signIn;
 if(signIn)stopBackendWake();
 const waking=!signIn&&!!backendWakeUrl;
 const heading=signIn?'Нужно войти в панель':waking?'Запускаем облачную панель':'Восстанавливаем подключение';
 const message=signIn?'Откройте личную панель через вход с ChatGPT.':waking?'Подключаем автоматически. Если фоновый запуск не завершится, откроем страницу сервера и вернём вас в панель.':'Сервер пока недоступен. Подключение повторится автоматически, пока эта страница открыта.';
 const attempt=++bootRetries;
 const wait=signIn?0:delay??Math.min(60000,5000*2**Math.floor((attempt-1)/3));
 bootRetryAt=Date.now()+wait;
 app.innerHTML=`<div class='boot connection-state' data-connection-state='${signIn?'signin':waking?'waking':'unavailable'}'><span class='brand-mark'>Z</span><h1>${heading}</h1><p>${message}</p>${signIn?`<a class='btn primary' href='/signin-with-chatgpt?return_to=%2F' target='_top'>Войти через ChatGPT</a>`:`<p class='connection-retry' role='status'>Нажимать «Повторить» не нужно.</p>${backendWakeUrl?`<button class='btn light' type='button' data-action='wake-server'>Открыть запуск сервера</button>`:''}`}</div>`;
 scheduleBoot();
}
function hostingPause(resumeAfter){
 stopBackendWake();backendWakeAttempted=false;backendWakeStartedAt=0;backendWakeConfirmed=false;backendWakeUrl=null;
 connectionPending=true;
 bootRetryAt=Math.max(Date.now()+60000,Number(resumeAfter)*1000||0);
 app.innerHTML=`<div class='boot connection-state' data-connection-state='hosting-quota'><span class='brand-mark'>Z</span><h1>Лимит хостинга исчерпан</h1><p>Render приостановил сервер из-за месячной квоты бесплатного размещения. Постоянное обновление страницы его не запустит.</p><p class='connection-retry' role='status'>Новая автоматическая проверка: ${esc(date(bootRetryAt/1000))}.</p></div>`;
 scheduleBoot();
}
function startPolling(){
 if(pollTimer!==null)return;
 pollTimer=setInterval(async()=>{
  if(!server||!authenticated||polling||accountWrites||profileBusy||Date.now()<pollRetryAt||document.visibilityState!=='visible'||dialog.open||document.activeElement?.matches('input,select,textarea'))return;
  polling=true;
  try{await sync(page!=='account');}
  catch(err){if(err.status===429){pollRetryAt=Date.now()+err.retryAfter;toast('Сервер запросил паузу. Обновление данных повторится позже.');}else if(!err.status||[502,503,504].includes(err.status)){await reconnect();}else toast(err.message);}
  finally{polling=false;}
 },5000);
}
async function boot(){
 if(booting||Date.now()<bootRetryAt||document.visibilityState==='hidden')return;
 clearTimeout(bootTimer);bootTimer=null;booting=true;connectionPending=true;
 app.innerHTML=`<div class='boot'><span class='brand-mark'>Z</span><p>Подключаемся к рабочему серверу…</p></div>`;
 try{
  wakeBackendFromBrowser();
  const controller=new AbortController();
  const timeout=setTimeout(()=>controller.abort(),65000);
  let response,h;
  try{
   response=await fetch('/health',{headers:{Accept:'application/json'},credentials:'same-origin',cache:'no-store',signal:controller.signal});
   if(!response.ok){let detail;try{detail=await response.json();}catch{}if(detail?.reason==='hosting_monthly_quota'){hostingPause(detail.resume_after);return;}connectionIssue(response.status,retryDelay(response,response.status===429?30000:5000));return;}
   h=await response.json();
  }finally{clearTimeout(timeout);}
  // Preview is an explicit server mode, never a fallback for HTTP or network errors.
  if(h?.service==='story-preview'){server=false;authenticated=false;connectionPending=false;stopBackendWake();bootRetries=0;bootRetryAt=0;render();return;}
  if(h?.service!=='story-cloud'||h.ok!==true){connectionIssue('invalid');return;}
  server=true;
  if(h.ready===false){connectionIssue('waking');return;}
  try{
   const d=await api('/api/auth/me',{authPrompt:false});
   csrf=d.csrf;
   const nextState=await api('/api/state',{authPrompt:false});
   state=nextState;authenticated=true;
  }catch(err){
   authenticated=false;
   if(err.status===401){connectionPending=false;stopBackendWake();render();showLogin();startPolling();return;}
   connectionIssue(err.status||0,err.status===429?err.retryAfter:undefined);return;
  }
  bootRetries=0;bootRetryAt=0;pollRetryAt=0;connectionPending=false;returnedFromWake=false;stopBackendWake();
  render();startPolling();
 }catch{connectionIssue(0);}
 finally{booting=false;}
}
boot();
