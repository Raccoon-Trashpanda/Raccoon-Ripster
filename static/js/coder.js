// ======================================================================
// Coder + Tagger + shared folder tree
// Extracted from app.js (mechanical split — same global functions, no behaviour
// change). Loaded AFTER app.js in index.html, so it sees S/api/toast/etc.
// ======================================================================

// ── Shared folder TREE (checkbox branches) for Coder & Tagger ────
async function mountFolderTree(container, onSelect) {
  if(!container) return;
  const state = { cur: null };
  container.innerHTML = '<div style="font-size:11px;color:var(--muted);padding:6px">Загрузка…</div>';
  try {
    const r = await api('GET','/api/coder/browse');
    container.innerHTML = '';
    (r.nodes||[]).forEach(n => container.appendChild(_treeNode(n, 0, state, onSelect)));
    if(!(r.nodes||[]).length) container.innerHTML = '<div style="font-size:11px;color:var(--muted);padding:6px">Пусто</div>';
  } catch(e){ container.innerHTML = '<div style="color:var(--red);font-size:11px;padding:6px">'+esc(e.message)+'</div>'; }
}
function _treeNode(n, depth, state, onSelect) {
  // Selectable when the folder has audio directly OR anywhere in its subfolders
  // (audio_deep) — so a multi-disc release root (CD 1/CD 2/…) can be picked.
  const selectable = !!(n.audio_deep || n.has_audio);
  const wrap = document.createElement('div');
  const head = document.createElement('div');
  head.style.cssText = `display:flex;align-items:center;gap:6px;padding:3px 4px;border-radius:5px;font-size:12px;margin-left:${depth*16}px`;
  head.onmouseover = ()=>head.style.background='var(--surface2)';
  head.onmouseout  = ()=>head.style.background='transparent';
  const caret = document.createElement('span');
  caret.textContent = n.has_subdirs ? '▸' : '';
  caret.style.cssText = 'flex:0 0 14px;cursor:'+(n.has_subdirs?'pointer':'default')+';color:var(--muted);user-select:none;text-align:center';
  const cb = document.createElement('input');
  cb.type='checkbox'; cb.disabled = !selectable;
  // Override the global input{width:100%;padding;border} rule that would stretch
  // the checkbox across the whole row.
  cb.style.cssText = 'flex:0 0 16px;width:16px;min-width:16px;height:16px;padding:0;margin:0;border-radius:3px;accent-color:var(--green);cursor:'+(selectable?'pointer':'default');
  const lbl = document.createElement('span');
  lbl.textContent = n.name + (n.tracks ? `  ·  ${n.tracks} тр.` : (n.has_subdirs ? '  ·  ▸ диски' : ''));
  lbl.style.cssText = 'flex:1 1 auto;min-width:0;text-align:left;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'+(selectable?'':'opacity:.5');
  head.append(caret, cb, lbl);
  const childBox = document.createElement('div'); childBox.style.display='none';
  let loaded=false;
  const expand = async ()=>{
    if(!n.has_subdirs) return;
    if(childBox.style.display==='none'){
      if(!loaded){ loaded=true; caret.textContent='▾';
        try { const r=await api('GET','/api/coder/browse?path='+encodeURIComponent(n.path));
          (r.nodes||[]).forEach(c=>childBox.appendChild(_treeNode(c,depth+1,state,onSelect))); } catch(e){}
      }
      childBox.style.display='block'; caret.textContent='▾';
    } else { childBox.style.display='none'; caret.textContent='▸'; }
  };
  caret.onclick = expand;
  lbl.onclick = ()=>{ if(selectable){ cb.checked=!cb.checked; cb.onchange(); } else if(n.has_subdirs){ expand(); } };
  cb.onchange = ()=>{
    if(cb.checked){
      if(state.cur && state.cur!==cb) state.cur.checked=false;
      state.cur = cb; head.style.outline='1px solid var(--green)';
      onSelect(n.path, n.name, n.tracks);
    } else { state.cur=null; head.style.outline='none'; onSelect('','',0); }
  };
  wrap.append(head, childBox);
  return wrap;
}

// ── Ripster Coder tab ───────────────────────────────────────────
let _coderFolders = [];
// ── XRECODE-style Coder table state ──────────────────────────────────────────
let _cxFiles = [], _cxDir = '', _cxAlbum = '', _cxArtist = '', _cxTotalSize = 0, _cxTotalDur = 0;
const CX_FORMATS = [
  {id:'mp3',  label:'MP3',  sub:'lossy',        ext:'mp3'},
  {id:'aac',  label:'AAC',  sub:'.m4a · lossy', ext:'m4a'},
  {id:'flac', label:'FLAC', sub:'lossless',     ext:'flac'},
  {id:'alac', label:'ALAC', sub:'.m4a · lossless', ext:'m4a'},
  {id:'ogg',  label:'OGG',  sub:'Vorbis',       ext:'ogg'},
  {id:'opus', label:'Opus', sub:'lossy',        ext:'opus'},
  {id:'wav',  label:'WAV',  sub:'PCM',          ext:'wav'},
];

