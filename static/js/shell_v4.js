/* Shell v4 — общая оболочка: роутер, «Пульт», «Система» с подвкладками.
 * Работает во всех дизайнах. Дизайн лишь решает, где разместить слоты.
 */
(function() {
  'use strict';

  var _curView = null;
  var _syncing = false;
  var _origShow = null;
  var _SYS_TABS = ['setup', 'console', 'admin', 'bot', 'telemetry'];

  var ROUTES = {
    pulse: 'pulse', queue: 'queue', search: 'search',
    history: 'history', watchlist: 'watchlist', releases: 'releases',
    upcoming: 'upcoming', bbc: 'bbc', soundcloud: 'soundcloud',
    library: 'library', coder: 'coder', tagger: 'tagger',
    spectrogram: 'spectrogram', stations: 'stations', digs: 'digs',
    stats: 'stats', settings: 'settings',
    'guest-tokens': 'guest-tokens', 'guest-prefs': 'guest-prefs'
  };

  var REDIRECTS = {
    quality: 'settings', tokens: 'settings',
    tags: 'settings', config: 'settings'
  };

  var SYS_REDIRECT = {
    setup: 'setup', console: 'console',
    admin: 'admin', bot: 'bot', telemetry: 'telemetry'
  };

  function tr(key) { return (typeof t === 'function') ? t(key) : key; }

  function init() {
    _wrapShowView();
    window.addEventListener('hashchange', onHash);
    document.addEventListener('keydown', onKey);
    var h = location.hash;
    if (h && h.length > 1) onHash();
  }

  function onHash() {
    if (_syncing) return;
    var raw = location.hash.replace(/^#\/?/, '');
    if (!raw) return;
    var parts = raw.split('/');
    var seg = parts[0];

    // Aurora использует составной вид «Система» с подвкладками — перенаправляем
    // bot/admin/setup/… туда. Классика имеет эти виды как отдельные — не трогаем.
    var auroraMode = !!(document.querySelector('.nav-scroll') || {}).dataset || {};
    var isAurora = auroraMode.aurora === '1' || document.documentElement.dataset.design === 'aurora';

    if (seg === 'system' && parts[1] && SYS_REDIRECT[parts[1]] && isAurora) {
      showSys(parts[1]);
      return;
    }
    if (SYS_REDIRECT[seg] && isAurora) { showSys(seg); return; }
    // «#/system» без подвкладки — это ровно тот адрес, который showView('system') сам кладёт в location.hash.
    // Без этой ветки hashchange (приходит асинхронно, флаг _syncing к тому времени уже снят) попадал в конец функции
    // и вызывал navigate('queue'): тап по «Система» и по её веткам «ничего не делал», вид откатывался на очередь (01.10.2026).
    if (seg === 'system' && isAurora) {
      if (_curView !== 'system') {
        var _last = 'setup';
        try { _last = sessionStorage.getItem('amd-sys-tab') || 'setup'; } catch (_) {}
        showSys(_last);
      }
      return;
    }
    if (REDIRECTS[seg]) {
      var stab = seg === 'config' ? 'global' : 'apple';
      navigate(REDIRECTS[seg]);
      setTimeout(function() {
        if (typeof showStab === 'function') {
          var el = document.querySelector('.stab[data-stab="' + stab + '"]');
          if (el) showStab(stab, el);
        }
      }, 0);
      return;
    }
    if (ROUTES[seg]) { navigate(ROUTES[seg]); return; }
    // Системные вкладки в Классике — как обычные виды.
    if (SYS_REDIRECT[seg]) { navigate(seg); return; }
    navigate('queue');
  }

  function _wrapShowView() {
    if (typeof showView !== 'function') return;
    _origShow = showView;
    window.showView = function(name, el) {
      _curView = name;
      _syncing = true;
      try { location.hash = '#/' + name; } catch (_) {}
      _syncing = false;
      return _origShow.call(this, name, el);
    };
  }

  function navigate(name) {
    if (typeof showView === 'function') {
      var el = document.querySelector('[data-view="' + name + '"]');
      showView(name, el);
    }
  }

  function currentView() { return _curView; }

  /* ── Система: подвкладки ── */

  function showSys(tab) {
    if (_SYS_TABS.indexOf(tab) < 0) tab = 'setup';
    navigate('system');
    _curView = 'system';
    _renderSysBar(tab);
    var cnt = document.getElementById('sys-content');
    if (cnt) {
      cnt.querySelectorAll('.sys-panel').forEach(function(p) {
        p.classList.toggle('active', p.id === 'sys-p-' + tab);
      });
    }
    _syncSysTabs(tab);
    _markHosts(tab);
    try { sessionStorage.setItem('amd-sys-tab', tab); } catch (_) {}
    _fireInit(tab);
  }

  function _renderSysBar(active) {
    var container = document.getElementById('view-system');
    if (!container || container.querySelector('.sys-bar')) {
      _syncSysTabs(active);
      return;
    }
    // 01.10.2026: main.css:537 растягивает любой прямой <div>-потомок .view.active на всю высоту (flex:1).
    // Плашки подвкладок и сетка Пульта получали ~490 px. Оборачиваем в .view-body — он исключён из того правила.
    var body = document.createElement('div');
    body.className = 'view-body';
    body.style.display = 'flex';
    body.style.flexDirection = 'column';
    body.style.overflow = 'auto';

    var bar = document.createElement('div');
    bar.className = 'sys-bar';
    bar.setAttribute('role', 'tablist');
    bar.setAttribute('aria-label', tr('sys.title'));
    _SYS_TABS.forEach(function(key) {
      var b = document.createElement('button');
      b.className = 'sys-tab' + (key === active ? ' active' : '');
      b.setAttribute('role', 'tab');
      b.setAttribute('data-sys', key);
      b.type = 'button';
      b.textContent = tr('sys.' + key);
      b.addEventListener('click', function() { showSys(key); });
      bar.appendChild(b);
    });
    body.appendChild(bar);

    var content = document.getElementById('sys-content');
    if (!content) {
      content = document.createElement('div');
      content.id = 'sys-content';
    }
    body.appendChild(content);
    container.appendChild(body);

    _SYS_TABS.forEach(function(key) {
      var panel = document.createElement('div');
      panel.id = 'sys-p-' + key;
      panel.className = 'sys-panel' + (key === active ? ' active' : '');
      content.appendChild(panel);
    });
    _populateSys();
  }

  function _populateSys() {
    // 01.10.2026: раньше содержимое видов Setup/Console/Admin/Бот КОПИРОВАЛОСЬ в панель (innerHTML) — получались два
    // комплекта одинаковых id; getElementById отдавал скрытый оригинал, данные писались в него, а в видимой копии
    // «Бот» оставался пустым. Теперь узлы ПЕРЕНОСЯТСЯ: один комплект id, оригинальный контейнер остаётся пустой
    // «меткой» (класс sys-moved — не показывается), а `active` на нём ставит showSys: опросчики Админа и Бота
    // смотрят именно на `#view-admin.active` / `#view-bot.active`.
    _SYS_TABS.forEach(function(key) {
      var src = document.getElementById('view-' + key);
      var dst = document.getElementById('sys-p-' + key);
      if (src && dst && !dst._populated) {
        while (src.firstChild) dst.appendChild(src.firstChild);
        src.classList.add('sys-moved');
        dst.querySelectorAll('script').forEach(function(old) {
          var s = document.createElement('script');
          for (var i = 0; i < old.attributes.length; i++) {
            var a = old.attributes[i];
            s.setAttribute(a.name, a.value);
          }
          s.textContent = old.textContent;
          old.replaceWith(s);
        });
        dst._populated = true;
      }
    });
  }

  function _markHosts(active) {
    _SYS_TABS.forEach(function(key) {
      var host = document.getElementById('view-' + key);
      if (host && host.classList.contains('sys-moved')) host.classList.toggle('active', key === active);
    });
  }

  function _syncSysTabs(active) {
    document.querySelectorAll('.sys-tab').forEach(function(tab) {
      tab.classList.toggle('active', tab.getAttribute('data-sys') === active);
      tab.setAttribute('aria-selected', tab.getAttribute('data-sys') === active ? 'true' : 'false');
    });
  }

  function _fireInit(name) {
    // Единый путь: если в приложении есть VIEW_INIT, берём оттуда.
    // Это тот же механизм, что и в showView — дублирования нет.
    if (typeof VIEW_INIT !== 'undefined' && VIEW_INIT[name]) {
      try { VIEW_INIT[name](); } catch (_) {}
      return;
    }
    // Фолбэк для старых сборок без VIEW_INIT:
    var fn = {
      setup: function() { if (typeof checkTools === 'function') checkTools(); if (typeof loadDeps === 'function') loadDeps(); },
      console: function() { if (typeof _refreshConsole === 'function') _refreshConsole(); },
      admin: function() { if (typeof adminInit === 'function') adminInit(); },
      bot: function() { if (typeof botInit === 'function') botInit(); },
      telemetry: function() { if (typeof telemetryInit === 'function') telemetryInit(); }
    }[name];
    try { if (fn) fn(); } catch (_) {}
  }

  /* ── Пульт ── */

  function renderPulse() {
    var el = document.getElementById('view-pulse');
    if (!el) return;
    var ctrl = new AbortController();
    setTimeout(function() { ctrl.abort(); }, 10000);
    fetch('/api/attention', { signal: ctrl.signal, credentials: 'same-origin' })
      .then(function(r) { return r.ok ? r.json() : null; })
      .then(function(data) { _drawPulse(el, data || {}); })
      .catch(function() { _drawPulse(el, {}); });
  }

  function _drawPulse(el, data) {
    var pc = (data.problems || []).length;
    var uc = (data.unknown || []).length;
    var sc = (data.snoozed || []).length;
    // 01.10.2026: main.css:537 растягивает прямой <div> вида; оборачиваем в .view-body,
    // чтобы баннер и сетка не растягивались на ~490 px.
    var body = el.querySelector('.view-body');
    if (!body) {
      body = document.createElement('div');
      body.className = 'view-body';
      body.style.overflow = 'auto';
      el.appendChild(body);
    }
    var h = '<div class="pulse-banner ' + (pc ? 'pulse-banner--warn' : 'pulse-banner--ok') + '">'
      + esc(pc ? tr('pulse.attention_n').replace('{n}', pc) : tr('pulse.all_ok')) + '</div>';
    h += '<div class="pulse-grid">';
    // Пульс скрытых процессов (сервер отдаёт метки времени в data.pulse): живой — метка свежее 3 минут.
    var pl = data.pulse || {};
    var now = Date.now() / 1000;
    [['app', 'pulse.proc_app'], ['bot', 'pulse.proc_bot'], ['governor', 'pulse.proc_governor']].forEach(function(pair) {
      var ts = pl[pair[0]];
      if (!ts) return;
      var age = Math.max(0, Math.round(now - ts));
      var alive = age < 180;
      h += _tile2(alive ? 'pulse-tile--ok' : 'pulse-tile--warn', tr(pair[1]), alive ? tr('pulse.alive') : tr('pulse.silent'),
                  tr('pulse.ago').replace('{s}', age), !alive);
    });
    h += _tile('pulse-tile--warn', tr('pulse.problems'), pc, !!pc);
    h += _tile('pulse-tile--unknown', tr('pulse.unknown'), uc, !!uc);
    h += _tile('pulse-tile--snoozed', tr('pulse.snoozed'), sc, !!sc);
    h += '</div>';
    var recent = (data.problems || []).slice(0, 5);
    if (recent.length) {
      h += '<div class="pulse-section">';
      h += '<div class="pulse-section__title">' + tr('pulse.recent') + '</div>';
      recent.forEach(function(r) {
        var sev = r.severity || 'warn';
        h += '<div class="pulse-rec pulse-rec--' + esc(sev) + '">';
        h += '<div class="pulse-rec__head">';
        h += '<span class="pulse-rec__svc">' + esc(r.service || '') + '</span>';
        h += '<span class="pulse-rec__ttl">' + esc(r.title || '') + '</span>';
        h += '</div>';
        if (r.evidence) h += '<div class="pulse-rec__ev">' + esc(r.evidence) + '</div>';
        // Машинные подсказки («POST /api/queue/retry/{id}») не показываем пользователю —
        // сворачиваем в кнопку с понятным именем.
        if (r.action) {
          if (/^(GET|POST|PUT|PATCH|DELETE)\s/.test(r.action)) {
            h += '<div class="pulse-rec__act"><button type="button" class="pulse-act-btn" data-act="' + esc(r.action) + '">' + esc(tr('pulse.retry')) + '</button></div>';
          } else {
            h += '<div class="pulse-rec__act">' + esc(r.action) + '</div>';
          }
        }
        h += '</div>';
      });
      h += '</div>';
    }
    body.innerHTML = h;
    // Делегированный обработчик кнопок действия.
    body.querySelectorAll('.pulse-act-btn').forEach(function(btn) {
      btn.addEventListener('click', function() {
        var parts = btn.dataset.act.split(/\s+/);
        var method = parts[0] || 'POST';
        var url = parts[1] || '';
        if (!url) return;
        btn.disabled = true;
        btn.textContent = '…';
        fetch(url, { method: method, credentials: 'same-origin' })
          .then(function(r) {
            btn.textContent = r.ok ? '✓' : '✕';
            setTimeout(function() { renderPulse(); }, 1500);
          })
          .catch(function() {
            btn.textContent = '✕';
            btn.disabled = false;
          });
      });
    });
  }

  function _tile2(cls, label, big, sub, lit) {
    return '<div class="pulse-tile ' + cls + (lit ? ' pulse-tile--lit' : '') + '">'
      + '<div class="pulse-tile__n">' + esc(big) + '</div>'
      + '<div class="pulse-tile__l">' + esc(label) + '</div>'
      + '<div class="pulse-tile__s">' + esc(sub || '') + '</div></div>';
  }

  function _tile(cls, label, count, lit) {
    return '<div class="pulse-tile ' + cls + (lit ? ' pulse-tile--lit' : '') + '">'
      + '<div class="pulse-tile__n">' + count + '</div>'
      + '<div class="pulse-tile__l">' + esc(label) + '</div></div>';
  }

  function esc(s) {
    if (typeof window.esc === 'function') return window.esc(s);
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  /* ── Клавиатура ── */

  function onKey(e) {
    if (e.key === 'Escape') {
      var popup = document.querySelector('.attn-popup:not(.attn-hidden)');
      if (popup) popup.classList.add('attn-hidden');
    }
  }

  window.RpShell = {
    init: init,
    showSystem: showSys,
    renderPulse: renderPulse,
    currentView: currentView,
    navigate: navigate,
    SYS_TABS: _SYS_TABS
  };
})();
