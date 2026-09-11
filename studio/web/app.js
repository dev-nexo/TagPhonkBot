
const tg = window.Telegram?.WebApp;
if (tg) {
  tg.ready(); tg.expand();
  try { tg.setHeaderColor('#08090d'); tg.setBackgroundColor('#08090d'); } catch (_) {}
}
const initData = tg?.initData || '';
const authHeaders = {'X-Telegram-Init-Data': initData};

let session = null, coverUrl = null;
let daw = null, selectedTrackId = null, selectedClipId = null, pendingTrackId = null;
let dawSaveTimer = null, pxPerSecond = 58, loopPreview = false, metronomeOn = false;
let audioCtx = null, activeSources = [], playStartedAt = 0, playFrom = 0, playPosition = 0, playing = false, rafId = null;
let masterAnalyser = null, reverbImpulse = null;
const decodedBuffers = new Map();

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const fmtMb = n => `${((n||0)/1024/1024).toFixed(1)} MB`;
const fmtTime = n => { const s=Math.max(0,Number(n)||0); const m=Math.floor(s/60); const sec=s-m*60; return `${m}:${sec.toFixed(2).padStart(5,'0')}`; };
const fmtShort = n => { const s=Math.max(0,Math.round(Number(n)||0)); return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`; };
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
function toast(text,ok=true){ const e=$('#toast'); e.textContent=text; e.className=`toast ${ok?'good':'bad'}`; setTimeout(()=>e.classList.add('hidden'),2400); }
function haptic(type='success'){ try{tg?.HapticFeedback?.notificationOccurred(type)}catch(_){} }

function showView(name){
  $$('.view').forEach(v=>v.classList.toggle('active',v.dataset.view===name));
  $$('[data-nav]').forEach(b=>b.classList.toggle('active',b.dataset.nav===name));
  if(name==='daw' && !daw) ensureDaw();
  window.scrollTo({top:0,behavior:'smooth'});
}
$$('[data-nav]').forEach(b=>b.onclick=()=>showView(b.dataset.nav));

/* ---------------- Editor ---------------- */
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
  $('#chipDuration').textContent=fmtShort(data.technical?.duration);
  $('#chipBitrate').textContent=`${data.technical?.bitrate_kbps||0} kbps`;
  $('#chipSize').textContent=fmtMb(data.technical?.size_bytes);
  refreshCover();
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
    downloadBlob(blob, session.download_name||'TagPhonk.mp3'); haptic();
  }catch(e){toast(e.message,false);}finally{busy(false);}
};

/* ---------------- Projects / Settings ---------------- */
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

/* ---------------- DAW state ---------------- */
async function ensureDaw(){
  if(daw)return;
  busy(true,'Создаю пустой Mini DAW…');
  try{
    daw=await api('/api/daw/projects',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'Untitled Mix'})});
    selectedTrackId=daw.tracks[0]?.id||null;
    renderDaw();
  }catch(e){toast(e.message,false);haptic('error');}
  finally{busy(false);}
}
$('#createDawBtn').onclick=ensureDaw;

function audibleDuration(c){
  return Math.max(.03,((c.trim_end??c.duration)-(c.trim_start||0))/Math.max(.5,Number(c.rate)||1));
}
function projectDuration(){
  if(!daw)return 0;
  let end=0;
  for(const c of daw.clips||[]) end=Math.max(end,(c.start||0)+audibleDuration(c));
  const bar=60/(Number(daw.bpm)||130)*4;
  if(daw.sequence_enabled && Object.values(daw.sequence||{}).some(r=>r.some(Boolean))) end=Math.max(end,bar*4);
  return Math.max(end,bar);
}
function dawPayload(){
  return {
    name:daw.name,
    bpm:Number(daw.bpm),
    master_gain:Number(daw.master_gain),
    normalize:!!daw.normalize,
    sequence_gain:Number(daw.sequence_gain),
    sequence_enabled:!!daw.sequence_enabled,
    tracks:daw.tracks,
    clips:daw.clips,
    sequence:daw.sequence
  };
}
function setSaveStatus(text,state=''){
  const e=$('#saveStatus'); if(!e)return;
  e.textContent=text; e.className=`save-state ${state}`;
}
function scheduleDawSave(){
  clearTimeout(dawSaveTimer);
  setSaveStatus('Изменено','pending');
  dawSaveTimer=setTimeout(saveDaw,520);
}
async function saveDaw(){
  if(!daw)return;
  setSaveStatus('Сохраняю…','pending');
  try{
    daw=await api(`/api/daw/projects/${daw.id}`,{
      method:'PATCH',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(dawPayload())
    });
    setSaveStatus('Сохранено','ok');
    syncDawInputs();
  }catch(e){
    setSaveStatus('Ошибка','bad');toast(e.message,false);
  }
}
function syncDawInputs(){
  if(!daw)return;
  $('#dawEmpty').classList.add('hidden'); $('#dawPanel').classList.remove('hidden');
  $('#dawName').value=daw.name||'Untitled';
  $('#dawBpm').value=Math.round(daw.bpm||130);
  $('#masterGain').value=daw.master_gain??1;
  $('#normalizeMix').checked=!!daw.normalize;
  $('#sequenceGain').value=daw.sequence_gain??.72;
  $('#dawLength').textContent=`/ ${fmtShort(projectDuration())}`;
  const tr=daw.tracks.find(t=>t.id===selectedTrackId)||daw.tracks[0];
  if(tr){
    selectedTrackId=tr.id;
    $('#selectedTrackLabel').textContent=tr.name;
  }
  $('#toggleSequence').textContent=daw.sequence_enabled?'● ON':'○ OFF';
  $('#toggleSequence').classList.toggle('on',!!daw.sequence_enabled);
}
function renderDaw(){
  if(!daw)return;
  syncDawInputs();
  renderRuler();
  renderTimeline();
  renderMixer();
  renderSequencer();
  updateTransport();
}
$('#dawName').onchange=e=>{if(daw){daw.name=e.target.value;scheduleDawSave();}};
$('#dawBpm').onchange=e=>{
  if(!daw)return;
  daw.bpm=Math.max(50,Math.min(220,Number(e.target.value)||130));
  renderRuler();renderTimeline();renderSequencer();syncDawInputs();scheduleDawSave();
};
$('#masterGain').oninput=e=>{if(daw){daw.master_gain=Number(e.target.value);scheduleDawSave();}};
$('#normalizeMix').onchange=e=>{if(daw){daw.normalize=e.target.checked;scheduleDawSave();}};
$('#sequenceGain').oninput=e=>{if(daw){daw.sequence_gain=Number(e.target.value);scheduleDawSave();}};
$('#dawZoom').oninput=e=>{pxPerSecond=Number(e.target.value);renderRuler();renderTimeline();};
$('#dawRewind').onclick=()=>{playPosition=0;updateTransport();updatePlayheads();};
$('#dawLoop').onclick=()=>{loopPreview=!loopPreview;$('#dawLoop').classList.toggle('active',loopPreview);toast(loopPreview?'Loop включён':'Loop выключен');};
$('#dawMetronome').onclick=()=>{metronomeOn=!metronomeOn;$('#dawMetronome').classList.toggle('active',metronomeOn);toast(metronomeOn?'Метроном включён':'Метроном выключен');};

/* ---------------- Track & audio add ---------------- */
$('#addTrackBtn').onclick=async()=>{
  if(!daw)return; busy(true,'Добавляю дорожку…');
  try{
    daw=await api(`/api/daw/projects/${daw.id}/tracks`,{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:`Track ${daw.tracks.length+1}`})
    });
    selectedTrackId=daw.tracks.at(-1).id;
    renderDaw();toast('Дорожка добавлена');
  }catch(e){toast(e.message,false);}finally{busy(false);}
};

function openAudioPicker(trackId){
  if(!daw){ensureDaw().then(()=>openAudioPicker(trackId));return;}
  pendingTrackId=trackId||selectedTrackId||daw.tracks[0]?.id;
  selectedTrackId=pendingTrackId;
  renderTimeline();syncDawInputs();
  const input=$('#dawFileInput');
  try{
    if(typeof input.showPicker==='function')input.showPicker();
    else input.click();
  }catch(_){input.click();}
}
$('#addAudioBtn').onclick=()=>openAudioPicker(selectedTrackId);
$('#dawFileInput').onchange=async e=>{
  const files=[...(e.target.files||[])];
  e.target.value='';
  for(const f of files)await uploadDawClip(f,pendingTrackId||selectedTrackId);
  pendingTrackId=null;
};

async function uploadDawClip(file,trackId=selectedTrackId){
  if(!daw)await ensureDaw();
  const target=trackId||selectedTrackId||daw.tracks[0]?.id;
  if(!target){toast('Нет дорожки для аудио',false);return;}
  selectedTrackId=target;
  const fd=new FormData();fd.append('file',file);
  busy(true,`Добавляю ${file.name||'аудио'}…`);
  try{
    daw=await api(`/api/daw/projects/${daw.id}/clips?track_id=${encodeURIComponent(target)}`,{method:'POST',body:fd});
    const latest=daw.clips.at(-1);
    selectedClipId=latest?.id||selectedClipId;
    renderDaw();toast('Клип добавлен');haptic();
  }catch(e){toast(e.message,false);haptic('error');}
  finally{busy(false);}
}
async function currentEditorToDaw(){
  if(!session){toast('Сначала открой MP3 в редакторе',false);return;}
  if(!daw)await ensureDaw();
  busy(true,'Переношу MP3 в DAW…');
  try{
    const blob=await api(`/api/studio/editor/${session.session_id}/download`);
    const file=new File([blob],session.download_name||'track.mp3',{type:'audio/mpeg'});
    await uploadDawClip(file,selectedTrackId);showView('daw');
  }catch(e){toast(e.message,false);}finally{busy(false);}
}
$('#currentToDawBtn').onclick=currentEditorToDaw;
$('#sendToDawBtn').onclick=currentEditorToDaw;

/* ---------------- Snap / seek ---------------- */
function snapStep(){
  if(!daw)return 0;
  const mode=$('#snapMode')?.value||'1/16';
  if(mode==='off')return 0;
  const beat=60/(Number(daw.bpm)||130);
  return {'1/4':beat,'1/8':beat/2,'1/16':beat/4,'1/32':beat/8}[mode]||0;
}
function snapTime(value){
  const step=snapStep();
  if(!step)return Math.max(0,value);
  return Math.max(0,Math.round(value/step)*step);
}
function seekTo(value){
  playPosition=Math.max(0,Math.min(projectDuration(),value));
  updateTransport();updatePlayheads();
}

/* ---------------- Timeline ---------------- */
function renderRuler(){
  if(!daw)return;
  const length=Math.max(16,Math.ceil(projectDuration()+4));
  const width=length*pxPerSecond;
  const markStep=pxPerSecond<45?4:2;
  let marks='';
  for(let s=0;s<=length;s+=markStep)marks+=`<span data-ruler-time="${s}" style="left:${s*pxPerSecond}px">${s}s</span>`;
  $('#timelineRuler').innerHTML=`<div class="ruler-inner" style="width:${width}px">${marks}<i class="ruler-playhead" style="left:${playPosition*pxPerSecond}px"></i></div>`;
  $('#timelineRuler').onclick=e=>{
    const rect=$('#timelineRuler').getBoundingClientRect();
    const scroller=$('#timeline');
    const x=e.clientX-rect.left+(scroller?.scrollLeft||0);
    seekTo(x/pxPerSecond);
  };
}
function renderTimeline(){
  if(!daw)return;
  const length=Math.max(16,Math.ceil(projectDuration()+4));
  const width=length*pxPerSecond;
  const clipsByTrack={};
  for(const t of daw.tracks)clipsByTrack[t.id]=[];
  for(const c of daw.clips)(clipsByTrack[c.track_id]||=[]).push(c);

  $('#timeline').innerHTML=daw.tracks.map(t=>{
    const clips=(clipsByTrack[t.id]||[]).map(c=>{
      const dur=audibleDuration(c),left=c.start*pxPerSecond,w=Math.max(46,dur*pxPerSecond);
      const selected=c.id===selectedClipId?' selected':'';
      const speed=Math.abs((c.rate||1)-1)>.01?` · ${(c.rate||1).toFixed(2)}x`:'';
      return `<button class="timeline-clip${selected}" data-clip="${c.id}" style="left:${left}px;width:${w}px">
        <b>${esc(c.filename)}</b><span>${fmtShort(dur)}${speed}</span><canvas data-wave="${c.id}"></canvas>
      </button>`;
    }).join('');

    return `<div class="timeline-row ${t.id===selectedTrackId?'selected-track':''}" data-track="${t.id}">
      <div class="track-head">
        <button class="track-select" data-select-track="${t.id}"><b>${esc(t.name)}</b><span>${t.mute?'MUTE':t.solo?'SOLO':'Track'}</span></button>
        <button class="track-plus" data-add-track="${t.id}" title="Добавить аудио">＋</button>
      </div>
      <div class="track-lane" data-lane="${t.id}" style="width:${width}px">${clips}<i class="playhead" style="left:${playPosition*pxPerSecond}px"></i></div>
    </div>`;
  }).join('');

  $$('[data-select-track]').forEach(b=>b.onclick=()=>{
    selectedTrackId=b.dataset.selectTrack;renderTimeline();renderMixer();syncDawInputs();
  });
  $$('[data-add-track]').forEach(b=>b.onclick=e=>{
    e.stopPropagation();openAudioPicker(b.dataset.addTrack);
  });
  $$('[data-clip]').forEach(el=>{
    el.onclick=()=>{
      if(el.dataset.dragged==='1'){el.dataset.dragged='0';return;}
      selectClip(el.dataset.clip);
    };
    enableClipDrag(el);
  });
  $$('[data-lane]').forEach(lane=>{
    lane.onclick=e=>{
      if(e.target!==lane)return;
      selectedTrackId=lane.dataset.lane;
      const rect=lane.getBoundingClientRect();
      seekTo((e.clientX-rect.left)/pxPerSecond);
      renderTimeline();renderMixer();syncDawInputs();
    };
    lane.addEventListener('dragover',e=>{e.preventDefault();lane.classList.add('drop-target');});
    lane.addEventListener('dragleave',()=>lane.classList.remove('drop-target'));
    lane.addEventListener('drop',async e=>{
      e.preventDefault();lane.classList.remove('drop-target');
      const file=[...(e.dataTransfer?.files||[])].find(f=>f.type.startsWith('audio/')||/\.(mp3|wav|m4a|aac|ogg|flac)$/i.test(f.name));
      if(file)await uploadDawClip(file,lane.dataset.lane);
    });
  });
  renderClipInspector();
  updatePlayheads();
  drawWaveforms();
}
function updatePlayheads(){
  $$('.playhead,.ruler-playhead').forEach(el=>el.style.left=`${playPosition*pxPerSecond}px`);
}
function selectClip(id){
  selectedClipId=id;
  const c=daw.clips.find(x=>x.id===id);
  if(c)selectedTrackId=c.track_id;
  renderTimeline();renderMixer();renderClipInspector();syncDawInputs();
}
function renderClipInspector(){
  const c=daw?.clips?.find(x=>x.id===selectedClipId);
  $('#clipInspector').classList.toggle('hidden',!c);
  if(!c)return;
  $('#clipName').textContent=c.filename;
  $('#clipStart').value=(c.start||0).toFixed(2);
  $('#clipTrimStart').value=(c.trim_start||0).toFixed(2);
  $('#clipTrimEnd').value=(c.trim_end??c.duration).toFixed(2);
  $('#clipGain').value=c.gain??1;
  $('#clipRate').value=c.rate??1;
  $('#clipFadeIn').value=c.fade_in??0;
  $('#clipFadeOut').value=c.fade_out??0;
  $('#clipTrack').innerHTML=daw.tracks.map(t=>`<option value="${t.id}" ${t.id===c.track_id?'selected':''}>${esc(t.name)}</option>`).join('');
}
$('#clipTrack').onchange=e=>{
  const c=daw?.clips?.find(x=>x.id===selectedClipId);if(!c)return;
  c.track_id=e.target.value;selectedTrackId=c.track_id;renderTimeline();renderMixer();scheduleDawSave();
};
for(const id of ['clipStart','clipTrimStart','clipTrimEnd','clipGain','clipRate','clipFadeIn','clipFadeOut']){
  $(`#${id}`).oninput=e=>{
    const c=daw?.clips?.find(x=>x.id===selectedClipId);if(!c)return;
    if(id==='clipStart')c.start=snapTime(Number(e.target.value)||0);
    if(id==='clipTrimStart')c.trim_start=Math.max(0,Math.min(c.duration-.03,Number(e.target.value)||0));
    if(id==='clipTrimEnd')c.trim_end=Math.max(c.trim_start+.03,Math.min(c.duration,Number(e.target.value)||c.duration));
    if(id==='clipGain')c.gain=Number(e.target.value);
    if(id==='clipRate')c.rate=Math.max(.5,Math.min(2,Number(e.target.value)||1));
    if(id==='clipFadeIn')c.fade_in=Math.max(0,Number(e.target.value)||0);
    if(id==='clipFadeOut')c.fade_out=Math.max(0,Number(e.target.value)||0);
    renderRuler();renderTimeline();scheduleDawSave();
  };
}
$('#deleteClipBtn').onclick=deleteSelectedClip;
async function deleteSelectedClip(){
  if(!daw||!selectedClipId)return;
  busy(true,'Удаляю клип…');
  try{
    daw=await api(`/api/daw/projects/${daw.id}/clips/${selectedClipId}`,{method:'DELETE'});
    decodedBuffers.delete(selectedClipId);selectedClipId=null;renderDaw();toast('Клип удалён');
  }catch(e){toast(e.message,false);}finally{busy(false);}
}
$('#duplicateClipBtn').onclick=duplicateSelectedClip;
async function duplicateSelectedClip(){
  if(!daw||!selectedClipId)return;
  busy(true,'Дублирую клип…');
  try{
    daw=await api(`/api/daw/projects/${daw.id}/clips/${selectedClipId}/duplicate`,{method:'POST'});
    selectedClipId=daw.clips.at(-1)?.id||selectedClipId;renderDaw();toast('Клип продублирован');
  }catch(e){toast(e.message,false);}finally{busy(false);}
}
$('#splitClipBtn').onclick=splitSelectedClip;
async function splitSelectedClip(){
  if(!daw||!selectedClipId)return;
  const c=daw.clips.find(x=>x.id===selectedClipId);if(!c)return;
  let position=playPosition;
  const end=c.start+audibleDuration(c);
  if(position<=c.start+.03||position>=end-.03)position=c.start+audibleDuration(c)/2;
  busy(true,'Режу клип…');
  try{
    daw=await api(`/api/daw/projects/${daw.id}/clips/${selectedClipId}/split`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({position})
    });
    selectedClipId=daw.clips.at(-1)?.id||selectedClipId;renderDaw();toast('Клип разрезан');
  }catch(e){toast(e.message,false);}finally{busy(false);}
}
function enableClipDrag(el){
  let startX=0,startVal=0,moved=false;
  el.addEventListener('pointerdown',e=>{
    if(e.button!==undefined&&e.button!==0)return;
    const c=daw?.clips?.find(x=>x.id===el.dataset.clip);if(!c)return;
    startX=e.clientX;startVal=c.start;moved=false;el.setPointerCapture?.(e.pointerId);
    const move=ev=>{
      const dx=ev.clientX-startX;if(Math.abs(dx)>3)moved=true;
      c.start=snapTime(startVal+dx/pxPerSecond);el.style.left=`${c.start*pxPerSecond}px`;
    };
    const up=()=>{
      window.removeEventListener('pointermove',move);window.removeEventListener('pointerup',up);
      if(moved){el.dataset.dragged='1';renderRuler();scheduleDawSave();}
    };
    window.addEventListener('pointermove',move);window.addEventListener('pointerup',up,{once:true});
  });
}
async function drawWaveforms(){
  const jobs=(daw?.clips||[]).map(async c=>{
    const canvas=document.querySelector(`canvas[data-wave="${c.id}"]`);if(!canvas)return;
    try{
      const buf=await decodeClip(c),data=buf.getChannelData(0);
      const rect=canvas.getBoundingClientRect(),w=Math.max(40,Math.floor(rect.width*devicePixelRatio)),h=Math.max(20,Math.floor(rect.height*devicePixelRatio));
      if(canvas.width!==w)canvas.width=w;if(canvas.height!==h)canvas.height=h;
      const ctx=canvas.getContext('2d');ctx.clearRect(0,0,w,h);
      const css=getComputedStyle(document.documentElement);ctx.strokeStyle=css.getPropertyValue('--wave').trim()||'#b69cff';ctx.lineWidth=Math.max(1,devicePixelRatio);
      const start=Math.floor((c.trim_start/c.duration)*data.length),end=Math.max(start+1,Math.floor((c.trim_end/c.duration)*data.length));
      const span=end-start,step=Math.max(1,Math.floor(span/w));ctx.beginPath();
      for(let x=0;x<w;x++){
        let peak=0;const a=start+x*step,b=Math.min(end,a+step);
        for(let i=a;i<b;i++)peak=Math.max(peak,Math.abs(data[i]||0));
        const y1=h/2-peak*h*.42,y2=h/2+peak*h*.42;ctx.moveTo(x,y1);ctx.lineTo(x,y2);
      }ctx.stroke();
    }catch(_){}
  });
  await Promise.allSettled(jobs);
}

