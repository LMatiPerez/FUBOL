"""
API + Servidor web para FUBOL TV sin popups
===================================================
Endpoints:
  GET /                    → Página principal con lista de partidos
  GET /api/partidos        → JSON con lista de partidos
  GET /api/stream?url=...  → JSON con URL del stream del partido
  GET /ver?url=...         → Página del player limpio

Correr:
  python app.py
  → http://localhost:8001
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from playwright.async_api import async_playwright
import httpx
import uvicorn

import scraper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Browser persistente (se lanza una sola vez al iniciar) ────────────────────
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


# ─────────────────────────────────────────────────────────────────────────────
# PÁGINAS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    with open("templates/index.html", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.get("/ver", response_class=HTMLResponse)
async def ver_partido(request: Request):
    with open("templates/player.html", encoding="utf-8") as f:
        return HTMLResponse(f.read())


# ─────────────────────────────────────────────────────────────────────────────
# API
# ─────────────────────────────────────────────────────────────────────────────

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
    Todos los pedidos al CDN salen desde la IP del servidor (= IP del token).
    """
    from urllib.parse import urljoin
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://latamvidz1.com/",
        "Origin": "https://latamvidz1.com",
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            resp = await client.get(url, headers=hdrs)

        ct = resp.headers.get("content-type", "application/octet-stream")

        if "mpegurl" in ct or ".m3u8" in url:
            base = url.rsplit("/", 1)[0] + "/"
            lines = []
            for line in resp.text.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    seg = stripped if stripped.startswith("http") else urljoin(base, stripped)
                    line = f"/api/proxy?url={seg}"
                lines.append(line)
            body = "\n".join(lines).encode()
            return StreamingResponse(
                iter([body]),
                media_type="application/vnd.apple.mpegurl",
                headers={"Access-Control-Allow-Origin": "*"},
            )

        # Segmento TS u otro binario — stream directo
        return StreamingResponse(
            resp.aiter_bytes(),
            media_type=ct,
            headers={"Access-Control-Allow-Origin": "*"},
        )
    except Exception as e:
        log.error(f"Proxy error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


@app.get("/player", response_class=HTMLResponse)
async def player_page(stream: str = Query(...), titulo: str = Query("")):
    """Página limpia con solo el video player (HLS.js) y botón de volver."""
    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{titulo or 'En vivo'} — FUBOL</title>
  <script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js"></script>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #000; color: #e2e8f0; font-family: 'Segoe UI', sans-serif; display: flex; flex-direction: column; height: 100vh; }}
    header {{ background: #111; padding: 10px 16px; display: flex; align-items: center; gap: 12px; flex-shrink: 0; }}
    header a {{ color: #4ade80; text-decoration: none; font-size: .9rem; font-weight: 600; }}
    header h2 {{ font-size: .95rem; color: #e2e8f0; flex: 1; }}
    #msg {{ padding: 8px 16px; font-size: .82rem; color: #9ca3af; background: #111; flex-shrink: 0; }}
    #msg.error {{ color: #fca5a5; }}
    video {{ flex: 1; width: 100%; background: #000; display: block; }}
    .spinner {{ display: inline-block; width: 14px; height: 14px; border: 2px solid #4ade80;
      border-top-color: transparent; border-radius: 50%; animation: spin .7s linear infinite;
      vertical-align: middle; margin-right: 6px; }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  </style>
</head>
<body>
<header>
  <a href="/">← Volver</a>
  <h2>{titulo or 'En vivo'}</h2>
</header>
<div id="msg"><span class="spinner"></span>Cargando stream...</div>
<video id="v" controls autoplay playsinline></video>
<script>
const m3u8 = {repr(stream)};
const proxyUrl = '/api/proxy?url=' + encodeURIComponent(m3u8);
const video = document.getElementById('v');
const msg = document.getElementById('msg');
let hls;

if (Hls.isSupported()) {{
  hls = new Hls();
  hls.loadSource(proxyUrl);
  hls.attachMedia(video);
  hls.on(Hls.Events.MANIFEST_PARSED, () => {{ msg.textContent = 'Reproduciendo'; video.play().catch(()=>{{}}); }});
  hls.on(Hls.Events.ERROR, (_, d) => {{
    if (d.fatal) {{ msg.className = 'error'; msg.textContent = 'Error de stream: ' + d.type; }}
  }});
}} else if (video.canPlayType('application/vnd.apple.mpegurl')) {{
  video.src = proxyUrl;
  video.play();
}} else {{
  msg.className = 'error'; msg.textContent = 'Tu navegador no soporta HLS. Usá Chrome.';
}}
</script>
</body>
</html>"""
    return HTMLResponse(html)


if __name__ == "__main__":
    port = int(__import__("os").environ.get("PORT", 8001))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
