"""
Scraper para pelotalibretv.su
==============================
Usa Playwright para:
  1. Obtener la lista de partidos del home
  2. Entrar al link de cada partido e interceptar la URL del stream (.m3u8 o iframe)
     sin disparar los popups/redirects de la página original.
"""
import asyncio
import re
import logging
from typing import Optional
from playwright.async_api import async_playwright, Page, Request
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE_URL    = "https://pelotalibretv.su"
AGENDA_URL  = "https://pelotalibretv.su/agenda/"

# Headers que simulan un navegador real
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
}

# Dominios de ads/trackers a bloquear
BLOCK_DOMAINS = [
    "doubleclick.net", "googlesyndication.com", "adservice.google",
    "popads.net", "popcash.net", "propellerads.com", "exoclick.com",
    "trafficjunky.com", "juicyads.com", "hilltopads.net", "adcash.com",
    "adskeeper.co.uk", "bidvertiser.com", "sublimemedia.net",
    "goatcounter.com", "hotjar.com", "intercom.io",
]


def debe_bloquear(url: str) -> bool:
    return any(d in url for d in BLOCK_DOMAINS)


async def _bloquear_ads(route, request):
    """Bloquea requests a dominios de publicidad."""
    if debe_bloquear(request.url):
        await route.abort()
    else:
        await route.continue_()


async def crear_pagina(context) -> Page:
    page = await context.new_page()
    await page.route("**/*", _bloquear_ads)
    # Cerrar automáticamente cualquier ventana/popup que se abra
    context.on("page", lambda p: asyncio.ensure_future(p.close()))
    return page


# ─────────────────────────────────────────────────────────────────────────────
# PASO 1: Obtener lista de partidos
# ─────────────────────────────────────────────────────────────────────────────

async def get_partidos_con_browser(browser) -> list[dict]:
    """Scrapea /agenda/ usando el browser compartido (más rápido)."""
    ctx = await browser.new_context(extra_http_headers=HEADERS)
    page = await crear_pagina(ctx)
    try:
        log.info(f"Cargando agenda: {AGENDA_URL}")
        await page.goto(AGENDA_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1000)
        html = await page.content()
        return _parsear_agenda(html)
    except Exception as e:
        log.error(f"Error scrapeando agenda: {e}")
        return []
    finally:
        await ctx.close()


# Mantener compatibilidad con el test directo
async def get_partidos() -> list[dict]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            return await get_partidos_con_browser(browser)
        finally:
            await browser.close()


def _parsear_agenda(html: str) -> list[dict]:
    """Parsea el HTML de /agenda/ y devuelve lista de partidos estructurados."""
    soup = BeautifulSoup(html, "html.parser")
    partidos = []

    for li in soup.select("li[class]"):
        cls = " ".join(li.get("class", []))
        if "subitem" in cls or "menu" in cls.lower():
            continue

        # Link principal del partido (href="#")
        a_principal = li.find("a", href="#")
        if not a_principal:
            continue

        # Título: texto del <a> sin el <span class="t">
        span_hora = a_principal.find("span", class_="t")
        hora = span_hora.get_text(strip=True) if span_hora else ""
        if span_hora:
            span_hora.extract()
        titulo_completo = a_principal.get_text(strip=True)

        # Separar competicion y partido
        if ":" in titulo_completo:
            competicion, partido = titulo_completo.split(":", 1)
        else:
            competicion, partido = "", titulo_completo

        # Clase CSS original (LIB, SUD, CONCACAFCHA, etc.) para el sprite
        torneo_cls = " ".join(li.get("class", []))

        # Opciones de canal (subitems)
        opciones = []
        for sub in li.select("li.subitem1 a"):
            href = sub.get("href", "")
            span_cal = sub.find("span")
            calidad = span_cal.get_text(strip=True).replace("Calidad ", "") if span_cal else ""
            if span_cal:
                span_cal.extract()
            canal = sub.get_text(strip=True)
            opciones.append({"canal": canal, "calidad": calidad, "url": href})

        partidos.append({
            "titulo": titulo_completo,
            "competicion": competicion.strip(),
            "partido": partido.strip(),
            "hora": hora,
            "opciones": opciones,
            "torneo_cls": torneo_cls,
        })

    log.info(f"Encontrados {len(partidos)} partidos en agenda")
    return partidos


# ─────────────────────────────────────────────────────────────────────────────
# PASO 2: Extraer stream de un partido
# ─────────────────────────────────────────────────────────────────────────────

import base64
from urllib.parse import urlparse, parse_qs

M3U8_PATTERN = re.compile(r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)', re.IGNORECASE)


def _decodificar_evento_url(eventos_url: str) -> str | None:
    """
    /eventos/?r=BASE64 → decodifica el base64 para obtener la URL del player.
    Evita una navegación extra con Playwright.
    """
    try:
        parsed = urlparse(eventos_url)
        r = parse_qs(parsed.query).get("r", [None])[0]
        if not r:
            return None
        # Agregar padding si falta
        r += "=" * (-len(r) % 4)
        return base64.b64decode(r).decode("utf-8")
    except Exception:
        return None