/* ---------------- Mixer ---------------- */
const fxPresets={
  clean:{bass:0,reverb:0,delay:0,highpass:20,lowpass:20000},
  dark:{bass:4,reverb:.16,delay:.04,highpass:34,lowpass:9200},
  phonk:{bass:7,reverb:.12,delay:.08,highpass:28,lowpass:15000},
  space:{bass:1,reverb:.55,delay:.33,highpass:45,lowpass:17000},
  radio:{bass:-2,reverb:.03,delay:0,highpass:360,lowpass:4300}
};
function renderMixer(){
  if(!daw)return;
  $('#mixer').innerHTML=daw.tracks.map(t=>`<div class="mixer-strip ${t.id===selectedTrackId?'active':''}" data-mix-track="${t.id}">
    <div class="mixer-head">
      <input data-mix-name="${t.id}" value="${esc(t.name)}">
      <div><button data-mute="${t.id}" class="${t.mute?'on':''}">M</button><button data-solo="${t.id}" class="${t.solo?'on solo':''}">S</button></div>
    </div>
    <div class="mixer-mini-actions">
      <button data-add-track="${t.id}">＋ Audio</button>
      <button data-delete-track="${t.id}" class="danger">× Track</button>
    </div>
    <label>FX PRESET
      <select data-fx-preset="${t.id}">
        <option value="">Custom</option><option value="clean">Clean</option><option value="dark">Dark</option><option value="phonk">Phonk</option><option value="space">Space</option><option value="radio">Radio</option>
      </select>
    </label>
    <label>VOL <input data-vol="${t.id}" type="range" min="0" max="2" step=".01" value="${t.volume}"></label>
    <label>PAN <input data-pan="${t.id}" type="range" min="-1" max="1" step=".01" value="${t.pan}"></label>
    <label>BASS <input data-bass="${t.id}" type="range" min="-12" max="12" step=".5" value="${t.fx.bass}"></label>
    <label>REVERB <input data-reverb="${t.id}" type="range" min="0" max="1" step=".01" value="${t.fx.reverb}"></label>
    <details><summary>Filter & Delay</summary>
      <label>HP <input data-hp="${t.id}" type="range" min="20" max="8000" step="10" value="${t.fx.highpass}"></label>
      <label>LP <input data-lp="${t.id}" type="range" min="400" max="20000" step="50" value="${t.fx.lowpass}"></label>
      <label>DELAY <input data-delay="${t.id}" type="range" min="0" max="1" step=".01" value="${t.fx.delay}"></label>
    </details>
  </div>`).join('');

  $$('[data-mix-track]').forEach(strip=>strip.onclick=e=>{
    if(['INPUT','BUTTON','SELECT','OPTION'].includes(e.target.tagName))return;
    selectedTrackId=strip.dataset.mixTrack;renderMixer();renderTimeline();syncDawInputs();
  });
  $$('[data-mix-name]').forEach(i=>i.onchange=()=>{
    const t=track(i.dataset.mixName);t.name=i.value.trim()||t.name;renderTimeline();syncDawInputs();scheduleDawSave();
  });
  $$('[data-add-track]').forEach(b=>b.onclick=e=>{e.stopPropagation();openAudioPicker(b.dataset.addTrack);});
  $$('[data-delete-track]').forEach(b=>b.onclick=()=>deleteTrack(b.dataset.deleteTrack));
  $$('[data-fx-preset]').forEach(sel=>sel.onchange=()=>{
    const t=track(sel.dataset.fxPreset),preset=fxPresets[sel.value];if(!t||!preset)return;
    Object.assign(t.fx,preset);renderMixer();scheduleDawSave();toast(`${sel.options[sel.selectedIndex].text} FX`);
  });
  bindTrackRange('vol','volume');bindTrackRange('pan','pan');
  bindTrackFx('bass','bass');bindTrackFx('reverb','reverb');bindTrackFx('hp','highpass');bindTrackFx('lp','lowpass');bindTrackFx('delay','delay');
  $$('[data-mute]').forEach(b=>b.onclick=()=>{const t=track(b.dataset.mute);t.mute=!t.mute;renderMixer();renderTimeline();scheduleDawSave();});
  $$('[data-solo]').forEach(b=>b.onclick=()=>{const t=track(b.dataset.solo);t.solo=!t.solo;renderMixer();renderTimeline();scheduleDawSave();});
}
function track(id){return daw?.tracks?.find(t=>t.id===id);}
function bindTrackRange(attr,key){$$(`[data-${attr}]`).forEach(i=>i.oninput=()=>{const t=track(i.dataset[attr]);if(t){t[key]=Number(i.value);scheduleDawSave();}});}
function bindTrackFx(attr,key){$$(`[data-${attr}]`).forEach(i=>i.oninput=()=>{const t=track(i.dataset[attr]);if(t){t.fx[key]=Number(i.value);scheduleDawSave();}});}
async function deleteTrack(id){
  if(!daw||daw.tracks.length<=1){toast('Нужна хотя бы одна дорожка',false);return;}
  busy(true,'Удаляю дорожку…');
  try{
    daw=await api(`/api/daw/projects/${daw.id}/tracks/${id}`,{method:'DELETE'});
    selectedTrackId=daw.tracks[0]?.id||null;renderDaw();toast('Дорожка удалена');
  }catch(e){toast(e.message,false);}finally{busy(false);}
}

