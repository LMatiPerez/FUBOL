"""
API + servidor web para FUBOL TV sin popups.

Endpoints:
  GET /                    -> Pagina principal con lista de partidos
  GET /api/partidos        -> JSON con lista de partidos
  GET /api/stream?url=...  -> JSON con URL del stream del partido
  GET /ver?url=...         -> Pagina del player limpio
"""
import html as html_lib
import json
import logging
from contextlib import asynccontextmanager
from urllib.parse import quote, urljoin

import httpx
import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from playwright.async_api import async_playwright

import scraper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

_pw = None
_browser = None


@asynccontextmanager
async def lifespan(app):
    global _pw, _browser
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(headless=True)
    log.info("Browser Playwright iniciado")
    yield
    await _browser.close()
    await _pw.stop()
    log.info("Browser cerrado")


app = FastAPI(title="FUBOL - Sin Popups", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    with open("templates/index.html", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/ver", response_class=HTMLResponse)
async def ver_partido(request: Request):
    with open("templates/player.html", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/partidos")
async def api_partidos():
    log.info("Scrapeando lista de partidos...")
    try:
        partidos = await scraper.get_partidos_con_browser(_browser)
        return JSONResponse({"ok": True, "partidos": partidos, "total": len(partidos)})
    except Exception as e:
        log.error(f"Error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/stream")
async def api_stream(url: str = Query(...)):
    log.info(f"Extrayendo stream de: {url}")
    try:
        result = await scraper.get_stream_con_browser(_browser, url)
        return JSONResponse({"ok": True, **result})
    except Exception as e:
        log.error(f"Error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/proxy")
async def proxy_stream(url: str = Query(...)):
    """
    Proxy para m3u8 y segmentos TS.
    Todos los pedidos al CDN salen desde la IP del servidor.
    """
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://latamvidz1.com/",
        "Origin": "https://latamvidz1.com",
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            resp = await client.get(url, headers=hdrs)
            ct = resp.headers.get("content-type", "application/octet-stream")
            body = await resp.aread()

        if "mpegurl" in ct or ".m3u8" in url:
            base = url.rsplit("/", 1)[0] + "/"
            lines = []
            text = body.decode(resp.encoding or "utf-8", errors="ignore")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    seg = stripped if stripped.startswith("http") else urljoin(base, stripped)
                    line = f"/api/proxy?url={quote(seg, safe='')}"
                lines.append(line)

            playlist = "\n".join(lines).encode("utf-8")
            return Response(
                content=playlist,
                status_code=resp.status_code,
                media_type="application/vnd.apple.mpegurl",
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Cache-Control": "no-store",
                },
            )

        return Response(
            content=body,
            status_code=resp.status_code,
            media_type=ct,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-store",
            },
        )
    except Exception as e:
        log.error(f"Proxy error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


@app.get("/canal", response_class=HTMLResponse)
async def canal_proxy(request: Request, stream: str = Query(...), titulo: str = Query("")):
    """
    Fetchea el player de latamvidz1.com y le agrega una barra superior propia.
    """
    user_ip = request.headers.get("x-forwarded-for", request.client.host or "").split(",")[0].strip()

    player_url = f"https://latamvidz1.com/canal.php?stream={stream}"
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://pelotalibretv.su/",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
        "X-Forwarded-For": user_ip,
        "X-Real-IP": user_ip,
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(player_url, headers=hdrs)

        if resp.status_code != 200:
            return HTMLResponse(f"<h2>Error {resp.status_code} al obtener el player</h2>", status_code=502)

        import re as _re

        html = resp.text
        html = _re.sub(r'<script[^>]*aclib[^>]*>.*?</script>', "", html, flags=_re.DOTALL | _re.IGNORECASE)
        html = _re.sub(r"aclib\.run\w+\([^)]*\);?", "", html)
        html = html.replace('src="//', 'src="https://')

        topbar_title = html_lib.escape(titulo or stream.upper())
        topbar = f"""<div style="position:fixed;top:0;left:0;right:0;z-index:9999;background:#1a1f2e;border-bottom:2px solid #2d6a4f;padding:9px 16px;display:flex;align-items:center;gap:12px">
  <a href="/" style="color:#4ade80;text-decoration:none;font-weight:600;font-size:.9rem">Volver</a>
  <span style="color:#e2e8f0;font-size:.9rem">{topbar_title}</span>
</div>
<div style="height:44px"></div>"""

        html = html.replace("<body>", f"<body>{topbar}", 1)
        return HTMLResponse(html)
    except Exception as e:
        log.error(f"Canal proxy error: {e}")
        return HTMLResponse(f"<h2>Error: {e}</h2>", status_code=502)


@app.get("/player", response_class=HTMLResponse)
async def player_page(stream: str = Query(...), titulo: str = Query("")):
    """Pagina HLS con controles utiles para recarga, PiP y reconexion."""
    titulo_seguro = html_lib.escape(titulo or "En vivo")
    stream_js = json.dumps(stream)

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{titulo_seguro} - FUBOL</title>
  <script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js"></script>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    html, body {{ height: 100%; overflow: hidden; }}
    body {{
      background: radial-gradient(circle at top, #101826 0%, #000 42%);
      color: #e2e8f0;
      font-family: 'Segoe UI', sans-serif;
      display: flex;
      flex-direction: column;
      min-height: 100vh;
    }}
    header {{
      background: #111;
      border-bottom: 1px solid #1f2937;
      padding: 10px 16px;
      display: flex;
      align-items: center;
      gap: 12px;
      flex: 0 0 auto;
      min-height: 52px;
    }}
    header a {{ color: #4ade80; text-decoration: none; font-size: .9rem; font-weight: 600; }}
    header h2 {{
      font-size: .95rem;
      color: #e2e8f0;
      flex: 1;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .player-shell {{
      position: relative;
      flex: 1 1 auto;
      min-height: 0;
      background: #000;
    }}
    #msg {{
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 9px 12px;
      font-size: .82rem;
      color: #d1d5db;
      background: rgba(17, 24, 39, .82);
      border: 1px solid rgba(74, 222, 128, .18);
      border-radius: 999px;
      backdrop-filter: blur(10px);
      max-width: min(72vw, 480px);
      transition: opacity .2s ease, transform .2s ease;
    }}
    #msg.hidden {{ opacity: 0; transform: translateY(-8px); }}
    #msg.warn {{ color: #fde68a; }}
    #msg.error {{
      color: #fecaca;
      border-color: rgba(248, 113, 113, .28);
      background: rgba(69, 10, 10, .78);
    }}
    #msg.ok {{
      color: #bbf7d0;
      border-color: rgba(74, 222, 128, .26);
      background: rgba(6, 78, 59, .72);
    }}
    video {{
      width: 100%;
      height: 100%;
      background: #000;
      display: block;
      object-fit: contain;
    }}
    .overlay {{
      position: absolute;
      inset: 12px 12px auto 12px;
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      pointer-events: none;
    }}
    .actions {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
      pointer-events: auto;
    }}
    .ctrl-btn {{
      appearance: none;
      border: 1px solid rgba(255, 255, 255, .12);
      background: rgba(17, 24, 39, .78);
      color: #e5e7eb;
      padding: 9px 12px;
      border-radius: 999px;
      font-size: .82rem;
      font-weight: 600;
      cursor: pointer;
      backdrop-filter: blur(10px);
      transition: background .18s ease, border-color .18s ease, color .18s ease;
    }}
    .ctrl-btn:hover {{
      background: rgba(31, 41, 55, .94);
      border-color: rgba(74, 222, 128, .36);
    }}
    .ctrl-btn.active {{
      color: #bbf7d0;
      border-color: rgba(74, 222, 128, .4);
    }}
    .ctrl-btn[hidden] {{ display: none; }}
    .spinner {{
      display: inline-block;
      width: 14px;
      height: 14px;
      border: 2px solid #4ade80;
      border-top-color: transparent;
      border-radius: 50%;
      animation: spin .7s linear infinite;
      flex: 0 0 auto;
    }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    @media (max-width: 760px) {{
      header {{
        min-height: 44px;
        padding: 8px 12px;
      }}
      header a {{ font-size: .82rem; }}
      header h2 {{ font-size: .86rem; }}
      .overlay {{
        inset: 8px 8px auto 8px;
        flex-direction: column;
        align-items: stretch;
      }}
      #msg {{
        max-width: 100%;
        width: fit-content;
        font-size: .75rem;
        padding: 7px 10px;
      }}
      .actions {{ justify-content: flex-start; }}
      .ctrl-btn {{
        font-size: .75rem;
        padding: 7px 10px;
      }}
    }}
  </style>