async def get_stream_con_browser(browser, partido_url: str) -> dict:
    """Extrae el m3u8 usando el browser compartido (rápido, sin relanzar Playwright)."""
    streams_capturados = []

    # Navegar por /eventos/ con Referer de pelotalibretv — latamvidz1 lo requiere
    ctx = await browser.new_context(extra_http_headers={
        **HEADERS,
        "Referer": "https://pelotalibretv.su/",
    })
    page = await crear_pagina(ctx)

    def on_request(request):
        url = request.url
        if ".m3u8" in url and url not in streams_capturados:
            log.info(f"  m3u8: {url[:80]}")
            streams_capturados.append(url)

    page.on("request", on_request)

    try:
        log.info(f"Cargando: {partido_url}")
        await page.goto(partido_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(8000)

        # También buscar m3u8 en el HTML renderizado
        html = await page.content()
        for m in M3U8_PATTERN.findall(html):
            if m not in streams_capturados:
                streams_capturados.append(m)

        m3u8_real = next((s for s in streams_capturados if ".m3u8" in s), None)

        iframes = await page.evaluate(
            "() => [...document.querySelectorAll('iframe')].map(f => f.src).filter(Boolean)"
        )

        return {
            "m3u8": m3u8_real,
            "iframe": iframes[0] if iframes else None,
        }
    except Exception as e:
        log.error(f"Error: {e}")
        return {"m3u8": None, "iframe": None, "error": str(e)}
    finally:
        await ctx.close()


async def get_stream_url(partido_url: str, timeout: int = 20) -> dict:
    """
    Navega al partido, intercepta requests de red y devuelve:
    {
        "m3u8": "...",          # URL directa del stream HLS si encontrada
        "iframes": [...],       # Lista de iframes con el player
        "streams_capturados": [...],  # Cualquier URL de stream capturada
        "html_snippet": "..."   # Snippet del HTML con el player
    }
    """
    streams_capturados = []
    iframes_encontrados = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(extra_http_headers=HEADERS)
        page = await crear_pagina(ctx)

        # Interceptar requests de red para capturar m3u8
        async def on_request(request: Request):
            url = request.url
            if any(ext in url for ext in [".m3u8", ".mpd", "playlist", "stream", "hls", "live"]):
                if url not in streams_capturados:
                    log.info(f"  Stream capturado via red: {url[:80]}")
                    streams_capturados.append(url)

        page.on("request", on_request)

        try:
            log.info(f"Cargando partido: {partido_url}")
            await page.goto(partido_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            html = await page.content()

            # Buscar m3u8 en el HTML
            m3u8_matches = M3U8_PATTERN.findall(html)
            for m in m3u8_matches:
                if m not in streams_capturados:
                    streams_capturados.append(m)

            # Buscar iframes con el player
            iframes_encontrados = await page.evaluate("""
                () => {
                    const frames = [];
                    document.querySelectorAll('iframe').forEach(f => {
                        if (f.src) frames.push(f.src);
                    });
                    return frames;
                }
            """)

            # Esperar un poco más para que carguen requests asíncronas
            await page.wait_for_timeout(3000)

            # Intentar hacer click en el player si hay un botón play
            try:
                await page.click('[class*="play"], [id*="play"], .jw-icon-display', timeout=3000)
                await page.wait_for_timeout(3000)
            except Exception:
                pass

            # Guardar HTML para debug
            with open("debug_partido.html", "w", encoding="utf-8") as f:
                f.write(html)

            # Extraer snippet del area del player
            snippet = await page.evaluate("""
                () => {
                    const player = document.querySelector(
                        '#player, .player, #video, .video, video, iframe'
                    );
                    return player ? player.outerHTML.substring(0, 500) : '';
                }
            """)

            # Priorizar URLs que son realmente m3u8
            m3u8_real = next(
                (s for s in streams_capturados if ".m3u8" in s),
                streams_capturados[0] if streams_capturados else None
            )

            result = {
                "m3u8": m3u8_real,
                "iframes": iframes_encontrados,
                "streams_capturados": streams_capturados,
                "html_snippet": snippet,
            }

            log.info(f"  Streams encontrados: {len(streams_capturados)}")
            log.info(f"  Iframes encontrados: {len(iframes_encontrados)}")
            return result

        except Exception as e:
            log.error(f"Error extrayendo stream: {e}")
            return {
                "m3u8": None,
                "iframes": iframes_encontrados,
                "streams_capturados": streams_capturados,
                "html_snippet": "",
                "error": str(e),
            }
        finally:
            await browser.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    async def test():
        print("=== TEST: Obteniendo partidos ===")
        partidos = await get_partidos()
        for p in partidos[:5]:
            print(f"  {p['titulo'][:60]:60s}  {p['url']}")

        if partidos:
            print(f"\n=== TEST: Extrayendo stream de: {partidos[0]['url']} ===")
            stream = await get_stream_url(partidos[0]['url'])
            print(f"  m3u8:     {stream['m3u8']}")
            print(f"  iframes:  {stream['iframes'][:3]}")
            print(f"  capturados: {stream['streams_capturados'][:3]}")

    asyncio.run(test())
