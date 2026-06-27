#!/usr/bin/env node
/**
 * Lucida SoundCloud runner — wraps the lucida library as a subprocess-friendly CLI.
 *
 * Usage:
 *   node runner.mjs <url> [--output=<dir>] [--oauth-token=<token>] [--hq]
 *
 * Stdout format (matches SpotiFLAC parser in soundcloud.py):
 *   Found Track: Artist - Title
 *   Found Album: Artist - Title
 *   Queued N tracks for download...
 *   [N/M] Downloading: Title
 *   [N/M] Success: Title - Artist
 *   [N/M] Failed: Title - error message
 *   Summary: N Success, N Failed. Output dir: /path
 *
 * Requires: Node.js 18+, ffmpeg in PATH (for HLS streams)
 */

import { createWriteStream } from 'node:fs'
import { mkdir, rm, rename } from 'node:fs/promises'
import { pipeline }           from 'node:stream/promises'
import { Readable }           from 'node:stream'
import { execFile }           from 'node:child_process'
import { promisify }          from 'node:util'
import path                   from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const execFileP = promisify(execFile)

// ── Args ──────────────────────────────────────────────────────────────────────
const argv      = process.argv.slice(2)
const url       = argv.find(a => !a.startsWith('--'))
const outputDir = argv.find(a => a.startsWith('--output='))?.slice('--output='.length) || 'downloads'
const oauthToken= argv.find(a => a.startsWith('--oauth-token='))?.slice('--oauth-token='.length)
const hq        = argv.includes('--hq')
// Cover-source override (MixesDB / YouTube art chosen in the mix drawer). When
// set, it replaces the SoundCloud artwork embedded into the file.
const coverOverride = argv.find(a => a.startsWith('--cover-url='))?.slice('--cover-url='.length) || ''