</head>
<body>
<header>
  <a href="/">Volver</a>
  <h2>{titulo_seguro}</h2>
</header>
<div class="player-shell">
  <video id="v" controls autoplay playsinline></video>
  <div class="overlay">
    <div id="msg" class="status">
      <span class="spinner" id="spinner"></span>
      <span id="status-text">Cargando stream...</span>
    </div>
    <div class="actions">
      <button class="ctrl-btn" id="reload-btn" type="button">Recargar</button>
      <button class="ctrl-btn" id="fit-btn" type="button">Rellenar</button>
      <button class="ctrl-btn" id="pip-btn" type="button">PiP</button>
    </div>
  </div>
</div>
<script>
const m3u8 = {stream_js};
const proxyUrl = '/api/proxy?url=' + encodeURIComponent(m3u8);
const video = document.getElementById('v');
const msg = document.getElementById('msg');
const spinner = document.getElementById('spinner');
const statusText = document.getElementById('status-text');
const reloadBtn = document.getElementById('reload-btn');
const fitBtn = document.getElementById('fit-btn');
const pipBtn = document.getElementById('pip-btn');
let hls = null;
let reconnectAttempts = 0;
let reconnectTimer = null;
let hideStatusTimer = null;
const MAX_RECONNECTS = 5;
let fitMode = localStorage.getItem('fubol_fit_mode') || 'contain';

