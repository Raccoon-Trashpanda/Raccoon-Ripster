// ======================================================================
// Lightbox click-to-zoom
// Extracted from app.js (mechanical split — same global functions, no behaviour
// change). Loaded AFTER app.js in index.html, so it sees S/api/toast/etc.
// ======================================================================

// ── Lightbox: click-to-zoom for any cover image ─────────────────────────
// Images with ``data-lightbox`` attribute (or a ``data-lightbox-src`` pointing
// at a higher-res URL) open fullscreen on click. Esc or backdrop-click closes.
function openLightbox(src, fallback){
  const box = document.getElementById('lightbox');
  const img = document.getElementById('lightbox-img');
  if(!box || !img) return;
  const hi   = src || fallback;
  const thumb = fallback || src;
  // Раньше сразу ставили крупную (1000×1000) и ждали onerror для отката. Но у
  // части радар-обложек (Tidal 1280, Apple 1000) крупная не ОШИБАЕТСЯ, а ВИСИТ —
  // onerror не срабатывает, и лайтбокс оставался чёрным пустым. Теперь показываем
  // thumbnail МГНОВЕННО (пусто не будет никогда), а крупную грузим в фоне и
  // подменяем, только когда она реально готова. Зависла/битая — остаётся thumb.
  img.onerror = null;
  img.src = thumb || '';
  if (hi && hi !== thumb) {
    const pre = new Image();
    pre.onload = () => { if (box.style.display !== 'none') img.src = hi; };
    pre.src = hi;
  }
  box.style.display = 'flex';
  // Lock body scroll while open
  document.body.style.overflow = 'hidden';
}
function closeLightbox(ev){
  // If called from a click event, only close if the click was on the backdrop
  // (not the image itself — image has stopPropagation).
  if(ev && ev.target && ev.target.id !== 'lightbox' && ev.target.tagName !== 'BUTTON') return;
  const box = document.getElementById('lightbox');
  const img = document.getElementById('lightbox-img');
  if(box) box.style.display = 'none';
  if(img) img.removeAttribute('src');
  document.body.style.overflow = '';
}

// Global delegation: any <img data-lightbox> anywhere in the UI becomes clickable.
// Prefer data-lightbox-src (high-res URL) over the img's own src — cover grids
// often show a small thumbnail but have a bigger cover available.
document.addEventListener('click', (ev) => {
  const img = ev.target.closest?.('img[data-lightbox]');
  if(!img) return;
  ev.preventDefault();
  ev.stopPropagation();
  // Правило по всему Ripster: зум открывает обложку в ~1000×1000. Сетки грузят
  // мелкий thumbnail (трафик), а увеличение апгрейдит адрес до крупного через
  // relCover. Если апгрейд не сработал/недоступен — показываем thumbnail.
  const base = img.dataset.lightboxSrc || img.src;
  const hi   = (typeof relCover === 'function') ? relCover(base, 1000) : base;
  openLightbox(hi, img.src || base);
});
// Esc closes lightbox (and only lightbox — other overlays have their own handlers)
document.addEventListener('keydown', (ev) => {
  if(ev.key === 'Escape') {
    const box = document.getElementById('lightbox');
    if(box && box.style.display !== 'none') closeLightbox();
  }
});

