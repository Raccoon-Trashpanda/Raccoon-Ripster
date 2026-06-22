"""
Apple Music authentication flow.

  GET  /apple/login            — login page with bookmarklet instructions
  GET  /api/apple/set-token    — receive MUT from bookmarklet (?mut=...)
  GET  /api/apple/auth-status  — current token status

The user drags the "🍎 Get Apple Token" bookmarklet to their browser bar,
visits music.apple.com, clicks the bookmarklet, which opens a tab to
/api/apple/set-token?mut=<token>.  That handler saves the token and closes
the tab via a self-closing success page.

Install: apple_auth.install(app, cfg, save_config_fn, broadcast_fn)
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()

_cfg: dict     = {}
_save_config   = None
_broadcast     = None


def install(app, ctx) -> None:
    global _cfg, _save_config, _broadcast
    _cfg         = ctx.config
    _save_config = ctx.save_config
    _broadcast   = ctx.broadcast
    app.include_router(router)


# ── bookmarklet code (injected into the page as a draggable link) ─────────────

# Extracts media-user-token via MusicKit (preferred) or cookies (fallback),
# then opens http://127.0.0.1:7799/api/apple/set-token?mut=<value> in a new tab.
_BOOKMARKLET_JS = (
    "javascript:(function(){"
    "var m;"
    "if(window.MusicKit){"
        "try{m=MusicKit.getInstance().musicUserToken;}catch(e){}"
    "}"
    "if(!m){"
        "var c=(document.cookie||'').split(';').find(function(x){"
            "return x.trim().toLowerCase().startsWith('media-user-token=');"
        "});"
        "if(c)m=decodeURIComponent(c.split('=').slice(1).join('=').trim());"
    "}"
    "if(m&&m.length>20){"
        "window.open('http://127.0.0.1:7799/api/apple/set-token?mut='+encodeURIComponent(m),'_blank');"
    "}else{"
        "alert('Apple Music token not found.\\n\\nMake sure you are logged in to music.apple.com and the page is fully loaded.');"
    "}"
    "})();"
)

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ripster — Apple Music Login</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0a0d;color:#e8e8ec;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display",system-ui,sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.card{background:#15151a;border:1px solid #222;border-radius:18px;padding:36px 38px;max-width:520px;width:100%;
  box-shadow:0 24px 80px rgba(0,0,0,.55)}
.logo{font-size:38px;margin-bottom:6px}
h1{font-size:24px;font-weight:800;letter-spacing:-.4px;margin-bottom:4px}
.sub{color:#7a7a85;font-size:13px;margin-bottom:28px;line-height:1.5}
.step{display:flex;gap:14px;align-items:flex-start;margin-bottom:22px}
.num{background:rgba(252,60,68,.15);color:#fc3c44;border:1px solid rgba(252,60,68,.3);
  border-radius:50%;width:28px;height:28px;flex-shrink:0;display:flex;align-items:center;
  justify-content:center;font-size:13px;font-weight:700;margin-top:1px}
.step-body{flex:1}
.step-title{font-size:13px;font-weight:700;margin-bottom:4px}
.step-text{font-size:12px;color:#7a7a85;line-height:1.55}
.bookmarklet-wrap{margin:10px 0 4px;background:#0a0a0d;border:1px dashed #333;
  border-radius:10px;padding:12px 16px;display:flex;align-items:center;gap:12px;
  color:#c7c7cc;font-size:12px}
.bookmarklet-btn{display:inline-block;padding:9px 18px;background:linear-gradient(135deg,#fc3c44,#ff6b35);
  color:#fff;border-radius:9px;font-size:14px;font-weight:700;text-decoration:none;cursor:grab;
  white-space:nowrap;box-shadow:0 4px 14px rgba(252,60,68,.35);transition:box-shadow .15s}
.bookmarklet-btn:hover{box-shadow:0 6px 20px rgba(252,60,68,.5)}
.open-btn{display:block;width:100%;padding:12px;margin-top:20px;
  background:rgba(10,132,255,.12);border:1px solid rgba(10,132,255,.3);
  color:#0a84ff;border-radius:10px;font-size:14px;font-weight:700;
  cursor:pointer;font-family:inherit;text-align:center;text-decoration:none;
  transition:background .15s}
.open-btn:hover{background:rgba(10,132,255,.2)}
.waiting{margin-top:24px;padding:14px 18px;background:rgba(10,132,255,.07);
  border:1px solid rgba(10,132,255,.15);border-radius:10px;
  font-size:13px;color:#0a84ff;display:flex;align-items:center;gap:10px}
.dot{width:8px;height:8px;border-radius:50%;background:#0a84ff;
  animation:pulse 1.4s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:.3;transform:scale(.85)}50%{opacity:1;transform:scale(1)}}
.success{margin-top:24px;padding:14px 18px;background:rgba(48,209,88,.07);
  border:1px solid rgba(48,209,88,.2);border-radius:10px;
  font-size:13px;color:#30d158;display:none;align-items:center;gap:10px}
.tip{margin-top:16px;font-size:11px;color:#4a4a52;line-height:1.5}
</style>
</head>
<body>
<div class="card">
  <div class="logo">🍎</div>
  <h1>Apple Music</h1>
  <p class="sub">Для скачивания треков нужен <b>media-user-token</b> — токен твоей сессии Apple Music.<br>
  Следуй шагам ниже, чтобы получить его автоматически.</p>

  <div class="step">
    <div class="num">1</div>
    <div class="step-body">
      <div class="step-title">Добавь закладку в браузер</div>
      <div class="step-text">Перетащи кнопку ниже в панель закладок браузера.</div>
      <div class="bookmarklet-wrap">
        <a href="BOOKMARKLET_PLACEHOLDER" class="bookmarklet-btn" title="Перетащи в панель закладок">🍎 Get Apple Token</a>
        <span>← перетащи в закладки</span>
      </div>
    </div>
  </div>

  <div class="step">
    <div class="num">2</div>
    <div class="step-body">
      <div class="step-title">Открой music.apple.com и войди</div>
      <div class="step-text">Нажми кнопку ниже. Если ты уже залогинен — всё готово. Если нет — войди через Apple ID.</div>
      <a href="https://music.apple.com" target="_blank" class="open-btn">🌐 Открыть music.apple.com →</a>
    </div>
  </div>

  <div class="step">
    <div class="num">3</div>
    <div class="step-body">
      <div class="step-title">Нажми закладку на странице Apple Music</div>
      <div class="step-text">Находясь на music.apple.com, нажми закладку <b>🍎 Get Apple Token</b> из панели закладок.<br>
      Откроется новая вкладка — токен будет сохранён автоматически.</div>
    </div>
  </div>

  <div class="waiting" id="waiting">
    <div class="dot"></div>
    Ожидаю токен… Этот экран обновится после успешного входа.
  </div>
  <div class="success" id="success">
    ✅ Токен получен! Можешь закрыть это окно.
  </div>

  <p class="tip">
    Проблемы? Убедись что панель закладок видима (Ctrl+Shift+B в Chrome/Firefox).<br>
    Или скопируй media-user-token вручную: DevTools → Application → Cookies → music.apple.com.
  </p>
</div>
<script>
try {
  const ws = new WebSocket('ws://127.0.0.1:7799/ws');
  ws.onmessage = function(e) {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === 'apple_authed') {
        document.getElementById('waiting').style.display = 'none';
        const s = document.getElementById('success');
        s.style.display = 'flex';
        s.textContent = '✅ Токен получен! Можешь закрыть это окно.';
        setTimeout(() => { try { window.close(); } catch(e) {} }, 2500);
      }
    } catch(e) {}
  };
} catch(e) {}
</script>
</body>
</html>"""