/* ---------------- Sequencer / pads ---------------- */
const seqLabels={kick:'KICK',snare:'SNARE',hat:'HAT',cow:'COWBELL'};
const sequencePresets={
  drift:{
    kick:[1,0,0,0,1,0,0,0,1,0,0,0,1,0,0,0],
    snare:[0,0,0,0,1,0,0,0,0,0,0,0,1,0,0,0],
    hat:[1,0,1,0,1,0,1,0,1,0,1,0,1,0,1,0],
    cow:[1,0,0,1,0,0,1,0,1,0,0,1,0,1,0,0]
  },
  hard:{
    kick:[1,0,1,0,1,0,0,1,1,0,1,0,1,0,0,1],
    snare:[0,0,0,0,1,0,0,0,0,0,0,0,1,0,1,0],
    hat:[1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1],
    cow:[1,0,1,0,0,1,0,1,1,0,0,1,0,1,1,0]
  },
  trap:{
    kick:[1,0,0,0,0,0,1,0,1,0,0,1,0,0,0,1],
    snare:[0,0,0,0,1,0,0,0,0,0,0,0,1,0,0,0],
    hat:[1,0,1,0,1,1,1,0,1,0,1,1,1,0,1,1],
    cow:[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]
  },
  four:{
    kick:[1,0,0,0,1,0,0,0,1,0,0,0,1,0,0,0],
    snare:[0,0,0,0,1,0,0,0,0,0,0,0,1,0,0,0],
    hat:[0,0,1,0,0,0,1,0,0,0,1,0,0,0,1,0],
    cow:[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]
  },
  empty:{kick:Array(16).fill(0),snare:Array(16).fill(0),hat:Array(16).fill(0),cow:Array(16).fill(0)}
};
function renderSequencer(){
  if(!daw)return;
  $('#sequencer').classList.toggle('disabled',!daw.sequence_enabled);
  $('#sequencer').innerHTML=Object.entries(seqLabels).map(([name,label])=>`<div class="seq-row"><b>${label}</b>${daw.sequence[name].map((on,i)=>`<button data-step="${name}:${i}" class="${on?'on':''}${i%4===0?' beat':''}"></button>`).join('')}</div>`).join('');
  $$('[data-step]').forEach(b=>b.onclick=()=>{
    const [name,idx]=b.dataset.step.split(':');
    daw.sequence_enabled=true;
    daw.sequence[name][Number(idx)]=daw.sequence[name][Number(idx)]?0:1;
    previewDrum(name);renderSequencer();syncDawInputs();renderRuler();scheduleDawSave();
  });
}
$('#toggleSequence').onclick=()=>{
  if(!daw)return;daw.sequence_enabled=!daw.sequence_enabled;renderSequencer();syncDawInputs();renderRuler();renderTimeline();scheduleDawSave();
};
$('#clearSequence').onclick=()=>{
  if(!daw)return;daw.sequence=structuredClone(sequencePresets.empty);daw.sequence_enabled=false;renderSequencer();syncDawInputs();renderRuler();scheduleDawSave();
};
$('#randomSequence').onclick=()=>{
  if(!daw)return;
  for(const name of Object.keys(daw.sequence)){
    const chance=name==='hat'?.55:name==='cow'?.26:.22;
    daw.sequence[name]=Array.from({length:16},(_,i)=>(i%4===0&&name==='snare'?0:(Math.random()<chance?1:0)));
  }
  daw.sequence_enabled=true;renderSequencer();syncDawInputs();renderRuler();scheduleDawSave();
};
$('#sequencePreset').onchange=e=>{
  if(!daw||!e.target.value)return;
  daw.sequence=structuredClone(sequencePresets[e.target.value]||sequencePresets.empty);
  daw.sequence_enabled=e.target.value!=='empty';
  renderSequencer();syncDawInputs();renderRuler();renderTimeline();scheduleDawSave();e.target.value='';
};
$$('[data-pad]').forEach(b=>b.onclick=()=>previewDrum(b.dataset.pad));

