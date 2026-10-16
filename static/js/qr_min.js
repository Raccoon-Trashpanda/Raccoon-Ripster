// Мини-генератор QR-кода: байтовый режим, уровень коррекции M, версии 1..10.
//
// Зачем в браузере: код перевыпускается каждые несколько минут и ссылка должна
// остаться внутри машины — внешний веб-сервис отдал бы бы код входа наружу, а
// серверный рендер потребовал бы нового эндпоинта и round-trip'а на каждый
// перевыпуск. Свой файл весят при этом меньше одной картинки-заглушки.
//
// Границы сознательные: длина ссылки `link.tidal.com/XXXXXXX` (29 символов) —
// это версия 3. Всё, что не влезает в v10 (213 байт на уровне M), возвращается
// как null: интерфейс тогда показывает ссылку и кнопку «копировать», и это
// честно, а не битая картинка.
//
// Раскладка модулей, ECC и поля формата сверены бит-в-бит с python-qrcode
// (tests/test_tidal_pool_ui.py: наша матрица совпадает с эталонной на одном из
// восьми масок). Выбор маски расходится: штраф считаем по ISO/IEC 18004, а
// python-qrcode — по своей упрощённой мере. Любая маска читывается сканером,
// поэтому расхождение — косметика, а не дефект.
var QR_M_BLOCKS = [
  [[1, 26, 16]], [[1, 44, 28]], [[1, 70, 44]], [[2, 50, 32]], [[2, 67, 43]],
  [[4, 43, 27]], [[4, 49, 31]], [[2, 60, 38], [2, 61, 39]],
  [[3, 58, 36], [2, 59, 37]], [[4, 69, 43], [1, 70, 44]]
];
var QR_M_ALIGN = [[], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34],
                  [6, 22, 38], [6, 24, 42], [6, 26, 46], [6, 28, 50]];

// GF(256) с примитивным многочленом 285 (x^8+x^4+x^3+x^2+1) — тот же, что у QR.
var QR_EXP = new Uint8Array(512), QR_LOG = new Uint8Array(256);
(function () {
  for (var i = 0; i < 8; i++) QR_EXP[i] = 1 << i;
  for (var i = 8; i < 256; i++)
    QR_EXP[i] = QR_EXP[i - 4] ^ QR_EXP[i - 5] ^ QR_EXP[i - 6] ^ QR_EXP[i - 8];
  for (var i = 0; i < 255; i++) QR_LOG[QR_EXP[i]] = i;
  for (var i = 255; i < 512; i++) QR_EXP[i] = QR_EXP[i - 255];
})();
function qrMul(a, b) { return (a === 0 || b === 0) ? 0 : QR_EXP[QR_LOG[a] + QR_LOG[b]]; }

function qrGenPoly(deg) {                 // произведение (x − α^i), i = 0..deg-1
  var g = [1];
  for (var i = 0; i < deg; i++) {
    var next = new Array(g.length + 1).fill(0);
    for (var j = 0; j < g.length; j++) {
      next[j] ^= g[j];
      next[j + 1] ^= qrMul(g[j], QR_EXP[i]);
    }
    g = next;
  }
  return g;
}

function qrEcBytes(data, deg) {
  var g = qrGenPoly(deg), rem = new Array(deg).fill(0);
  for (var i = 0; i < data.length; i++) {
    var factor = rem[0] ^ data[i];
    rem = rem.slice(1).concat([0]);
    if (factor === 0) continue;
    for (var j = 0; j < deg; j++) rem[j] ^= qrMul(g[j + 1], factor);
  }
  return rem;
}