@router.get("/apple/login", response_class=HTMLResponse)
async def apple_login_page():
    page = _LOGIN_HTML.replace("BOOKMARKLET_PLACEHOLDER", _BOOKMARKLET_JS)
    return HTMLResponse(page)


@router.get("/api/apple/set-token", response_class=HTMLResponse)
async def apple_set_token(mut: str = ""):
    """Receive media-user-token via GET param (opened by bookmarklet).

    Saves the token to config, broadcasts apple_authed, and returns a
    self-closing HTML page.
    """
    mut = mut.strip()

    if not mut or len(mut) < 20:
        return HTMLResponse(_result_page(
            ok=False,
            title="Токен не получен",
            body="Получен пустой или слишком короткий токен.<br>"
                 "Убедись, что ты залогинен на music.apple.com и попробуй снова.",
        ), status_code=400)

    _cfg["media-user-token"] = mut
    if _save_config:
        _save_config(_cfg)
    if _broadcast:
        import asyncio
        asyncio.create_task(_broadcast({
            "type": "apple_authed",
            "mut_length": len(mut),
        }))

    return HTMLResponse(_result_page(
        ok=True,
        title="✅ Токен сохранён",
        body=f"media-user-token ({len(mut)} символов) сохранён в Ripster.<br>"
             "Это окно закроется через несколько секунд.",
    ))


