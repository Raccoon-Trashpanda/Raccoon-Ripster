// ======================================================================
// Statistics view
// Extracted from app.js (mechanical split — same global functions, no behaviour
// change). Loaded AFTER app.js in index.html, so it sees S/api/toast/etc.
// ======================================================================

// ── Statistics ────────────────────────────────────────────────────
let _statsPeriod = 'week';

async function loadStats(period) {
  if (period) _statsPeriod = period;

  // Period tab highlight
  document.querySelectorAll('#stats-period-tabs .stab').forEach(b => {
    const on = b.dataset.p === _statsPeriod;
    b.style.borderBottomColor = on ? 'var(--green)' : 'transparent';
    b.style.color      = on ? 'var(--text)' : '';
    b.style.fontWeight = on ? '700' : '';
  });

  const set = (id, html) => { const el = document.getElementById(id); if (el) el.innerHTML = html; };
  const note = msg => set('stats-hero', `<div style="grid-column:1/-1;color:var(--muted);font-size:12px;padding:8px 0">${msg}</div>`);

  let d = null, httpStatus = 0;
  try {
    const resp = await fetch(`/api/stats?period=${_statsPeriod}`);
    httpStatus = resp.status;
    d = await resp.json().catch(() => null);
  } catch (_) { d = null; }

  if (httpStatus === 401 || (d && d.error === 'unauthorized')) {
    note(window.t('st.unauth'));
    return;
  }
  if (!d || d.error) {
    note(`${window.t('st.unavail')}${d && d.error ? ': ' + esc(d.error) : ''}`);
    return;
  }
  const t = d.totals || {};

  // ── Hero cards ──
  const hero = [
    { icon: '⬇',  label: window.t('st.h_downloads'),     val: t.downloads,       color: 'var(--green)'  },
    { icon: '♪',  label: window.t('st.h_tracks'),        val: t.tracks,          color: 'var(--blue)'   },
    { icon: '🎧', label: window.t('st.h_streams'), val: t.stream_sessions, color: 'var(--red)'    },
    { icon: '👤', label: window.t('st.h_guests'),        val: t.guests,          color: 'var(--purple)' },
  ];
  set('stats-hero', hero.map(c => `
    <div class="card" style="padding:14px 16px;display:flex;align-items:center;gap:12px">
      <div style="font-size:24px;line-height:1">${c.icon}</div>
      <div style="min-width:0">
        <div style="font-size:24px;font-weight:800;color:${c.color};font-family:var(--mono);line-height:1.1">${_fmt(c.val || 0)}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:1px">${c.label}</div>
      </div>
    </div>`).join(''));

  // ── Bar-list helper ──
  function bars(items, opts) {
    opts = opts || {};
    const nameKey  = opts.nameKey  || 'name';
    const countKey = opts.countKey || 'count';
    const color    = opts.color    || 'var(--green)';
    const lw       = opts.labelWidth || 120;
    if (!items || !items.length)
      return '<div style="color:var(--muted);font-size:11px;padding:3px 0">' + window.t('st.no_data') + '</div>';
    const max = opts.max || Math.max(...items.map(r => r[countKey] || 0), 1);
    return items.map(r => {
      const pct  = Math.round((r[countKey] || 0) / max * 100);
      const name = r[nameKey] || '—';
      const bcol = typeof color === 'function' ? color(r) : color;
      const bdg  = opts.badge ? opts.badge(r) : '';
      return `<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
        <div style="width:${lw}px;font-size:11px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex-shrink:0" title="${esc(name)}">${esc(name)}</div>
        ${bdg}
        <div style="flex:1;height:8px;background:var(--surface2);border-radius:4px;overflow:hidden;min-width:24px">
          <div style="width:${pct}%;height:100%;background:${bcol};border-radius:4px;transition:width .4s"></div>
        </div>
        <div style="font-size:10px;color:var(--muted);font-family:var(--mono);width:32px;text-align:right;flex-shrink:0">${_fmt(r[countKey] || 0)}</div>
      </div>`;
    }).join('');
  }

  const STREAM_COLOR = { qobuz:'#1870f5', tidal:'#00d4b3', deezer:'#a238ff', bbc:'#e4003b', generic:'var(--muted2)' };
  const STREAM_LABEL = { qobuz:'Qobuz', tidal:'Tidal', deezer:'Deezer', bbc:'BBC', generic:window.t('st.other') };

  // ── Listening: split tiles ──
  const splitTiles = [
    { label: window.t('st.previews'), val: t.preview_sessions || 0, color: 'var(--green)' },
    { label: 'BBC Sounds',    val: t.bbc_sessions || 0,     color: '#e4003b'      },
  ].map(c => `
    <div style="background:var(--surface2);border-radius:9px;padding:10px 12px">
      <div style="font-size:20px;font-weight:800;color:${c.color};font-family:var(--mono)">${_fmt(c.val)}</div>
      <div style="font-size:10px;color:var(--muted);margin-top:2px">${c.label}</div>
    </div>`).join('');
  const typeTiles = (d.by_stream_type || []).map(r => `
    <div style="background:var(--surface2);border-radius:9px;padding:10px 12px">
      <div style="font-size:20px;font-weight:800;color:${STREAM_COLOR[r.name] || 'var(--muted2)'};font-family:var(--mono)">${_fmt(r.count || 0)}</div>
      <div style="font-size:10px;color:var(--muted);margin-top:2px">${STREAM_LABEL[r.name] || esc(r.name || '—')}</div>
    </div>`).join('');
  set('stats-listen-split', splitTiles + typeTiles);

  // ── Listening: top played ──
  const topL = d.top_streams || [];
  set('stats-listen-top', topL.length
    ? '<div style="font-size:11px;color:var(--muted);margin-bottom:7px">' + window.t('st.top_played') + '</div>' +
      bars(topL, {
        color: r => STREAM_COLOR[r.stream_type] || 'var(--green)',
        badge: r => {
          const st = r.stream_type || 'generic';
          const c  = STREAM_COLOR[st] || 'var(--muted2)';
          return `<div style="font-size:8px;font-weight:700;text-transform:uppercase;letter-spacing:.4px;color:${c};background:${c}22;border-radius:4px;padding:2px 6px;flex-shrink:0">${STREAM_LABEL[st] || esc(st)}</div>`;
        },
      })
    : '<div style="color:var(--muted);font-size:11px">' + window.t('st.nothing_played') + '</div>');

  // ── Listening history — recent plays, newest first ──
  const recent = d.recent_listens || [];
  set('stats-listen-recent', recent.length
    ? '<div style="font-size:11px;color:var(--muted);margin-bottom:7px">' + window.t('st.listen_history') + '</div>' +
      recent.slice(0, 40).map(e => {
        const st  = e.type || 'generic';
        const c   = STREAM_COLOR[st] || 'var(--muted2)';
        const dt  = new Date((e.ts || 0) * 1000);
        const tm  = dt.toDateString() === new Date().toDateString()
          ? dt.toTimeString().slice(0, 5)
          : `${dt.getDate()}.${String(dt.getMonth()+1).padStart(2,'0')} ${dt.toTimeString().slice(0,5)}`;
        return `<div style="display:flex;align-items:center;gap:8px;padding:3px 0;font-size:11px;border-top:1px solid var(--surface2)">
          <span style="color:var(--muted2);font-family:var(--mono);width:78px;flex-shrink:0">${tm}</span>
          <span style="font-size:8px;font-weight:700;text-transform:uppercase;color:${c};background:${c}22;border-radius:4px;padding:2px 6px;flex-shrink:0">${STREAM_LABEL[st] || esc(st)}</span>
          <span style="color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(e.name)}">${esc(e.name)}</span>
        </div>`;
      }).join('')
    : '');

  // ── Service / quality ──
  set('stats-by-service', bars((d.by_service || []).map(r => ({ name: r.label || r.name || '—', count: r.count })), { color:'var(--green)' }));
  set('stats-by-quality', bars((d.by_quality || []).map(r => ({ name: (r.name || '—').toUpperCase(), count: r.count })), { color:'var(--orange)' }));

  // ── Top artists ──
  set('stats-by-artist', bars((d.by_artist || []).slice(0, 20), { color:'var(--purple)', labelWidth:110 }));

  // ── Timeline by day ──
  const days = d.by_day || [];
  const tlMax = Math.max(...days.map(r => r.count || 0), 1);
  set('stats-timeline', days.length
    ? days.map(r => {
        const pct = Math.max(Math.round((r.count || 0) / tlMax * 100), 2);
        return `<div title="${esc(r.date || '')}: ${_fmt(r.count || 0)}" style="flex:1;min-width:11px;max-width:30px;background:var(--green);border-radius:3px 3px 0 0;height:${pct}%;min-height:2px;opacity:.85"></div>`;
      }).join('')
    : '<div style="color:var(--muted);font-size:11px">' + window.t('st.no_data') + '</div>');
  const tlStep = days.length > 30 ? Math.ceil(days.length / 10) : (days.length > 14 ? 3 : 1);
  set('stats-tl-labels', days.map((r, i) =>
    `<div style="flex:1;min-width:11px;max-width:30px;text-align:center;overflow:hidden">${i % tlStep === 0 ? esc((r.date || '').slice(5)) : ''}</div>`).join(''));

  // ── By hour ──
  const hours = Array.isArray(d.by_hour) ? d.by_hour : [];
  const hMax = Math.max(...hours.map(h => h.count || 0), 1);
  set('stats-by-hour', hours.map(h => {
    const pct = Math.round((h.count || 0) / hMax * 100);
    const col = h.hour < 6 ? 'var(--muted2)' : h.hour < 12 ? 'var(--blue)' : h.hour < 18 ? 'var(--green)' : 'var(--purple)';
    return `<div title="${String(h.hour).padStart(2,'0')}:00 — ${_fmt(h.count || 0)}" style="flex:1;background:${col};opacity:${0.25 + pct/100*0.75};border-radius:2px 2px 0 0;height:${Math.max(pct,3)}%;min-height:3px"></div>`;
  }).join(''));
  set('stats-hour-labels', [0,6,12,18,23].map(h =>
    `<div style="flex:${h===0?1:h===23?1:6};text-align:${h===0?'left':h===23?'right':'center'}">${String(h).padStart(2,'0')}</div>`).join(''));

  // ── By weekday ──
  const wd = d.by_weekday || [];
  set('stats-by-weekday', bars(wd, {
    labelWidth: 28,
    color: r => r.day >= 5 ? 'var(--orange)' : 'var(--blue)',
    max: Math.max(...wd.map(r => r.count || 0), 1),
  }));

  // ── Guests ──
  set('stats-guests', `
    <div style="display:flex;gap:28px;flex-wrap:wrap">
      <div><span style="font-size:20px;font-weight:800;color:var(--purple);font-family:var(--mono)">${_fmt(t.guests || 0)}</span>
        <span style="font-size:12px;color:var(--muted);margin-left:6px">${window.t('st.uniq_guests')}</span></div>
      <div><span style="font-size:20px;font-weight:800;color:var(--orange);font-family:var(--mono)">${_fmt(Math.round(t.guest_minutes || 0))}</span>
        <span style="font-size:12px;color:var(--muted);margin-left:6px">${window.t('st.min_online')}</span></div>
    </div>`);

  const footEl = document.getElementById('stats-footer');
  if (footEl) footEl.textContent = window.t('st.data_for') +
    ({ day:window.t('st.p_day'), week:window.t('st.p_week'), month:window.t('st.p_month'), year:window.t('st.p_year'), all:window.t('st.p_all') }[_statsPeriod] || _statsPeriod);
}

function _fmt(n) {
  if (n == null) return '0';
  return n >= 1000 ? (n/1000).toFixed(1)+'k' : String(n);
}

