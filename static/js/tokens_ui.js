// ======================================================================
// Tokens view UI
// Extracted from app.js (mechanical split — same global functions, no behaviour
// change). Loaded AFTER app.js in index.html, so it sees S/api/toast/etc.
// ======================================================================

// ── TOKENS ───────────────────────────────────────────────────
function _isMasked(v){ return typeof v === 'string' && v.startsWith('••'); }

// Set a secret/password field: if value is masked, clear the field and show
// a placeholder so the user sees an empty input (safe to paste into) instead
// of a mask string that would be re-sent and blocked by the server.
// Never overwrites a field the user is currently editing (document.activeElement check).
function _setSecret(id, v) {
  const el = document.getElementById(id);
  if (!el) return;
  if (document.activeElement === el) return; // user is typing — don't clobber
  if (_isMasked(v)) {
    const m = v.match(/\((\d+)\s/);
    el.placeholder = m ? ti('s.saved_chars', {n: m[1]}) : t('s.saved_replace');
    el.value = '';
  } else {
    el.placeholder = el.dataset.ph || '••••••••';
    el.value = v || '';
  }
}

function loadTokensToUI() {
  // If the server redacted a secret, show the field as empty with a hint
  // in the placeholder — never echo the mask dots into the input.
  const mut    = S.config['media-user-token']    || '';
  const bearer = S.config['authorization-token'] || '';
  const sf     = S.config['storefront']          || '';
  const mutEl    = document.getElementById('t-mut');
  const bearerEl = document.getElementById('t-bearer');
  if(mutEl){
    if(_isMasked(mut)){
      mutEl.value = '';
      mutEl.placeholder = ti('s.saved_chars2', {n: mut.match(/\d+/)?.[0]||'?'});
    } else { mutEl.value = mut; }
  }
  if(bearerEl){
    if(_isMasked(bearer)){
      bearerEl.value = '';
      bearerEl.placeholder = ti('s.saved_chars2', {n: bearer.match(/\d+/)?.[0]||'?'});
    } else { bearerEl.value = bearer; }
  }
  setVal('t-sf', sf);
}

async function saveTokens() {
  const mut    = document.getElementById('t-mut').value.trim();
  const bearer = document.getElementById('t-bearer').value.trim();
  const sf     = document.getElementById('t-sf').value.trim();
  // Only send fields the user actually filled in — empty means "keep existing".
  const patch  = {storefront: sf};
  if(mut    && !_isMasked(mut))    patch['media-user-token']     = mut;
  if(bearer && !_isMasked(bearer)) patch['authorization-token']  = bearer;
  await api('POST','/api/config', patch);
  if(mut)    S.config['media-user-token']    = mut;
  if(bearer) S.config['authorization-token'] = bearer;
  S.config['storefront'] = sf;
  updatePills();
  toast('Tokens saved!');
  // also notify via WebSocket so server can use them immediately
  if(ws?.readyState===WebSocket.OPEN && (mut || bearer)) {
    ws.send(JSON.stringify({type:'token_update', bearer: bearer||undefined, mut: mut||undefined}));
  }
}

async function autoFetchBearer() {
  const btn = document.getElementById('btn-autofetch');
  const status = document.getElementById('bearer-status');
  btn.disabled = true;
  btn.textContent = '⏳ Fetching…';
  status.textContent = 'Connecting to Apple Music…';
  try {
    const r = await fetch('/api/fetch-bearer');
    const data = await r.json();
    if(r.ok && data.token) {
      document.getElementById('t-bearer').value = data.token;
      S.config['authorization-token'] = data.token;
      status.textContent = '✓ Got token: ' + data.token.slice(0,20) + '…';
      status.style.color = 'var(--green)';
      updatePills();
      toast('Bearer token auto-fetched! 🎉', 'var(--green)');
    } else {
      status.textContent = data.detail || 'Failed';
      status.style.color = 'var(--red)';
      toast('Auto-fetch failed — paste manually', 'var(--red)');
    }
  } catch(e) {
    status.textContent = 'Network error: ' + e.message;
    status.style.color = 'var(--red)';
    toast('Error: ' + e.message, 'var(--red)');
  } finally {
    btn.disabled = false;
    btn.textContent = '⚡ Auto-fetch from Apple Music';
  }
}

function copyAllTokens() {
  const lines = [
    S.config['media-user-token']&&`media-user-token: ${S.config['media-user-token']}`,
    S.config['authorization-token']&&`bearer: ${S.config['authorization-token']}`,
    S.config['storefront']&&`storefront: ${S.config['storefront']}`,
  ].filter(Boolean).join('\n');
  if(lines){ navigator.clipboard.writeText(lines); toast('Copied!'); }
  else toast('No tokens saved yet','var(--orange)');
}

