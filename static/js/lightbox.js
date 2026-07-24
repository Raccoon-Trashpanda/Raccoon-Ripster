// ======================================================================
// Lightbox click-to-zoom
// Extracted from app.js (mechanical split — same global functions, no behaviour
// change). Loaded AFTER app.js in index.html, so it sees S/api/toast/etc.
// ======================================================================

// ── Lightbox: click-to-zoom for any cover image ─────────────────────────
// Images with ``data-lightbox`` attribute (or a ``data-lightbox-src`` pointing
// at a higher-res URL) open fullscreen on click. Esc or backdrop-click closes.
function openLightbox(src){
  const box = document.getElementById('lightbox');
  const img = document.getElementById('lightbox-img');
  if(!box || !img || !src) return;
  img.src = src;
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
  const src = img.dataset.lightboxSrc || img.src;
  openLightbox(src);
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
async function loadWatchlist() {
  const r = await api('GET','/api/watchlist');
  const items = r.items||[];
  const list  = document.getElementById('wl-list');
  const emp   = document.getElementById('wl-empty');
  if(emp) emp.style.display = items.length?'none':'';
  if(!list) return;
  list.innerHTML = items.map(w => `
    <div style="display:flex;align-items:center;gap:10px;padding:9px 12px;background:var(--surface);border:1px solid var(--border);border-radius:10px;margin-bottom:7px">
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;font-weight:600;color:var(--text)">${esc(w.name||w.url)}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:2px">
          ${w.service||'apple'} · ${w.auto_download?t('wl.auto_dl'):t('wl.notify_only')}
          ${w.last_check?' · '+t('wl.checked_at')+' '+new Date(w.last_check).toLocaleString('ru'):''}
          ${w.last_release?'<span style="color:var(--green);margin-left:6px">' + t('wl.new_release') + '</span>':''}
        </div>
      </div>
      <label style="display:flex;align-items:center;gap:5px;font-size:11px;color:var(--muted);cursor:pointer;white-space:nowrap">
        <input type="checkbox" ${w.auto_download?'checked':''} onchange="wlToggleAuto('${w.id}',this.checked)"/> ${t('wl.auto_short')}
      </label>
      <button onclick="wlRemove('${w.id}')"
        style="padding:4px 8px;background:var(--surface);border:1px solid var(--border);border-radius:6px;font-size:11px;cursor:pointer;color:var(--muted);font-family:var(--font)">
        ✕
      </button>
    </div>`).join('');
  loadWlSuggestions();
}

async function wlAdd() {
  const name = document.getElementById('wl-name')?.value?.trim();
  const url  = document.getElementById('wl-url')?.value?.trim();
  const svc  = document.getElementById('wl-svc')?.value||'apple';
  const auto = document.getElementById('wl-auto')?.checked !== false;
  if(!name && !url){ toast(t('lb.enter_artist')); return; }
  const r = await api('POST','/api/watchlist',{name,url,service:svc,auto_download:auto});
  if(r.ok){ toast(`+ ${name||url} → watchlist`,'var(--green)'); loadWatchlist(); document.getElementById('wl-name').value=''; document.getElementById('wl-url').value=''; }
  else toast(t('t.error_c')+(r.detail||''),'var(--red)');
}

async function wlRemove(id) {
  await api('DELETE','/api/watchlist/'+id);
  loadWatchlist();
}

async function wlToggleAuto(id, val) {
  // Update via re-add (simple)
  toast(val?t('wl.auto_on'):t('wl.notify_only'));
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