function setStatus(text, tone = '', autoHide = false) {{
  msg.className = tone ? 'status ' + tone : 'status';
  statusText.textContent = text;
  msg.classList.remove('hidden');
  spinner.hidden = tone === 'ok' || tone === 'error';
  clearTimeout(hideStatusTimer);
  if (autoHide) {{
    hideStatusTimer = setTimeout(() => msg.classList.add('hidden'), 2200);
  }}
}}

function clearReconnectTimer() {{
  if (reconnectTimer) {{
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }}
}}

function applyFitMode() {{
  video.style.objectFit = fitMode;
  fitBtn.textContent = fitMode === 'contain' ? 'Rellenar' : 'Ajustar';
  fitBtn.classList.toggle('active', fitMode === 'cover');
}}

function toggleFitMode() {{
  fitMode = fitMode === 'contain' ? 'cover' : 'contain';
  localStorage.setItem('fubol_fit_mode', fitMode);
  applyFitMode();
}}

function updatePipButton() {{
  pipBtn.textContent = document.pictureInPictureElement ? 'Salir PiP' : 'PiP';
}}

async function togglePip() {{
  if (!document.pictureInPictureEnabled || !video.requestPictureInPicture) return;
  try {{
    if (document.pictureInPictureElement) {{
      await document.exitPictureInPicture();
    }} else {{
      await video.requestPictureInPicture();
    }}
  }} catch (_error) {{
    setStatus('No se pudo activar PiP.', 'warn', true);
  }}
  updatePipButton();
}}

function destroyPlayer() {{
  clearReconnectTimer();
  if (hls) {{
    hls.destroy();
    hls = null;
  }}
  video.pause();
  video.removeAttribute('src');
  video.load();
}}

