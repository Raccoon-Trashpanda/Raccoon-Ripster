// ======================================================================
// Settings per-service color picker
// Extracted from app.js (mechanical split — same global functions, no behaviour
// change). Loaded AFTER app.js in index.html, so it sees S/api/toast/etc.
// ======================================================================

// ── Settings → Цвета сервисов: per-service color picker ───────────────────
const _SVC_PICKER_LIST = ['apple','qobuz','tidal','deezer','spotify','soundcloud','bbc','beatport'];
function renderSvcColorGrid() {
  const grid = document.getElementById('svc-color-grid');
  if (!grid) return;
  const cfg = (S.config && S.config['service-colors']) || {};
  grid.innerHTML = _SVC_PICKER_LIST.map(svc => {
    const val = cfg[svc] || SVC_BRAND[svc] || '#888888';
    return `
      <label style="display:flex;align-items:center;gap:8px;padding:6px 10px;background:var(--surface);border:1px solid var(--border);border-radius:8px;cursor:pointer">
        <input type="color" value="${val}" onchange="saveSvcColor('${svc}',this.value)"
          style="width:32px;height:24px;padding:0;border:none;background:transparent;cursor:pointer;flex-shrink:0"/>
        <span style="font-size:12px;font-weight:700;color:${val}">${esc(_svcLabel(svc))}</span>
      </label>`;
  }).join('');
}
async function saveSvcColor(svc, color) {
  const cfg = {...(S.config['service-colors'] || {}), [svc]: color};
  S.config['service-colors'] = cfg;
  try { await api('POST', '/api/config', {'service-colors': cfg}); } catch {}
  renderSvcColorGrid();
  // Re-render dynamic views so the new colour shows everywhere immediately.
  try { renderQueue?.(); } catch {}
  try { _scRender?.(); } catch {}
  try { _applyRelFilter?.(); } catch {}
  try { _libApplyFilter?.(); } catch {}
}
async function resetSvcColors() {
  S.config['service-colors'] = {};
  try { await api('POST', '/api/config', {'service-colors': {}}); } catch {}
  renderSvcColorGrid();
  try { renderQueue?.(); } catch {}
  try { _scRender?.(); } catch {}
  try { _applyRelFilter?.(); } catch {}
}

// Holds pending Spotify picker data by notif id, keyed so button handlers
// can fetch url/quality without stuffing JSON into HTML attributes (which
// breaks on the double-quotes in ``https://``).
const _spPickerData = new Map();

function _showSpotifyChoiceToast(url, quality) {
  const stack = document.getElementById('notif-stack');
  if(!stack) return;
  const id = 'sp_choice_' + Date.now();
  _spPickerData.set(id, { url, quality });

  const el = document.createElement('div');
  el.className = 'notif notif-enter';
  el.id = id;
  el.style.maxWidth = '340px';
  el.innerHTML = `
    <div class="notif-dot" style="background:#1db954;color:#1db954"></div>
    <div class="notif-body">
      <div class="notif-msg">Spotify — конвертировать через:</div>
      <div class="sp-picker-btns" style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
        <button data-target="apple"
          style="padding:5px 11px;background:rgba(192,132,160,.15);border:1px solid rgba(192,132,160,.25);border-radius:8px;font-size:11px;font-weight:700;color:var(--red);cursor:pointer;font-family:var(--font)">Apple Music</button>
        <button data-target="deezer"
          style="padding:5px 11px;background:rgba(162,56,255,.18);border:1px solid rgba(162,56,255,.3);border-radius:8px;font-size:11px;font-weight:700;color:#a238ff;cursor:pointer;font-family:var(--font)">Deezer</button>
        <button data-target="qobuz"
          style="padding:5px 11px;background:rgba(27,104,211,.18);border:1px solid rgba(27,104,211,.3);border-radius:8px;font-size:11px;font-weight:700;color:#1b68d3;cursor:pointer;font-family:var(--font)">Qobuz</button>
      </div>
      <label class="sp-remember" style="display:flex;align-items:center;gap:6px;margin-top:8px;font-size:11px;color:var(--muted);cursor:pointer;user-select:none">
        <input type="checkbox" class="sp-remember-chk" style="accent-color:#1db954"/>
        Запомнить выбор (не спрашивать снова)
      </label>
    </div>
    <div class="notif-close" onclick="_closeNotif('${id}')">✕</div>`;

  // Wire up button handlers via JS — this is the key fix.
  el.querySelectorAll('.sp-picker-btns button').forEach(btn => {
    btn.addEventListener('click', () => {
      const target   = btn.dataset.target;
      const remember = !!el.querySelector('.sp-remember-chk')?.checked;
      _chooseSpTarget(id, target, remember);
    });
  });

  stack.appendChild(el);
  requestAnimationFrame(()=>requestAnimationFrame(()=>el.classList.remove('notif-enter')));
  // Auto-dismiss after 15s — bit longer now that there's a checkbox to read.
  _notifTimers.set(id, setTimeout(()=>_closeNotif(id), 15000));
}

