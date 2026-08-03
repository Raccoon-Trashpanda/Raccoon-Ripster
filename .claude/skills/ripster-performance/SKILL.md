---
name: ripster-performance
description: What can and cannot be accelerated in Ripster, measured rather than assumed. READ THIS BEFORE any performance work, and especially before touching hardware acceleration — the vendor accelerators everyone reaches for (Intel QSV, AMD AMF, NVIDIA NVENC/NVDEC, Media Foundation) are VIDEO engines and cannot touch lossless audio, which is what Ripster spends its time on. Also covers what is already parallel, what is slow on purpose, and the measurement recipe. Triggers - "ускорить", "тормозит", "лагает на слабой машине", "GPU/аппаратное ускорение", "hwaccel", "why is X slow", parallel downloads, segmented/byte-range downloads, optimising the radar feed or any long list.
---

# Ripster performance: measured facts

**The dev box is a Ryzen 7600X + RTX 4070 Ti.** Timing anything here and calling
it fast proves nothing about the machines this has to stay usable on. Measure
**work done** — DOM nodes, processes spawned, bytes, request counts — because
that is machine-independent, and cutting it helps exactly the weak machines the
numbers here would hide.

## The hardware-acceleration question, settled (2026-07-25)

Measured with the shipped ffmpeg 8.0.1:

| | count |
|---|---|
| hardware **video** encoders (NVENC / AMF / QSV / Media Foundation) | 15 |
| GPU filters (CUDA / OpenCL / Vulkan) | 47 — every one `V->V` |
| hardware **audio** encoders | **0** |
| hardware **audio** decoders | **0** |
| audio filters with any GPU backend | **0** |

The only "hardware" audio paths that exist at all are Media Foundation's
`aac_mf` / `ac3_mf` / `mp3_mf`, all **lossy**. Our own codecs report
`Threading capabilities: none` for both `alac` and `flac` — single-threaded,
CPU-only, no GPU path anywhere.

**So: QSV / AMF / NVENC cannot accelerate downloading, transcoding, decode-checks
or spectrograms.** Do not spend a session wiring `-hwaccel` into the audio path.
The only accelerators that exist for audio work are **process-level parallelism**
and **doing less work**.

Where hwaccel *would* genuinely apply is video — Apple music videos via gamdl.
If video re-encoding ever appears, pick the encoder at runtime (NVENC → AMF →
QSV → D3D11VA → software) rather than hardcoding a vendor, and always keep the
software fallback.

## Already parallel — don't "add" it again

- **Apple segmented download**: `fetchRanged` in `utils/runv2/runv2.go` — 8MB
  chunks, up to 6 concurrent, for files ≥16MB whose server advertises
  `Accept-Ranges: bytes`. This is real multi-connection downloading and it is in
  the shipped binary.
  - **A bare `status == 206` check is NOT sufficient**: an Apple CDN edge will
    answer 206 for a *different* range than requested and you assemble a corrupt
    file. The `Content-Range` echo check is what catches it. Keep it.
  - Not covered: files above `max-memory-limit` (256MB) take the streaming path,
    because the segmented path buffers in RAM. Long ALAC DJ mixes land there.
    Fixable by writing chunks to disk; nobody has needed it yet.
- **Integrity verify**: decode-checks run in a thread pool (cores-1, max 8,
  `integrity-verify-workers`). Measured 3.57s → 1.40s on 16 tracks.
- **HTTP pooling**: 30 keep-alive / 100 max connections, already set.

## Slow on purpose — do not "fix"

- **HTTP/2 is disabled deliberately** (`_HTTP2 = False` in `http_client.py`).
  Apple/iTunes drops pooled h2 connections with GOAWAY and httpx surfaces it as a
  search failure. Enabling it is a regression, not an optimisation.
- **`apple-parallel-tracks` is off** — it crashed the whole service. Leave it.
- **`max-parallel`, per-service rate limits** — these exist to avoid bans and
  Apple device-limit errors, not because nobody thought of raising them. Raising
  them is the owner's call, not a silent optimisation.
- **Per-track decryption is inherently serial**: `downloadAndDecryptFile` streams
  through ONE socket to the wrapper. The download can be parallel; the decrypt
  cannot. That, not HTTP, is the per-track floor.

## Measured and NOT worth optimising

- Tag reading: **~1 ms/file**.
- Every hot endpoint except the radar sources: 19–185 ms (config, queue, history,
  stats, Spotify feed, watchlist, suggestions). Don't optimise what isn't slow.

## The recipe

**1. Time every endpoint before touching anything.** This is what found that the
only slow endpoints were the two added that same day:

```python
for p in ["/api/config","/api/queue","/api/history?limit=300","/api/stats?period=all",
          "/api/spotify/releases?days=30&types=album,single,compilation",
          "/api/watchlist","/api/releases/bbc?days=30"]:
    t=time.time(); fetch(p); print(p, round((time.time()-t)*1000), "ms")
```
Flag anything over ~300ms; leave the rest alone.

**2. For UI work, count nodes, not milliseconds.** The radar renders
`_REL_PAGE_SIZE = 120` cards × ~30 elements ≈ 3600 nodes, rebuilt through
`innerHTML`. Two classes of bug live here:
- an input wired straight to a full re-render (search was `oninput=` with **no
  debounce** — every keystroke discarded and re-parsed all 3600 nodes);
- no `content-visibility` / `contain`, so the browser lays out and paints cards
  that are off-screen.

**3. Cache third-party sources on disk and serve stale-while-revalidate.** An
in-memory-only cache pays its cold cost again after every restart. See
`ripster/routes/radar.py`.

**4. Prove the optimisation didn't change behaviour**, not just that it is
faster — the parallel verify was accepted only after asserting identical
checked/ok/corrupt output against the serial path.