function scheduleReconnect(reason = '') {{
  if (reconnectTimer) return;
  if (reconnectAttempts >= MAX_RECONNECTS) {{
    setStatus('No pudimos reconectar la señal. Usa Recargar.', 'error');
    return;
  }}

  reconnectAttempts += 1;
  const delay = Math.min(1500 * reconnectAttempts, 7000);
  const suffix = reason ? ' ' + reason : '';
  setStatus('Reconectando' + suffix + ' (' + reconnectAttempts + '/' + MAX_RECONNECTS + ')...', 'warn');
  reconnectTimer = setTimeout(() => {{
    reconnectTimer = null;
    initPlayer(true);
  }}, delay);
}}

function initHlsPlayer() {{
  hls = new Hls({{
    enableWorker: true,
    lowLatencyMode: false,
    backBufferLength: 90,
  }});

  hls.loadSource(proxyUrl);
  hls.attachMedia(video);

  hls.on(Hls.Events.MANIFEST_PARSED, () => {{
    reconnectAttempts = 0;
    setStatus('Reproduciendo en vivo.', 'ok', true);
    video.play().catch(() => {{}});
  }});

  hls.on(Hls.Events.ERROR, (_event, data) => {{
    if (!data.fatal) return;

    if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {{
      setStatus('Se perdió la conexión. Reintentando...', 'warn');
      try {{ hls.startLoad(); }} catch (_error) {{}}
      scheduleReconnect('por red');
      return;
    }}

    if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {{
      setStatus('Recuperando audio y video...', 'warn');
      try {{
        hls.recoverMediaError();
      }} catch (_error) {{
        scheduleReconnect('por media');
      }}
      return;
    }}

    scheduleReconnect('por error fatal');
  }});
}}

async function initNativePlayer() {{
  video.src = proxyUrl;
  await video.play().catch(() => {{}});
}}

async function initPlayer(isReconnect = false) {{
  destroyPlayer();
  setStatus(isReconnect ? 'Reconectando stream...' : 'Cargando stream...');

  if (window.Hls && Hls.isSupported()) {{
    initHlsPlayer();
    return;
  }}

  if (video.canPlayType('application/vnd.apple.mpegurl')) {{
    await initNativePlayer();
    return;
  }}

  setStatus('Tu navegador no soporta HLS. Usa Chrome o Safari.', 'error');
}}

reloadBtn.addEventListener('click', () => {{
  reconnectAttempts = 0;
  initPlayer(false);
}});

fitBtn.addEventListener('click', toggleFitMode);
pipBtn.addEventListener('click', togglePip);

video.addEventListener('playing', () => {{
  reconnectAttempts = 0;
  setStatus('En vivo.', 'ok', true);
}});

video.addEventListener('waiting', () => {{
  if (!video.paused) {{
    setStatus('Buffering...', 'warn');
  }}
}});

video.addEventListener('stalled', () => {{
  if (!video.paused) {{
    scheduleReconnect('por corte');
  }}
}});

video.addEventListener('error', () => {{
  scheduleReconnect('por video');
}});

video.addEventListener('enterpictureinpicture', updatePipButton);
video.addEventListener('leavepictureinpicture', updatePipButton);

document.addEventListener('keydown', (event) => {{
  const tag = document.activeElement && document.activeElement.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA') return;

  if (event.code === 'Space') {{
    event.preventDefault();
    if (video.paused) video.play().catch(() => {{}});
    else video.pause();
  }}

  if (event.key.toLowerCase() === 'm') {{
    video.muted = !video.muted;
  }}

  if (event.key.toLowerCase() === 'f') {{
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {{}});
    else if (video.requestFullscreen) video.requestFullscreen().catch(() => {{}});
  }}

  if (event.key.toLowerCase() === 'r') {{
    reconnectAttempts = 0;
    initPlayer(false);
  }}
}});

if (!document.pictureInPictureEnabled || !video.requestPictureInPicture) {{
  pipBtn.hidden = true;
}}

applyFitMode();
updatePipButton();
initPlayer(false);
</script>
</body>
</html>"""
    return HTMLResponse(html)


if __name__ == "__main__":
    port = int(__import__("os").environ.get("PORT", 8001))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