async function _chooseSpTarget(notifId, target, remember) {
  const ctx = _spPickerData.get(notifId);
  if(!ctx) return;                         // already handled or expired
  _spPickerData.delete(notifId);
  _closeNotif(notifId);

  // Persist the preference IMMEDIATELY so if the convert call is slow
  // and the user tries another URL, the new choice is already remembered.
  if(remember) {
    try {
      await api('POST','/api/config',{ 'spotify-default-target': target });
      if(S.config) S.config['spotify-default-target'] = target;
      toast(`Spotify → ${_svcLabel(target)} (запомнено)`, _svcColor(target));
    } catch(e) {
      console.warn('save remember:', e);
    }
  } else {
    toast('Конвертирую Spotify…', '#1db954');
  }

  const r = await api('POST','/api/convert/spotify', { url: ctx.url, target });
  if(r.ok && r.target?.url) {
    await api('POST','/api/queue/add', { url: r.target.url, quality: resolveQuality(target), title: r.target.title });
    document.getElementById('url-input').value = '';
    detectUrlService('');
    toast('+ '+r.target.title, _svcColor(target), _svcLabel(target));
  } else {
    toast('Не найдено на '+_svcLabel(target), 'var(--orange)', r.error||'');
  }
}

function detectSvcFromUrl(url) {
  if(url.includes('music.apple.com'))  return 'apple';
  if(url.includes('qobuz.com'))        return 'qobuz';
  if(url.includes('deezer.com'))       return 'deezer';
  if(url.includes('tidal.com'))        return 'tidal';
  if(url.includes('soundcloud.com'))   return 'soundcloud';
  if(url.includes('spotify.com'))      return 'spotify';
  if(url.includes('beatport.com'))     return 'beatport';
  if(url.includes('music.yandex.'))    return 'yandex';
  if(url.includes('music.amazon.'))    return 'amazon';
  return null;
}

// Show a modal asking which engine/service to use for this URL
function showUrlServiceModal(url, quality, detectedSvc) {
  const existing = document.getElementById('url-svc-modal');
  if(existing) existing.remove();

  const SVC_INFO = {
    apple:    {label:'Apple Music', color:'#fc3c44', engines:['AMD v2','gamdl','zhaarey']},
    qobuz:    {label:'Qobuz',       color:'#1b68d3', engines:['Qobuz API']},
    deezer:   {label:'Deezer',      color:'#a238ff', engines:['Deezer ARL']},
    tidal:    {label:'Tidal',       color:'#00d4b3', engines:['Tidal API']},
    spotify:  {label:'Spotify',     color:'#1db954', engines:['→ Apple Music','→ Deezer','→ Qobuz']},
    beatport: {label:'Beatport',    color:'#01f49c', engines:['OrpheusDL']},
  };

  const svcInfo = SVC_INFO[detectedSvc] || {label:detectedSvc,color:'var(--muted)',engines:['Авто']};
  const shortUrl = url.length > 60 ? url.slice(0,57)+'…' : url;

  const modal = document.createElement('div');
  modal.id = 'url-svc-modal';
  modal.style.cssText = 'position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.7);backdrop-filter:blur(4px)';

  const isSpotify = detectedSvc === 'spotify';
  const targetOptions = isSpotify
    ? ['apple','qobuz','deezer']
    : [detectedSvc];

  const targetBtns = targetOptions.map(t => {
    const ti = SVC_INFO[t]||{label:t,color:'var(--muted)'};
    return `<button onclick="chooseUrlSvc(${JSON.stringify(url)},${JSON.stringify(quality)},${JSON.stringify(detectedSvc)},${JSON.stringify(t)})"
      style="flex:1;padding:10px 14px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);border-radius:9px;cursor:pointer;font-family:var(--font);transition:.15s;text-align:center"
      onmouseover="this.style.borderColor='${ti.color}'" onmouseout="this.style.borderColor='rgba(255,255,255,.12)'">
      <div style="font-size:13px;font-weight:700;color:${ti.color}">${ti.label}</div>
      ${isSpotify?'<div style="font-size:10px;color:var(--muted);margin-top:3px">конвертировать</div>':''}
    </button>`;
  }).join('');

  modal.innerHTML = `<div style="background:var(--surface,#1c1c1e);border:1px solid rgba(255,255,255,.1);border-radius:16px;padding:24px;width:420px;max-width:90vw">
    <div style="font-size:11px;color:var(--muted,#888);margin-bottom:4px">Определён сервис</div>
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:14px">
      <div style="width:10px;height:10px;border-radius:50%;background:${svcInfo.color}"></div>
      <div style="font-size:16px;font-weight:700;color:var(--text)">${svcInfo.label}</div>
    </div>
    <div style="font-size:11px;color:var(--muted,#888);font-family:monospace;background:rgba(0,0,0,.3);border-radius:7px;padding:7px 10px;margin-bottom:16px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${shortUrl}</div>
    <div style="font-size:12px;font-weight:600;color:var(--text);margin-bottom:10px">${isSpotify?'Конвертировать и скачать через:':'Скачать через:'}</div>
    <div style="display:flex;gap:8px;margin-bottom:16px">${targetBtns}</div>
    <div style="display:flex;justify-content:flex-end">
      <button onclick="document.getElementById('url-svc-modal').remove()"
        style="padding:6px 14px;background:transparent;border:1px solid rgba(255,255,255,.1);border-radius:8px;cursor:pointer;font-size:12px;color:var(--muted,#888);font-family:var(--font)">
        Отмена
      </button>
    </div>
  </div>`;

  document.body.appendChild(modal);
  modal.onclick = e => { if(e.target===modal) modal.remove(); };
}

