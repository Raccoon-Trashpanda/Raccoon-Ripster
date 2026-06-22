// Layout diagnostic — открой Settings → Deezer, потом из консоли:
//   fetch('/static/diag-layout.js').then(r=>r.text()).then(eval)
(function () {
  const vb = document.querySelector('#view-settings .view-body');
  const p  = document.querySelector('#stab-deezer');
  if (!vb) { console.warn('view-body not found'); return; }
  const vbcs = getComputedStyle(vb);
  console.log('%c=== view-body ===', 'color:#c084a0;font-weight:bold');
  console.log({
    display:        vbcs.display,
    flexDir:        vbcs.flexDirection,
    alignItems:     vbcs.alignItems,
    justifyContent: vbcs.justifyContent,
    padding:        vbcs.padding,
    height:         vb.offsetHeight,
    scrollH:        vb.scrollHeight,
  });
  console.log('%c=== stab-deezer ===', 'color:#c084a0;font-weight:bold');
  if (p) {
    const pcs = getComputedStyle(p);
    console.log({
      display:   pcs.display,
      marginTop: pcs.marginTop,
      position:  pcs.position,
      top:       pcs.top,
      offsetTop: p.offsetTop,
      height:    p.offsetHeight,
    });
  } else {
    console.log('NOT FOUND (открой Settings → Deezer таб)');
  }
  console.log('%c=== children of view-body ===', 'color:#c084a0;font-weight:bold');
  console.table(
    Array.from(vb.children).map(c => ({
      tag:     c.tagName,
      id:      c.id || '',
      cls:     (c.className || '').slice(0, 50),
      display: getComputedStyle(c).display,
      h:       c.offsetHeight,
      top:     c.offsetTop,
    })),
  );
})();
