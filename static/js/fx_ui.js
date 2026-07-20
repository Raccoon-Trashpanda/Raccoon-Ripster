/* fx_ui.js — Yandex-style polish:
   1) a thin network-activity loading bar across the very top (NProgress-style),
      driven by a fetch() wrapper so it reflects real library/network loading;
   2) a cursor-following soft glow + glossy sheen on buttons (the highlight
      tracks the mouse position inside each button).
   Self-contained, additive, no dependency on app.js internals. */
(function () {
  'use strict';

  // ── 1. Top network loading bar ──────────────────────────────────────────
  let inflight = 0, startT = null, shown = false, bar = null;
  function el() { if (!bar) bar = document.getElementById('net-progress'); return bar; }
  function show() {            // only fires if loading is STILL going after the delay
    if (shown || inflight <= 0) return;
    shown = true;
    const b = el(); if (b) b.classList.add('running');
  }
  function begin() {
    inflight++;
    // Debounce: don't flash the bar for quick background polls — only show it
    // when a request is genuinely slow (still in flight after 280ms).
    if (inflight === 1) { clearTimeout(startT); startT = setTimeout(show, 280); }
  }
  function end() {
    inflight = Math.max(0, inflight - 1);
    if (inflight !== 0) return;
    clearTimeout(startT);
    if (!shown) return;       // fast poll finished before the bar ever appeared
    const b = el();
    if (b) b.classList.remove('running');
    shown = false;
  }
  // Background status/queue polls run every few seconds — they must NOT flash
  // the bar. Only genuine content/navigation loads drive it.
  const SKIP = /(\/api\/queue|status|releases|telemetry|wrapper-status|wrapper\/logs|\/ping)/i;
  const _fetch = window.fetch;
  if (typeof _fetch === 'function') {
    window.fetch = function (input) {
      let url = '';
      try { url = typeof input === 'string' ? input : (input && input.url) || ''; } catch (_) {}
      if (SKIP.test(url)) return _fetch.apply(this, arguments);   // background poll — no bar
      begin();
      let p;
      try { p = _fetch.apply(this, arguments); } catch (e) { end(); throw e; }
      return p.then(r => { end(); return r; }, e => { end(); throw e; });
    };
  }

  // ── 2. Cursor-following glow on buttons ─────────────────────────────────
  const SEL = '.btn-red,.btn-ghost,.btn-orange,.pp-transport,.pp-extra,' +
              '#pp-play,#pp-play-big,.nav-item,.lang-flag-btn';
  function tag(root) {
    try { (root || document).querySelectorAll(SEL).forEach(b => b.classList.add('fx-glow')); } catch (_) {}
  }
  document.addEventListener('mousemove', (e) => {
    const t = e.target;
    const b = t && t.closest ? t.closest('.fx-glow') : null;
    if (!b) return;
    const r = b.getBoundingClientRect();
    if (!r.width) return;
    b.style.setProperty('--mx', ((e.clientX - r.left) / r.width * 100) + '%');
    b.style.setProperty('--my', ((e.clientY - r.top) / r.height * 100) + '%');
  }, { passive: true });

  function init() {
    tag();
    // Views/cards load dynamically — tag new buttons as they appear.
    try {
      const mo = new MutationObserver((muts) => {
        for (const m of muts) for (const n of m.addedNodes) {
          if (n.nodeType !== 1) continue;
          if (n.matches && n.matches(SEL)) n.classList.add('fx-glow');
          tag(n);
        }
      });
      mo.observe(document.body, { childList: true, subtree: true });
    } catch (_) {}
  }
  if (document.readyState !== 'loading') init();
  else document.addEventListener('DOMContentLoaded', init);
})();
