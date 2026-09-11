
const tg = window.Telegram?.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
  try { tg.setHeaderColor('#09090d'); tg.setBackgroundColor('#09090d'); } catch(e) {}
}

const initData = tg?.initData || '';
const headers = { 'X-Telegram-Init-Data': initData };

function mb(n){ return `${(n/1024/1024).toFixed(1)} MB`; }

async function api(path, options={}) {
  const opts = {...options, headers: {...headers, ...(options.headers||{})}};
  const r = await fetch(path, opts);
  const data = await r.json().catch(()=>({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

async function load() {
  try {
    const [me,status,projects,presets] = await Promise.all([
      api('/api/studio/me'),
      fetch('/api/studio/status').then(r=>r.json()),
      api('/api/studio/projects'),
      api('/api/studio/presets'),
    ]);

    document.getElementById('hello').textContent = `Привет, ${me.user.first_name || 'user'}`;
    document.getElementById('projectsCount').textContent = me.stats.projects || 0;
    document.getElementById('processedSize').textContent = mb(me.stats.bytes_processed || 0);
    document.getElementById('dbMode').textContent = status.database === 'postgres' ? 'Postgres' : 'Local';

    const s = me.settings || {};
    document.getElementById('filenameTemplate').value = s.filename_template || '{artist} - {title}';
    document.getElementById('defaultGenre').value = s.default_genre || 'Phonk';
    document.getElementById('removeComments').checked = !!s.remove_comments;
    document.getElementById('normalizeCover').checked = !!s.normalize_cover;

    renderProjects(projects.items || []);
    renderPresets([...(presets.builtin||[]), ...(presets.custom||[])]);
  } catch(err) {
    document.getElementById('hello').textContent =
      initData ? `Ошибка: ${err.message}` : 'Открой Mini App внутри Telegram';
  }
}

function renderProjects(items) {
  const el = document.getElementById('projects');
  if (!items.length) { el.innerHTML = '<div class="empty">Пока пусто. Пришли MP3 боту.</div>'; return; }
  el.innerHTML = items.map(p => `
    <div class="project">
      <b>${escapeHtml((p.artist||'—') + ' — ' + (p.title||p.filename||'—'))}</b>
      <span>${escapeHtml(p.album||'')} · ${mb(p.size_bytes||0)}</span>
    </div>`).join('');
}

function renderPresets(items) {
  const el = document.getElementById('presets');
  el.innerHTML = items.map(p => `<div class="preset">🧩 ${escapeHtml(p.name||'Preset')}</div>`).join('');
}

function escapeHtml(v) {
  return String(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
}

document.getElementById('saveSettings').onclick = async () => {
  const body = {
    filename_template: document.getElementById('filenameTemplate').value,
    default_genre: document.getElementById('defaultGenre').value,
    remove_comments: document.getElementById('removeComments').checked,
    normalize_cover: document.getElementById('normalizeCover').checked,
  };
  try {
    await api('/api/studio/settings', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)
    });
    tg?.HapticFeedback?.notificationOccurred('success');
    tg?.showPopup?.({title:'TagPhonk',message:'Настройки сохранены',buttons:[{type:'ok'}]});
  } catch(e) { tg?.showAlert?.(e.message); }
};

document.getElementById('reload').onclick = load;
document.getElementById('openBot').onclick = () => {
  if (tg) tg.close();
};

document.querySelectorAll('[data-tool]').forEach(btn => {
  btn.onclick = () => tg?.showPopup?.({
    title:'TagPhonk Studio',
    message:'Эта операция запускается из Studio-меню под текущим MP3 в чате бота.',
    buttons:[{type:'ok'}]
  });
});

load();