function qrUtf8(text) {
  if (typeof TextEncoder !== 'undefined') return Array.from(new TextEncoder().encode(text));
  var out = [];
  for (var i = 0; i < text.length; i++) {
    var c = text.charCodeAt(i);
    if (c < 0x80) out.push(c);
    else if (c < 0x800) out.push(0xC0 | (c >> 6), 0x80 | (c & 63));
    else out.push(0xE0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
  }
  return out;
}

function qrDataCodewords(version) {
  return QR_M_BLOCKS[version - 1].reduce(function (s, b) { return s + b[0] * b[2]; }, 0);
}

// Число байт заголовка режима+длины зависит от версии: счётчик длины в
// байтовом режиме 8 бит для v1..9 и 16 бит для v10+.
function qrNeededBits(byteLen, version) {
  return 4 + (version >= 10 ? 16 : 8) + byteLen * 8;
}

function qrPickVersion(byteLen) {
  for (var v = 1; v <= 10; v++)
    if (qrNeededBits(byteLen, v) <= qrDataCodewords(v) * 8) return v;
  return null;
}

function qrBitStream(bytes, version) {
  var bits = [], total = qrDataCodewords(version) * 8;
  function put(val, len) { for (var i = len - 1; i >= 0; i--) bits.push((val >> i) & 1); }
  put(4, 4);                                          // режим: байты
  put(bytes.length, version >= 10 ? 16 : 8);
  for (var i = 0; i < bytes.length; i++) put(bytes[i], 8);
  for (var i = 0; i < 4 && bits.length < total; i++) bits.push(0);   // терминатор
  while (bits.length % 8) bits.push(0);
  for (var pad = 0; bits.length < total; pad++) put(pad % 2 ? 0x11 : 0xEC, 8);
  var out = [];
  for (var i = 0; i < bits.length; i += 8) {
    var b = 0;
    for (var j = 0; j < 8; j++) b = (b << 1) | bits[i + j];
    out.push(b);
  }
  return out;
}

function qrInterleave(cw, version) {
  var groups = [], offset = 0;
  QR_M_BLOCKS[version - 1].forEach(function (blk) {
    for (var n = 0; n < blk[0]; n++) {
      var data = cw.slice(offset, offset + blk[2]);
      offset += blk[2];
      groups.push({data: data, ec: qrEcBytes(data, blk[1] - blk[2])});
    }
  });
  var out = [], maxData = Math.max.apply(null, groups.map(function (g) { return g.data.length; }));
  for (var i = 0; i < maxData; i++)
    groups.forEach(function (g) { if (i < g.data.length) out.push(g.data[i]); });
  var maxEc = Math.max.apply(null, groups.map(function (g) { return g.ec.length; }));
  for (var i = 0; i < maxEc; i++)
    groups.forEach(function (g) { if (i < g.ec.length) out.push(g.ec[i]); });
  return out;
}

function qrMaskFn(m) {
  return [
    function (r, c) { return (r + c) % 2 === 0; },
    function (r, c) { return r % 2 === 0; },
    function (r, c) { return c % 3 === 0; },
    function (r, c) { return (r + c) % 3 === 0; },
    function (r, c) { return (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0; },
    function (r, c) { return (r * c) % 2 + (r * c) % 3 === 0; },
    function (r, c) { return ((r * c) % 2 + (r * c) % 3) % 2 === 0; },
    function (r, c) { return ((r * c) % 3 + (r + c) % 2) % 2 === 0; }
  ][m];
}

function qrBchDigit(n) { var d = 0; while (n) { d++; n >>>= 1; } return d; }
function qrBch(data, poly, bits) {
  var d = data << bits;
  while (qrBchDigit(d) - qrBchDigit(poly) >= 0) d ^= poly << (qrBchDigit(d) - qrBchDigit(poly));
  return ((data << bits) | d);
}

// Оценка маски (ISO 18004, правила 1..4): выбирается вариант, который
// сканеры читают надёжнее. Это не косметика: плохая маска ловится камерой
// телефона через раз.
function qrPenalty(m) {
  var n = m.length, p = 0;
  function runs(get) {
    for (var a = 0; a < n; a++) {
      var len = 1;
      for (var b = 1; b < n; b++) {
        if (get(a, b) === get(a, b - 1)) { len++; continue; }
        if (len >= 5) p += 3 + (len - 5);
        len = 1;
      }
      if (len >= 5) p += 3 + (len - 5);
    }
  }
  runs(function (a, b) { return m[a][b]; });
  runs(function (a, b) { return m[b][a]; });
  for (var r = 0; r < n - 1; r++)
    for (var c = 0; c < n - 1; c++) {
      var v = m[r][c];
      if (v === m[r][c + 1] && v === m[r + 1][c] && v === m[r + 1][c + 1]) p += 3;
    }
  var pat1 = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0], pat2 = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1];
  function hitsPat(line, pat) {
    for (var i = 0; i < 11; i++) if ((line[i] ? 1 : 0) !== pat[i]) return false;
    return true;
  }
  function scan(get) {                        // строки и столбцы одним проходом
    for (var a = 0; a < n; a++)
      for (var b = 0; b + 11 <= n; b++) {
        var line = [];
        for (var i = 0; i < 11; i++) line.push(get(a, b + i));
        if (hitsPat(line, pat1) || hitsPat(line, pat2)) p += 40;
      }
  }
  scan(function (a, b) { return m[a][b]; });
  scan(function (a, b) { return m[b][a]; });
  var dark = 0;
  for (var r = 0; r < n; r++) for (var c = 0; c < n; c++) if (m[r][c]) dark++;
  p += Math.floor(Math.abs(dark / (n * n) * 100 - 50) / 5) * 10;
  return p;
}

