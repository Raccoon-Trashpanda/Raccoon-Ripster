const _VIEW_FILES = [
  'queue','settings','releases','soundcloud','bbc','coder',
  'spectrogram','search','history','console','setup'
];

async function _loadAllViews() {
  const results = await Promise.all(
    _VIEW_FILES.map(n =>
      fetch(`/static/views/${n}.html?v=62`)
        .then(r => { if (!r.ok) throw new Error(`views/${n}.html ${r.status}`); return r.text(); })
    )
  );
  _VIEW_FILES.forEach((n, i) => {
    const el = document.getElementById('view-' + n);
    if (!el) return;
    el.innerHTML = results[i];
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