/* ---------------- Web Audio preview ---------------- */
async function getAudioContext(){
  if(!audioCtx)audioCtx=new (window.AudioContext||window.webkitAudioContext)();
  if(audioCtx.state==='suspended')await audioCtx.resume();
  return audioCtx;
}
async function decodeClip(c){
  if(decodedBuffers.has(c.id))return decodedBuffers.get(c.id);
  const blob=await api(`/api/daw/projects/${daw.id}/clips/${c.id}/audio`);
  const ab=await blob.arrayBuffer(),ctx=await getAudioContext(),buf=await ctx.decodeAudioData(ab.slice(0));
  decodedBuffers.set(c.id,buf);return buf;
}
function makeReverbImpulse(ctx){
  if(reverbImpulse)return reverbImpulse;
  const len=Math.floor(ctx.sampleRate*2.2),buffer=ctx.createBuffer(2,len,ctx.sampleRate);
  for(let ch=0;ch<2;ch++){const d=buffer.getChannelData(ch);for(let i=0;i<len;i++)d[i]=(Math.random()*2-1)*Math.pow(1-i/len,2.7);}
  reverbImpulse=buffer;return buffer;
}
function connectTrackGraph(ctx,t,master){
  const hp=ctx.createBiquadFilter();hp.type='highpass';hp.frequency.value=t.fx.highpass;
  const lp=ctx.createBiquadFilter();lp.type='lowpass';lp.frequency.value=t.fx.lowpass;
  const bass=ctx.createBiquadFilter();bass.type='lowshelf';bass.frequency.value=90;bass.gain.value=t.fx.bass;
  const pan=ctx.createStereoPanner();pan.pan.value=t.pan;
  const dry=ctx.createGain();dry.gain.value=1;
  hp.connect(lp).connect(bass).connect(pan).connect(dry).connect(master);

  if((t.fx.delay||0)>.01){
    const delay=ctx.createDelay(1);delay.delayTime.value=.18;
    const feedback=ctx.createGain();feedback.gain.value=Math.min(.65,(t.fx.delay||0)*.58);
    const wet=ctx.createGain();wet.gain.value=Math.min(.45,(t.fx.delay||0)*.45);
    pan.connect(delay).connect(feedback).connect(delay);delay.connect(wet).connect(master);
  }
  if((t.fx.reverb||0)>.01){
    const convolver=ctx.createConvolver();convolver.buffer=makeReverbImpulse(ctx);
    const wet=ctx.createGain();wet.gain.value=Math.min(.6,(t.fx.reverb||0)*.55);
    pan.connect(convolver).connect(wet).connect(master);
  }
  return hp;
}
function stopPreview(keepPosition=false){
  for(const s of activeSources){try{s.stop()}catch(_){}}activeSources=[];
  playing=false;cancelAnimationFrame(rafId);masterAnalyser=null;
  if(!keepPosition)playPosition=0;
  $('#dawPlay').textContent='▶';$('#masterMeterFill').style.width='0%';updateTransport();updatePlayheads();
}
async function playPreview(){
  if(!daw)return;
  if(!(daw.clips||[]).length && !daw.sequence_enabled && !metronomeOn){toast('Добавь аудио на дорожку или включи sequencer',false);return;}
  if(playing){
    playPosition=Math.min(projectDuration(),playFrom+(audioCtx.currentTime-playStartedAt));
    stopPreview(true);return;
  }
  const ctx=await getAudioContext();
  const master=ctx.createGain();master.gain.value=daw.master_gain??1;
  const compressor=ctx.createDynamicsCompressor();compressor.threshold.value=-7;compressor.knee.value=8;compressor.ratio.value=4;compressor.attack.value=.006;compressor.release.value=.18;
  masterAnalyser=ctx.createAnalyser();masterAnalyser.fftSize=256;
  master.connect(compressor).connect(masterAnalyser).connect(ctx.destination);

  const anySolo=daw.tracks.some(t=>t.solo);
  const activeTracks=new Set(daw.tracks.filter(t=>!t.mute&&(!anySolo||t.solo)).map(t=>t.id));
  const from=playPosition>=projectDuration()-.05?0:playPosition;
  playFrom=from;playStartedAt=ctx.currentTime;playing=true;$('#dawPlay').textContent='Ⅱ';

  for(const c of daw.clips){
    if(!activeTracks.has(c.track_id))continue;
    const t=track(c.track_id),clipEnd=c.start+audibleDuration(c);
    if(clipEnd<=from)continue;
    try{
      const buf=await decodeClip(c),src=ctx.createBufferSource(),clipGain=ctx.createGain();
      src.buffer=buf;src.playbackRate.value=Math.max(.5,Math.min(2,c.rate||1));
      const graph=connectTrackGraph(ctx,t,master);
      clipGain.gain.value=(c.gain??1)*(t.volume??1);src.connect(clipGain).connect(graph);

      const lateVisual=Math.max(0,from-c.start);
      const rate=src.playbackRate.value;
      const when=ctx.currentTime+Math.max(0,c.start-from);
      const offset=(c.trim_start||0)+lateVisual*rate;
      const sourceDur=Math.max(.02,(c.trim_end??c.duration)-offset);
      const visualDur=sourceDur/rate;

      const base=(c.gain??1)*(t.volume??1);
      const fadeIn=Math.max(0,c.fade_in||0),fadeOut=Math.max(0,c.fade_out||0);
      if(lateVisual<=.001&&fadeIn>.01){
        clipGain.gain.setValueAtTime(0,when);clipGain.gain.linearRampToValueAtTime(base,when+Math.min(fadeIn,visualDur/2));
      }
      if(fadeOut>.01){
        const fadeStart=when+Math.max(0,visualDur-Math.min(fadeOut,visualDur/2));
        clipGain.gain.setValueAtTime(base,fadeStart);clipGain.gain.linearRampToValueAtTime(0,when+visualDur);
      }
      src.start(when,offset,sourceDur);activeSources.push(src);
    }catch(e){console.warn('clip preview',e);}
  }

  if(daw.sequence_enabled)scheduleSequence(ctx,master,from,projectDuration());
  if(metronomeOn)scheduleMetronome(ctx,master,from,projectDuration());
  updateClockLoop();
}
function updateClockLoop(){
  if(!playing)return;
  playPosition=Math.min(projectDuration(),playFrom+(audioCtx.currentTime-playStartedAt));
  updateTransport();updatePlayheads();updateMeter();
  if(playPosition>=projectDuration()-.01){
    if(loopPreview){stopPreview();playPosition=0;playPreview();return;}
    stopPreview();return;
  }
  rafId=requestAnimationFrame(updateClockLoop);
}
function updateTransport(){
  $('#dawTime').textContent=fmtTime(playPosition);
  $('#dawLength').textContent=`/ ${fmtShort(projectDuration())}`;
}
function updateMeter(){
  if(!masterAnalyser)return;
  const data=new Uint8Array(masterAnalyser.fftSize);masterAnalyser.getByteTimeDomainData(data);
  let sum=0;for(const v of data){const n=(v-128)/128;sum+=n*n;}
  const rms=Math.sqrt(sum/data.length),pct=Math.min(100,Math.max(2,rms*180));
  $('#masterMeterFill').style.width=`${pct}%`;
}
$('#dawPlay').onclick=playPreview;
$('#dawStop').onclick=()=>stopPreview();

