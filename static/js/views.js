const _VIEW_FILES = [
  'queue','settings','releases','soundcloud','bbc','library','coder','tagger',
  'stats','spectrogram','search','history','watchlist','console',
  'guest-tokens','guest-prefs','setup','admin','bot','telemetry','digs'
];

// Only fetch a fragment when its container actually exists in THIS build's
// index.html. The public build ships fewer views than the owner build (no
// admin/bot/telemetry/guest-*), and a fragment nobody can display is a pure 404.
//
// And never let one bad fragment kill the boot: this used to be Promise.all, so
// a single 404 rejected the whole thing, `await _loadAllViews()` threw in app.js's
// load handler, and everything after it — applyLang, loadQualities, connectWS,
// loadAppInfo — never ran. The result was a black content area with a live
// sidebar and "v–" as the version, for every public user. allSettled degrades to
// "that one view is empty" instead.
async function _loadAllViews() {
  const wanted = _VIEW_FILES.filter(n => document.getElementById('view-' + n));
  const results = await Promise.allSettled(
    wanted.map(n =>
      fetch(`/static/views/${n}.html?v=106`)
        .then(r => { if (!r.ok) throw new Error(`views/${n}.html ${r.status}`); return r.text(); })
    )
  );
  wanted.forEach((n, i) => {
    const el = document.getElementById('view-' + n);
    if (!el) return;
    if (results[i].status !== 'fulfilled') {
      console.warn('[views] fragment missing, view left empty:', n, results[i].reason);
      return;
    }
    el.innerHTML = results[i].value;
    // Setting innerHTML NEVER executes <script> tags — so any inline <script>
    // inside a view fragment was silently dead. (This is why settings.html's
    // wrapper selector buttons did nothing: setAppleWrapper was never defined,
    // so onclick threw ReferenceError.) Re-create each script node so it runs.
    el.querySelectorAll('script').forEach(old => {
      const s = document.createElement('script');
      for (const a of old.attributes) s.setAttribute(a.name, a.value);
      s.textContent = old.textContent;
      old.replaceWith(s);
    });
  });
  if (typeof applyLang === 'function') applyLang();
}
