"""
Diagnóstico de búsqueda. Ponlo en la carpeta del bot (donde están main.py y ia_core/) y corre:
    python probar_busqueda.py
Prueba cada motor por separado y dice cuál funciona desde TU teléfono/IP.
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.getcwd())
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import aiohttp
from ia_core.busqueda_web_mixin import BusquedaWebMixin, FEEDS_EXTRA


class Prueba(BusquedaWebMixin):
    pass


def linea(ok, nombre, detalle, seg):
    print(f"{'✅' if ok else '❌'} {nombre:<22} {seg:>5.1f}s  {detalle}")


async def medir(nombre, coro, mostrar=lambda r: str(r)[:90]):
    t = time.monotonic()
    try:
        r = await coro
        linea(bool(r), nombre, mostrar(r) if r else "sin resultado", time.monotonic() - t)
        return r
    except Exception as e:
        linea(False, nombre, f"excepción: {e!r}"[:90], time.monotonic() - t)


async def main():
    p = Prueba()
    print(f"\nSEARXNG_URL = {os.getenv('SEARXNG_URL') or '(no definida)'}")
    print("\n── 0) Motor propio ──")
    async with aiohttp.ClientSession() as s:
        await medir("SearXNG", p._candidatos_searxng(s, "quién es Lionel Messi"),
                    lambda r: f"{len(r)} resultados: " + " | ".join(x["titulo"][:30] for x in r[:3]))
        await medir("Wikipedia", p._candidatos_wikipedia(s, "Lionel Messi"),
                    lambda r: f"{len(r)} resultados: " + " | ".join(x["titulo"][:30] for x in r[:3]))
    await medir("Motor propio (todo)", p._buscar_propio("quién es Lionel Messi"), lambda r: str(r)[:160])

    print("\n── 1) Búsqueda web: cada proveedor por separado ──")
    await medir("Gemini+Google Search", p._buscar_gemini("últimas noticias de México"))
    await medir("Google News RSS", p._buscar_gnews("selección mexicana"))
    await medir("Google CSE (opcional)", p._buscar_google_cse("hola"))
    async with aiohttp.ClientSession() as s:
        for nombre, metodo, url, parser, _, extra in p._motores_scrapers("que significa albur"):
            t = time.monotonic()
            res, motivo = await p._intentar_busqueda(s, metodo, url, "que significa albur", parser, extra)
            linea(bool(res), f"scraper {nombre}", (res or motivo)[:90], time.monotonic() - t)
            await asyncio.sleep(1.6)

    print("\n── 2) Noticias (para el noticiero) ──")
    async with aiohttp.ClientSession() as s:
        for nombre, url in FEEDS_EXTRA:
            await medir(f"feed {nombre}", p._bajar_feed(s, url, nombre), lambda r: f"{len(r)} noticias")
        await medir("Google News MX", p._bajar_feed(
            s, "https://news.google.com/rss?hl=es-419&gl=MX&ceid=MX:es-419", "", "MX"), lambda r: f"{len(r)} noticias")
    noticias = await medir("noticiero Latam (3)", p.obtener_noticias_latam(3),
                           lambda r: " | ".join(f"{n['titulo'][:40]} ({n['fuente']})" for n in r))

    print("\n── 3) Éxitos musicales (para programas) ──")
    await medir("Apple Music MX top 5", p.obtener_exitos("mx", 5),
                lambda r: " | ".join(f"{x['titulo']} - {x['artista']}" for x in r))

    print("\n── 4) Lo que verá la IA con search_duckduckgo ──")
    await medir("search_duckduckgo", p.search_duckduckgo("quién es el presidente de México"), lambda r: str(r)[:160])
    print()


asyncio.run(main())