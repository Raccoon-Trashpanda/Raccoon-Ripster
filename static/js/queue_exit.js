/* queue_exit.js — интерпретатор анимаций ухода плиток из очереди.
 * Задача 064. Данные фаз — в этом же файле (объект ANIMS).
 * Вход: элемент плитки (.qi) и id анимации. Соседи смыкаются пружинкой 520 мс.
 * prefers-reduced-motion → затухание 240 мс. Только transform/opacity.
 * Лимит: ≤12 одновременных, >30 — без анимации (схлопывание).
 */
(function() {
  'use strict';

  var mq = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)');
  function reduced() { return mq && mq.matches; }

  function rnd(a, b) { return a + Math.random() * (b - a); }

  /* ── Данные анимаций (9 безопасных). ──
   * Формат фазы: {ms, ez:[a,b,c,d], to:{x,y,r,sx,sy,op}, detach:0..1}
   * Значения: число или {b, H, W, L, R, U, D, G, sG, K, Z, d, r:[min,max]}
   * Контекст (ctxOf): H/W — размеры плитки, L/R/U/D — пути выхода,
   * G — зазор до потолка, sG = √G, K — качок ±(2.2..4.2)°, Z — знак ±1. */

  var EZ = {
    out:    [0, 0, .58, 1],
    inout:  [.42, 0, .58, 1],
    wind:   [.3, .6, .4, 1],
    acc:    [.55, 0, .95, .55],
    hit:    [.1, .7, .3, 1]
  };

  var ANIMS = [];

  ANIMS.push({id: 'left', origin: '50% 50%', phases: [
    {ms: 150, ez: EZ.wind, to: {x: 20, r: 3.2, sx: 1.035, sy: .965, y: -1}},
    {ms: 360, ez: EZ.acc, detach: .42, to: {x: {L: -1}, r: {r: [-13, -7]}, sx: 1.06, sy: .95, y: {r: [14, 34]}, op: .5}}
  ]});

  ANIMS.push({id: 'right', origin: '50% 50%', phases: [
    {ms: 150, ez: EZ.wind, to: {x: -16, r: -3.2, sx: 1.035, sy: .965, y: -1}},
    {ms: 430, ez: EZ.acc, detach: .42, to: {x: {R: 1}, r: {r: [7, 13]}, sx: 1.06, sy: .95, y: {r: [14, 34]}, op: .5}}
  ]});

  ANIMS.push({id: 'up', origin: '50% 50%', phases: [
    {ms: 170, ez: EZ.wind, to: {y: {d: 8, H: -.06}, sx: 1.06, sy: .88, r: {K: -.5}}},
    {ms: 114, ez: [.5, .05, .95, .55], to: {y: {G: -.55, H: .05}, sx: .95, sy: 1.1, r: {K: .6}}},
    {ms: 76, ez: [.4, 0, .9, .6], to: {y: {G: -1, H: .07}, sx: .92, sy: 1.14, r: {K: 1}}},
    {ms: 95, ez: EZ.hit, to: {y: {G: -1, H: -.17}, sx: {b: 1, d: .26}, sy: {b: .76, d: -.1}, r: {K: .3}}},
    {ms: 168, ez: [.2, .6, .3, 1], to: {y: {G: -1, d: 26}, sx: .97, sy: 1.06, r: {K: -1.4}}},
    {ms: 146, ez: [.4, 0, .6, 1], to: {y: {G: -1, d: 5.7}, sx: 1.02, sy: .985, r: {K: .8}}},
    {ms: 134, ez: [.4, 0, .6, 1], to: {y: {G: -1, d: 15.6}, sx: 1, sy: 1, r: {K: -.45}}},
    {ms: 112, ez: [.4, 0, .6, 1], to: {y: {G: -1, d: 10.9}, r: {K: .2}}},
    {ms: 300, ez: EZ.inout, to: {y: {G: -1, d: 10.9, b: 2}, r: -1.2, sx: 1.01, sy: .99}},
    {ms: 100, ez: EZ.out, detach: 0, to: {y: {G: -1, d: 10.9, b: -6}, r: -2.4, sx: 1.06, sy: 1.08}},
    {ms: 210, ez: [.6, 0, .9, .4], to: {y: {G: -1, d: 10.9, H: .28}, r: 5, sx: 1.2, sy: .34, op: .85}},
    {ms: 150, ez: [.5, 0, .9, .6], to: {y: {G: -1, d: 10.9, H: .42}, r: 12, sx: .06, sy: .04, op: 0}}
  ]});

  ANIMS.push({id: 'rocket', origin: '50% 50%', phases: [
    {ms: 70, ez: EZ.inout, to: {x: -3, r: -.6, sy: .97}},
    {ms: 70, ez: EZ.inout, to: {x: 3, r: .6, sy: .97}},
    {ms: 70, ez: EZ.inout, to: {x: -2, r: -.4, sy: .95}},
    {ms: 260, ez: [.4, 0, .9, .5], detach: .2, to: {x: 0, r: 0, y: {U: -.22}, sx: .97, sy: 1.08}},
    {ms: 380, ez: [.6, 0, 1, .55], to: {y: {U: -1}, sx: .9, sy: 1.25, r: {r: [-2, 2]}, op: .9}}
  ]});

  ANIMS.push({id: 'hatch', origin: '0% 0%', phases: [
    {ms: 110, ez: EZ.out, to: {y: 2, sy: .985}},
    {ms: 340, ez: [.55, 0, .95, .6], detach: 0, to: {r: 96, y: 2, sy: 1}},
    {ms: 300, ez: [.5, 0, .9, .6], to: {r: 112, y: {D: 1}, op: .2}}
  ]});

  ANIMS.push({id: 'blackhole', origin: '50% 50%', phases: [
    {ms: 220, ez: EZ.wind, to: {r: 6, sx: 1.02, sy: 1.02}},
    {ms: 560, ez: [.6, 0, .9, .5], detach: .1, to: {x: {W: .3}, r: 560, sx: .03, sy: .03, op: .1}},
    {ms: 140, ez: EZ.out, to: {op: 0}}
  ]});

  ANIMS.push({id: 'ribbon', origin: '50% 50%', phases: [
    {ms: 180, ez: EZ.wind, detach: 0, to: {sy: .16, sx: 1.02}},
    {ms: 200, ez: EZ.inout, to: {x: {W: -.06}, y: {U: -.15}, sy: .14, r: -4}},
    {ms: 200, ez: EZ.inout, to: {x: {W: .08}, y: {U: -.45}, r: 5}},
    {ms: 220, ez: EZ.inout, to: {x: {W: -.1}, y: {U: -.8}, r: -6, sx: .9, op: .85}},
    {ms: 200, ez: [.6, 0, .9, .5], to: {y: {U: -1.25}, sx: .5, sy: .06, op: 0}}
  ]});

  ANIMS.push({id: 'balloon', origin: '50% 50%', phases: [
    {ms: 240, ez: EZ.wind, to: {sx: 1.08, sy: 1.1, y: -4}},
    {ms: 420, ez: EZ.inout, detach: .1, to: {y: {G: -.35}, x: 18, r: 5, sx: 1.04, sy: 1.06}},
    {ms: 420, ez: EZ.inout, to: {y: {G: -.7}, x: -16, r: -5}},
    {ms: 360, ez: EZ.inout, to: {y: {G: -.88}, x: 10, r: 3, sx: 1.12, sy: 1.14}},
    {ms: 70, ez: EZ.hit, to: {sx: 1.32, sy: 1.32, op: .9}},
    {ms: 120, ez: EZ.out, to: {sx: 1.6, sy: 1.6, op: 0}}
  ]});

  ANIMS.push({id: 'pixels', origin: '50% 50%', phases: [
    {ms: 170, ez: EZ.wind, to: {sx: .985, sy: .985}},
    {ms: 120, ez: EZ.out, detach: 0, to: {op: 0}},
    {ms: 200, ez: [0, 0, 1, 1], to: {op: 0}}
  ]});

  /* ── Интерпретатор ── */

  function resolveVal(v, c) {
    if (typeof v === 'number') return v;
    var s = v.b || 0;
    if (v.H != null) s += v.H * c.H;
    if (v.W != null) s += v.W * c.W;
    if (v.L != null) s += v.L * c.L;
    if (v.R != null) s += v.R * c.R;
    if (v.U != null) s += v.U * c.U;
    if (v.D != null) s += v.D * c.D;
    if (v.G != null) s += v.G * c.G;
    if (v.sG != null) s += v.sG * c.sG;
    if (v.K != null) s += v.K * c.K;
    if (v.Z != null) s += v.Z * c.Z;
    if (v.d != null) s += v.d;
    if (v.r) s += rnd(v.r[0], v.r[1]);
    return s;
  }

  function ctxOf(el) {
    var rect = el.getBoundingClientRect();
    var parent = el.parentElement;
    var pRect = parent ? parent.getBoundingClientRect() : rect;
    var H = el.offsetHeight, W = el.offsetWidth;
    var G = Math.max(0, rect.top - pRect.top - 6);
    return {
      H: H, W: W,
      L: (rect.right - pRect.left) + 80,
      R: (pRect.right - rect.left) + 80,
      U: (rect.top - pRect.top) + H + 60,
      D: (pRect.bottom - rect.top) + 60,
      G: G,
      sG: Math.sqrt(G),
      K: (Math.random() < .5 ? -1 : 1) * rnd(2.2, 4.2),
      Z: Math.random() < .5 ? -1 : 1
    };
  }

  function cssTransform(st) {
    return 'translate(' + st.x.toFixed(2) + 'px,' + st.y.toFixed(2) + 'px) ' +
           'rotate(' + st.r.toFixed(2) + 'deg) ' +
           'scale(' + st.sx.toFixed(3) + ',' + st.sy.toFixed(3) + ')';
  }

  function neighborsAfter(el) {
    var result = [], n = el.nextElementSibling;
    while (n) {
      if (n.classList.contains('qi') && !n.dataset.leaving) result.push(n);
      n = n.nextElementSibling;
    }
    return result;
  }

  function detachTile(el) {
    if (el.dataset.detached) return;
    el.dataset.detached = '1';
    var after = neighborsAfter(el);
    var h = el.offsetHeight + 10;
    var top = el.offsetTop, left = el.offsetLeft, w = el.offsetWidth;
    el.style.position = 'absolute';
    el.style.top = top + 'px';
    el.style.left = left + 'px';
    el.style.width = w + 'px';
    el.style.margin = '0';
    if (reduced()) return;
    after.forEach(function(n, k) {
      n.animate([
        {transform: 'translateY(' + h + 'px)', easing: 'cubic-bezier(.2,.75,.3,1)'},
        {transform: 'translateY(-8px)', offset: .52, easing: 'ease-in-out'},
        {transform: 'translateY(3px)', offset: .76, easing: 'ease-in-out'},
        {transform: 'translateY(-1px)', offset: .9, easing: 'ease-out'},
        {transform: 'translateY(0)'}
      ], {duration: 520, delay: k * 26, fill: 'backwards'});
    });
  }

  function removeEl(el) {
    el.getAnimations().forEach(function(a) { a.cancel(); });
    el.remove();
  }

  function runPhase(node, fromSt, toSt, ms, ez) {
    var frames = [
      {transform: cssTransform(fromSt), opacity: fromSt.op},
      {transform: cssTransform(toSt), opacity: toSt.op}
    ];
    var a = node.animate(frames, {
      duration: ms,
      fill: 'forwards',
      easing: 'cubic-bezier(' + ez.join(',') + ')'
    });
    return a.finished.then(function() { return a; }, function() { return a; });
  }

  function playAnim(el, anim) {
    var c = ctxOf(el);
    var st = {x: 0, y: 0, r: 0, sx: 1, sy: 1, op: 1};
    el.style.transformOrigin = anim.origin;
    el.style.willChange = 'transform, opacity';
    el.style.zIndex = '4';
    el.classList.add('flying');

    var p = Promise.resolve();
    anim.phases.forEach(function(ph) {
      p = p.then(function() {
        var to = {x: st.x, y: st.y, r: st.r, sx: st.sx, sy: st.sy, op: st.op};
        for (var k in ph.to) {
          to[k] = resolveVal(ph.to[k], c);
        }
        if (ph.detach != null) {
          var delayMs = Math.round(ph.ms * ph.detach);
          setTimeout(function() { detachTile(el); }, delayMs);
        }
        var from = {x: st.x, y: st.y, r: st.r, sx: st.sx, sy: st.sy, op: st.op};
        st = to;
        return runPhase(el, from, to, ph.ms, ph.ez);
      });
    });
    return p.then(function() { removeEl(el); });
  }

  function fadeOut(el) {
    detachTile(el);
    return runPhase(el,
      {x: 0, y: 0, r: 0, sx: 1, sy: 1, op: 1},
      {x: 0, y: 0, r: 0, sx: 1, sy: 1, op: 0},
      240, EZ.out
    ).then(function() { removeEl(el); });
  }

  /* ── Публичный API ── */

  var _activeCount = 0;
  var MAX_SIMULTANEOUS = 12;
  var NO_ANIM_THRESHOLD = 30;

  function byId(id) {
    for (var i = 0; i < ANIMS.length; i++) {
      if (ANIMS[i].id === id) return ANIMS[i];
    }
    return null;
  }

  function enabledAnims() {
    var cfg = (window.S && S.config) || {};
    var list = cfg['queue-exit-anims'];
    if (!list) return ANIMS.map(function(a) { return a.id; });
    if (typeof list === 'string') {
      try { list = JSON.parse(list); } catch(e) { return []; }
    }
    if (!Array.isArray(list)) return [];
    return list.filter(function(id) { return byId(id); });
  }

  function pickRandom(ids) {
    if (!ids.length) return null;
    return ids[Math.floor(Math.random() * ids.length)];
  }

  /**
   * Play exit animation on a queue tile.
   * @param {HTMLElement} el — the .qi element
   * @param {string} [animId] — specific animation id, or omit for random
   * @returns {Promise} resolves when animation completes and element is removed
   */
  function leave(el, animId) {
    if (!el || el.dataset.leaving) return Promise.resolve();
    el.dataset.leaving = '1';

    if (reduced()) return fadeOut(el);

    var enabled = enabledAnims();
    if (!enabled.length) return fadeOut(el);

    var id = (animId && byId(animId)) ? animId : pickRandom(enabled);
    if (!id) return fadeOut(el);

    _activeCount++;
    var anim = byId(id);
    return playAnim(el, anim).then(function() {
      _activeCount--;
    });
  }

  /**
   * Mass exit: play animations on multiple tiles with a wave step.
   * @param {HTMLElement[]} els — array of .qi elements
   * @param {number} [stepMs=260] — delay between each tile
   * @returns {Promise}
   */
  function leaveWave(els, stepMs) {
    stepMs = stepMs || 260;
    if (!els.length) return Promise.resolve();

    if (els.length > NO_ANIM_THRESHOLD) {
      els.forEach(function(el) { el.dataset.leaving = '1'; });
      return Promise.resolve();
    }

    var promises = els.map(function(el, i) {
      return new Promise(function(resolve) {
        setTimeout(function() {
          if (_activeCount >= MAX_SIMULTANEOUS) {
            fadeOut(el).then(resolve);
          } else {
            leave(el).then(resolve);
          }
        }, i * stepMs);
      });
    });
    return Promise.all(promises);
  }

  window.RpQueueExit = {
    leave: leave,
    leaveWave: leaveWave,
    anims: ANIMS,
    reduced: reduced
  };

})();