async function coderInit() {
  cxBuildFmtGrid();
  cxSetAction();
  const ca = document.getElementById('coder-auto');
  if(ca) ca.checked = !!(S.config && S.config['coder-auto']);
  // Mount the folder tree once — hidden until the user clicks "📂 Обзор".
  const tree = document.getElementById('coder-tree');
  if(tree && !tree.dataset.mounted){
    tree.dataset.mounted = '1';
    await mountFolderTree(tree, (path)=>{
      if(path){ tree.style.display='none'; cxLoadFiles(path); }
    });
  }
  const p = document.getElementById('coder-path')?.value.trim();
  if(p && !_cxFiles.length) cxLoadFiles(p);
}

function cxBrowseToggle(){
  const t = document.getElementById('coder-tree');
  if(t) t.style.display = (t.style.display==='none' ? '' : 'none');
}

function cxBuildFmtGrid(){
  const sel = document.getElementById('coder-fmt');
  if(sel && !sel.options.length)
    sel.innerHTML = CX_FORMATS.map(f=>`<option value="${f.id}">${f.label}</option>`).join('');
  const grid = document.getElementById('cx-fmt-grid');
  if(grid && !grid.dataset.built){
    grid.dataset.built = '1';
    grid.innerHTML = CX_FORMATS.map((f,i)=>
      `<label class="cx-fmt${i===0?' active':''}" data-fmt="${f.id}">
         <input type="radio" name="cx-fmt" value="${f.id}"${i===0?' checked':''} onchange="cxFmtPick('${f.id}')"/>
         <span class="cx-fmt-l">${f.label}</span><span class="cx-fmt-s">${f.sub}</span>
       </label>`).join('');
  }
  coderFmtChange();
}

function cxFmtPick(val){
  const sel = document.getElementById('coder-fmt'); if(sel) sel.value = val;
  document.querySelectorAll('#cx-fmt-grid .cx-fmt').forEach(l=>l.classList.toggle('active', l.dataset.fmt===val));
  coderFmtChange();
  cxRenderResultCol();
}
function cxFmt(){ return document.getElementById('coder-fmt')?.value || 'mp3'; }
function cxFmtExt(){ return (CX_FORMATS.find(f=>f.id===cxFmt())||{}).ext || 'mp3'; }
function cxAction(){ return document.querySelector('input[name=cx-action]:checked')?.value || 'encode'; }

function cxBytes(n){
  if(!n) return '—';
  const u=['Б','КБ','МБ','ГБ']; let i=0; n=+n;
  while(n>=1024 && i<u.length-1){ n/=1024; i++; }
  return n.toFixed(i?2:0).replace('.',',')+' '+u[i];
}
function cxDur(s){
  s=Math.round(s||0); if(!s) return '';
  const h=Math.floor(s/3600), m=Math.floor(s%3600/60), ss=s%60;
  return (h?h+':':'')+String(m).padStart(h?2:1,'0')+':'+String(ss).padStart(2,'0');
}

async function cxLoadFiles(dir){
  if(!dir){ toast('Укажи путь к папке','var(--red)'); return; }
  document.getElementById('coder-path').value = dir;
  const tb = document.getElementById('cx-tbody');
  if(tb) tb.innerHTML = `<tr><td colspan="5" class="cx-empty">Загрузка…</td></tr>`;
  try{
    const r = await api('POST','/api/coder/files',{dir});
    _cxFiles=r.files||[]; _cxDir=r.dir||dir; _cxAlbum=r.album||''; _cxArtist=r.artist||'';
    _cxTotalSize=r.total_size||0; _cxTotalDur=r.total_dur||0;
    cxRenderTable();
    const si=document.getElementById('coder-srcinfo'); if(si) si.textContent='';
  }catch(e){
    if(tb) tb.innerHTML = `<tr><td colspan="5" class="cx-empty" style="color:var(--red)">${esc(e.message)}</td></tr>`;
  }
}

function cxRenderTable(){
  const tb = document.getElementById('cx-tbody'); if(!tb) return;
  const sumEl = document.getElementById('cx-summary');
  if(!_cxFiles.length){
    tb.innerHTML = `<tr><td colspan="5" class="cx-empty">Нет аудиофайлов в папке</td></tr>`;
    if(sumEl) sumEl.textContent=''; return;
  }
  const title = [_cxArtist,_cxAlbum].filter(Boolean).join(' - ') || _cxDir.split(/[\\/]/).pop();
  const hdr = `${title} (${_cxFiles.length}, ${cxBytes(_cxTotalSize)}${_cxTotalDur?', '+cxDur(_cxTotalDur):''})`;
  let h = `<tr class="cx-grp"><td class="cx-c-cb"></td><td class="cx-c-num"></td><td colspan="3" class="cx-grp-name" title="${esc(_cxDir)}">📁 ${esc(hdr)}</td></tr>`;
  _cxFiles.forEach((f,i)=>{
    h += `<tr data-name="${esc(f.name)}">
      <td class="cx-c-cb"><input type="checkbox" class="cx-row-cb" checked onchange="cxUpdateCount()"/></td>
      <td class="cx-c-num">${String(i+1).padStart(2,'0')}</td>
      <td class="cx-c-src" title="${esc(f.name)}">${esc(f.name)}</td>
      <td class="cx-c-dst cx-dst">${esc(cxResultName(f.name))}</td>
      <td class="cx-c-size">${cxBytes(f.size)}</td>
    </tr>`;
  });
  tb.innerHTML = h;
  const all = document.getElementById('cx-all'); if(all) all.checked = true;
  cxUpdateCount();
}

