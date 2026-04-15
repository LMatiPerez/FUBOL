"""
API + Servidor web para PelotaLibre TV sin popups
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
from fastapi.staticfiles import StaticFiles
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

app = FastAPI(title="PelotaLibre - Sin Popups", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


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
    Proxy del stream m3u8 para evitar problemas de IP/CORS.
    El browser pide los segmentos a nuestro servidor, que los busca desde la IP del servidor.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://pelotalibretv.su/",
        "Origin": "https://pelotalibretv.su",
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(url, headers=headers)

        content_type = resp.headers.get("content-type", "application/octet-stream")

        # Si es m3u8, reescribir las URLs de segmentos para que también pasen por el proxy
        if "mpegurl" in content_type or url.endswith(".m3u8"):
            from urllib.parse import urljoin
            base = url.rsplit("/", 1)[0] + "/"
            lines = []
            for line in resp.text.splitlines():
                if line and not line.startswith("#"):
                    seg_url = line if line.startswith("http") else urljoin(base, line)
                    line = f"/api/proxy?url={httpx.URL(seg_url)}"
                lines.append(line)
            body = "\n".join(lines).encode()
            return StreamingResponse(
                iter([body]),
                media_type="application/vnd.apple.mpegurl",
                headers={"Access-Control-Allow-Origin": "*"},
            )

        return StreamingResponse(
            resp.aiter_bytes(),
            media_type=content_type,
            headers={"Access-Control-Allow-Origin": "*"},
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


if __name__ == "__main__":
    port = int(__import__("os").environ.get("PORT", 8001))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
