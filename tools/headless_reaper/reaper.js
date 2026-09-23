/*
 * Node twin of reaper.py — Tracker #44 shared headless-Chrome reaper.
 * Same contract: marker-prefixed user-data-dir, tree kill on every exit path,
 * startup sweep of leftovers. NEVER kill chrome.exe by name; only processes
 * whose command line names a user-data-dir under our prefix, or descendants
 * of those / of our own launcher.
 *
 * Usage (ESM harnesses import reaper.mjs, CJS requires this file):
 *   const { withOwnedChrome } = require('.../reaper.js');
 *   await withOwnedChrome({ port }, async (br) => { ...br.port, br.pid... });
 */
'use strict';
const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');

const PREFIX = process.env.HEADLESS_VERIFY_PREFIX || 'ripster-verify';
const STALE_GRACE_SEC = Number(process.env.HEADLESS_VERIFY_STALE_GRACE || 300);
// Playwright's own temp automation profiles (never the owner's profile).
const EXTRA_MARKERS = (process.env.HEADLESS_VERIFY_EXTRA_MARKERS
  || 'playwright_chromiumdev_profile-').split(',').filter(Boolean);

const DIR_RE = new RegExp('^' + PREFIX.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') +
  '-(\\d+)-(\\d{14})(?:-[A-Za-z0-9._-]+)*$');
const UDD_RE = /--user-data-dir=(?:"([^"]+)"|([^\s"]+))/i;

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  path.join(process.env.LOCALAPPDATA || '', 'Google/Chrome/Application/chrome.exe'),
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
].filter(Boolean);

function findChrome() {
  for (const c of CHROME_CANDIDATES) if (fs.existsSync(c)) return c;
  throw new Error('Chrome not found — set CHROME_PATH');
}

/* ─────────── pure ownership logic (mirrors reaper.py) ─────────── */

function extractUserDataDir(cmdline) {
  const m = UDD_RE.exec(cmdline || '');
  return m ? (m[1] || m[2]) : null;
}