function cxResultName(name){
  const act = cxAction();
  if(act==='merge') return '→ один файл + .cue';
  if(act==='retag') return '(теги на месте, по ISRC)';
  if(act==='split') return '(разрез по .cue → split/)';
  if(document.getElementById('coder-rename')?.checked) return '↻ по шаблону тегов';
  return name.replace(/\.[^.]+$/,'') + '.' + cxFmtExt();
}
function cxRenderResultCol(){
  document.querySelectorAll('#cx-tbody tr[data-name]').forEach(tr=>{
    const cell = tr.querySelector('.cx-dst');
    if(cell) cell.textContent = cxResultName(tr.dataset.name);
  });
}
function cxOnRenameToggle(on){
  const t=document.getElementById('coder-tmpl'); if(t) t.style.display = on?'':'none';
  cxRenderResultCol();
}

function cxToggleAll(on){
  document.querySelectorAll('#cx-tbody .cx-row-cb').forEach(cb=>cb.checked=on);
  cxUpdateCount();
}
function cxSelectedNames(){
  return [...document.querySelectorAll('#cx-tbody tr[data-name]')]
    .filter(tr=>tr.querySelector('.cx-row-cb')?.checked)
    .map(tr=>tr.dataset.name);
}
function cxUpdateCount(){
  const sel = cxSelectedNames();
  const sumSel = _cxFiles.filter(f=>sel.includes(f.name)).reduce((a,f)=>a+(f.size||0),0);
  const s = document.getElementById('cx-summary');
  if(s) s.innerHTML = _cxFiles.length
    ? `Выбрано <b>${sel.length}</b> из ${_cxFiles.length} · ${cxBytes(sumSel)}${_cxTotalDur?' · '+cxDur(_cxTotalDur):''}`
    : '';
  const all = document.getElementById('cx-all');
  if(all) all.checked = _cxFiles.length>0 && sel.length===_cxFiles.length;
}

function cxSetAction(){
  const act = cxAction();
  const nw = document.getElementById('coder-name-wrap'); if(nw) nw.style.display = act==='merge'?'':'none';
  const sr = document.getElementById('cx-split-reenc-wrap'); if(sr) sr.style.display = act==='split'?'':'none';
  const os = document.getElementById('cx-outset'); if(os) os.style.display = act==='retag'?'none':'';
  cxRenderResultCol();
  if(act==='merge' && _cxDir){
    const nm = document.getElementById('coder-name');
    if(nm && !nm.value)
      api('POST','/api/coder/preview',{dir:_cxDir}).then(p=>{ if(p&&p.ok&&p.name&&!nm.value) nm.value=p.name; }).catch(()=>{});
  }
}

