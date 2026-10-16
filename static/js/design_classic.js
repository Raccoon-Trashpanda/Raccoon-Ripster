/* Classic Design — адаптер существующего DOM.
 * Не меняет разметку, просто регистрирует текущее состояние как дизайн «Классика».
 * reload: true — смена дизайна перезагружает страницу, чтобы DOM гарантированно
 * вернулся к исходному состоянию (Aurora заменяет меню, и без перезагрузки
 * классические nav-item не восстановить).
 */
(function() {
  'use strict';

  window.RpDesigns.register({
    id: 'classic',
    title: (typeof t === 'function' ? t('design.classic.title') : 'Classic'),
    description: (typeof t === 'function' ? t('design.classic.desc') : ''),
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
    styles: [],
    apply() {
      document.documentElement.dataset.design = 'classic';
    }
  });
})();