def _cookies_path() -> Path:
    """The cookies.txt path gamdl reads (gamdl-cookies-path), resolved relative
    to the app working dir when not absolute."""
    p = (_cfg.get("gamdl-cookies-path") or "cookies.txt").strip() or "cookies.txt"
    return Path(p)


@router.get("/api/apple/cookies-status")
async def apple_cookies_status():
    p = _cookies_path()
    n = 0
    if p.exists():
        try:
            n = len([l for l in p.read_text(encoding="utf-8", errors="ignore").splitlines()
                     if l.strip() and not l.lstrip().startswith("#")])
        except Exception:
            pass
    return {"exists": p.exists(), "lines": n, "path": str(p)}


@router.post("/api/apple/cookies")
async def apple_save_cookies(request: Request):
    """Save a pasted Netscape cookies.txt (from a logged-in music.apple.com) to
    the gamdl cookies path. Empty body clears it."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    text = (data.get("text") or "").strip()
    p = _cookies_path()
    if not text:
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass
        return {"ok": True, "exists": False, "lines": 0}
    try:
        if p.parent and str(p.parent) not in ("", "."):
            p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + ("" if text.endswith("\n") else "\n"), encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": str(e)}
    lines = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    looks_apple = "apple.com" in text.lower()
    return {"ok": True, "exists": True, "lines": len(lines),
            "looks_apple": looks_apple, "path": str(p)}


@router.get("/api/apple/auth-status")
async def apple_auth_status():
    mut    = (_cfg.get("media-user-token") or "").strip()
    bearer = (_cfg.get("authorization-token") or "").strip()
    return {
        "mut_set":     bool(mut),
        "mut_length":  len(mut),
        "bearer_set":  bool(bearer),
        "storefront":  _cfg.get("storefront", ""),
    }


# ── HTML helpers ──────────────────────────────────────────────────────────────

def _result_page(*, ok: bool, title: str, body: str) -> str:
    color  = "#30d158" if ok else "#fc3c44"
    bg     = "rgba(48,209,88,.07)"  if ok else "rgba(252,60,68,.07)"
    border = "rgba(48,209,88,.2)"   if ok else "rgba(252,60,68,.2)"
    close_script = (
        "<script>setTimeout(function(){try{window.close();}catch(e){}},2500);</script>"
        if ok else ""
    )
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Ripster — Apple Music</title>
<style>
body{{margin:0;background:#0a0a0d;color:#e8e8ec;
  font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;
  display:flex;align-items:center;justify-content:center;min-height:100vh}}
.card{{background:#15151a;border:1px solid #222;border-radius:14px;padding:32px 34px;
  max-width:400px;width:100%;text-align:center}}
.icon{{font-size:48px;margin-bottom:12px}}
h1{{font-size:20px;font-weight:800;margin-bottom:8px;color:{color}}}
.box{{background:{bg};border:1px solid {border};border-radius:9px;
  padding:14px 16px;font-size:13px;color:#c7c7cc;line-height:1.6;margin-top:14px}}
.note{{font-size:11px;color:#4a4a52;margin-top:12px}}
</style></head><body>
<div class="card">
  <div class="icon">{'🍎' if ok else '⚠️'}</div>
  <h1>{title}</h1>
  <div class="box">{body}</div>
  {'<p class="note">Закрой это окно и вернись в Ripster.</p>' if ok else ''}
</div>
{close_script}
</body></html>"""