async function cxRun(){
  const dir = document.getElementById('coder-path').value.trim();
  if(!dir){ toast('Выбери папку — «📂 Обзор» или путь','var(--red)'); return; }
  const act = cxAction(), fmt = cxFmt();
  const btn = document.getElementById('coder-run'), out = document.getElementById('coder-result');
  btn.disabled = true; const _t = btn.textContent; btn.textContent = (window.t?t('cd.working'):'Работаю…'); out.textContent='';
  const _stopBtn = document.getElementById('coder-stop'); if(_stopBtn) _stopBtn.disabled = false;
  try{
    let r;
    if(act==='encode'){
      const only = cxSelectedNames();
      if(!only.length) throw new Error('Не выбрано ни одного файла');
      const insrc = document.getElementById('cx-insrc').checked;
      const outdir = insrc ? (dir + '/converted/' + fmt.toUpperCase())
                           : (document.getElementById('cx-outdir').value.trim());
      r = await api('POST','/api/coder/convert',{dir, fmt, only,
        bitrate:document.getElementById('coder-br').value,
        sample_rate:document.getElementById('coder-srate').value||'',
        bit_depth:document.getElementById('coder-bd').value||'',
        normalize:document.getElementById('coder-norm').checked,
        rename:document.getElementById('coder-rename').checked,
        rename_template:document.getElementById('coder-tmpl').value||'',
        out_dir: outdir});
      out.innerHTML = `<span style="color:var(--green)">✓ ${r.converted} → ${fmt.toUpperCase()}</span>${r.failed?` · ${r.failed} ошибок`:''} → ${esc(r.out_dir||'')}`;
    } else if(act==='merge'){
      r = await api('POST','/api/coder/mix',{dir, name:document.getElementById('coder-name').value.trim(), fmt:(['flac','alac'].includes(fmt)?fmt:'mp3')});
      const nm=(r.names&&r.names.length)?r.names.join(', '):(r.name||'');
      out.innerHTML = `<span style="color:var(--green)">✓ ${esc(nm)}</span> + .cue → ${esc(r.out_dir||'')}` + (r.warning?` · <span style="color:var(--orange)">${esc(r.warning)}</span>`:'');
    } else if(act==='split'){
      const reenc=document.getElementById('coder-split-reenc').checked;
      r = await api('POST','/api/coder/split',{dir, fmt:reenc?fmt:'source', bitrate:document.getElementById('coder-br').value});
      out.innerHTML = `<span style="color:var(--green)">✓ ${r.converted} треков</span>${r.failed?` · <span style="color:var(--red)">${r.failed} ошибок</span>`:''} → ${esc(r.out_dir||'')}`;
    } else if(act==='retag'){
      r = await api('POST','/api/coder/retag',{dir});
      out.innerHTML = `<span style="color:var(--green)">✓ перетеговано ${r.retagged}</span> · <span style="color:var(--muted)">проверено ${r.checked}, пропущено ${r.skipped}</span>`;
    }
    if(r && r.cancelled){
      const msg = window.t ? t('cd.stopped') : 'Остановлено';
      out.innerHTML = `<span style="color:var(--orange)">⏹ ${esc(msg)}</span>` +
        (r.converted ? ` · ${r.converted} готово до отмены` : '');
      toast('⏹ '+msg,'var(--orange)');
    } else {
      toast('🎛 Готово','var(--green)');
    }
  }catch(e){ out.innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`; toast('Coder: '+e.message,'var(--red)'); }
  finally{ btn.disabled=false; btn.textContent=_t;
    const box=document.getElementById('coder-progress'); if(box) box.style.display='none'; }
}

// STOP: cooperative cancel — the worker stops between files (current file finishes
// cleanly) or the mix builder kills ffmpeg and drops the partial. cxRun's awaited
// response comes back {cancelled:true} and paints the "stopped" state.
async function cxStop(){
  const sb = document.getElementById('coder-stop');
  if(sb){ sb.disabled = true; sb.textContent = window.t ? t('cd.working') : '…'; }
  try{ await api('POST','/api/coder/cancel',{}); }
  catch(e){ /* no-op: nothing running is fine */ }
  const lbl = document.getElementById('coder-plabel');
  if(lbl) lbl.textContent = window.t ? t('cd.stopped') : 'Остановлено';
  setTimeout(()=>{ if(sb){ sb.disabled=false; sb.textContent = window.t?t('cd.stop'):'⏹ Стоп'; } }, 1500);
}
function coderFmtChange() {
  const fmt = document.getElementById('coder-fmt').value;
  const lossy = ['mp3','aac','ogg','opus'].includes(fmt);
  document.getElementById('coder-br-wrap').style.display = lossy ? '' : 'none';
  const bd = document.getElementById('coder-bd-wrap');
  if(bd) bd.style.display = ['flac','alac','wav'].includes(fmt) ? '' : 'none';
}
async function coderSplit() {
  const dir = _coderDir();
  if(!dir){ toast('Выбери папку с .cue','var(--red)'); return; }
  const reenc = document.getElementById('coder-split-reenc')?.checked;
  const fmt = reenc ? document.getElementById('coder-fmt').value : 'source';
  const btn = document.getElementById('coder-split'); if(btn) btn.disabled = true;
  const res = document.getElementById('coder-split-res'); if(res) res.textContent = 'Режу…';
  try {
    const r = await api('POST','/api/coder/split',{dir, fmt, bitrate:document.getElementById('coder-br').value});
    if(res) res.innerHTML = `<span style="color:var(--green)">✓ ${r.converted} треков</span>${r.failed?` · <span style="color:var(--red)">${r.failed} ошибок</span>`:''} → ${esc(r.out_dir)}`;
    toast(`✂ Разрезано ${r.converted} треков`,'var(--green)');
  } catch(e){ if(res) res.innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`; toast('CUE-сплит: '+e.message,'var(--red)'); }
  finally { if(btn) btn.disabled = false; }
}

