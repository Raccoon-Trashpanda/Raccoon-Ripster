/* Design Registry — механизм смены дизайна (макета) приложения.
 * Три оси: Дизайн (макет) · Цветовая схема · Форма (скин).
 * Этот файл — реестр дизайнов и переключатель.
 */
(function() {
  'use strict';

  const REQUIRED_SLOTS = ['attention', 'hints', 'queue-exit', 'first-run', 'palette'];
  const LS_KEY = 'amd-design';
  const CONFIG_KEY = 'ui-design';

  const _registry = {};
  let _current = null;

  window.RpDesigns = {
    register(spec) {
      if (!spec || !spec.id) {
        console.error('[RpDesigns] register: missing id');
        return false;
      }
      if (_registry[spec.id]) {
        console.warn(`[RpDesigns] design '${spec.id}' already registered, skipping`);
        return false;
      }
      const missing = REQUIRED_SLOTS.filter(s => !spec.slots || !spec.slots[s]);
      if (missing.length) {
        console.error(`[RpDesigns] design '${spec.id}' missing slots: ${missing.join(', ')}`);
        return false;
      }
      _registry[spec.id] = spec;
      console.log(`[RpDesigns] registered design: ${spec.id}`);
      return true;
    },

    init() {
      const stored = localStorage.getItem(LS_KEY) || 'classic';
      const serverDesign = (typeof S !== 'undefined' && S.config && S.config[CONFIG_KEY]) || null;
      const target = serverDesign || stored;
      this.switchTo(target, false);
      // Восстановить вид, который был открыт до перезагрузки (смена дизайна).
      try {
        const restore = sessionStorage.getItem('amd-restore-view');
        if (restore) {
          sessionStorage.removeItem('amd-restore-view');
          setTimeout(function() {
            if (typeof showView === 'function') {
              var el = document.querySelector('.nav-item[data-view="' + restore + '"]')
                    || document.querySelector('[data-view="' + restore + '"]');
              showView(restore, el);
            }
          }, 500);
        }
      } catch (_) {}
    },

    switchTo(id, persist) {
      if (persist === undefined) persist = true;
      const spec = _registry[id];
      if (!spec) {
        console.error(`[RpDesigns] design '${id}' not found, falling back to classic`);
        if (id !== 'classic') {
          this.switchTo('classic', persist);
          if (persist && typeof saveSetting === 'function') {
            saveSetting(CONFIG_KEY, 'classic');
          }
          localStorage.setItem(LS_KEY, 'classic');
        }
        return false;
      }
      try {
        if (spec.styles && Array.isArray(spec.styles)) {
          spec.styles.forEach(href => {
            if (!document.querySelector(`link[href*="${href}"]`)) {
              const link = document.createElement('link');
              link.rel = 'stylesheet';
              link.href = href;
              document.head.appendChild(link);
            }
          });
        }
        if (typeof spec.apply === 'function') {
          spec.apply();
        }
        _current = id;
        localStorage.setItem(LS_KEY, id);
        if (persist && typeof saveSetting === 'function') {
          saveSetting(CONFIG_KEY, id);
        }
        document.documentElement.dataset.design = id;
        console.log(`[RpDesigns] switched to: ${id}`);
        return true;
      } catch (err) {
        console.error(`[RpDesigns] failed to apply design '${id}':`, err);
        if (id !== 'classic') {
          this.switchTo('classic', persist);
          if (persist && typeof saveSetting === 'function') {
            saveSetting(CONFIG_KEY, 'classic');
          }
          localStorage.setItem(LS_KEY, 'classic');
        }
        return false;
      }
    },

    current() {
      return _current;
    },

    list() {
      return Object.keys(_registry);
    },

    getSpec(id) {
      return _registry[id] || null;
    },

    renderPicker(containerId) {
      const container = document.getElementById(containerId);
      if (!container) return;
      const ids = Object.keys(_registry);
      if (!ids.length) {
        container.textContent = 'No designs registered';
        return;
      }
      while (container.firstChild) container.removeChild(container.firstChild);
      const grid = document.createElement('div');
      grid.className = 'ds-grid';
      ids.forEach(id => {
        const spec = _registry[id];
        const tile = document.createElement('div');
        tile.className = 'ds-tile' + (_current === id ? ' active' : '');
        tile.dataset.design = id;
        tile.tabIndex = 0;
        tile.setAttribute('role', 'radio');
        tile.setAttribute('aria-checked', _current === id ? 'true' : 'false');
        const title = document.createElement('div');
        title.className = 'ds-title';
        title.textContent = spec.title || id;
        tile.appendChild(title);
        if (spec.description) {
          const desc = document.createElement('div');
          desc.className = 'ds-desc';
          desc.textContent = spec.description;
          tile.appendChild(desc);
        }
        tile.addEventListener('click', () => {
          if (_current === id) return;
          const needsReload = spec.reload !== false;
          const ok = window.RpDesigns.switchTo(id, true);
          if (ok && needsReload) {
            const currentView = (window.RpShell && typeof RpShell.currentView === 'function')
              ? RpShell.currentView() : null;
            if (currentView) {
              sessionStorage.setItem('amd-restore-view', currentView);
            }
            location.reload();
          }
        });
        tile.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            tile.click();
          }
        });
        grid.appendChild(tile);
      });
      container.appendChild(grid);
    },

    _syncPicker() {
      document.querySelectorAll('.ds-tile').forEach(tile => {
        const isActive = tile.dataset.design === _current;
        tile.classList.toggle('active', isActive);
        tile.setAttribute('aria-checked', isActive ? 'true' : 'false');
      });
    },

    REQUIRED_SLOTS,
    LS_KEY,
    CONFIG_KEY
  };
})();