function isOwnedCmdline(cmdline, prefix = PREFIX) {
  const udd = extractUserDataDir(cmdline);
  if (!udd) return false;
  const base = udd.replace(/["'\\/]+$/, '').split(/[\\/]/).pop();
  return base.startsWith(prefix + '-') || EXTRA_MARKERS.some(m => base.startsWith(m));
}

/** For our marker dirs: is the launcher pid baked into the name still alive?
 *  false ⇒ run was killed outright ⇒ its chrome is an ORPHAN at any age.
 *  true  ⇒ verification in flight — no sweep may kill it.
 *  null  ⇒ not our naming scheme (fall back to the grace window). */
function launcherAlive(dirPath) {
  const m = DIR_RE.exec((dirPath || '').replace(/["']/g, '')
    .replace(/[\\/]+$/, '').split(/[\\/]/).pop());
  if (!m) return null;
  const pid = Number(m[1]);
  try { process.kill(pid, 0); return true; } catch (e) {
    return e.code === 'ESRCH' ? false : true;  // EPERM-ish ⇒ assume alive
  }
}

const BROWSER_NAMES = new Set(['chrome.exe', 'msedge.exe', 'chromium.exe']);

/** procs: [{pid,ppid,name,cmdline}]. Returns provably-ours subset: marker
 *  match, descendants of marked procs (crashpad etc.), and browser-named
 *  descendants of launcherPid (a launcher's non-browser children, e.g. a dev
 *  server, are NOT browsers and must not be selected). */
function selectOwned(procs, launcherPid = null, prefix = PREFIX) {
  const byParent = new Map();
  for (const p of procs) {
    if (!byParent.has(p.ppid)) byParent.set(p.ppid, []);
    byParent.get(p.ppid).push(p);
  }
  const marked = new Set(procs.filter(p => isOwnedCmdline(p.cmdline, prefix)).map(p => p.pid));
  const ours = new Set();
  const stack = [...marked];
  while (stack.length) {
    for (const c of byParent.get(stack.pop()) || []) {
      if (!ours.has(c.pid)) { ours.add(c.pid); stack.push(c.pid); }
    }
  }
  for (const m of marked) ours.add(m);
  if (launcherPid != null) {
    const lstack = [launcherPid];
    while (lstack.length) {
      for (const c of byParent.get(lstack.pop()) || []) {
        if (ours.has(c.pid) || c.pid === launcherPid) continue;
        if (BROWSER_NAMES.has((c.name || '').toLowerCase()) || marked.has(c.pid)) {
          ours.add(c.pid);
          lstack.push(c.pid);
        }
      }
    }
  }
  if (launcherPid != null) ours.delete(launcherPid);
  return procs.filter(p => ours.has(p.pid));
}

function dirTimestampMs(name) {
  const m = DIR_RE.exec(name);
  if (!m) return null;
  const [, , ts] = m;
  const d = new Date(+ts.slice(0, 4), +ts.slice(4, 6) - 1, +ts.slice(6, 8),
    +ts.slice(8, 10), +ts.slice(10, 12), +ts.slice(12, 14));
  return d.getTime();
}

/* ─────────── live process enumeration (PowerShell CIM) ─────────── */

function listProcs() {
  const ps =
    'Get-CimInstance Win32_Process | Where-Object { $_.CommandLine } | ' +
    'ForEach-Object { ' +
    '$cl=$_.CommandLine -replace "`t"," "; ' +
    '"{0}`t{1}`t{2}`t{3}`t{4}" -f $_.ProcessId,$_.ParentProcessId,$_.WorkingSetSize,$_.Name,$cl }';
  const r = spawnSync('powershell', ['-NoProfile', '-Command', ps],
    { encoding: 'utf8', timeout: 60000, windowsHide: true });
  const out = [];
  for (const line of (r.stdout || '').split(/\r?\n/)) {
    const parts = line.split('\t');
    if (parts.length < 5) continue;
    const pid = Number(parts[0]), ppid = Number(parts[1]), ws = Number(parts[2]);
    if (!Number.isFinite(pid)) continue;
    out.push({ pid, ppid, ws, name: parts[3], cmdline: parts.slice(4).join('\t') });
  }
  return out;
}

function killTreeSync(pid) {
  try {
    spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'],
      { stdio: 'ignore', timeout: 30000 });
  } catch { /* already gone */ }
}

function newProfileDir(suffix = '') {
  const now = new Date();
  const p2 = n => String(n).padStart(2, '0');
  const ts = now.getFullYear() + p2(now.getMonth() + 1) + p2(now.getDate()) +
    p2(now.getHours()) + p2(now.getMinutes()) + p2(now.getSeconds());
  const nonce = Math.random().toString(16).slice(2, 8);
  const clean = suffix ? '-' + String(suffix).replace(/[^A-Za-z0-9._-]/g, '-') : '';
  const dir = path.join(os.tmpdir(), `${PREFIX}-${process.pid}-${ts}${clean}-${nonce}`);
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

function removeDirTolerant(dir, tries = 8, delayMs = 400) {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  return (async () => {
    for (let i = 0; i < tries; i++) {
      if (!fs.existsSync(dir)) return true;
      try { fs.rmSync(dir, { recursive: true, force: true, maxRetries: 2 }); } catch { }
      if (!fs.existsSync(dir)) return true;
      await sleep(delayMs);
    }
    return !fs.existsSync(dir);
  })();
}

/** Report (and optionally kill) leftovers of previously-killed runs. */
function sweepStale({ graceSec = STALE_GRACE_SEC, reap = false } = {}) {
  const now = Date.now();
  const stale = []; const fresh = [];
  for (const p of selectOwned(listProcs())) {
    const udd = extractUserDataDir(p.cmdline);
    const alive = udd ? launcherAlive(udd) : false;
    let isOrphan;
    if (alive === false) isOrphan = true;          // launcher died outright
    else if (alive === true) isOrphan = false;     // run in flight — hands off
    else {
      let ageMs = udd && dirTimestampMs(path.basename(udd));
      if (ageMs == null && udd && fs.existsSync(udd)) ageMs = fs.statSync(udd).birthtimeMs;
      isOrphan = ageMs == null || (now - ageMs) / 1000 >= graceSec;
    }
    (isOrphan ? stale : fresh).push({ ...p, dir: udd });
  }
  const killed = [];
  if (reap) for (const p of stale) { killTreeSync(p.pid); killed.push(p.pid); }

  const liveDirs = new Set([...stale, ...fresh].map(p => p.dir).filter(Boolean)
    .map(d => d.toLowerCase()));
  const removedDirs = []; const lockedDirs = [];
  if (reap) {
    for (const e of fs.readdirSync(os.tmpdir())) {
      if (!DIR_RE.test(e) && !EXTRA_MARKERS.some(m => e.startsWith(m))) continue;
      const full = path.join(os.tmpdir(), e);
      if (liveDirs.has(full.toLowerCase())) continue;
      if (launcherAlive(full) === true) continue;  // run still starting
      const t = dirTimestampMs(e);
      const ageSec = t == null ? null : (now - t) / 1000;
      if (ageSec != null && ageSec < graceSec) continue;
      if (ageSec == null) { try { if ((now - fs.statSync(full).birthtimeMs) / 1000 < graceSec) continue; } catch { } }
      try { fs.rmSync(full, { recursive: true, force: true, maxRetries: 2 }); removedDirs.push(full); }
      catch { lockedDirs.push(full); }
    }
  }
  return { staleProcs: stale.map(p => ({ pid: p.pid, rss: p.ws, dir: p.dir })),
    killed, removedDirs, lockedDirs, freshSkipped: fresh.length };
}

function freePort() {
  return new Promise((res, rej) => {
    const s = net.createServer();
    s.once('error', rej);
    s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)); });
  });
}