async function chooseUrlSvc(url, quality, srcSvc, targetSvc) {
  const modal = document.getElementById('url-svc-modal');
  if(modal) modal.remove();

  if(srcSvc === 'spotify') {
    // Convert Spotify → target service then add with target-service quality
    toast('Конвертирую Spotify…', '#1db954');
    const r = await api('POST','/api/convert/spotify',{url,target:targetSvc});
    if(r.ok && r.target?.url) {
      await api('POST','/api/queue/add',{url:r.target.url, quality: resolveQuality(targetSvc), title:r.target.title});
      document.getElementById('url-input').value='';
      toast('+ '+r.target.title+' → очередь','#1db954');
    } else {
      toast('Не найдено: '+(r.error||url),'var(--orange)');
    }
    return;
  }

  await _doAddUrl(url, quality, targetSvc);
}

async function _doAddUrl(url, quality, svc) {
  const r = await api('POST','/api/queue/add',{url,quality});
  if(r.ok) {
    document.getElementById('url-input').value='';
    detectUrlService(''); // clear indicator
    toast(r.count > 1 ? `Добавлено ${r.count} треков в очередь` : 'Добавлено в очередь');
    pullQueue();   // reflect immediately even if the WS push is lagging/dead
    // ISRC cross-service check (non-blocking; skip for tidal — no metadata ISRC)
    if(svc !== 'tidal') _checkIsrc(url, svc);
  } else if(r.spotify) {
    // Backend rejected Spotify URL — spotify-engine not set to a direct engine
    toast('Включи OrpheusDL в Settings → Spotify', 'var(--orange)');
  } else if(r.duplicate) {
    toast('Уже в очереди', 'var(--muted)');
  } else {
    toast(r.msg || r.detail || 'Ошибка URL', 'var(--red)');
  }
}

function _isrcQualLabel(svc, m) {
  if (svc === 'qobuz') {
    if (m.hires && m.bit_depth >= 20)
      return `Hi-Res ${m.bit_depth}bit / ${m.sample_rate}kHz`;
    return m.hires ? 'Hi-Res FLAC' : 'FLAC 16bit';
  }
  if (svc === 'tidal') {
    const q = (m.audio_quality || '').toUpperCase();
    if (q === 'MASTER')   return 'MQA Master';
    if (q === 'HI_RES')   return 'Hi-Res';
    if (q === 'LOSSLESS') return 'FLAC';
    if (q === 'HIGH')     return 'AAC 320';
    return 'FLAC';
  }
  if (svc === 'deezer') return 'FLAC / MP3';
  return '';
}