// Simple HTML-escape for text we insert as innerHTML rather than as attributes.
function esc(s){
  return (s==null?'':String(s))
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function resolveQuality(service) {
  const c = S.config || {};
  if(service === 'spotify') {
    const eng = c['spotify-engine'] || 'convert';
    if(eng === 'orpheus_spotify') return c['orpheus-quality'] || 'hifi';
    return c['quality'] || 'alac';
  }
  const svcKey = {
    deezer: 'deezer-quality', qobuz: 'qobuz-quality', tidal: 'tidal-quality',
    beatport: 'beatport-quality', yandex: 'yandex-quality', amazon: 'amazon-quality',
  };
  const k = svcKey[service];
  if (k) return c[k] || {
    deezer: 'flac', qobuz: '27', tidal: 'lossless',
    beatport: 'hifi', yandex: 'flac', amazon: 'High',
  }[service];
  // Services that simply do not have an Apple-style quality ladder. Falling
  // through to the global (Apple) default made a BBC card claim 'alac' before
  // the real list loaded — BBC Sounds only ever yields MP3 320.
  const fixed = { bbc: 'mp3', soundcloud: 'hq' };
  if (fixed[service]) return fixed[service];
  return c['quality'] || 'alac';
}

async function searchAddToQueue(url, title, artist) {
  const task = { url, quality: resolveQuality(detectSvcFromUrl(url) || 'apple'), title, artist };
  const r = await api('POST', '/api/queue/add', task);
  if(r.ok) toast('+ '+title+' → '+t('q.queue_word'));
  else toast(t('t.error_c')+r.detail,'var(--red)');
}

function toggleBatch() {
  const a = document.getElementById('batch-area');
  if(a) a.style.display = a.style.display==='none' ? '' : 'none';
  // Populate batch quality select
  const bq = document.getElementById('batch-quality');
  if(bq && !bq.options.length) {
    QUALITIES.forEach(q => { const o=document.createElement('option'); o.value=q.id; o.textContent=q.label; bq.appendChild(o); });
  }
}

async function addBatch() {
  const text = document.getElementById('batch-urls')?.value||'';
  const qual = document.getElementById('batch-quality')?.value || S.config['quality'] || 'alac';
  const r = await api('POST', '/api/queue/batch', {text, quality: qual});
  if(r.ok){ toast(ti('lb.added_links',{n:r.added})); document.getElementById('batch-urls').value=''; }
  else toast(t('t.error_c')+(r.error||''),'var(--red)');
}

async function convertSpotifyFromSearch() {
  const url = document.getElementById('search-q')?.value?.trim() || prompt(t('lb.paste_sp'));
  if(!url || !url.includes('spotify.com')){ toast(t('lb.enter_sp')); return; }
  const svc = document.getElementById('search-svc')?.value || 'apple';
  toast(t('t.conv_sp'),'var(--blue)');
  const r = await api('POST','/api/convert/spotify',{url, target: svc});
  if(r.ok && r.target?.url){
    toast(t('lb.found_c')+(r.target.title||r.target.url),'var(--green)');
    await api('POST','/api/queue/add',{url: r.target.url, quality: resolveQuality(svc), title: r.target.title});
    toast(t('t.added_q_x'),'var(--green)');
  } else {
    toast(t('t.not_found_c')+(r.error||''),'var(--red)');
  }
}

// ══ HISTORY ══════════════════════════════════════════════════════
async function loadHistory() {
  const svc       = document.getElementById('hist-filter')?.value || '';
  const statusF   = document.getElementById('hist-status-filter')?.value || '';
  const list      = document.getElementById('history-list');
  const emp       = document.getElementById('history-empty');
  const cnt       = document.getElementById('hist-count');
  const r = await api('GET', '/api/history?limit=300' + (svc?'&service='+svc:''));
  let items = r.items || [];
  if(statusF) items = items.filter(h => (h.status || 'done') === statusF);
  if(cnt) cnt.textContent = items.length;
  if(emp) emp.style.display = items.length ? 'none' : '';
  if(!list) return;

  const SVC_COLOR = {apple:'#fc3c44', deezer:'#a238ff', qobuz:'#1b68d3', tidal:'#00d4b3', spotify:'#1db954'};
  const SVC_LABEL = {apple:'A', deezer:'D', qobuz:'Q', tidal:'T', spotify:'S'};
  const statusIcon = s => s === 'error' ? '<span style="color:var(--red);font-weight:700">✗</span>'
                        : s === 'cancelled' ? '<span style="color:var(--orange)">⏹</span>'
                        : '<span style="color:var(--green);font-weight:700">✓</span>';

  list.innerHTML = items.map(h => {
    const col = SVC_COLOR[h.service] || '#888';
    const lbl = SVC_LABEL[h.service] || '?';
    const ts  = h.ts ? new Date(h.ts).toLocaleString('ru') : '';
    const title = esc(h.title || _titleFromUrl(h.url));
    const artist = esc(h.artist || '');
    const tracksInfo = h.tracks > 1 ? ' · '+ti('q.n_tracks',{n:h.tracks}) : '';
    const art = h.artworkUrl ? `<img src="${esc(h.artworkUrl)}" style="width:100%;height:100%;object-fit:cover;border-radius:6px" loading="lazy"/>` : lbl;
    return `
    <div class="hist-row" style="display:flex;align-items:center;gap:12px;padding:10px 12px;background:var(--surface);border:1px solid var(--border);border-radius:10px">
      <div style="width:40px;height:40px;border-radius:7px;background:${col};color:#fff;font-size:13px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0;overflow:hidden">${art}</div>
      <div style="flex:1;min-width:0">
        <div style="display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
          ${statusIcon(h.status || 'done')} <span style="overflow:hidden;text-overflow:ellipsis">${title}</span>
        </div>
        <div style="font-size:11px;color:var(--muted);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
          ${artist ? artist + ' · ' : ''}${ts} · ${(h.quality||'?').toUpperCase()}${tracksInfo}
        </div>
      </div>
      <button onclick="redownload(${esc(JSON.stringify(h.url))}, ${esc(JSON.stringify(h.quality||''))})"
        style="padding:5px 11px;background:rgba(192,132,160,.1);border:1px solid rgba(192,132,160,.2);border-radius:7px;font-size:11px;font-weight:700;color:var(--red);cursor:pointer;font-family:var(--font);white-space:nowrap;flex-shrink:0">
        ↺ ${t('lb.retry')}
      </button>
    </div>`;
  }).join('');
}

async function redownload(url, quality) {
  const r = await api('POST','/api/queue/add',{url, quality});
  if(r.ok) toast(t('t.added_q_x'));
  else toast(t('t.error'),'var(--red)');
}

async function clearHistory() {
  // Period selector: "" = everything, "h:N" = older than N hours, "d:N" = older
  // than N days. Maps to the backend's DELETE /api/history?hours=&days= window.
  const sel = document.getElementById('hist-clear-period');
  const v = sel ? sel.value : '';
  let qs = '', what = t('h.clr_confirm_all') || 'всю историю';
  if (v.startsWith('h:'))      { qs = '?hours=' + v.slice(2); what = ti('lb.hist_older_h',{n:v.slice(2)}); }
  else if (v.startsWith('d:')) { qs = '?days='  + v.slice(2); what = ti('lb.hist_older_d',{n:v.slice(2)}); }
  if(!confirm(t('lb.clear_word') + ' ' + what + '?')) return;
  const r = await api('DELETE','/api/history' + qs);
  loadHistory();
  const n = (r && typeof r.removed !== 'undefined') ? r.removed : '';
  toast(t('lb.hist_clear') + (n !== '' && n !== 'all' ? ` (${n})` : ''));
}

// ══ WATCHLIST ═════════════════════════════════════════════════════
// Держим загруженные подписки здесь, чтобы поиск и переключение «артисты /
// лейблы» перерисовывали список без похода в сеть. `var`, а не `let`: имена
// вроде _wlItems слишком общие, а повторное объявление на верхнем уровне
// роняет ВЕСЬ файл (см. скилл ripster-frontend-file-drift).
var _wlItems = [];
var _wlKind  = 'all';
var _wlFilterTimer = null;

async function loadWatchlist() {
  wlPopulateSvc();                       // выпадашка сервисов из настроенных токенов
  const r = await api('GET','/api/watchlist');
  _wlItems = r.items||[];
  const emp = document.getElementById('wl-empty');
  if(emp) emp.style.display = _wlItems.length?'none':'';
  wlRenderList();
  loadWlSuggestions();
}

// Поиск бьёт по innerHTML всего списка, поэтому НЕ на каждую букву: на радаре
// это уже стоило заметных подтормаживаний на ~3600 узлах (ripster-performance).
function wlFilterChanged() {
  clearTimeout(_wlFilterTimer);
  _wlFilterTimer = setTimeout(wlRenderList, 140);
}

function wlSetKind(kind, btn) {
  _wlKind = kind;
  document.querySelectorAll('.wl-kind-tab').forEach(b => b.classList.toggle('active', b === btn));
  wlRenderList();
}

function _wlVisible() {
  const q = (document.getElementById('wl-search')?.value || '').trim().toLowerCase();
  return _wlItems.filter(w => {
    const kind = (w.kind === 'label') ? 'label' : 'artist';
    if (_wlKind !== 'all' && kind !== _wlKind) return false;
    if (!q) return true;
    // Ищем и по имени, и по ссылке: артиста иногда помнишь по адресу страницы.
    return ((w.name || '') + ' ' + (w.url || '')).toLowerCase().includes(q);
  });
}

function wlRenderList() {
  const list = document.getElementById('wl-list');
  if(!list) return;
  const items = _wlVisible();

  const cnt = document.getElementById('wl-count');
  if (cnt) {
    const labels = _wlItems.filter(w => w.kind === 'label').length;
    cnt.textContent = (items.length === _wlItems.length)
      ? ti('wl.count_all',    {n: _wlItems.length, labels: labels})
      : ti('wl.count_shown',  {n: items.length,    total: _wlItems.length});
  }
  const nm = document.getElementById('wl-nomatch');
  if (nm) nm.style.display = (!items.length && _wlItems.length) ? '' : 'none';

  list.innerHTML = items.map(w => `
    <div style="display:flex;align-items:center;gap:10px;padding:9px 12px;background:var(--surface);border:1px solid var(--border);border-radius:10px;margin-bottom:7px">
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;font-weight:600;color:var(--text)${w.kind==='label'?';cursor:pointer':''}"
          ${w.kind==='label'?`onclick="openLabelPage('${escJ(w.name||'')}')" title="${t('lbl.open_page')}" onmouseover="this.style.textDecoration='underline'" onmouseout="this.style.textDecoration='none'"`:''}>${w.kind==='label'?'🏷 ':''}${esc(w.name||w.url)}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:2px">
          ${w.service||'apple'} · ${w.auto_download?t('wl.auto_dl'):t('wl.notify_only')}
          ${w.last_check?' · '+t('wl.checked_at')+' '+new Date(w.last_check).toLocaleString('ru'):''}
          ${w.last_release?'<span style="color:var(--muted2);margin-left:6px">' +
             // Это ПОСЛЕДНИЙ известный релиз (точка отсчёта), а не новинка —
             // подпись «новый релиз» здесь вводила в заблуждение: запись только
             // что создана, качать нечего, а выглядело как пропущенная загрузка.
             t('wl.last_known') + ': ' + esc(String(w.last_release).slice(0,38)) + '</span>':''}
        </div>
      </div>
      ${w.kind==='label'?`<button onclick="wlDownloadLatest('${w.id}')" title="${t('wl.dl_latest_t')}"
        style="padding:4px 9px;background:rgba(62,207,170,.14);border:1px solid rgba(62,207,170,.3);border-radius:6px;font-size:11px;cursor:pointer;color:var(--green);font-family:var(--font);white-space:nowrap">
        ⬇ ${t('wl.dl_latest')}
      </button>`:''}
      <label style="display:flex;align-items:center;gap:5px;font-size:11px;color:var(--muted);cursor:pointer;white-space:nowrap">
        <input type="checkbox" ${w.auto_download?'checked':''} onchange="wlToggleAuto('${w.id}',this.checked)"/> ${t('wl.auto_short')}
      </label>
      <button onclick="wlRemove('${w.id}')"
        style="padding:4px 8px;background:var(--surface);border:1px solid var(--border);border-radius:6px;font-size:11px;cursor:pointer;color:var(--muted);font-family:var(--font)">
        ✕
      </button>
    </div>`).join('');
}

// Лейбл отслеживается по названию, ссылка ему не нужна — прячем поле, чтобы
// пустой инпут не выглядел как забытое обязательное поле.
function wlKindChanged() {
  const kind = document.getElementById('wl-kind')?.value || 'artist';
  const url  = document.getElementById('wl-url');
  const name = document.getElementById('wl-name');
  if(url)  url.style.display = (kind === 'label') ? 'none' : '';
  if(name) name.placeholder = (kind === 'label') ? t('wl.label_ph') : t('wl.name_ph');
  wlPopulateSvc();
}

// Сервисы, КУДА можно качать, из реально настроенных токенов/аккаунтов. Раньше
// выпадашка знала только apple/soundcloud/deezer — и НЗ-аккаунт Tidal (ранняя
// витрина) в вишлисте выбрать было нельзя, прок терялся. Следим всегда через
// Apple, а `service` = целевой сервис скачивания (см. release-availability-matrix).
function _wlDownloadSvcs() {
  const c = (window.S && S.config) || {};
  // «Авто» — брать из первого доступного по порядку качества (матрица доступности
  // на бэке сама выберет, где релиз уже отдаётся раньше всех).
  const out = [{ v: 'auto', label: t('wl.svc_auto') }];
  out.push({ v: 'apple', label: 'Apple Music' });                 // всегда (враппер/куки)
  if (c['tidal-token'])                                    out.push({ v: 'tidal',  label: 'Tidal' });
  if (c['qobuz-auth-token'] || (c['qobuz-email'] && c['qobuz-password']))
                                                           out.push({ v: 'qobuz',  label: 'Qobuz' });
  if (c['deezer-arl'])                                     out.push({ v: 'deezer', label: 'Deezer' });
  if (c['soundcloud-oauth-token'])                         out.push({ v: 'soundcloud', label: 'SoundCloud' });
  return out;
}

function wlPopulateSvc() {
  const sel = document.getElementById('wl-svc');
  if (!sel) return;
  const svcs = _wlDownloadSvcs();
  const cur  = sel.value;
  sel.innerHTML = svcs.map(s => `<option value="${s.v}">${esc(s.label)}</option>`).join('');
  if (svcs.some(s => s.v === cur)) sel.value = cur;   // сохранить выбор при перерисовке
}

async function wlAdd() {
  const name = document.getElementById('wl-name')?.value?.trim();
  const url  = document.getElementById('wl-url')?.value?.trim();
  const svc  = document.getElementById('wl-svc')?.value||'apple';
  const auto = document.getElementById('wl-auto')?.checked !== false;
  const kind = document.getElementById('wl-kind')?.value || 'artist';
  if(kind === 'label' ? !name : (!name && !url)){ toast(t(kind==='label'?'wl.enter_label':'lb.enter_artist')); return; }
  const r = await api('POST','/api/watchlist',{name,url,service:svc,auto_download:auto,kind});
  if(r.ok){
    // Сервер проверяет лейбл сразу: если каталоги ничего не вернули, запись
    // создана, но следить не за чем — честно сказать, а не молча «добавлено».
    if(r.warning) toast(r.warning,'var(--orange)','',8000);
    else toast(`+ ${name||url} → watchlist` + (r.found ? ` (${r.found})` : ''),'var(--green)');
    loadWatchlist();
    document.getElementById('wl-name').value=''; document.getElementById('wl-url').value='';
  }
  else toast(t('t.error_c')+(r.detail||''),'var(--red)');
}

// Подписка запоминает точку отсчёта, поэтому чекер ждёт СЛЕДУЮЩИЙ релиз.
// Эта кнопка — явное «хочу текущий», чтобы не сидеть в ожидании месяц.
async function wlDownloadLatest(id) {
  toast(t('wl.dl_latest_go'), 'var(--muted)');
  const r = await api('POST', '/api/watchlist/' + id + '/download-latest', {count: 1});
  if (r.ok) {
    toast('⬇ ' + (r.queued || []).join(', '), 'var(--green)');
    if (typeof pullQueue === 'function') pullQueue();
  } else {
    toast(r.error || t('t.error_c'), 'var(--orange)', '', 7000);
  }
}

async function wlRemove(id) {
  await api('DELETE','/api/watchlist/'+id);
  loadWatchlist();
}

async function wlToggleAuto(id, val) {
  // До 22.08.2026 здесь стоял только toast с припиской «Update via re-add
  // (simple)» — то есть галочка показывала сообщение и НЕ ходила на сервер.
  // Снаружи это выглядело как рабочий переключатель: галка вставала, надпись
  // менялась, а при следующей загрузке страницы всё возвращалось. Так у
  // владельца накопились 187 подписок с автоскачиванием, которые нечем было
  // выключить.
  const r = await api('POST', '/api/watchlist/auto', {id, auto_download: val});
  if (!r || r.ok === false) { toast(t('err.generic'), 'var(--red)'); return; }
  toast(val ? t('wl.auto_on') : t('wl.notify_only'));
  loadWatchlist();
}

// Пакетное включение/выключение по видам подписок.
async function wlBulkAuto(scope, val) {
  const r = await api('POST', '/api/watchlist/auto', {scope, auto_download: val});
  if (!r || r.ok === false) { toast(t('err.generic'), 'var(--red)'); return; }
  // Говорим ЧИСЛО, а не «готово»: пакетное действие без счётчика неотличимо от
  // действия вхолостую, и понять, попало ли оно куда надо, нельзя.
  toast(ti('wl.bulk_done', {n: r.changed, total: r.matched}));
  loadWatchlist();
}

async function wlCheckNow() {
  // The WS events (watchlist_check_*) drive the status line now.
  // A toast would be redundant.
  await api('POST','/api/watchlist/check');
}

// ── Follow an artist in one click (artist page / SC channel) ──────────────
// The watchlist can only actually poll Apple artists and SoundCloud channels,
// so a follow from any other service's artist page is watched BY NAME on Apple —
// the backend resolves the id and tells us whether it succeeded.
let _wlIdx = null;   // Map(normalised name -> watchlist item)

const _wlNorm = s => (s||'').trim().toLowerCase().replace(/\s+/g,' ');

async function wlIndex(force) {
  if(_wlIdx && !force) return _wlIdx;
  const m = new Map();
  try {
    const r = await api('GET','/api/watchlist');
    for(const it of (r.items||[])) m.set(_wlNorm(it.name), it);
  } catch(e){ /* offline → treat as "not watched", the button still works */ }
  _wlIdx = m;
  return m;
}

function wlIsWatched(name){ return !!(_wlIdx && _wlIdx.get(_wlNorm(name))); }

function wlFollowButton(service, name, url){
  const on = wlIsWatched(name);
  return `<button onclick="wlToggleArtist('${escJ(service)}','${escJ(name)}','${escJ(url||'')}')"
    style="padding:8px 16px;border-radius:9px;background:${on?'var(--surface)':'transparent'};color:${on?'var(--red)':'var(--muted)'};border:1px solid ${on?'var(--red)':'var(--border)'};font-size:12px;font-weight:700;cursor:pointer;font-family:var(--font);display:inline-flex;align-items:center;gap:6px">
    ${on?'★':'☆'} ${on ? t('wl.following') : t('wl.follow')}
  </button>`;
}

async function wlToggleArtist(service, name, url){
  await wlIndex();
  const existing = _wlIdx.get(_wlNorm(name));
  if(existing){
    await api('DELETE','/api/watchlist/'+existing.id);
    await wlIndex(true);
    toast(ti('wl.unfollowed',{name}));
  } else {
    const sc = service === 'soundcloud';
    const r = await api('POST','/api/watchlist', {
      name, url: sc ? (url||'') : '', service: sc ? 'soundcloud' : 'apple',
      auto_download: false,
    });
    await wlIndex(true);
    if(r && r.ok && r.resolved) toast(ti('wl.followed',{name}), 'var(--green)');
    // Added, but nothing will ever check it — say so instead of a green tick.
    else if(r && r.ok)          toast(ti('wl.follow_unresolved',{name}), 'var(--orange)', '', 5000);
    else                        toast(t('t.error_c')+((r&&r.detail)||''), 'var(--red)');
  }
  if(typeof renderArtistPage === 'function' && typeof Detail !== 'undefined' && Detail.currentArtist)
    renderArtistPage();
  const scFollow = document.getElementById('sc-channel-follow');
  if(scFollow && scFollow.innerHTML) scFollow.innerHTML = wlFollowButton(service, name, url);
  // Only repaint the watchlist view when it is actually on screen — otherwise
  // every follow from the search tab would fire a pointless fetch.
  const wlView = document.getElementById('view-watchlist');
  if(wlView && wlView.style.display !== 'none') loadWatchlist();
}

// ── Follow a LABEL ────────────────────────────────────────────────────────
// Отдельно от артиста, потому что у записи другой `kind`: бэкенд по нему
// выбирает другой способ проверки — по названию через каталоги, а не по id
// артиста, которого у лейбла нет.
function wlLabelFollowButton(name){
  const it = _wlIdx && _wlIdx.get(_wlNorm(name));
  const on = !!(it && it.kind === 'label');
  return `<button onclick="wlToggleLabel('${escJ(name)}')"
    style="padding:8px 16px;border-radius:9px;background:${on?'var(--surface)':'transparent'};color:${on?'var(--green)':'var(--muted)'};border:1px solid ${on?'var(--green)':'var(--border)'};font-size:12px;font-weight:700;cursor:pointer;font-family:var(--font);display:inline-flex;align-items:center;gap:6px">
    ${on?'★':'☆'} ${on ? t('lbl.following') : t('lbl.follow')}
  </button>`;
}

async function wlToggleLabel(name){
  await wlIndex();
  const ex = _wlIdx.get(_wlNorm(name));
  if(ex && ex.kind === 'label'){
    await api('DELETE','/api/watchlist/'+ex.id);
    await wlIndex(true);
    toast(ti('wl.unfollowed',{name}));
  } else {
    // service:'auto' — «качать оттуда, где релиз реально есть»: следим-то мы
    // всегда через Spotify/Deezer, это единственные каталоги, отвечающие на
    // вопрос «что выпустил лейбл».
    const r = await api('POST','/api/watchlist',{name, kind:'label', service:'auto', auto_download:false});
    await wlIndex(true);
    if(r && r.ok && r.found)  toast(ti('wl.followed',{name}), 'var(--green)');
    else if(r && r.ok)        toast(r.warning || ti('wl.follow_unresolved',{name}), 'var(--orange)', '', 7000);
    else                      toast(t('t.error_c')+((r&&r.detail)||''), 'var(--red)');
  }
  if(typeof renderLabelPage === 'function' && typeof Detail !== 'undefined' && Detail.currentLabel)
    renderLabelPage();
  const wlView2 = document.getElementById('view-watchlist');
  if(wlView2 && wlView2.style.display !== 'none') loadWatchlist();
}

// ── Smart suggestions ─────────────────────────────────────────────────────
// Everything here comes from the local stats DB (own downloads + plays), so
// each card states a fact about the user's own library rather than a guess.
let _wlSug = [];

const WL_SVC_LBL = {apple:'Apple Music', soundcloud:'SoundCloud', deezer:'Deezer'};

async function loadWlSuggestions() {
  const box = document.getElementById('wl-sug-box');
  if(!box) return;
  let r;
  try { r = await api('GET','/api/watchlist/suggestions?limit=12'); }
  catch(e){ box.style.display='none'; return; }
  _wlSug = (r && r.suggestions) || [];
  if(!_wlSug.length){ box.style.display='none'; return; }
  box.style.display='';

  const card = (s,i) => `
    <div style="display:flex;align-items:center;gap:10px;padding:8px 10px;background:rgba(0,0,0,.16);border:1px solid var(--border);border-radius:9px;margin-bottom:6px">
      <div style="flex:1;min-width:0">
        <div style="font-size:12px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(s.name)}</div>
        <div style="font-size:10px;color:var(--muted);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
          ${esc(ti(s.reason, s.reason_args||{}))}
        </div>
      </div>
      <span style="font-size:9px;color:var(--muted);border:1px solid var(--border);border-radius:5px;padding:2px 6px;white-space:nowrap">${esc(WL_SVC_LBL[s.service]||s.service)}</span>
      <button onclick="wlSugAccept(${i})" data-i18n-title="wls.add_t" title="Следить"
        style="padding:4px 10px;background:var(--red);color:#fff;border:none;border-radius:6px;font-size:11px;font-weight:700;cursor:pointer;font-family:var(--font)">+</button>
      <button onclick="wlSugDismiss(${i})" data-i18n-title="wls.hide_t" title="Скрыть"
        style="padding:4px 8px;background:transparent;border:1px solid var(--border);border-radius:6px;font-size:11px;cursor:pointer;color:var(--muted);font-family:var(--font)">✕</button>
    </div>`;

  const grp = (kind, labelKey) => {
    const list = _wlSug.map((s,i)=>[s,i]).filter(([s])=>s.kind===kind);
    if(!list.length) return '';
    return `<div style="font-size:10px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin:8px 0 5px">${esc(t(labelKey))}</div>`
         + list.map(([s,i])=>card(s,i)).join('');
  };

  document.getElementById('wl-sug-list').innerHTML =
    grp('top','wls.g_top') + grp('discovery','wls.g_discovery');
  applyLang();
}

async function wlSugAccept(i) {
  const s = _wlSug[i];
  if(!s) return;
  const r = await api('POST','/api/watchlist/suggestions/accept', {
    name: s.name, service: s.service, apple_id: s.apple_id,
    sc_permalink: s.sc_permalink, key: s.key, auto_download: false,
  });
  if(r && r.ok){
    toast(`+ ${s.name} → ${t('v.watchlist')}`, 'var(--green)');
    loadWatchlist();
  } else {
    // Honest failure: the artist could not be resolved, so no dud entry was
    // created that would silently never be checked.
    const k = (r && r.error==='sc_channel_not_found') ? 'wls.e_sc' : 'wls.e_apple';
    toast(ti(k,{name:s.name}), 'var(--red)', '', 4000);
  }
}

async function wlSugDismiss(i) {
  const s = _wlSug[i];
  if(!s) return;
  await api('POST','/api/watchlist/suggestions/dismiss', {key: s.key});
  loadWlSuggestions();
}

async function wlSugReset() {
  await api('POST','/api/watchlist/suggestions/reset');
  loadWlSuggestions();
}