// ── Coder: retag from service (ISRC) + batch convert ──────────────────
async function coderRetag() {
  const dir = _coderDir();
  if(!dir){ toast('Выбери папку','var(--red)'); return; }
  const btn = document.getElementById('coder-retag'); if(btn) btn.disabled = true;
  const res = document.getElementById('coder-retag-res'); if(res) res.textContent = 'Ретег…';
  try {
    const r = await api('POST','/api/coder/retag',{dir});
    if(res) res.innerHTML = `<span style="color:var(--green)">✓ перетеговано ${r.retagged}</span> · <span style="color:var(--muted)">проверено ${r.checked}, пропущено ${r.skipped}</span>`;
    toast(`🏷 Ретег: ${r.retagged} исправлено`,'var(--green)');
  } catch(e){ if(res) res.innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`; toast('Ретег: '+e.message,'var(--red)'); }
  finally { if(btn) btn.disabled = false; }
}

let _coderBatch = [];
function _coderBatchRender() {
  const box = document.getElementById('coder-batch-list'); if(!box) return;
  box.innerHTML = _coderBatch.map((d,i)=>`<div style="display:flex;align-items:center;gap:8px;font-size:11px;padding:3px 8px;border:1px solid var(--border);border-radius:6px;background:var(--surface)">
    <span style="flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${esc(d)}">${esc(d)}</span>
    <span onclick="coderBatchRemove(${i})" style="cursor:pointer;color:var(--muted)" title="Убрать">✕</span></div>`).join('');
}
function coderBatchAdd() {
  const dir = _coderDir();
  if(!dir){ toast('Выбери папку выше','var(--red)'); return; }
  if(_coderBatch.includes(dir)){ toast('Уже в пакете','var(--muted)'); return; }
  _coderBatch.push(dir); _coderBatchRender();
  toast(`В пакете: ${_coderBatch.length}`,'var(--green)');
}
function coderBatchRemove(i){ _coderBatch.splice(i,1); _coderBatchRender(); }
function coderBatchClear(){ _coderBatch=[]; _coderBatchRender(); const r=document.getElementById('coder-batch-res'); if(r) r.textContent=''; }
async function coderBatchRun() {
  if(!_coderBatch.length){ toast('Пакет пуст — добавь папки','var(--red)'); return; }
  const fmt = document.getElementById('coder-fmt').value;
  const opts = { fmt, bitrate:document.getElementById('coder-br').value,
    sample_rate:document.getElementById('coder-srate')?.value||'',
    bit_depth:document.getElementById('coder-bd')?.value||'',
    normalize:document.getElementById('coder-norm')?.checked||false,
    rename:document.getElementById('coder-rename')?.checked||false,
    rename_template:document.getElementById('coder-tmpl')?.value||'' };
  const btn = document.getElementById('coder-batch-run'); if(btn) btn.disabled = true;
  const res = document.getElementById('coder-batch-res');
  let ok=0, fail=0, conv=0; const total=_coderBatch.length;
  for(let i=0;i<total;i++){
    if(res) res.textContent = `Конвертирую ${i+1}/${total}…`;
    try {
      const r = await api('POST','/api/coder/convert',{dir:_coderBatch[i], ...opts});
      if(r.ok){ ok++; conv += (r.converted||0); } else fail++;
    } catch(e){ fail++; }
  }
  if(btn) btn.disabled = false;
  if(res) res.innerHTML = `<span style="color:var(--green)">✓ ${ok}/${total} папок · ${conv} файлов</span>${fail?` · <span style="color:var(--red)">${fail} ошибок</span>`:''}`;
  toast(`📚 Пакет: ${ok}/${total} папок (${conv} файлов)`,'var(--green)');
}

// Per-track match: for loose files (no album), look each file up on the chosen
// service by its ISRC (exact) or artist+title, and fill the row from the match.
async function taggerMatchAll() {
  if(!_tagRows.length){ toast('Нет файлов','var(--red)'); return; }
  const svc = document.getElementById('tag-svc').value;
  const trs = document.querySelectorAll('#tag-tbody tr');
  const res = document.getElementById('tag-applyres');
  let ok=0, miss=0; const total=_tagRows.length;
  for(let i=0;i<total;i++){
    const r=_tagRows[i], tr=trs[i]; if(!tr) continue;
    const numCell=tr.querySelector('td'); if(numCell) numCell.innerHTML='<span class="qi-spinner"></span>';
    try {
      const m = await api('POST','/api/tagger/match',{path:r.path, service:svc});
      if(m.matched && m.proposed){
        const p=m.proposed;
        const set=(cls,v)=>{ const inp=tr.querySelector('.'+cls+' input'); if(inp&&v!=null&&v!==''){ inp.value=v; inp.style.background='rgba(94,200,224,.12)'; } };
        set('tc-title',p.title); set('tc-artist',p.artist); set('tc-album',p.album);
        set('tc-albumartist',p.albumartist); set('tc-track',p.track); set('tc-year',p.year); set('tc-genre',p.genre);
        if(p.cover) r._cover=p.cover;
        if(p.disc!=null) r._disc=p.disc;
        ok++; if(numCell) numCell.innerHTML='<span style="color:var(--green)">✓</span>';
      } else { miss++; if(numCell) numCell.innerHTML='<span style="color:var(--muted)">—</span>'; }
    } catch(e){ miss++; if(numCell) numCell.innerHTML='<span style="color:var(--red)">✗</span>'; }
    if(res) res.textContent=`Матчинг… ${ok+miss}/${total}`;
  }
  if(res) res.innerHTML=`<span style="color:var(--green)">✓ ${ok} сматчено</span>${miss?` · <span style="color:var(--muted)">${miss} не найдено</span>`:''}`;
  toast(`🔍 Сматчено ${ok}/${total}`,'var(--green)');
}
async function coderPickFolder() {
  const dir = document.getElementById('coder-path').value.trim();
  if(!dir) return;
  // preview → name suggestion + lossless hint
  try {
    const p = await api('POST','/api/coder/preview',{dir});
    if(p.ok){
      document.getElementById('coder-srcinfo').innerHTML =
        `${p.count} треков · ${p.codec||p.source_ext} ${p.lossless?'<span style="color:var(--green)">lossless</span>':'<span style="color:var(--orange)">lossy</span>'}`;
      const nm = document.getElementById('coder-name'); if(nm) nm.value = p.name||'';
    }
  } catch(e){}
}
function coderModeChange() {
  const on = document.getElementById('coder-merge').checked;
  document.getElementById('coder-name-wrap').style.display = on ? '' : 'none';
  const rb = document.getElementById('coder-rename-block');
  if(rb) rb.style.display = on ? 'none' : '';   // rename-by-tags is per-track only
  if(on) coderPickFolder();
}
function _coderDir() {
  return document.getElementById('coder-path').value.trim();
}
async function coderRun() {
  const dir = _coderDir();
  if(!dir){ toast('Выбери папку или путь','var(--red)'); return; }
  const fmt = document.getElementById('coder-fmt').value;
  const merge = document.getElementById('coder-merge').checked;
  const btn = document.getElementById('coder-run');
  const out = document.getElementById('coder-result');
  btn.disabled = true; const _txt = btn.textContent; btn.textContent = 'Работаю…'; out.textContent = '';
  try {
    let r;
    if(merge){
      r = await api('POST','/api/coder/mix',{dir, name:document.getElementById('coder-name').value.trim(), fmt: (['flac','alac'].includes(fmt)?fmt:'mp3')});
      const nm = (r.names&&r.names.length)?r.names.join(', '):(r.name||'');
      const dn = r.multi?` (${r.discs} диска → ${r.names.length} миксов)`:'';
      out.innerHTML = `<span style="color:var(--green)">✓ ${esc(nm)}</span>${dn} + .cue → ${esc(r.out_dir)}` + (r.warning?` · <span style="color:var(--orange)">${esc(r.warning)}</span>`:'');
    } else {
      r = await api('POST','/api/coder/convert',{dir, fmt, bitrate:document.getElementById('coder-br').value,
            sample_rate:document.getElementById('coder-srate')?.value||'',
            bit_depth:document.getElementById('coder-bd')?.value||'',
            normalize:document.getElementById('coder-norm')?.checked||false,
            rename:document.getElementById('coder-rename')?.checked||false,
            rename_template:document.getElementById('coder-tmpl')?.value||''});
      out.innerHTML = `<span style="color:var(--green)">✓ ${r.converted} → ${fmt.toUpperCase()}</span>${r.failed?` · ${r.failed} ошибок`:''} → ${esc(r.out_dir)}`;
    }
    toast('🎛 Готово','var(--green)');
  } catch(e){ out.innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`; }
  finally { btn.disabled = false; btn.textContent = _txt; }
}
function coderProgress(m) {
  const box = document.getElementById('coder-progress');
  if(!box) return;
  box.style.display = 'block';
  const bar = document.getElementById('coder-pbar');
  if(bar) bar.style.width = (m.pct||0) + '%';
  const lbl = document.getElementById('coder-plabel');
  const opName = window.t
    ? t(m.op==='mix'?'cd.op_mix':m.op==='split'?'cd.op_split':'cd.op_convert')
    : (m.op==='mix'?'Склейка':m.op==='split'?'Сплит':'Конвертация');
  const trk = window.t ? t('cd.op_track') : 'трек';
  if(lbl) lbl.textContent = `${opName}: ${m.label||''} — ${trk} ${m.current}/${m.total} · ${m.pct||0}%`;
  if((m.pct||0) >= 100) setTimeout(()=>{ box.style.display='none'; }, 1800);
}
async function coderDownloadConvert() {
  const url = document.getElementById('coder-url').value.trim();
  if(!url){ toast('Вставь ссылку','var(--red)'); return; }
  // Just enqueue the download in the service's NATIVE quality — we do NOT touch
  // the global transcode setting (that silently degraded every download). Convert
  // afterwards here in Coder by picking the downloaded folder.
  document.getElementById('url-input').value = url;
  await addUrl();
  document.getElementById('coder-url').value = '';
  toast('Скачиваю в исходном качестве. После загрузки выбери папку тут и сконвертируй.','var(--green)',7000);
  showView('queue', document.querySelector('[data-view=queue]'));
}

