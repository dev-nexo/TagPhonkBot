
const tg = window.Telegram?.WebApp;
if (tg) {
  tg.ready(); tg.expand();
  try { tg.setHeaderColor('#09090d'); tg.setBackgroundColor('#09090d'); } catch (_) {}
}
const initData = tg?.initData || '';
const authHeaders = {'X-Telegram-Init-Data': initData};
let session = null, coverUrl = null;

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const fmtMb = n => `${((n||0)/1024/1024).toFixed(1)} MB`;
const fmtTime = n => { const s=Math.max(0,Math.round(Number(n)||0)); return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`; };
const esc = v => String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));

async function api(path, options={}){
  const r = await fetch(path,{...options,headers:{...authHeaders,...(options.headers||{})}});
  const type=r.headers.get('content-type')||'';
  if(!r.ok){
    const d=type.includes('json')?await r.json().catch(()=>({})):{};
    throw new Error(d.detail||`HTTP ${r.status}`);
  }
  return type.includes('json')?r.json():r.blob();
}
function busy(on,text='Работаю…'){ $('#loadingText').textContent=text; $('#loading').classList.toggle('hidden',!on); }
function toast(text,ok=true){ const e=$('#toast'); e.textContent=text; e.className=`toast ${ok?'good':'bad'}`; setTimeout(()=>e.classList.add('hidden'),2200); }
function haptic(type='success'){ try{tg?.HapticFeedback?.notificationOccurred(type)}catch(_){} }

function showView(name){
  $$('.view').forEach(v=>v.classList.toggle('active',v.dataset.view===name));
  $$('[data-nav]').forEach(b=>b.classList.toggle('active',b.dataset.nav===name));
  window.scrollTo({top:0,behavior:'smooth'});
}
$$('[data-nav]').forEach(b=>b.onclick=()=>showView(b.dataset.nav));

async function refreshCover(){
  if(coverUrl){URL.revokeObjectURL(coverUrl);coverUrl=null;}
  if(!session?.cover?.exists){$('#coverImage').classList.add('hidden');$('#coverFallback').classList.remove('hidden');return;}
  try{
    const blob=await api(`/api/studio/editor/${session.session_id}/cover`);
    coverUrl=URL.createObjectURL(blob); $('#coverImage').src=coverUrl;
    $('#coverImage').classList.remove('hidden'); $('#coverFallback').classList.add('hidden');
  }catch(_){}
}
function renderEditor(data){
  session=data; $('#emptyEditor').classList.add('hidden'); $('#editorPanel').classList.remove('hidden');
  const f=data.fields||{};
  $$('[data-field]').forEach(i=>i.value=f[i.dataset.field]||'');
  $('#trackTitle').textContent=f.title||'Без названия';
  $('#trackArtist').textContent=f.artist||'Неизвестный исполнитель';
  $('#chipDuration').textContent=fmtTime(data.technical?.duration);
  $('#chipBitrate').textContent=`${data.technical?.bitrate_kbps||0} kbps`;
  $('#chipSize').textContent=fmtMb(data.technical?.size_bytes);
  refreshCover();
}
function resetEditor(){
  session=null; $('#editorPanel').classList.add('hidden'); $('#emptyEditor').classList.remove('hidden');
  $('#fileInput').value=''; if(coverUrl){URL.revokeObjectURL(coverUrl);coverUrl=null;}
}

async function uploadMp3(file){
  if(!file)return; const fd=new FormData();fd.append('file',file);busy(true,'Загружаю MP3…');
  try{renderEditor(await api('/api/studio/editor/open',{method:'POST',body:fd}));showView('editor');toast('MP3 открыт');haptic();}
  catch(e){toast(e.message,false);haptic('error');}finally{busy(false);}
}
$('#fileInput').onchange=e=>uploadMp3(e.target.files?.[0]);
$('#newFileBtn').onclick=()=>$('#fileInput').click();

$('#saveBtn').onclick=async()=>{
  if(!session)return; const fields={}; $$('[data-field]').forEach(i=>fields[i.dataset.field]=i.value); busy(true,'Сохраняю…');
  try{renderEditor(await api(`/api/studio/editor/${session.session_id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({fields})}));toast('Сохранено');haptic();}
  catch(e){toast(e.message,false);}finally{busy(false);}
};
$('#undoBtn').onclick=async()=>{
  if(!session)return; busy(true,'Отменяю…');
  try{renderEditor(await api(`/api/studio/editor/${session.session_id}/undo`,{method:'POST'}));toast('Изменение отменено');}
  catch(e){toast(e.message,false);}finally{busy(false);}
};
$('#coverInput').onchange=async e=>{
  const file=e.target.files?.[0]; if(!session||!file)return; const fd=new FormData();fd.append('file',file);busy(true,'Сохраняю обложку…');
  try{renderEditor(await api(`/api/studio/editor/${session.session_id}/cover`,{method:'POST',body:fd}));toast('Обложка сохранена');}
  catch(err){toast(err.message,false);}finally{busy(false);e.target.value='';}
};
$('#removeCoverBtn').onclick=async()=>{
  if(!session)return;busy(true,'Удаляю обложку…');
  try{renderEditor(await api(`/api/studio/editor/${session.session_id}/cover`,{method:'DELETE'}));toast('Обложка удалена');}
  catch(e){toast(e.message,false);}finally{busy(false);}
};
$('#downloadBtn').onclick=async()=>{
  if(!session)return;busy(true,'Готовлю MP3…');
  try{
    const blob=await api(`/api/studio/editor/${session.session_id}/download`);
    const u=URL.createObjectURL(blob),a=document.createElement('a');a.href=u;a.download=session.download_name||'TagPhonk.mp3';
    document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(u),4000);haptic();
  }catch(e){toast(e.message,false);}finally{busy(false);}
};