function drumHit(ctx,name,when,dest,level=1){
  if(name==='kick'||name==='cow'){
    const osc=ctx.createOscillator(),g=ctx.createGain();
    osc.type=name==='kick'?'sine':'square';
    osc.frequency.setValueAtTime(name==='kick'?145:560,when);
    osc.frequency.exponentialRampToValueAtTime(name==='kick'?48:510,when+(name==='kick'?.22:.12));
    g.gain.setValueAtTime((name==='kick'?.7:.18)*level,when);g.gain.exponentialRampToValueAtTime(.001,when+(name==='kick'?.38:.2));
    osc.connect(g).connect(dest);osc.start(when);osc.stop(when+.42);activeSources.push(osc);
  }else{
    const len=Math.floor(ctx.sampleRate*(name==='snare'?.18:.07)),buffer=ctx.createBuffer(1,len,ctx.sampleRate),d=buffer.getChannelData(0);
    for(let i=0;i<len;i++)d[i]=(Math.random()*2-1)*(1-i/len);
    const src=ctx.createBufferSource();src.buffer=buffer;
    const f=ctx.createBiquadFilter();f.type='highpass';f.frequency.value=name==='hat'?5500:1200;
    const g=ctx.createGain();g.gain.value=(name==='hat'?.12:.3)*level;
    src.connect(f).connect(g).connect(dest);src.start(when);activeSources.push(src);
  }
}
function scheduleSequence(ctx,dest,from,to){
  if(!daw.sequence_enabled)return;
  const step=60/daw.bpm/4,bar=step*16,first=Math.floor(from/bar)*bar;
  for(let base=first;base<to+.001;base+=bar){
    for(const [name,row] of Object.entries(daw.sequence)){
      row.forEach((on,i)=>{
        const t=base+i*step;
        if(on&&t>=from&&t<to)drumHit(ctx,name,ctx.currentTime+(t-from),dest,daw.sequence_gain);
      });
    }
  }
}
function scheduleMetronome(ctx,dest,from,to){
  const beat=60/daw.bpm,first=Math.floor(from/beat)*beat;
  for(let t=first;t<to+.001;t+=beat){
    if(t<from)continue;
    const osc=ctx.createOscillator(),g=ctx.createGain();osc.frequency.value=((Math.round(t/beat)%4)===0)?1100:760;
    g.gain.setValueAtTime(.11,ctx.currentTime+(t-from));g.gain.exponentialRampToValueAtTime(.001,ctx.currentTime+(t-from)+.045);
    osc.connect(g).connect(dest);osc.start(ctx.currentTime+(t-from));osc.stop(ctx.currentTime+(t-from)+.05);activeSources.push(osc);
  }
}
async function previewDrum(name){
  const ctx=await getAudioContext(),g=ctx.createGain();g.connect(ctx.destination);drumHit(ctx,name,ctx.currentTime+.01,g,daw?.sequence_gain||.7);
}