// ── Теггер (Mp3tag-style) ───────────────────────────────────────
let _tagRows = [];
let _tagFolders = [];
async function taggerInit() {
  const tree = document.getElementById('tag-tree');
  if(tree) await mountFolderTree(tree, (path, name, tracks)=>{
    document.getElementById('tag-path').value = path || '';
    if(path) taggerLoad();           // auto-load tags on selection
  });
}
function taggerPickFolder() {
  const f = _tagFolders[+document.getElementById('tag-src').value];
  if(f) document.getElementById('tag-path').value = f.dir;
}
function _tagCell(v, minw=90) {
  return `<input value="${(v||'').replace(/"/g,'&quot;')}" style="width:100%;min-width:${minw}px;background:transparent;border:1px solid transparent;border-radius:4px;color:var(--text);font-size:12px;padding:3px 5px" onfocus="this.style.borderColor='var(--border2)'" onblur="this.style.borderColor='transparent'"/>`;
}
function _tagRender() {
  const tb = document.getElementById('tag-tbody');
  if(!_tagRows.length){ tb.innerHTML = '<tr><td colspan="9" style="padding:18px;text-align:center;color:var(--muted)">Пусто</td></tr>'; return; }
  tb.innerHTML = _tagRows.map((r,i)=>`<tr data-i="${i}" style="border-top:1px solid var(--border)">
    <td style="padding:3px 8px;color:var(--muted)">${r.track||i+1}</td>
    <td style="padding:3px 8px;color:var(--muted);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc((r.subdir?r.subdir+'/':'')+r.file)}">${r.subdir?`<span style="color:#5ec8e0">${esc(r.subdir)}/</span>`:''}${esc(r.file)}</td>
    <td style="padding:3px 4px" class="tc-title">${_tagCell(r.title)}</td>
    <td style="padding:3px 4px" class="tc-artist">${_tagCell(r.artist)}</td>
    <td style="padding:3px 4px" class="tc-album">${_tagCell(r.album)}</td>
    <td style="padding:3px 4px" class="tc-albumartist">${_tagCell(r.albumartist)}</td>
    <td style="padding:3px 4px" class="tc-track">${_tagCell(r.track,40)}</td>
    <td style="padding:3px 4px" class="tc-year">${_tagCell(r.year,46)}</td>
    <td style="padding:3px 4px" class="tc-genre">${_tagCell(r.genre,70)}</td></tr>`).join('');
}
async function taggerLoad() {
  const dir = document.getElementById('tag-path').value.trim();
  if(!dir){ toast('Выбери папку или путь','var(--red)'); return; }
  try {
    const r = await api('POST','/api/tagger/read',{dir});
    _tagRows = r.rows || [];
    _tagRender();
    document.getElementById('tag-srcinfo').textContent = `${_tagRows.length} файлов загружено`;
  } catch(e){ toast('Теггер: '+e.message,'var(--red)'); }
}
async function taggerFetchAlbum() {
  const url = document.getElementById('tag-url').value.trim();
  if(!url){ toast('Вставь ссылку альбома','var(--red)'); return; }
  if(!_tagRows.length){ toast('Сначала загрузи папку','var(--red)'); return; }
  try {
    const a = await api('POST','/api/tagger/album',{url});
    const tracks = a.tracks || [];
    document.getElementById('tag-albuminfo').innerHTML =
      `<span style="color:var(--green)">«${esc(a.album)}» — ${a.count} тр. · ${esc(a.albumartist||'')} · ${esc(a.year||'')}</span>`;
    // Show the canonical tracklist so you can verify it's the right release
    // (and spot a track-count mismatch) BEFORE it overwrites your files.
    const tl = document.getElementById('tag-tracklist');
    if(tl){
      const mism = tracks.length !== _tagRows.length;
      tl.style.display = 'block';
      tl.innerHTML =
        `<div style="position:sticky;top:0;background:var(--surface2);padding:5px 10px;font-size:11px;font-weight:700;display:flex;justify-content:space-between;align-items:center">
           <span>Треклист релиза — ${tracks.length} тр.</span>
           ${mism?`<span style="color:var(--orange)">⚠ файлов загружено ${_tagRows.length}</span>`:`<span style="color:var(--green)">✓ совпадает с файлами (${_tagRows.length})</span>`}
         </div>` +
        tracks.map(t=>`<div style="display:flex;gap:8px;padding:3px 10px;font-size:11px;border-top:1px solid var(--border)">
          <span style="color:var(--muted);min-width:22px;text-align:right">${t.num||''}</span>
          <span style="color:var(--text);flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(t.title||'')}</span>
          <span style="color:var(--muted);max-width:42%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;text-align:right">${esc(t.artist||'')}</span>
        </div>`).join('');
    }
    const byNum = {}; tracks.forEach(t=>{ if(t.num) byNum[t.num]=t; });
    // When track count matches file count, map SEQUENTIALLY by position — this
    // is what makes multi-disc releases line up (disc1 then disc2…), since both
    // the files and the tracklist are ordered disc-major. Otherwise fall back to
    // matching by the file's own track number.
    const seq = tracks.length === _tagRows.length;
    const trs = document.querySelectorAll('#tag-tbody tr');
    _tagRows.forEach((r,i)=>{
      const t = seq ? tracks[i] : (byNum[parseInt(r.track)] || tracks[i]);
      r._albumartist=a.albumartist; r._year=a.year; r._label=a.label; r._cover=a.cover; r._tracktotal=a.count;
      const tr = trs[i]; if(!tr || !t) return;
      const set=(cls,v)=>{ const inp=tr.querySelector('.'+cls+' input'); if(inp){ inp.value=v||''; inp.style.background='rgba(94,200,224,.12)'; } };
      r._disc = t.disc || r.disc || '';
      set('tc-title',t.title); set('tc-artist',t.artist); set('tc-album',a.album);
      set('tc-albumartist',a.albumartist); set('tc-track',t.num||r.track); set('tc-year',a.year);
    });
    toast('Треклист лёг на файлы — проверь и применяй','var(--green)');
  } catch(e){ toast('Теггер: '+e.message,'var(--red)'); }
}
async function taggerSearch() {
  const q = document.getElementById('tag-q').value.trim();
  const svc = document.getElementById('tag-svc').value;
  if(!q){ toast('Введи запрос','var(--red)'); return; }
  const box = document.getElementById('tag-results');
  box.innerHTML = '<div style="font-size:11px;color:var(--muted);padding:6px">Ищу…</div>';
  try {
    const r = await api('POST','/api/tagger/search',{query:q,service:svc});
    const res = r.results||[];
    if(!res.length){ box.innerHTML='<div style="font-size:11px;color:var(--muted);padding:6px">Ничего не найдено</div>'; return; }
    box.innerHTML = res.map(x=>`<div onclick="taggerPickResult('${encodeURIComponent(x.url)}')" style="display:flex;align-items:center;gap:9px;padding:6px 8px;border:1px solid var(--border);border-radius:8px;margin-bottom:4px;cursor:pointer" onmouseover="this.style.background='var(--surface2)'" onmouseout="this.style.background='transparent'">
      ${x.cover?`<img src="${esc(x.cover)}" style="width:34px;height:34px;border-radius:5px;object-fit:cover"/>`:'<div style="width:34px;height:34px;border-radius:5px;background:var(--surface2);display:flex;align-items:center;justify-content:center">🎵</div>'}
      <div style="min-width:0"><div style="font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(x.title)}</div>
      <div style="font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"><span style="color:var(--text)">${esc(x.artist||'—')}</span><span style="color:var(--muted)">${x.year?' · '+esc(x.year):''}${x.tracks?' · '+x.tracks+' тр.':''}</span></div></div></div>`).join('');
  } catch(e){ box.innerHTML=`<div style="font-size:11px;color:var(--red);padding:6px">${esc(e.message)}</div>`; }
}
function taggerPickResult(encUrl) {
  const url = decodeURIComponent(encUrl);
  document.getElementById('tag-url').value = url;
  document.getElementById('tag-results').innerHTML = '';
  taggerFetchAlbum();
}
async function taggerApplyAll() {
  if(!_tagRows.length){ toast('Нет файлов','var(--red)'); return; }
  const clear = document.getElementById('tag-clear').checked;
  const keep  = document.getElementById('tag-keepcover').checked;
  const embed = document.getElementById('tag-embedcover')?.checked;
  const res   = document.getElementById('tag-applyres');
  const btn   = document.getElementById('tag-apply'); btn.disabled = true;
  const box   = document.getElementById('tag-progress'); const bar = document.getElementById('tag-pbar');
  if(box){ box.style.display='block'; if(bar) bar.style.width='0'; }
  const trs = document.querySelectorAll('#tag-tbody tr');
  let ok=0, fail=0, coversDone=0; const total=_tagRows.length;
  for(let i=0;i<total;i++){
    const r=_tagRows[i], tr=trs[i]; if(!tr) continue;
    const numCell = tr.querySelector('td');                 // first cell = #
    if(numCell) numCell.innerHTML = '<span class="qi-spinner"></span>';
    const g=cls=>tr.querySelector('.'+cls+' input')?.value.trim()||'';
    const fields={ title:g('tc-title'), artist:g('tc-artist'), album:g('tc-album'), year:g('tc-year'),
      albumartist:g('tc-albumartist')||r._albumartist||'', track:g('tc-track')||r.track||(i+1),
      disc:r._disc||r.disc||'', genre:g('tc-genre'), tracktotal:r._tracktotal||total, label:r._label||'' };
    const payload={path:r.path,fields,clear,keep_cover:keep};
    if(embed && r._cover){ payload.cover=r._cover; payload.embed_cover=true; }
    let good=false, cov=false;
    try { const rr=await api('POST','/api/tagger/apply',payload); good=!!rr.ok; cov=!!rr.cover; }
    catch(e){}
    good?ok++:fail++; if(cov) coversDone++;
    if(numCell) numCell.innerHTML = good ? '<span style="color:var(--green)">✓</span>' : '<span style="color:var(--red)">✗</span>';
    res.textContent=`Применяю… ${ok+fail}/${total}`;
    if(bar) bar.style.width = Math.round((ok+fail)/total*100)+'%';
  }
  btn.disabled=false;
  res.innerHTML=`<span style="color:var(--green)">✓ ${ok} применено</span>${coversDone?` · <span style="color:var(--muted)">🖼 ${coversDone} обложек</span>`:''}${fail?` · <span style="color:var(--red)">${fail} ошибок</span>`:''}`;
  if(box) setTimeout(()=>{ box.style.display='none'; }, 1800);
  toast('🏷 Теги записаны','var(--green)');
}