async function loadProjects(){
  try{
    const d=await api('/api/studio/projects'),items=d.items||[];
    $('#projects').innerHTML=items.length?items.map(p=>`<button class="project" data-project="${p.id}"><div class="project-icon">♫</div><div class="project-copy"><b>${esc((p.artist||'—')+' — '+(p.title||p.filename||'—'))}</b><span>${esc(p.album||p.filename||'')} · ${fmtMb(p.size_bytes)}</span></div><span class="chev">›</span></button>`).join(''):'<div class="empty-state">Пока пусто. Отправь MP3 боту или загрузи здесь.</div>';
    $$('[data-project]').forEach(b=>b.onclick=()=>openProject(b.dataset.project));
  }catch(e){$('#projects').innerHTML=`<div class="empty-state">${esc(e.message)}</div>`;}
}
async function openProject(id){
  busy(true,'Открываю проект…');
  try{renderEditor(await api(`/api/studio/projects/${id}/open`,{method:'POST'}));showView('editor');toast('Проект открыт');}
  catch(e){toast(e.message,false);}finally{busy(false);}
}
$('#reloadProjects').onclick=loadProjects;

$('#saveSettings').onclick=async()=>{
  const body={filename_template:$('#filenameTemplate').value,default_genre:$('#defaultGenre').value,remove_comments:$('#removeComments').checked,normalize_cover:$('#normalizeCover').checked};
  try{await api('/api/studio/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});toast('Настройки сохранены');haptic();}
  catch(e){toast(e.message,false);}
};

async function init(){
  if(!initData){$('#hello').textContent='Открой через Telegram';toast('Открой Studio из Telegram',false);return;}
  try{
    const me=await api('/api/studio/me');$('#hello').textContent=me.user?.first_name?`Привет, ${me.user.first_name}`:'MP3 editor';
    $('#projectsCount').textContent=me.stats?.projects||0;$('#processedSize').textContent=fmtMb(me.stats?.bytes_processed);
    const s=me.settings||{};$('#filenameTemplate').value=s.filename_template||'{artist} - {title}';$('#defaultGenre').value=s.default_genre||'Phonk';$('#removeComments').checked=!!s.remove_comments;$('#normalizeCover').checked=!!s.normalize_cover;
    await loadProjects();
  }catch(e){$('#hello').textContent='Ошибка';toast(e.message,false);}
}
init();
