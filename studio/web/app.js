
const tg = window.Telegram?.WebApp;
if (tg) {
  tg.ready(); tg.expand();
  try { tg.setHeaderColor('#08090d'); tg.setBackgroundColor('#08090d'); } catch (_) {}
}
const initData = tg?.initData || '';
const authHeaders = {'X-Telegram-Init-Data': initData};

let session = null, coverUrl = null;
let daw = null, selectedTrackId = null, selectedClipId = null;
let dawSaveTimer = null, pxPerSecond = 52;
let audioCtx = null, activeSources = [], playStartedAt = 0, playFrom = 0, playPosition = 0, playing = false, rafId = null;
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
  busy(true,'Создаю Mini DAW…');
  try{
    daw=await api('/api/daw/projects',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'Untitled Mix'})});
    selectedTrackId=daw.tracks[0]?.id||null;
    renderDaw();
  }catch(e){toast(e.message,false);}
  finally{busy(false);}
}
$('#createDawBtn').onclick=ensureDaw;

function projectDuration(){
  if(!daw)return 0;
  let end=0;
  for(const c of daw.clips||[]) end=Math.max(end,(c.start||0)+Math.max(.03,(c.trim_end||c.duration)-(c.trim_start||0)));
  const bar=60/(Number(daw.bpm)||130)*4;
  if(Object.values(daw.sequence||{}).some(r=>r.some(Boolean))) end=Math.max(end,bar*4);
  return Math.max(end,bar);
}
function dawPayload(){
  return {
    name:daw.name,bpm:Number(daw.bpm),master_gain:Number(daw.master_gain),normalize:!!daw.normalize,sequence_gain:Number(daw.sequence_gain),
    tracks:daw.tracks,clips:daw.clips,sequence:daw.sequence
  };
}
function scheduleDawSave(){
  clearTimeout(dawSaveTimer);
  dawSaveTimer=setTimeout(saveDaw,260);
}
async function saveDaw(){
  if(!daw)return;
  try{
    daw=await api(`/api/daw/projects/${daw.id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(dawPayload())});
    renderTimeline(); renderMixer(); renderSequencer(); syncDawInputs();
  }catch(e){toast(e.message,false);}
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
  if(tr){selectedTrackId=tr.id;$('#selectedTrackLabel').textContent=tr.name;}
}
function renderDaw(){
  if(!daw)return;
  syncDawInputs(); renderRuler(); renderTimeline(); renderMixer(); renderSequencer();
}
$('#dawName').onchange=e=>{if(daw){daw.name=e.target.value;scheduleDawSave();}};
$('#dawBpm').onchange=e=>{if(daw){daw.bpm=Math.max(50,Math.min(220,Number(e.target.value)||130));renderDaw();scheduleDawSave();}};
$('#masterGain').oninput=e=>{if(daw){daw.master_gain=Number(e.target.value);scheduleDawSave();}};
$('#normalizeMix').onchange=e=>{if(daw){daw.normalize=e.target.checked;scheduleDawSave();}};
$('#sequenceGain').oninput=e=>{if(daw){daw.sequence_gain=Number(e.target.value);scheduleDawSave();}};
$('#dawZoom').oninput=e=>{pxPerSecond=Number(e.target.value);renderRuler();renderTimeline();};

$('#addTrackBtn').onclick=async()=>{
  if(!daw)return; busy(true,'Добавляю дорожку…');
  try{daw=await api(`/api/daw/projects/${daw.id}/tracks`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:`Track ${daw.tracks.length+1}`})});selectedTrackId=daw.tracks.at(-1).id;renderDaw();}
  catch(e){toast(e.message,false);}finally{busy(false);}
};

$('#dawFileInput').onchange=e=>{ const f=e.target.files?.[0]; if(f) uploadDawClip(f); e.target.value=''; };
async function uploadDawClip(file){
  if(!daw)await ensureDaw();
  if(!selectedTrackId)selectedTrackId=daw.tracks[0]?.id;
  const fd=new FormData();fd.append('file',file);busy(true,'Добавляю аудио…');
  try{
    daw=await api(`/api/daw/projects/${daw.id}/clips?track_id=${encodeURIComponent(selectedTrackId)}`,{method:'POST',body:fd});
    renderDaw();toast('Клип добавлен');haptic();
  }catch(e){toast(e.message,false);haptic('error');}finally{busy(false);}
}
async function currentEditorToDaw(){
  if(!session){toast('Сначала открой MP3 в редакторе',false);return;}
  if(!daw)await ensureDaw();
  busy(true,'Переношу MP3 в DAW…');
  try{
    const blob=await api(`/api/studio/editor/${session.session_id}/download`);
    const file=new File([blob],session.download_name||'track.mp3',{type:'audio/mpeg'});
    await uploadDawClip(file); showView('daw');
  }catch(e){toast(e.message,false);}finally{busy(false);}
}
$('#currentToDawBtn').onclick=currentEditorToDaw;
$('#sendToDawBtn').onclick=currentEditorToDaw;

/* ---------------- Timeline ---------------- */
function renderRuler(){
  if(!daw)return;
  const length=Math.max(16,Math.ceil(projectDuration()+4));
  const width=length*pxPerSecond;
  let marks='';
  for(let s=0;s<=length;s+=2) marks+=`<span style="left:${s*pxPerSecond}px">${s}s</span>`;
  $('#timelineRuler').innerHTML=`<div style="width:${width}px">${marks}</div>`;
}
function renderTimeline(){
  if(!daw)return;
  const length=Math.max(16,Math.ceil(projectDuration()+4));
  const width=length*pxPerSecond;
  const clipsByTrack={};
  for(const t of daw.tracks)clipsByTrack[t.id]=[];
  for(const c of daw.clips)(clipsByTrack[c.track_id]||=[]).push(c);

  $('#timeline').innerHTML=daw.tracks.map((t,ti)=>{
    const clips=(clipsByTrack[t.id]||[]).map(c=>{
      const dur=Math.max(.08,(c.trim_end-c.trim_start));
      const left=c.start*pxPerSecond, w=Math.max(40,dur*pxPerSecond);
      const selected=c.id===selectedClipId?' selected':'';
      return `<button class="timeline-clip${selected}" data-clip="${c.id}" style="left:${left}px;width:${w}px"><b>${esc(c.filename)}</b><span>${fmtShort(dur)}</span><i></i></button>`;
    }).join('');
    return `<div class="timeline-row ${t.id===selectedTrackId?'selected-track':''}" data-track="${t.id}">
      <button class="track-head" data-select-track="${t.id}"><b>${esc(t.name)}</b><span>${t.mute?'MUTE':t.solo?'SOLO':'Track'}</span></button>
      <div class="track-lane" style="width:${width}px">${clips}</div>
    </div>`;
  }).join('');
  $$('[data-select-track]').forEach(b=>b.onclick=()=>{selectedTrackId=b.dataset.selectTrack;renderTimeline();syncDawInputs();});
  $$('[data-clip]').forEach(el=>{el.onclick=e=>{if(el.dataset.dragged==='1'){el.dataset.dragged='0';return;}selectClip(el.dataset.clip);}; enableClipDrag(el);});
  renderClipInspector();
}
function selectClip(id){selectedClipId=id;const c=daw.clips.find(x=>x.id===id);if(c)selectedTrackId=c.track_id;renderTimeline();renderClipInspector();syncDawInputs();}
function renderClipInspector(){
  const c=daw?.clips?.find(x=>x.id===selectedClipId);
  $('#clipInspector').classList.toggle('hidden',!c);
  if(!c)return;
  $('#clipStart').value=c.start.toFixed(2);$('#clipTrimStart').value=c.trim_start.toFixed(2);$('#clipTrimEnd').value=c.trim_end.toFixed(2);$('#clipGain').value=c.gain;
}
for(const id of ['clipStart','clipTrimStart','clipTrimEnd','clipGain']){
  $(`#${id}`).oninput=e=>{
    const c=daw?.clips?.find(x=>x.id===selectedClipId);if(!c)return;
    if(id==='clipStart')c.start=Math.max(0,Number(e.target.value)||0);
    if(id==='clipTrimStart')c.trim_start=Math.max(0,Math.min(c.duration-.03,Number(e.target.value)||0));
    if(id==='clipTrimEnd')c.trim_end=Math.max(c.trim_start+.03,Math.min(c.duration,Number(e.target.value)||c.duration));
    if(id==='clipGain')c.gain=Number(e.target.value);
    renderRuler();renderTimeline();scheduleDawSave();
  };
}
$('#deleteClipBtn').onclick=async()=>{
  if(!daw||!selectedClipId)return;busy(true,'Удаляю клип…');
  try{daw=await api(`/api/daw/projects/${daw.id}/clips/${selectedClipId}`,{method:'DELETE'});decodedBuffers.delete(selectedClipId);selectedClipId=null;renderDaw();}
  catch(e){toast(e.message,false);}finally{busy(false);}
};
function enableClipDrag(el){
  let startX=0,startVal=0,moved=false;
  el.addEventListener('pointerdown',e=>{
    if(!daw)return;const c=daw.clips.find(x=>x.id===el.dataset.clip);if(!c)return;
    startX=e.clientX;startVal=c.start;moved=false;el.setPointerCapture?.(e.pointerId);
    const move=ev=>{const dx=ev.clientX-startX;if(Math.abs(dx)>3)moved=true;c.start=Math.max(0,Math.round((startVal+dx/pxPerSecond)*20)/20);el.style.left=`${c.start*pxPerSecond}px`;};
    const up=()=>{window.removeEventListener('pointermove',move);window.removeEventListener('pointerup',up);if(moved){el.dataset.dragged='1';renderRuler();scheduleDawSave();}};
    window.addEventListener('pointermove',move);window.addEventListener('pointerup',up,{once:true});
  });
}

/* ---------------- Mixer ---------------- */
function renderMixer(){
  if(!daw)return;
  $('#mixer').innerHTML=daw.tracks.map(t=>`<div class="mixer-strip ${t.id===selectedTrackId?'active':''}" data-mix-track="${t.id}">
    <div class="mixer-head"><input data-mix-name="${t.id}" value="${esc(t.name)}"><div><button data-mute="${t.id}" class="${t.mute?'on':''}">M</button><button data-solo="${t.id}" class="${t.solo?'on solo':''}">S</button></div></div>
    <label>VOL <input data-vol="${t.id}" type="range" min="0" max="2" step=".01" value="${t.volume}"></label>
    <label>PAN <input data-pan="${t.id}" type="range" min="-1" max="1" step=".01" value="${t.pan}"></label>
    <label>BASS <input data-bass="${t.id}" type="range" min="-12" max="12" step=".5" value="${t.fx.bass}"></label>
    <label>REVERB <input data-reverb="${t.id}" type="range" min="0" max="1" step=".01" value="${t.fx.reverb}"></label>
    <details><summary>Filter</summary>
      <label>HP <input data-hp="${t.id}" type="range" min="20" max="8000" step="10" value="${t.fx.highpass}"></label>
      <label>LP <input data-lp="${t.id}" type="range" min="400" max="20000" step="50" value="${t.fx.lowpass}"></label>
      <label>DELAY <input data-delay="${t.id}" type="range" min="0" max="1" step=".01" value="${t.fx.delay}"></label>
    </details>
  </div>`).join('');
  $$('[data-mix-track]').forEach(s=>s.onclick=e=>{if(e.target.tagName!=='INPUT'&&e.target.tagName!=='BUTTON'){selectedTrackId=s.dataset.mixTrack;renderMixer();renderTimeline();syncDawInputs();}});
  $$('[data-mix-name]').forEach(i=>i.onchange=()=>{const t=track(i.dataset.mixName);t.name=i.value.trim()||t.name;renderTimeline();syncDawInputs();scheduleDawSave();});
  bindTrackRange('vol','volume');bindTrackRange('pan','pan');bindTrackFx('bass','bass');bindTrackFx('reverb','reverb');bindTrackFx('hp','highpass');bindTrackFx('lp','lowpass');bindTrackFx('delay','delay');
  $$('[data-mute]').forEach(b=>b.onclick=()=>{const t=track(b.dataset.mute);t.mute=!t.mute;renderMixer();renderTimeline();scheduleDawSave();});
  $$('[data-solo]').forEach(b=>b.onclick=()=>{const t=track(b.dataset.solo);t.solo=!t.solo;renderMixer();renderTimeline();scheduleDawSave();});
}
function track(id){return daw.tracks.find(t=>t.id===id);}
function bindTrackRange(attr,key){$$(`[data-${attr}]`).forEach(i=>i.oninput=()=>{track(i.dataset[attr])[key]=Number(i.value);scheduleDawSave();});}
function bindTrackFx(attr,key){$$(`[data-${attr}]`).forEach(i=>i.oninput=()=>{track(i.dataset[attr]).fx[key]=Number(i.value);scheduleDawSave();});}

/* ---------------- Sequencer ---------------- */
const seqLabels={kick:'KICK',snare:'SNARE',hat:'HAT',cow:'COWBELL'};
function renderSequencer(){
  if(!daw)return;
  $('#sequencer').innerHTML=Object.entries(seqLabels).map(([name,label])=>`<div class="seq-row"><b>${label}</b>${daw.sequence[name].map((on,i)=>`<button data-step="${name}:${i}" class="${on?'on':''}${i%4===0?' beat':''}"></button>`).join('')}</div>`).join('');
  $$('[data-step]').forEach(b=>b.onclick=()=>{const [name,idx]=b.dataset.step.split(':');daw.sequence[name][Number(idx)]=daw.sequence[name][Number(idx)]?0:1;b.classList.toggle('on');previewDrum(name);scheduleDawSave();renderRuler();});
}

/* ---------------- Web Audio preview ---------------- */
async function getAudioContext(){
  if(!audioCtx)audioCtx=new (window.AudioContext||window.webkitAudioContext)();
  if(audioCtx.state==='suspended')await audioCtx.resume();
  return audioCtx;
}
async function decodeClip(c){
  if(decodedBuffers.has(c.id))return decodedBuffers.get(c.id);
  const blob=await api(`/api/daw/projects/${daw.id}/clips/${c.id}/audio`);
  const ab=await blob.arrayBuffer();const ctx=await getAudioContext();const buf=await ctx.decodeAudioData(ab.slice(0));decodedBuffers.set(c.id,buf);return buf;
}
function stopPreview(keepPosition=false){
  for(const s of activeSources){try{s.stop()}catch(_){}}activeSources=[];playing=false;cancelAnimationFrame(rafId);
  if(!keepPosition)playPosition=0;$('#dawPlay').textContent='▶';updateTransport();
}
async function playPreview(){
  if(!daw)return;if(playing){playPosition=Math.min(projectDuration(),playFrom+(audioCtx.currentTime-playStartedAt));stopPreview(true);return;}
  const ctx=await getAudioContext();const master=ctx.createGain();master.gain.value=daw.master_gain??1;master.connect(ctx.destination);
  const anySolo=daw.tracks.some(t=>t.solo);const activeTracks=new Set(daw.tracks.filter(t=>!t.mute&&(!anySolo||t.solo)).map(t=>t.id));
  const from=playPosition>=projectDuration()-.05?0:playPosition;playFrom=from;playStartedAt=ctx.currentTime;playing=true;$('#dawPlay').textContent='Ⅱ';
  for(const c of daw.clips){
    if(!activeTracks.has(c.track_id))continue;
    const t=track(c.track_id);const clipEnd=c.start+(c.trim_end-c.trim_start);if(clipEnd<=from)continue;
    try{
      const buf=await decodeClip(c);const src=ctx.createBufferSource();src.buffer=buf;
      const hp=ctx.createBiquadFilter();hp.type='highpass';hp.frequency.value=t.fx.highpass;
      const lp=ctx.createBiquadFilter();lp.type='lowpass';lp.frequency.value=t.fx.lowpass;
      const bass=ctx.createBiquadFilter();bass.type='lowshelf';bass.frequency.value=90;bass.gain.value=t.fx.bass;
      const gain=ctx.createGain();gain.gain.value=c.gain*t.volume;
      const pan=ctx.createStereoPanner();pan.pan.value=t.pan;
      src.connect(hp).connect(lp).connect(bass).connect(gain).connect(pan).connect(master);
      const late=Math.max(0,from-c.start);const when=ctx.currentTime+Math.max(0,c.start-from);const offset=c.trim_start+late;const dur=Math.max(.02,c.trim_end-offset);
      src.start(when,offset,dur);activeSources.push(src);
    }catch(e){console.warn('clip preview',e);}
  }
  scheduleSequence(ctx,master,from,projectDuration());
  updateClockLoop();
}
function updateClockLoop(){
  if(!playing)return;playPosition=Math.min(projectDuration(),playFrom+(audioCtx.currentTime-playStartedAt));updateTransport();
  if(playPosition>=projectDuration()-.01){stopPreview();return;}rafId=requestAnimationFrame(updateClockLoop);
}
function updateTransport(){$('#dawTime').textContent=fmtTime(playPosition);$('#dawLength').textContent=`/ ${fmtShort(projectDuration())}`;}
$('#dawPlay').onclick=playPreview;$('#dawStop').onclick=()=>stopPreview();
function drumHit(ctx,name,when,dest,level=1){
  if(name==='kick'||name==='cow'){
    const osc=ctx.createOscillator(),g=ctx.createGain();osc.type=name==='kick'?'sine':'square';osc.frequency.setValueAtTime(name==='kick'?145:560,when);osc.frequency.exponentialRampToValueAtTime(name==='kick'?48:510,when+(name==='kick'?.22:.12));g.gain.setValueAtTime((name==='kick'?.7:.18)*level,when);g.gain.exponentialRampToValueAtTime(.001,when+(name==='kick'?.38:.2));osc.connect(g).connect(dest);osc.start(when);osc.stop(when+.42);activeSources.push(osc);
  }else{
    const len=Math.floor(ctx.sampleRate*(name==='snare'?.18:.07)),buffer=ctx.createBuffer(1,len,ctx.sampleRate),d=buffer.getChannelData(0);for(let i=0;i<len;i++)d[i]=(Math.random()*2-1)*(1-i/len);
    const src=ctx.createBufferSource();src.buffer=buffer;const f=ctx.createBiquadFilter();f.type='highpass';f.frequency.value=name==='hat'?5500:1200;const g=ctx.createGain();g.gain.value=(name==='hat'?.12:.3)*level;src.connect(f).connect(g).connect(dest);src.start(when);activeSources.push(src);
  }
}
function scheduleSequence(ctx,dest,from,to){
  const step=60/daw.bpm/4,bar=step*16;const first=Math.floor(from/bar)*bar;
  for(let base=first;base<to+.001;base+=bar)for(const [name,row] of Object.entries(daw.sequence))row.forEach((on,i)=>{const t=base+i*step;if(on&&t>=from&&t<to)drumHit(ctx,name,ctx.currentTime+(t-from),dest,daw.sequence_gain);});
}
async function previewDrum(name){const ctx=await getAudioContext(),g=ctx.createGain();g.connect(ctx.destination);drumHit(ctx,name,ctx.currentTime+.01,g,daw?.sequence_gain||.7);}

/* ---------------- Export ---------------- */
$('#renderMixBtn').onclick=async()=>{
  if(!daw)return;await saveDaw();const fmt=$('#exportFormat').value;busy(true,'Рендерю финальный микс…');
  try{const blob=await api(`/api/daw/projects/${daw.id}/render?fmt=${encodeURIComponent(fmt)}`,{method:'POST'});downloadBlob(blob,`${daw.name||'TagPhonk'}.${fmt}`);toast('Микс готов');haptic();}
  catch(e){toast(e.message,false);haptic('error');}finally{busy(false);}
};
function downloadBlob(blob,name){const u=URL.createObjectURL(blob),a=document.createElement('a');a.href=u;a.download=name;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(u),5000);}

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
