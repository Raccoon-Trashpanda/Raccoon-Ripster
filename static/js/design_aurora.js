/* Aurora — новый дизайн оболочки.
 * Альтернативное меню с группировкой: «Система» объединяет отладку и админ.
 * Регистрируется в RpDesigns, загружает shell_v4.css.
 */
(function() {
  'use strict';

  var MENU = [
    { section: 'nav.s_main', items: [
      { view: 'queue', icon: '⬇', key: 'nav.queue' },
      { view: 'search', icon: '🔍', key: 'nav.search' },
      { view: 'pulse', icon: '◎', key: 'nav.pulse' },
      { view: 'history', icon: '◷', key: 'nav.history', owner: true },
      { view: 'watchlist', icon: '★', key: 'nav.watchlist', owner: true },
      { view: 'releases', icon: '♻', key: 'nav.releases', owner: true },
      { view: 'upcoming', icon: '📅', key: 'nav.upcoming', owner: true },
      { view: 'bbc', icon: '📻', label: 'BBC Sounds' },
      { view: 'soundcloud', icon: '☁', label: 'SoundCloud' }
    ]},
    { section: 'nav.s_lib', items: [
      { view: 'library', icon: '🎵', key: 'nav.library', owner: true },
      { view: 'coder', icon: '🎛', label: 'Ripster Coder', owner: true },
      { view: 'tagger', icon: '🏷', key: 'nav.tagger', owner: true },
      { view: 'spectrogram', icon: '📊', key: 'nav.spectrum', owner: true },
      { view: 'stations', icon: '◉', key: 'nav.stations', owner: true },
      { view: 'digs', icon: '◆', key: 'nav.digs', owner: true },
      { view: 'stats', icon: '📈', key: 'nav.stats', owner: true }
    ]},
    { section: 'sys.title', system: true, items: [
      { view: 'setup', icon: '🔧', label: 'Setup' },
      { view: 'console', icon: '🖥', label: 'Console' },
      { view: 'admin', icon: '⚙', label: 'Admin', owner: true },
      { view: 'bot', icon: '🦝', key: 'nav.bot', owner: true },
      { view: 'telemetry', icon: '🛰', key: 'nav.telemetry', owner: true }
    ]},
    { section: 'nav.s_config', items: [
      { view: 'settings', icon: '⚙️', key: 'nav.settings', owner: true }
    ]}
  ];

  window.RpDesigns.register({
    id: 'aurora',
    title: (typeof t === 'function' ? t('design.aurora.title') : 'Aurora'),
    description: (typeof t === 'function' ? t('design.aurora.desc') : ''),
    reload: true,
    slots: {
      attention: '.topbar',
      hints: '.main',
      'queue-exit': '.card-grid',
      'first-run': 'body',
      palette: 'body',
      pulse: '#view-pulse',
      system: '#view-system'
    },
    styles: ['/static/css/shell_v4.css?v=5'],
    apply: applyAurora
  });

  function tr(key) { return (typeof t === 'function') ? t(key) : key; }

  function applyAurora() {
    document.documentElement.dataset.design = 'aurora';
    var sidebar = document.querySelector('.sidebar');
    if (!sidebar) return;
    var scroll = sidebar.querySelector('.nav-scroll');
    if (!scroll || scroll.dataset.aurora === '1') return;
    scroll.dataset.aurora = '1';
    scroll.innerHTML = buildMenu();
    scroll.querySelectorAll('.aur-item').forEach(function(el) {
      el.addEventListener('click', function() {
        var v = el.dataset.view;
        if (v === 'system') {
          var first = (window.RpShell && RpShell.SYS_TABS && RpShell.SYS_TABS[0]) || 'setup';
          if (window.RpShell) RpShell.showSystem(first);
        } else if (typeof showView === 'function') {
          showView(v, el);
        }
        _syncActive(v);
      });
    });
  }

  function buildMenu() {
    var h = '';
    MENU.forEach(function(group) {
      h += '<div class="nav-section">' + esc(tr(group.section)) + '</div>';
      if (group.system) {
        // 01.10.2026: «Система» — один пункт в меню, без подпунктов слева.
        // Подвкладки (Setup/Console/Admin/Бот/Диагностика) показываются только плашками сверху вида.
        h += '<div class="aur-item" data-view="system">'
          + '<span class="icon">🗂</span> <span>' + esc(tr('sys.title')) + '</span>'
          + '</div>';
      } else {
        group.items.forEach(function(item) {
          var cls = 'aur-item';
          if (item.owner) cls += ' owner-only';
          h += '<div class="' + cls + '" data-view="' + item.view + '">'
            + '<span class="icon">' + item.icon + '</span> '
            + '<span>' + esc(item.key ? tr(item.key) : item.label) + '</span>'
            + '</div>';
        });
      }
    });
    return h;
  }

  function _syncActive(viewName) {
    var scroll = document.querySelector('.nav-scroll');
    if (!scroll) return;
    scroll.querySelectorAll('.aur-item').forEach(function(el) {
      el.classList.toggle('active', el.dataset.view === viewName);
    });
    // Раскрыть подвкладки «Система», если активна одна из них.
    var sysViews = ['setup', 'console', 'admin', 'bot', 'telemetry'];
    if (sysViews.indexOf(viewName) >= 0) {
      var sysItem = scroll.querySelector('.aur-item[data-view="system"]');
      if (sysItem) sysItem.classList.add('active');
    }
  }

  function esc(s) {
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }
})();
