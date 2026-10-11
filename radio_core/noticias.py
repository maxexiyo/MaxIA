"""Titulares de TODA Latinoamérica, recientes y de lo que más se está hablando ahora (Google Noticias RSS, sin API key).

  obtener_noticias(max_total, excluir) -> lista de {"titulo","fuente","pais","url","fecha"(datetime|None),"resumen","relevancia"}
  · Solo noticias de las últimas 24 h (si hay pocas, hasta 48 h).
  · Orden por relevancia: posición en el feed del país (lo más destacado hoy) + cuántas fuentes/países repiten el tema + frescura.
  · Máximo 2 por país; no se repiten los ya dados (`excluir`).
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

import aiohttp

logger = logging.getLogger(__name__)

PAISES = {
    "MX": "México", "AR": "Argentina", "CO": "Colombia", "CL": "Chile", "PE": "Perú", "VE": "Venezuela",
    "EC": "Ecuador", "BO": "Bolivia", "UY": "Uruguay", "PY": "Paraguay", "CR": "Costa Rica", "PA": "Panamá",
    "DO": "República Dominicana", "GT": "Guatemala", "CU": "Cuba", "SV": "El Salvador", "HN": "Honduras",
    "NI": "Nicaragua", "PR": "Puerto Rico",
}
EXTRA = [("BBC Mundo", "https://feeds.bbci.co.uk/mundo/rss.xml"),
         ("France 24", "https://www.france24.com/es/america-latina/rss"),
         ("DW Español", "https://rss.dw.com/xml/rss-sp-all")]

# Transmisión compartida por varios servidores: se evitan titulares muy gráficos.
FILTRO = re.compile(r"asesin|masacre|feminicid|violaci[oó]n|decapit|descuartiz|balacera|ejecutad|cad[aá]ver|"
                    r"suicid|abuso sexual|pederast|tortur", re.IGNORECASE)
MAX_POR_PAIS = 2
_STOP = set("sobre entre desde hasta tras para como pero este esta estos estas luego donde cuando cada todo todos "
            "mientras durante según segun hoy ayer ahora tras ante contra tiene tienen será podría podria nuevo nueva".split())


def _limpiar(t: str) -> str:
    t = re.sub(r"<[^>]+>", " ", t or "")
    return re.sub(r"\s+", " ", t).strip()


def parsear_rss(xml_texto: str, pais: str = "", fuente_def: str = "") -> list:
    """RSS → [{titulo, fuente, pais, url, fecha, resumen, pos}] (pos = lugar en el feed, 0 = el más destacado)."""
    try:
        root = ET.fromstring(xml_texto)
    except ET.ParseError:
        return []
    salida = []
    for pos, it in enumerate(root.iter("item")):
        titulo = _limpiar(it.findtext("title"))
        if not titulo:
            continue
        fuente_el = it.find("source")
        fuente = _limpiar(fuente_el.text) if fuente_el is not None and fuente_el.text else fuente_def
        if fuente and titulo.endswith(" - " + fuente):
            titulo = titulo[:-(len(fuente) + 3)].strip()
        elif not fuente and " - " in titulo:
            titulo, fuente = titulo.rsplit(" - ", 1)
        fecha = None
        try:
            fecha = parsedate_to_datetime((it.findtext("pubDate") or "").strip())
            if fecha.tzinfo is None:
                fecha = fecha.replace(tzinfo=timezone.utc)
        except Exception:
            pass
        resumen = _limpiar(it.findtext("description"))
        if len(resumen) < 40 or resumen.lower().startswith(titulo.lower()[:30]):
            resumen = ""
        salida.append({"titulo": titulo, "fuente": fuente or "Google Noticias", "pais": pais,
                       "url": (it.findtext("link") or "").strip(), "fecha": fecha, "resumen": resumen[:300], "pos": pos})
    return salida


async def _bajar(sesion, url: str, pais: str, fuente: str = "") -> list:
    try:
        async with sesion.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
            if r.status != 200:
                return []
            return parsear_rss(await r.text(), pais, fuente)
    except Exception as e:
        logger.warning(f"[radio] noticias {pais or fuente} falló: {e!r}")
        return []


def _tokens(titulo: str) -> set:
    return {w for w in re.findall(r"[a-záéíóúñü0-9]{5,}", titulo.lower()) if w not in _STOP}


def ordenar(items: list, ahora=None, excluir=None) -> list:
    """Filtra (frescura, repetidas, gráficas) y ordena por relevancia. Pura: se puede probar sin red."""
    ahora = ahora or datetime.now(timezone.utc)
    excluir = excluir or set()
    for horas in (24, 48):
        lim = ahora - timedelta(hours=horas)
        validas = [n for n in items if n["fecha"] and n["fecha"] >= lim and n["titulo"] not in excluir
                   and not FILTRO.search(n["titulo"] + " " + n["resumen"])]
        if len(validas) >= 14:
            break
    vistos, unicas = set(), []
    for n in validas:
        firma = re.sub(r"\W+", "", n["titulo"].lower())[:50]
        if firma not in vistos:
            vistos.add(firma)
            unicas.append(n)
    # cuántos titulares comparten palabras clave con éste (el mismo tema en varias fuentes = lo que "se está hablando")
    toks = [_tokens(n["titulo"]) for n in unicas]
    for i, n in enumerate(unicas):
        coincide = sum(1 for j, t in enumerate(toks) if j != i and len(toks[i] & t) >= 2)
        horas_ago = max(0.0, (ahora - n["fecha"]).total_seconds() / 3600)
        n["relevancia"] = round(coincide * 3 + max(0, 10 - n["pos"]) * 0.8 + max(0, 24 - horas_ago) / 6, 2)
    unicas.sort(key=lambda n: n["relevancia"], reverse=True)
    return unicas


def elegir(ordenadas: list, cantidad: int) -> list:
    """Las más relevantes, máximo MAX_POR_PAIS por país, mezclando países."""
    por_pais, elegidas = {}, []
    for n in ordenadas:
        k = n["pais"] or n["fuente"]
        if por_pais.get(k, 0) >= MAX_POR_PAIS:
            continue
        por_pais[k] = por_pais.get(k, 0) + 1
        elegidas.append(n)
        if len(elegidas) >= cantidad:
            break
    return elegidas


async def obtener_noticias(max_total: int = 9, excluir=None, candidatas: bool = False) -> list:
    """Hasta `max_total` titulares recientes y relevantes de países distintos.
    candidatas=True devuelve la lista completa ordenada (para que la IA elija)."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        tareas = [_bajar(s, f"https://news.google.com/rss?hl=es-419&gl={c}&ceid={c}:es-419", p) for c, p in PAISES.items()]
        tareas += [_bajar(s, u, "", f) for f, u in EXTRA]
        listas = await asyncio.gather(*tareas)
    items = [n for l in listas for n in l]
    ordenadas = ordenar(items, excluir=excluir)
    return ordenadas[:60] if candidatas else elegir(ordenadas, max_total)