async function _checkIsrc(url, skipSvc = '') {
  try {
    const r = await api('POST', '/api/isrc/resolve', { url, skip: skipSvc });
    if (!r.ok || !r.matches) return;
    const svcs = Object.keys(r.matches);
    if (!svcs.length) return;

    const stack = document.getElementById('notif-stack');
    if (!stack) return;

    const title  = r.title  ? `«${esc(r.title)}»`  : 'Трек';
    const artist = r.artist ? ` — ${esc(r.artist)}` : '';

    const n = document.createElement('div');
    n.className = 'notif';
    // display:block overrides the .notif class's `display:flex` — otherwise the
    // title and the per-service rows lay out horizontally and the title column
    // collapses to one word per line. We want a vertical stack here.
    n.style.cssText = 'display:block;background:var(--surface2);border:1px solid var(--border2);border-radius:10px;padding:10px 14px;font-size:11.5px;width:300px;max-width:calc(100vw - 24px);position:relative';

    const rowsHtml = svcs.map(s =>
      `<div class="isrc-row" data-svc="${s}" style="display:flex;align-items:center;gap:8px;padding:5px 6px;margin:0 -6px;cursor:pointer;border-radius:6px">
        <span style="color:${_svcColor(s)};font-weight:700;min-width:50px">${esc({qobuz:'Qobuz',tidal:'Tidal',deezer:'Deezer'}[s]||s)}</span>
        <span style="color:var(--muted);flex:1;font-size:10.5px">${esc(_isrcQualLabel(s, r.matches[s]))}</span>
        <span style="opacity:.7;font-size:13px" title="Добавить в очередь">⬇</span>
      </div>`
    ).join('');

    n.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:7px">
        <span style="font-weight:600;color:var(--text);line-height:1.3">🔍 ${title}${artist}</span>
        <span class="isrc-x" style="cursor:pointer;color:var(--muted);padding-left:10px;font-size:18px;line-height:1;flex-shrink:0">×</span>
      </div>
      ${rowsHtml}`;

    n.querySelector('.isrc-x').onclick = e => { e.stopPropagation(); n.remove(); };

    n.querySelectorAll('.isrc-row').forEach(row => {
      const svc = row.dataset.svc;
      const m   = r.matches[svc];
      if (!m) return;
      row.addEventListener('mouseenter', () => row.style.background = 'var(--surface3,rgba(255,255,255,.05))');
      row.addEventListener('mouseleave', () => row.style.background = '');
      row.addEventListener('click', async () => {
        const trackUrl = m.track_url || m.url;
        if (!trackUrl) return;
        const res = await api('POST', '/api/queue/add', { url: trackUrl });
        if (res.ok) { toast('Добавлено в очередь'); n.remove(); }
        else toast(res.msg || res.detail || 'Ошибка', 'var(--red)');
      });
    });

    stack.appendChild(n);
    setTimeout(() => { try { n.remove(); } catch(_){} }, 18000);
  } catch(_) {}
}

function addCurrentPage() {
  document.getElementById('url-input').value = location.href;
  addUrl();
}

function renderQueue() {
  const el = document.getElementById('queue-list');
  const empty = document.getElementById('queue-empty');
  if(!S.queue.length){ empty.style.display='flex'; el.innerHTML=''; el.appendChild(empty); return; }
  empty.style.display='none';

  // keep existing items, add/remove as needed
  const existing = new Map([...el.querySelectorAll('.qi')].map(n=>[n.dataset.id,n]));
  const ids = new Set(S.queue.map(t=>t.id));

  // remove gone
  existing.forEach((node,id)=>{ if(!ids.has(id)) node.remove(); });

  S.queue.forEach(task=>{
    // Per-task try/catch: a single malformed task (missing meta etc.) must NOT
    // throw out of forEach and abort the loop — that left every task AFTER it
    // unrendered ("some tasks show, some don't"). Skip the bad one, keep going.
    try {
      if(existing.has(task.id)){
        updateQueueItem(task, existing.get(task.id));
      } else {
        el.appendChild(buildQueueItem(task));
      }
    } catch(e){
      console.error('renderQueue: skipped bad task', task && task.id, e);
    }
  });

  // badge
  const pending = S.queue.filter(t=>t.status==='queued').length;
  const badge = document.getElementById('queue-badge');
  if(pending>0){ badge.textContent=pending; badge.style.display=''; }
  else badge.style.display='none';
}

// Cache of qualities per engine so we don't refetch for every row
const _QUALITIES_BY_ENGINE = {};
async function _qualitiesForEngine(engine) {
  if(!engine) return QUALITIES;
  if(_QUALITIES_BY_ENGINE[engine]) return _QUALITIES_BY_ENGINE[engine];
  try {
    const svcMap = {deezer:'deezer',qobuz:'qobuz',tidal:'tidal',soundcloud:'soundcloud',beatport:'beatport'};
    const svc = svcMap[engine] || 'apple';
    const qs = await (await fetch(`/api/qualities?service=${svc}`)).json();
    _QUALITIES_BY_ENGINE[engine] = Array.isArray(qs) ? qs : QUALITIES;
    return _QUALITIES_BY_ENGINE[engine];
  } catch(e) { return QUALITIES; }
}

function _qualityFor(task) {
  // Look up the per-engine quality list first, fall back to the Apple one.
  const list = _QUALITIES_BY_ENGINE[task.engine] || QUALITIES;
  return list.find(x => x.id === task.quality)
      || { color:'#888', label: (task.quality||'—').toUpperCase(), badge:'—', sub:'' };
}

function _tracksDone(task) {
  // Returns {done, total} using the most accurate source available.
  // Prefer engine's actual current/total (when total > 1, i.e. real track counter).
  // Fall back to estimating from progress% × meta.trackCount.
  const metaTotal = (task.meta?.trackCount || task.meta?.totalTracks || 0);
  const isSingle  = task.meta?.type === 'song' || task.meta?.type === 'track';
  const tc = isSingle ? 1 : metaTotal;

  // StreamripMixin: explicit track-completion counter (total=0 sentinel events)
  if ((task._tracksCompleted || 0) > 0) {
    return { done: task._tracksCompleted, total: tc || task._tracksCompleted };
  }

  const engTotal   = task._progTotal   || 0;
  const engCurrent = task._progCurrent || 0;

  // Engine reports actual track N/M (not percentage 0-100)
  if (engTotal > 1 && engTotal !== 100) {
    const total = tc > 1 ? Math.max(tc, engTotal) : engTotal;
    return { done: engCurrent, total };
  }

  // Only percentage known — estimate
  const pct = Math.max(0, Math.min(100, task.progress || 0));
  if (tc > 1) {
    return { done: pct >= 100 ? tc : Math.floor(pct / 100 * tc), total: tc };
  }
  return { done: 0, total: tc };
}

function _renderBlocks(progress, trackCount, color, isRunning, task) {
  const pct = Math.max(0, Math.min(100, progress || 0));
  const tc  = trackCount || 0;

  // Animated raccoon for ANY running task — sits to the LEFT of the bar/blocks.
  const raccoon = isRunning
    ? `<span class="qi-raccoon" style="display:inline-block;animation:qiRaccoonBob .9s ease-in-out infinite;font-size:14px;line-height:1;margin-right:6px;vertical-align:middle">🦝</span>`
    : '';

  // No track count known — raccoon + 6-char filled bar
  if (!tc) {
    if (!isRunning) return `<span style="opacity:.15;font-family:monospace;letter-spacing:0">░░░░░░░░</span>`;
    const filledW = Math.round(pct / 100 * 6);
    return `<span style="display:inline-flex;align-items:center">${raccoon}` +
      `<span style="font-family:monospace;letter-spacing:0">` +
        `<span style="color:${color}">${'█'.repeat(filledW)}</span>` +
        `<span style="opacity:.15">${'░'.repeat(6 - filledW)}</span>` +
      `</span></span>`;
  }

  // Known track count — show real blocks (capped at 20 visually)
  const n = Math.min(tc, 20);
  const { done: doneTracks } = task ? _tracksDone(task) : { done: pct >= 100 ? tc : Math.floor(pct / 100 * tc) };
  const done = Math.min(Math.round(doneTracks / tc * n), n);

  if (pct >= 100)
    return `<span style="color:${color};font-family:monospace;letter-spacing:0">${'█'.repeat(n)}</span>`;

  if (!isRunning)
    return `<span style="opacity:.15;font-family:monospace;letter-spacing:0">${'█'.repeat(n)}</span>`;

  const empty = Math.max(0, n - done - 1);
  let h = `<span style="display:inline-flex;align-items:center">${raccoon}<span style="font-family:monospace;letter-spacing:0">`;
  if (done  > 0) h += `<span style="color:${color}">${'█'.repeat(done)}</span>`;
  h += `<span class="qi-blk-cur" style="color:${color}">█</span>`;
  if (empty > 0) h += `<span style="opacity:.15">${'█'.repeat(empty)}</span>`;
  h += '</span></span>';
  return h;
}

function _blocksInfo(progress, trackCount, task) {
  const pct = Math.max(0, Math.min(100, progress || 0));
  const n   = trackCount || 0;
  if (n > 1) {
    const { done, total } = task ? _tracksDone(task) : { done: pct >= 100 ? n : Math.floor(pct / 100 * n), total: n };
    return `${done}/${total}`;
  }
  return pct > 0 && pct < 100 ? `${Math.round(pct)}%` : '';
}

// Per-task log lines for the queue-tile panel. Guests get a laconic subset
// (milestones only) so raw engine output never reaches them.
function _visibleLog(task) {
  let lines = task.log || [];
  if (_isGuest()) {
    lines = lines.filter(t => _isMilestone({
      level: /ERROR|✗/.test(t) ? 'error' : /WARN|⚠/.test(t) ? 'warn'
           : /✓|Done|Saved/.test(t) ? 'success' : 'stdout',
      text: t,
    }));
  }
  return lines.slice(-20);
}

function _qiStatusChip(task) {
  if(task.partial || task._partial) return `<span class="qi-st st-partial">⚠ частично</span>`;
  if(task.status==='running') return `<span class="qi-st st-run"><span class="qi-spinner"></span>${task._retry_count?('догрузка '+task._retry_count):(task._auto_retry?'догрузка':'качаю')}</span>`;
  if(task.status==='done')   return `<span class="qi-st st-done">✓ готово</span>`;
  if(task.status==='error')  return `<span class="qi-st st-err">✗ ошибка</span>`;
  if(task.status==='paused') return `<span class="qi-st">⏸ пауза</span>`;
  return `<span class="qi-st st-q">в очереди</span>`;
}

function buildQueueItem(task) {
  // Kick off a quality-list fetch for this engine so the badge updates
  // automatically on the next render pass.
  if(task.engine && !_QUALITIES_BY_ENGINE[task.engine]) {
    _qualitiesForEngine(task.engine).then(()=>updateQueueItem(task));
  }
  const q = _qualityFor(task);
  const m = task.meta;
  const el = document.createElement('div');
  const _isPartial = !!(task.partial || task._partial);
  el.className = `qi ${task.status}${_isPartial?' partial':''}`;
  el.dataset.id = task.id;
  el.dataset.st = task.status;
  el.dataset.partial = String(_isPartial);
  el.style.setProperty('--qi-p', (task.progress||0) + '%');
  const _isSingleTrack = m?.type === 'song' || m?.type === 'track';
  const trackCount = _isSingleTrack ? 1 : (m?.trackCount || m?.totalTracks || 0);
  const trackInfo  = trackCount > 1 ? `${trackCount} треков` : (trackCount === 1 ? '1 трек' : '');
  const hasMeta    = m && (m.title || m.artist);
  const typeLabel  = _typeLabel(m);
  const durInfo    = (m && m.duration && ['soundcloud','bbc'].includes(m.service)) ? _scDur(m.duration) : '';
  const artistLine = hasMeta
    ? [m.artist || '—', m.year, m.label, typeLabel, trackInfo, durInfo].filter(Boolean).join(' · ')
    : (m?.meta_error ? `⚠ ${m.meta_error}` : (m?.enriched ? '' : 'Получаю метаданные…'));
  const logLines   = (task.log || []).slice(-20);
  const logHtml    = logLines.map(l => {
    const lvl = /ERROR|✗/.test(l) ? 'error' : /WARN|⚠/.test(l) ? 'warn' : /✓|Done|Saved/.test(l) ? 'success' : /INFO|STEP/.test(l) ? 'info' : 'stdout';
    return `<div class="ll-${lvl}">${esc(l)}</div>`;
  }).join('');
  // ── compact-row state: progress, count, status chip, actions ──
  const _pct      = Math.max(0, Math.min(100, task.progress||0));
  const _partial  = task.partial || task._partial;
  const _got      = task.got || task._got || (task._files?.length) || 0;
  const _tdone    = trackCount > 1 ? _tracksDone(task).done : 0;
  const _countTxt = _partial ? `${_got||_tdone}/${trackCount}`
                  : trackCount > 1 ? `${_tdone}/${trackCount}`
                  : (_pct>0 && _pct<100 ? `${Math.round(_pct)}%` : '');
  const _showBar  = task.status==='running' || task.status==='queued';
  const _st       = _qiStatusChip(task);
  const _acts =
    (task.service==='spotify' ? `<button class="dl-action-btn" onclick="isrcUpgrade('${task.id}')" title="🎯 Найти лучше (ISRC)" style="color:#c084f5;border-color:#c084f544">🎯</button>` : '') +
    (task.status==='done' ? `<button class="dl-action-btn dl-btn" onclick="downloadTask('${task.id}')" title="${t('btn.download')}" style="color:#3ecfaa;border-color:#3ecfaa44">⬇${(task._dl_file||0)>0?`<span class="dl-cnt">${task._dl_file}</span>`:''}</button><button class="dl-action-btn dl-zip-btn" onclick="downloadTaskZip('${task.id}')" title="${t('q.dl_zip')}" style="color:#7c9fff;border-color:#7c9fff44">📦${(task._dl_zip||0)>0?`<span class="dl-cnt">${task._dl_zip}</span>`:''}</button><button class="dl-action-btn dl-cloud-btn" onclick="uploadToCloud('${task.id}',this)" title="Внешняя ссылка (Gofile)" style="color:#f0a050;border-color:#f0a05044">🔗${(task._dl_gofile||0)>0?`<span class="dl-cnt">${task._dl_gofile}</span>`:''}</button>${(((trackCount||0)>1)||((m?.totalTracks||0)>1)||((m?.trackCount||0)>1))?`<button class="dl-action-btn owner-only" onclick="coderMix('${task.id}')" title="🎚 Ripster Coder: склеить DJ-mix + CUE" style="color:#c9a0ff;border-color:#c9a0ff44">🎚</button>`:''}` : '') +
    ((task.status==='error'||task.status==='cancelled'||_partial) ? `<button class="dl-action-btn" onclick="retryTask('${task.id}')" title="↺ Догрузить недостающие треки (пропускает уже скачанные)" style="color:#ffd60a;border-color:#ffd60a44">↺</button>` : '');

  el.innerHTML = `
    <div class="qi-art">${m?.artworkUrl?`<img src="${esc(m.artworkUrl)}" data-cover data-lightbox onload="this.classList.add('loaded')" style="cursor:zoom-in" loading="lazy"/>`:'🎵'}</div>
    <div class="qi-body">
      <div class="qi-l1">
        <span class="qi-title">${esc(m?.title || _titleFromUrl(task.url))}</span>
        ${artistLine?`<span class="qi-artist">— ${esc(artistLine)}</span>`:''}
      </div>
      <div class="qi-l2">
        <span class="qi-badge" style="background:${q.color}22;color:${q.color}">${esc(q.label)}</span>
        ${_showBar?`<div class="qi-prog-wrap"><div class="qi-prog-bar" style="width:${_pct}%;background:${q.color}"></div></div>`:''}
        ${_countTxt?`<span class="qi-count">${_countTxt}</span>`:''}
        ${_st}
        ${logLines.length?`<button class="qi-log-toggle" onclick="toggleTaskLog('${task.id}',this)" title="показать лог">▶${logLines.length}</button>`:''}
        <div class="qi-actions">${_acts}</div>
      </div>
      ${logLines.length?`<div class="qi-log-panel" id="qi-log-${task.id}">${logHtml}</div>`:''}
    </div>
    <button class="qi-close owner-only" onclick="removeTask('${task.id}')" title="Удалить">✕</button>
  `;
  return el;
}

// CODER / CONVERTER / TAGGER (file converter + tag editor UI) → moved to its own module file (see index.html).

function toggleTaskLog(id, btn) {
  const panel = document.getElementById(`qi-log-${id}`);
  if(!panel) return;
  const open = panel.style.display === 'block';
  panel.style.display = open ? 'none' : 'block';
  if(!open) panel.scrollTop = panel.scrollHeight;
  if(btn) btn.textContent = btn.textContent.replace(/^[▶▼]/, open ? '▶' : '▼');
}

function _typeLabel(m) {
  if(!m) return '';
  if(m.albumType) return m.albumType;
  return {
    albums:'Альбом', album:'Альбом',
    single:'Сингл', ep:'EP', compilation:'Сборник',
    songs:'Трек', song:'Трек', track:'Трек',
    playlists:'Плейлист', playlist:'Плейлист',
    artist:'Артист', 'music-videos':'Видео',
  }[m.type] || '';
}

function _titleFromUrl(url) {
  try {
    const u = new URL(url);
    const parts = u.pathname.split('/').filter(Boolean).map(p => {
      try { return decodeURIComponent(p); } catch(_) { return p; }
    });
    const idx = parts.findIndex(p => ['album','track','song','playlist','artist'].includes(p));
    if(idx >= 0) return `${parts[idx]} · ${parts[idx+1]||''}`;
    return url;
  } catch(_) { return url; }
}


function updateQueueItem(task, el) {
  el = el || document.querySelector(`.qi[data-id="${task.id}"]`);
  if(!el) return;
  const _partial = String(!!(task.partial || task._partial));
  // A status (or partial) change alters the action set + layout → full rebuild,
  // preserving an open log panel. Resume-in-place keeps the SAME card (same id).
  if(el.dataset.st !== task.status || el.dataset.partial !== _partial) {
    const logOpen = el.querySelector('.qi-log-panel')?.style.display === 'block';
    const fresh = buildQueueItem(task);
    if(logOpen){
      const p = fresh.querySelector('.qi-log-panel'); if(p) p.style.display='block';
      const tg = fresh.querySelector('.qi-log-toggle'); if(tg) tg.textContent = tg.textContent.replace(/^▶/,'▼');
    }
    el.replaceWith(fresh);
    return;
  }
  // Same status → cheap in-place update (no flicker, no rebuild).
  const q   = _qualityFor(task);
  const pct = Math.max(0, Math.min(100, task.progress||0));
  el.style.setProperty('--qi-p', pct + '%');
  const bar = el.querySelector('.qi-prog-bar');
  if(bar){ bar.style.width = pct + '%'; bar.style.background = q.color; }
  const m  = task.meta || {};
  const _isSingle = m.type === 'song' || m.type === 'track';
  const tc = _isSingle ? 1 : (m.trackCount || m.totalTracks || 0);
  const cntEl = el.querySelector('.qi-count');
  if(cntEl) cntEl.textContent = tc > 1 ? `${_tracksDone(task).done}/${tc}`
                              : (pct>0 && pct<100 ? `${Math.round(pct)}%` : '');
  const stEl = el.querySelector('.qi-st');
  if(stEl) stEl.outerHTML = _qiStatusChip(task);
  const badgeEl = el.querySelector('.qi-badge');
  if(badgeEl){ badgeEl.textContent = q.label; badgeEl.style.background = q.color+'22'; badgeEl.style.color = q.color; }
  // Metadata enrichment — title / artist / cover appear once they arrive.
  if(m.title || m.artist){
    const titleEl = el.querySelector('.qi-title');
    if(titleEl) titleEl.textContent = m.title || _titleFromUrl(task.url);
    const tcInfo  = tc > 1 ? `${tc} треков` : (tc === 1 ? '1 трек' : '');
    const durInfo = (m.duration && ['soundcloud','bbc'].includes(m.service)) ? _scDur(m.duration) : '';
    const line = [m.artist || '—', m.year, m.label, _typeLabel(m), tcInfo, durInfo].filter(Boolean).join(' · ');
    let artistEl = el.querySelector('.qi-artist');
    if(artistEl) artistEl.textContent = '— ' + line;
    else if(titleEl && line){ const s=document.createElement('span'); s.className='qi-artist'; s.textContent='— '+line; titleEl.after(s); }
    const artEl = el.querySelector('.qi-art');
    if(artEl && m.artworkUrl && !artEl.querySelector('img'))
      artEl.innerHTML = `<img src="${esc(m.artworkUrl)}" data-cover data-lightbox onload="this.classList.add('loaded')" style="cursor:zoom-in" loading="lazy"/>`;
  }
  // Download counters on existing action buttons (done state).
  if(task.status === 'done'){
    const _setCnt = (sel, n) => {
      const btn = el.querySelector(sel); if(!btn) return;
      let cnt = btn.querySelector('.dl-cnt');
      if(n > 0){ if(!cnt){ cnt=document.createElement('span'); cnt.className='dl-cnt'; btn.appendChild(cnt);} cnt.textContent=n; }
      else if(cnt) cnt.remove();
    };
    _setCnt('.dl-btn', task._dl_file||0);
    _setCnt('.dl-zip-btn', task._dl_zip||0);
    _setCnt('.dl-cloud-btn', task._dl_gofile||0);
  }
}

function statusLabel(task) {
  if(task.status==='running'){
    const _isSingle = task.meta?.type === 'song' || task.meta?.type === 'track';
    const tc = _isSingle ? 1 : (task.meta?.trackCount || task.meta?.totalTracks || 0);
    const spin = '<span class="qi-spinner"></span>';
    if(tc > 1){
      const done = Math.min(tc, Math.floor((task.progress||0)/100*tc));
      return `${spin}${done}/${tc}`;
    }
    return `${spin}${task.progress||0}%`;
  }
  if(task.status==='done')    return t('status.done');
  if(task.status==='error')   return t('status.error');
  if(task.status==='paused')  return t('status.paused');
  return t('status.queued');
}

async function removeTask(id) {
  // Optimistic: drop the card from the UI FIRST so ✕ feels instant (don't wait for
  // the DELETE round-trip or a WS queue_update). Reconcile via pullQueue on failure.
  S.queue = S.queue.filter(t => t.id !== id);
  renderQueue(); updateTransport();
  try { await api('DELETE',`/api/queue/${id}`); }
  catch(e){ toast('Не удалось удалить задачу','var(--red)'); pullQueue(); }
}

async function retryTask(id) {
  const r = await api('POST', `/api/queue/retry/${id}`);
  if(r.ok) toast(r.reused ? '↺ Повтор запущен' : '↺ Добавлено в очередь');
  else if(r.duplicate) toast('Уже в очереди', 'var(--muted)');
  else toast(r.msg || 'Ошибка повтора', 'var(--red)');
}

async function clearDone() {
  // Finished = done / error / cancelled.
  const done = S.queue.filter(t => t.status==='done' || t.status==='error' || t.status==='cancelled');
  if(!done.length){ toast('Нет готовых задач для очистки','var(--muted)'); return; }
  const removed = [];
  for(const t of done){
    try { await api('DELETE',`/api/queue/${t.id}`); removed.push(t.id); }
    catch(e){ /* keep it; reported below */ }
  }
  // Self-refresh: don't depend on the WS queue_update arriving — right after a
  // server restart / reconnect it can be missed, which made the button look dead.
  if(removed.length){
    const gone = new Set(removed);
    S.queue = S.queue.filter(t => !gone.has(t.id));
    renderQueue(); updateTransport();
  }
  const failed = done.length - removed.length;
  if(failed) toast(`Не удалось убрать ${failed} — сервер ответил ошибкой`,'var(--orange)');
  else toast(`Убрано: ${removed.length}`,'var(--green)');
}

