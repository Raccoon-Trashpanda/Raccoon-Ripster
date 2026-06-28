// ======================================================================
// Config YAML editor UI
// Extracted from app.js (mechanical split — same global functions, no behaviour
// change). Loaded AFTER app.js in index.html, so it sees S/api/toast/etc.
// ======================================================================

// ── CONFIG YAML ───────────────────────────────────────────────
function renderConfig() {
  const c = S.config;
  const q = QUALITIES.find(x=>x.id===c['quality'])||QUALITIES[0]||{};
  const yaml = `# Apple Music Downloader — config.yaml
# Quality: ${q.label||''} (${q.sub||''})  flag: ${q.flag||'(default)'}

media-user-token: "${c['media-user-token']||''}"
authorization-token: ""  # auto-fetched from browser
storefront: "${c['storefront']||'us'}"
language: "${c['language']||''}"

embed-cover: ${c['embed-cover']!==false}
cover-size: ${c['cover-size']==='original'?'0':c['cover-size']||'3000x3000'}
cover-format: ${c['cover-format']||'jpg'}
save-artist-cover: ${!!c['save-cover-to-folder']}
save-animated-artwork: false

embed-lrc: ${c['embed-lrc']!==false}
save-lrc-file: ${!!c['save-lrc-file']}
lrc-type: "${c['lrc-type']||'lyrics'}"
lrc-format: "${c['lrc-format']||'lrc'}"

alac-save-folder: "${c['save-path']||'downloads'}"
atmos-save-folder: "${c['save-path']||'downloads'}/Atmos"
aac-save-folder: "${c['save-path']||'downloads'}/AAC"

decrypt-m3u8-port: "${c['decrypt-port']||'127.0.0.1:10020'}"
get-m3u8-port: "${c['m3u8-port']||'127.0.0.1:20020'}"
max-memory-limit: ${c['max-memory']||256}
atmos-max: ${c['atmos-max']||2448}`;

  const el = document.getElementById('config-code');
  if(el) el.innerHTML = yaml
    .replace(/^([\w-]+):/gm, '<span class="ck">$1</span>:')
    .replace(/(#.*)$/gm, '<span class="cm">$1</span>')
    .replace(/"([^"]*)"/g, '<span class="cs">"$1"</span>');

  // CLI commands
  const cmdsEl = document.getElementById('cli-cmds');
  if(cmdsEl) {
    if(!S.queue.length){
      cmdsEl.innerHTML = `<div style="font-size:12px;color:var(--muted)">Add items to queue to see commands</div>`;
    } else {
      cmdsEl.innerHTML = S.queue.map(t=>{
        const q2 = QUALITIES.find(x=>x.id===t.quality)||QUALITIES[0]||{flag:''};
        const flag = q2.flag?q2.flag+' ':'';
        return `<div class="code-block" style="font-size:10.5px;padding:8px 12px">go run main.go ${flag}"${t.url}"</div>`;
      }).join('');
    }
  }
}

function copyConfig() {
  const c = S.config;
  const q = QUALITIES.find(x=>x.id===c['quality'])||{label:'',sub:'',flag:''};
  const yaml = `media-user-token: "${c['media-user-token']||''}"\nstorefront: "${c['storefront']||'us'}"\nquality: ${c['quality']||'alac'}\nembed-cover: ${c['embed-cover']!==false}\ncover-size: ${c['cover-size']||'3000x3000'}\nembed-lrc: ${c['embed-lrc']!==false}`;
  navigator.clipboard.writeText(yaml);
  toast('config.yaml copied!');
}
function refreshConfig(){ renderConfig(); toast('Refreshed'); }

// CONSOLE (log console view: render, copy, download, fix-deps) → moved to its own module file (see index.html).