/**
 * Launch an owned headless Chrome with tree-kill guaranteed on every exit:
 * 'exit' event (covers normal end, process.exit(), uncaughtException) and
 * SIGINT/SIGTERM handlers. Returns { proc, pid, port, userDataDir, cleanup() }.
 */
async function launchOwnedChrome({ headless = true, port = null, extraArgs = [],
  initialUrl = 'about:blank', profileSuffix = '', sweep = true } = {}) {
  if (sweep) { try { sweepStale({ reap: true }); } catch { } }
  if (!port) port = await freePort();
  const udd = newProfileDir(profileSuffix);
  const chrome = findChrome();
  const args = [chrome, `--user-data-dir=${udd}`, `--remote-debugging-port=${port}`,
    '--no-first-run', '--no-default-browser-check', '--disable-gpu'];
  if (headless) args.splice(1, 0, '--headless=new');
  args.push(...extraArgs, initialUrl);

  const proc = spawn(args[0], args.slice(1), { stdio: 'ignore' }); // NOT detached

  const kill = () => killTreeSync(proc.pid);
  process.once('exit', kill);
  const onSignal = () => { kill(); process.exit(130); };
  process.once('SIGINT', onSignal);
  process.once('SIGTERM', onSignal);

  return {
    proc, pid: proc.pid, port, userDataDir: udd,
    kill,
    async cleanup() {
      kill();
      process.off('exit', kill);
      process.off('SIGINT', onSignal);
      process.off('SIGTERM', onSignal);
      // stragglers still naming our dir (crashpad, reparented helpers)
      for (const p of selectOwned(listProcs())) {
        const d = extractUserDataDir(p.cmdline);
        if (d && d.toLowerCase() === udd.toLowerCase()) killTreeSync(p.pid);
      }
      const ok = await removeDirTolerant(udd);
      if (!ok) console.log(`[reaper] profile dir still locked, left in place (processes are dead): ${udd}`);
    },
  };
}

async function withOwnedChrome(opts, fn) {
  const br = await launchOwnedChrome(opts);
  try {
    return await fn(br);
  } finally {
    await br.cleanup();
  }
}

module.exports = { PREFIX, STALE_GRACE_SEC, findChrome, isOwnedCmdline,
  extractUserDataDir, selectOwned, newProfileDir, killTreeSync, listProcs,
  sweepStale, launchOwnedChrome, withOwnedChrome, removeDirTolerant, freePort };