/* ---------------- Export ---------------- */
$('#renderMixBtn').onclick=async()=>{
  if(!daw)return;
  await saveDaw();
  const fmt=$('#exportFormat').value;
  busy(true,'Рендерю финальный микс…');
  try{
    const blob=await api(`/api/daw/projects/${daw.id}/render?fmt=${encodeURIComponent(fmt)}`,{method:'POST'});
    downloadBlob(blob,`${daw.name||'TagPhonk'}.${fmt}`);toast('Микс готов');haptic();
  }catch(e){toast(e.message,false);haptic('error');}
  finally{busy(false);}
};
function downloadBlob(blob,name){
  const u=URL.createObjectURL(blob),a=document.createElement('a');a.href=u;a.download=name;
  document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(u),5000);
}

/* ---------------- Desktop shortcuts ---------------- */
window.addEventListener('keydown',e=>{
  const tag=document.activeElement?.tagName;
  if(['INPUT','TEXTAREA','SELECT'].includes(tag))return;
  const dawVisible=$('[data-view="daw"]')?.classList.contains('active');
  if(!dawVisible)return;
  if(e.code==='Space'){e.preventDefault();playPreview();}
  if((e.key==='Delete'||e.key==='Backspace')&&selectedClipId){e.preventDefault();deleteSelectedClip();}
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='d'&&selectedClipId){e.preventDefault();duplicateSelectedClip();}
  if(e.key.toLowerCase()==='s'&&selectedClipId){e.preventDefault();splitSelectedClip();}
});

/* ---------------- Init ---------------- */
async function init(){
  if(!initData){$('#hello').textContent='Открой через Telegram';toast('Открой Studio из Telegram',false);return;}
  try{
    const me=await api('/api/studio/me');$('#hello').textContent=me.user?.first_name?`Привет, ${me.user.first_name}`:'Studio';
    $('#projectsCount').textContent=me.stats?.projects||0;$('#processedSize').textContent=fmtMb(me.stats?.bytes_processed);
    const s=me.settings||{};$('#filenameTemplate').value=s.filename_template||'{artist} - {title}';$('#defaultGenre').value=s.default_genre||'Phonk';$('#removeComments').checked=!!s.remove_comments;$('#normalizeCover').checked=!!s.normalize_cover;
    await loadProjects();
  }catch(e){$('#hello').textContent='Ошибка';toast(e.message,false);}
}
init();
