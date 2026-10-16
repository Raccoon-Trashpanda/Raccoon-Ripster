/* Настройки авто-уборки очереди (ключи queue-tidy-*, queue-stale-*; цикл — ripster/queue_tidy.py).
 * + Выбор анимаций ухода плиток (queue-exit-anims; задача 064).
 * Один namespace, без верхнеуровневых let/const. Значения клампятся так же, как на сервере. */
(function () {
  'use strict';

  var FIELDS = [
    { id: 's-qt-done-min',  key: 'queue-tidy-done-minutes', lo: 0, hi: 30,  def: 0 },
    { id: 's-qt-error-h',   key: 'queue-tidy-error-hours',  lo: 0, hi: 168, def: 24 },
    { id: 's-qt-stale-h',   key: 'queue-stale-hours',       lo: 0, hi: 168, def: 24 }
  ];

  var ANIM_LABELS = {
    left:      '← Влево',
    right:     '→ Вправо',
    up:        '↑ В потолок',
    rocket:    '🚀 Ракета',
    hatch:     '⬇ Люк',
    blackhole: '🕳 Чёрная дыра',
    ribbon:    '🎀 Лента',
    balloon:   '🎈 Шар',
    pixels:    '▦ Пиксели'
  };

  function clamp(v, f) {
    var n = parseInt(v, 10);
    if (isNaN(n)) n = f.def;
    return Math.max(f.lo, Math.min(f.hi, n));
  }

  function fill(c) {
    c = c || {};
    FIELDS.forEach(function (f) {
      var el = document.getElementById(f.id);
      if (el) el.value = clamp(c[f.key], f);
    });
    var inst = document.getElementById('s-qt-instant');
    if (inst) inst.checked = (c['queue-tidy-instant'] === true);
    fillAnimTiles(c);
  }

  function save(id) {
    var f = FIELDS.filter(function (x) { return x.id === id; })[0];
    var el = document.getElementById(id);
    if (!f || !el) return;
    var v = clamp(el.value, f);
    el.value = v;
    saveSetting(f.key, v);
  }

  function fillAnimTiles(c) {
    var container = document.getElementById('s-qe-tiles');
    if (!container) return;
    var enabled = c['queue-exit-anims'];
    if (typeof enabled === 'string') {
      try { enabled = JSON.parse(enabled); } catch(e) { enabled = null; }
    }
    if (!Array.isArray(enabled)) {
      enabled = Object.keys(ANIM_LABELS);
    }
    var enabledSet = {};
    enabled.forEach(function(id) { enabledSet[id] = true; });

    container.innerHTML = '';
    Object.keys(ANIM_LABELS).forEach(function(id) {
      var label = document.createElement('label');
      label.style.cssText = 'display:flex;align-items:center;gap:5px;padding:4px 10px;border-radius:8px;background:var(--card2,#232734);border:1px solid var(--line,#2a2e39);cursor:pointer;font-size:12px;user-select:none';
      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = !!enabledSet[id];
      cb.dataset.animId = id;
      cb.onchange = function() { saveAnimConfig(); };
      label.appendChild(cb);
      label.appendChild(document.createTextNode(ANIM_LABELS[id]));
      container.appendChild(label);
    });
  }

  function saveAnimConfig() {
    var container = document.getElementById('s-qe-tiles');
    if (!container) return;
    var checked = [];
    var cbs = container.querySelectorAll('input[type=checkbox]');
    for (var i = 0; i < cbs.length; i++) {
      if (cbs[i].checked) checked.push(cbs[i].dataset.animId);
    }
    saveSetting('queue-exit-anims', JSON.stringify(checked));
  }

  function previewAnim() {
    if (typeof RpQueueExit === 'undefined') return;
    var container = document.getElementById('s-qe-tiles');
    if (!container) return;
    var checked = [];
    var cbs = container.querySelectorAll('input[type=checkbox]');
    for (var i = 0; i < cbs.length; i++) {
      if (cbs[i].checked) checked.push(cbs[i].dataset.animId);
    }
    if (!checked.length) {
      if (typeof toast === 'function') toast(t('s.qe_select_one'), 'var(--muted)');
      return;
    }
    var preview = document.getElementById('s-qe-preview-tile');
    if (!preview) {
      preview = document.createElement('div');
      preview.id = 's-qe-preview-tile';
      preview.className = 'qi';
      preview.style.cssText = 'height:60px;background:var(--card,#1c1f27);border:1px solid var(--line,#2a2e39);border-radius:10px;margin-top:8px;display:flex;align-items:center;padding:0 14px;gap:10px;color:var(--muted);font-size:12px';
      preview.textContent = t('s.qe_sample_tile');
      container.parentElement.appendChild(preview);
    }
    preview.style.display = 'flex';
    preview.dataset.leaving = '';
    preview.dataset.detached = '';
    var id = checked[Math.floor(Math.random() * checked.length)];
    RpQueueExit.leave(preview, id).then(function() {
      setTimeout(function() {
        preview.style.display = 'flex';
        preview.dataset.leaving = '';
        preview.dataset.detached = '';
        preview.style.transform = '';
        preview.style.opacity = '';
        preview.style.position = '';
        preview.style.top = '';
        preview.style.left = '';
        preview.style.width = '';
        preview.style.margin = '';
      }, 400);
    });
  }

  window.RpQueueTidy = { fill: fill, save: save, previewAnim: previewAnim };
})();