if (!url) {
  console.error('Error: URL не указан')
  console.error('Usage: node runner.mjs <url> [--output=<dir>] [--oauth-token=<token>] [--hq]')
  process.exit(1)
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function sanitize(str) {
  return (str || 'track')
    .replace(/[/\\:*?"<>|\r\n]/g, '_')
    .replace(/\s+/g, ' ')
    .trim() || 'track'
}

function mimeToExt(mimeType = '') {
  if (mimeType.includes('mpeg'))  return 'mp3'
  if (mimeType.includes('aac'))   return 'm4a'
  if (mimeType.includes('mp4'))   return 'm4a'
  if (mimeType.includes('ogg'))   return 'ogg'
  if (mimeType.includes('opus'))  return 'opus'
  return 'mp3'
}

function pad(n, total) {
  return `[${n}/${total}]`
}

// SoundCloud serves a tiny "-large" (100x100) artwork by default; the same CDN
// path exposes a 500x500 ("-t500x500") and "-original". Upgrade the URL so the
// embedded + folder cover is full-size instead of a thumbnail.
function _upgradeScArt(url = '') {
  if (!url || !url.includes('sndcdn.com')) return url
  return url.replace(/-(large|t67x67|t120x120|t200x200|badge|small|tiny|mini|crop|t\d+x\d+)\.(jpg|png)/i,
                     '-t500x500.$2')
}

// Extract {title, artist, album, coverUrl} from a Lucida metadata object.
function _metaOf(md = {}) {
  const covers = md.coverArtwork || []
  const cover  = [...covers].sort((a, b) => (b.width || 0) - (a.width || 0))[0]
  return {
    title:    md.title || '',
    artist:   md.artists?.[0]?.name || '',
    album:    md.album?.name || md.album?.title || '',
    coverUrl: _upgradeScArt(cover?.url || ''),
  }
}

// Like _metaOf but applies the --cover-url override (if any) so the embedded
// artwork comes from the user's chosen source instead of SoundCloud.
function _metaCover(md = {}) {
  const m = _metaOf(md)
  if (coverOverride) m.coverUrl = coverOverride
  return m
}

// Heartbeat: emit a log line every N seconds while the wrapped promise runs.
// Needed because the queue runner kills processes that go silent for too long,
// and both ``pipeline()`` and ``ffmpeg`` (with -loglevel error) produce no
// output during long downloads / encodes of multi-hour mixes.
async function withHeartbeat(label, intervalMs, fn) {
  const start = Date.now()
  let beats = 0
  const t = setInterval(() => {
    beats++
    const secs = ((Date.now() - start) / 1000).toFixed(0)
    console.log(`  ⏱ ${label} … ${secs}s`)
  }, intervalMs)
  try { return await fn() }
  finally { clearInterval(t) }
}

// Retry transient NETWORK failures (undici throws bare "Error: terminated" when a
// SoundCloud/CDN connection drops mid-request). Without this a single blip during
// the initial resolve or a track stream kills the ENTIRE job with no Summary line —
// which the Python side could only report as the misleading "проверь Node.js/Lucida".
// Permanent errors (404, "not a track", auth) are NOT retried — they don't match.
const _TRANSIENT = /terminated|fetch failed|ECONNRESET|ETIMEDOUT|socket hang ?up|EAI_AGAIN|network|aborted|premature close|other side closed|UND_ERR/i
async function withRetry(label, fn, tries = 3) {
  let lastErr
  for (let attempt = 1; attempt <= tries; attempt++) {
    try { return await fn() }
    catch (e) {
      lastErr = e
      const msg = (e?.message || String(e) || '').split('\n')[0]
      if (attempt < tries && _TRANSIENT.test(msg)) {
        const wait = 1500 * attempt
        console.log(`  ↻ ${label}: сетевой обрыв (${msg}) — повтор ${attempt}/${tries - 1} через ${(wait / 1000).toFixed(0)}s`)
        await new Promise(r => setTimeout(r, wait))
        continue
      }
      throw e
    }
  }
  throw lastErr
}

async function _downloadCover(url, dest) {
  if (!url) return false
  try {
    const r = await fetch(url)
    if (!r.ok || !r.body) return false
    await pipeline(Readable.fromWeb(r.body), createWriteStream(dest))
    return true
  } catch {
    return false
  }
}

// Transcode the raw download to a tagged MP3 with embedded cover art.
// Returns the final file path (the .mp3, or the original on ffmpeg failure).
async function finalize(rawPath, meta) {
  const dir   = path.dirname(rawPath)
  const stem  = path.basename(rawPath, path.extname(rawPath))
  const isMp3 = path.extname(rawPath).toLowerCase() === '.mp3'
  const mp3Path   = path.join(dir, stem + '.mp3')
  const coverPath = path.join(dir, '.' + stem + '.cover')
  const tmpOut    = path.join(dir, '.' + stem + '.tmp.mp3')

  console.log('  ⚙ Кодирую в MP3 + теги…')
  const hasCover = await _downloadCover(meta.coverUrl, coverPath)

  const args = ['-y', '-loglevel', 'error', '-i', rawPath]
  if (hasCover) args.push('-i', coverPath)
  args.push('-map', '0:a:0')
  if (hasCover) args.push('-map', '1:v:0', '-c:v', 'copy', '-disposition:v', 'attached_pic')
  args.push('-c:a', isMp3 ? 'copy' : 'libmp3lame')
  if (!isMp3) args.push('-b:a', '320k')
  args.push('-id3v2_version', '3')
  if (meta.title)  args.push('-metadata', `title=${meta.title}`)
  if (meta.artist) args.push('-metadata', `artist=${meta.artist}`)
  if (meta.album)  args.push('-metadata', `album=${meta.album}`)
  args.push(tmpOut)

  try {
    await withHeartbeat('кодирую', 25_000, () =>
      execFileP('ffmpeg', args, { maxBuffer: 1 << 25 }))
  } catch (e) {
    await rm(tmpOut,    { force: true }).catch(() => {})
    await rm(coverPath, { force: true }).catch(() => {})
    console.error(`  ⚠ ffmpeg не справился (${(e.message || '').split('\n')[0]}) — оставляю как есть`)
    return rawPath
  }
  await rm(coverPath, { force: true }).catch(() => {})
  await rm(rawPath,   { force: true }).catch(() => {})
  if (mp3Path !== rawPath) await rm(mp3Path, { force: true }).catch(() => {})
  await rename(tmpOut, mp3Path)
  return mp3Path
}

// ── Import Lucida (installed via npm in this directory) ───────────────────────
// Lucida is cloned + compiled into ./lucida-src/build by the installer.
const _here  = path.dirname(fileURLToPath(import.meta.url))
const _build = path.join(_here, 'lucida-src', 'build')
let Lucida, Soundcloud
try {
  ;({ default: Lucida }     = await import(pathToFileURL(path.join(_build, 'index.js')).href))
  ;({ default: Soundcloud } = await import(pathToFileURL(path.join(_build, 'streamers', 'soundcloud', 'main.js')).href))
} catch (e) {
  console.error(`Error: Lucida не найден — ${e.message}`)
  console.error('Открой Settings → SoundCloud и нажми «Установить движок»')
  process.exit(1)
}

// ── Download single track ─────────────────────────────────────────────────────
async function downloadTrack(lucida, trackUrl, destDir, num, total) {
  const label = `${pad(num, total)}`
  let title = '?'
  try {
    const tr = await withRetry('resolve', () => lucida.getByUrl(trackUrl))
    if (tr.type !== 'track') throw new Error('Not a track response')

    const artist = tr.metadata.artists?.[0]?.name || 'Unknown'
    title = tr.metadata.title || `Track ${num}`
    console.log(`${label} Downloading: ${title}`)

    let raw
    await withRetry('качаю', async () => {
      const sr  = await tr.getStream(hq)
      const ext = mimeToExt(sr.mimeType)
      raw = path.join(destDir, sanitize(`${String(num).padStart(2, '0')} ${artist} - ${title}.${ext}`))
      await withHeartbeat('качаю', 25_000, () =>
        pipeline(sr.stream, createWriteStream(raw)))
    })
    await finalize(raw, _metaCover(tr.metadata))
    console.log(`${label} Success: ${title} - ${artist}`)
    return true
  } catch (e) {
    console.error(`${label} Failed: ${title} - ${e?.message || String(e) || 'неизвестная ошибка'}`)
    return false
  }
}

// ── Main ──────────────────────────────────────────────────────────────────────
const lucida = new Lucida({
  modules: { soundcloud: new Soundcloud({ oauthToken, dispatcher: undefined }) }
})

try {
  const result = await withRetry('resolve', () => lucida.getByUrl(url))
  await mkdir(outputDir, { recursive: true })

  if (result.type === 'track') {
    const artist = result.metadata.artists?.[0]?.name || 'Unknown'
    const title  = result.metadata.title || 'track'
    console.log(`Found Track: ${artist} - ${title}`)
    console.log('Queued 1 tracks for download...')
    console.log(`[1/1] Downloading: ${title}`)

    // Each track gets its OWN subfolder (like albums) so every queue task has a
    // unique, isolated output dir — /api/download-file can then hand a guest just
    // this task's file(s) instead of the whole shared save root.
    let _tname = sanitize(`${artist} - ${title}`)
    if (!_tname || _tname.length < 2) _tname = sanitize(title) || sanitize(artist) || `track_${Date.now()}`
    const trackDir = path.join(outputDir, _tname)
    await mkdir(trackDir, { recursive: true })
    // Save a folder cover.jpg next to the track so delivery sends art + audio
    // together (the bot picks up cover.jpg from the task folder).
    await _downloadCover(_metaCover(result.metadata).coverUrl, path.join(trackDir, 'cover.jpg'))

    try {
      let raw
      await withRetry('качаю', async () => {
        const sr  = await result.getStream(hq)
        const ext = mimeToExt(sr.mimeType)
        raw = path.join(trackDir, sanitize(`${artist} - ${title}.${ext}`))
        await withHeartbeat('качаю', 25_000, () =>
          pipeline(sr.stream, createWriteStream(raw)))
      })
      await finalize(raw, _metaCover(result.metadata))
      console.log(`[1/1] Success: ${title} - ${artist}`)
      console.log(`Summary: 1 Success, 0 Failed. Output dir: ${trackDir}`)
    } catch (e) {
      console.error(`[1/1] Failed: ${title} - ${e?.message || String(e) || 'неизвестная ошибка'}`)
      console.log(`Summary: 0 Success, 1 Failed. Output dir: ${trackDir}`)
      process.exitCode = 1
    }

  } else if (result.type === 'album') {
    const artist = result.metadata.artists?.[0]?.name || 'Unknown'
    const title  = result.metadata.title || 'playlist'
    const tracks = result.tracks || []
    console.log(`Found Album: ${artist} - ${title}`)
    console.log(`Queued ${tracks.length} tracks for download...`)

    let _aname = sanitize(`${artist} - ${title}`)
    if (!_aname || _aname.length < 2) _aname = sanitize(title) || sanitize(artist) || `album_${Date.now()}`
    const albumDir = path.join(outputDir, _aname)
    await mkdir(albumDir, { recursive: true })
    // Folder cover.jpg so delivery hands over art + tracks together.
    await _downloadCover(_metaCover(result.metadata).coverUrl, path.join(albumDir, 'cover.jpg'))

    let ok = 0, failed = 0
    for (let i = 0; i < tracks.length; i++) {
      const track = tracks[i]
      const trackUrl = track.url || track.permalink_url
      if (!trackUrl) {
        console.error(`${pad(i + 1, tracks.length)} Failed: ${track.title || '?'} - нет URL`)
        failed++
        continue
      }
      const success = await downloadTrack(lucida, trackUrl, albumDir, i + 1, tracks.length)
      if (success) ok++; else failed++
    }
    console.log(`Summary: ${ok} Success, ${failed} Failed. Output dir: ${albumDir}`)
    if (failed > 0) process.exitCode = 1

  } else if (result.type === 'artist') {
    console.error('Error: Страница артиста — скачивай конкретный трек или плейлист')
    process.exitCode = 1
  } else {
    console.error(`Error: Неизвестный тип: ${result.type}`)
    process.exitCode = 1
  }
} catch (e) {
  console.error(`Error: ${e.message}`)
  process.exitCode = 1
} finally {
  await lucida.disconnect().catch(() => {})
}