async function taggerRename(dry) {
  const template = document.getElementById('tag-mask').value.trim();
  if(!template){ toast('Введи маску','var(--red)'); return; }
  if(!_tagRows.length){ toast('Сначала загрузи папку','var(--red)'); return; }
  const files = _tagRows.map(r=>r.path);
  const prev  = document.getElementById('tag-renamepreview');
  const res   = document.getElementById('tag-renameres');
  const btn   = document.getElementById('tag-renamebtn');
  try {
    const r = await api('POST','/api/tagger/rename',{files,template,dry_run:dry});
    if(dry){
      const rows = r.preview||[];
      prev.style.display='block';
      prev.innerHTML = rows.map(x=>`<div style="display:flex;gap:8px;align-items:center;padding:3px 10px;border-top:1px solid var(--border)">
        <span style="flex:1;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${esc(x.old)}">${esc(x.old)}</span>
        <span style="color:${x.change?'var(--orange)':'var(--muted)'}">→</span>
        <span style="flex:1;color:${x.change?'var(--text)':'var(--muted)'};white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${esc(x.new)}">${esc(x.new)}</span></div>`).join('');
      res.innerHTML = `<span style="color:var(--muted)">Изменится: ${r.count} из ${rows.length}</span>`;
      const can = r.count>0;
      btn.disabled=!can; btn.style.opacity=can?'1':'.5';
    } else {
      res.innerHTML = `<span style="color:var(--green)">✓ Переименовано ${r.renamed}</span>`;
      prev.style.display='none'; btn.disabled=true; btn.style.opacity='.5';
      toast(`✏️ Переименовано ${r.renamed}`,'var(--green)');
      await taggerLoad();   // reload so the table shows the new filenames
    }
  } catch(e){ toast('Переименование: '+e.message,'var(--red)'); }
}

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
  // Optimistic: drop the card from the UI immediately so ✕ feels instant, then
  // tell the server. The periodic pullQueue / WS reconciles if the delete failed.
  S.queue = S.queue.filter(t => t.id !== id);
  renderQueue(); updateTransport();
  try { await api('DELETE', `/api/queue/${id}`); } catch(e) { pullQueue(); }
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