// Собирает матрицу: служебные модули, данные зигзагом, маска, поля формата и
// версии. Возвращает [[true|false]] или null, если текст длиннее v10.
function qrMatrix(text, forcedMask) {
  var bytes = qrUtf8(text);
  var version = qrPickVersion(bytes.length);
  if (!version) return null;
  var size = version * 4 + 17;
  var m = [], res = [];
  for (var i = 0; i < size; i++) { m.push(new Array(size).fill(false)); res.push(new Array(size).fill(false)); }

  function set(r, c, dark) {
    if (r < 0 || c < 0 || r >= size || c >= size) return;
    m[r][c] = dark; res[r][c] = true;
  }
  function finder(row, col) {
    for (var r = -1; r <= 7; r++)
      for (var c = -1; c <= 7; c++) {
        var dark = (0 <= r && r <= 6 && (c === 0 || c === 6)) ||
                   (0 <= c && c <= 6 && (r === 0 || r === 6)) ||
                   (2 <= r && r <= 4 && 2 <= c && c <= 4);
        set(row + r, col + c, dark);
      }
  }
  finder(0, 0); finder(size - 7, 0); finder(0, size - 7);
  // Порядок важен: выравнивания РАССТАВЛЯЮТСЯ ДО тайминга, иначе центр сетки,
  // лежащий на линии тайминга (это бывает с v7 и новее), был бы «занят» и
  // пропущен — 25 модулей превращались в data-ячейки, и код становился
  // нечитаемым.
  QR_M_ALIGN[version - 1].forEach(function (row) {
    QR_M_ALIGN[version - 1].forEach(function (col) {
      if (res[row][col]) return;
      for (var r = -2; r <= 2; r++)
        for (var c = -2; c <= 2; c++)
          set(row + r, col + c, Math.abs(r) === 2 || Math.abs(c) === 2 || (r === 0 && c === 0));
    });
  });
  for (var i = 8; i < size - 8; i++) {
    if (!res[i][6]) set(i, 6, i % 2 === 0);
    if (!res[6][i]) set(6, i, i % 2 === 0);
  }
  set(size - 8, 8, true);                                  // тёмный модуль
  for (var i = 0; i < 9; i++) { if (!res[8][i]) { res[8][i] = true; } if (!res[i][8]) { res[i][8] = true; } }
  for (var i = 0; i < 8; i++) { res[8][size - 1 - i] = true; res[size - 1 - i][8] = true; }
  if (version >= 7)
    for (var i = 0; i < 6; i++)
      for (var j = 0; j < 3; j++) { res[i][size - 11 + j] = true; res[size - 11 + j][i] = true; }

  var cw = qrInterleave(qrBitStream(bytes, version), version);
  var bitIdx = 0, up = true;
  for (var pair = size - 1; pair > 0; pair -= 2) {
    // Столбец 6 занят вертикальным таймингом — пара смещается на него же, и
    // дальше все пары идущие за ним сдвигаются на единицу.
    var col = pair <= 6 ? pair - 1 : pair;
    for (var step = 0; step < size; step++) {
      var row = up ? size - 1 - step : step;
      for (var k = 0; k < 2; k++) {
        var c = col - k;
        if (res[row][c]) continue;
        var dark = bitIdx < cw.length * 8 ? ((cw[bitIdx >> 3] >> (7 - (bitIdx & 7))) & 1) === 1 : false;
        bitIdx++;
        m[row][c] = dark;
      }
    }
    up = !up;
  }

  function paint(mask) {
    var out = m.map(function (row) { return row.slice(); });
    if (mask >= 0) {
      var fn = qrMaskFn(mask);
      for (var r = 0; r < size; r++)
        for (var c = 0; c < size; c++)
          if (!res[r][c] && fn(r, c)) out[r][c] = !out[r][c];
    }
    var info = qrBch((0 << 3) | Math.max(mask, 0), 0b10100110111, 10) ^ 0b101010000010010;
    for (var i = 0; i < 15; i++) {
      var b = ((info >> i) & 1) === 1;
      if (i < 6) out[i][8] = b; else if (i < 8) out[i + 1][8] = b; else out[size - 15 + i][8] = b;
      if (i < 8) out[8][size - 1 - i] = b; else if (i === 8) out[8][15 - i - 1 + 1] = b;
      else out[8][15 - i - 1] = b;
    }
    out[size - 8][8] = true;
    if (version >= 7) {
      var vinfo = qrBch(version, 0b1111100100101, 12);
      for (var i = 0; i < 18; i++) {
        var vb = ((vinfo >> i) & 1) === 1;
        out[Math.floor(i / 3)][(i % 3) + size - 8 - 3] = vb;
        out[(i % 3) + size - 8 - 3][Math.floor(i / 3)] = vb;
      }
    }
    return out;
  }

  if (forcedMask !== undefined && forcedMask !== null) return paint(forcedMask);
  var best = null, bestScore = Infinity;
  for (var mask = 0; mask < 8; mask++) {
    var cand = paint(mask), score = qrPenalty(cand);
    if (score < bestScore) { bestScore = score; best = cand; }
  }
  return best;
}

// SVG-узлы для вставки в DOM: без растровых картинок, поэтому QR резок и на
// экране телефона, и на 4K-мониторе.
function qrSvg(text, px) {
  var mm = qrMatrix(text);
  if (!mm) return '';
  var n = mm.length, q = px / n, parts = [];
  for (var r = 0; r < n; r++)
    for (var c = 0; c < n; c++)
      if (mm[r][c]) parts.push('M' + (c * q).toFixed(2) + ',' + (r * q).toFixed(2) + 'h' + q.toFixed(2) +
                               'v' + q.toFixed(2) + 'h-' + q.toFixed(2) + 'z');
  return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' + px + ' ' + px +
         '" width="' + px + '" height="' + px + '" shape-rendering="crispEdges">' +
         '<rect width="' + px + '" height="' + px + '" fill="#fff"/>' +
         '<path d="' + parts.join('') + '" fill="#000"/></svg>';
}
