"""Búsqueda en internet para la radio, con CACHÉ y conocimiento propio primero.

  buscar_info(consulta, timeout=45, clave=None) -> texto corto con lo encontrado, o None
  exitos_del_pais(pais)                          -> [{"artista","titulo"}] con lo más escuchado en Apple Music (opcional)

Orden que sigue buscar_info:
  1. CACHÉ en disco: si esa consulta (o `clave`) ya se resolvió antes, se devuelve al instante, sin gastar nada.
  2. CONOCIMIENTO PROPIO de Gemini, sin buscadores (llamada barata). Si responde NO_SE, se pasa al siguiente paso.
  3. BÚSQUEDA EN INTERNET (tu BusquedaWebMixin o, si no existe, Gemini + Google Search).
  Lo que se encuentra se guarda. Si no se encontró nada, también se recuerda unos días para no reintentar en
  cada canción. Los errores (timeouts, llaves caídas) NO se guardan.

`clave` sirve para agrupar: si varias canciones son del mismo artista, pasa clave=artista y la consulta sobre
el ARTISTA (no sobre la canción) para que se busque una sola vez por artista.
`ttl_dias` acorta cuánto vale lo guardado SOLO para esa llamada (p. ej. 0.25 para noticias, que caducan rápido).
`conocimiento=False` salta el paso 2 en esa llamada: úsalo para cosas que cambian o que exigen precisión
(noticias de hoy, datos exactos como minutos/segundos), donde la memoria del modelo no sirve.

Variables de entorno (opcionales):
  RADIO_IA_WEB=0               apaga la búsqueda en internet (por defecto está encendida)
  RADIO_IA_WEB_DATOS=0         NO busca datos/anécdotas antes de cada canción (lo usa quien llama a este módulo)
  RADIO_GEMINI_WEB_MODEL       modelo de Gemini para el respaldo directo y el conocimiento propio (por defecto el de ia.py)
  RADIO_IA_CONOCIMIENTO=0      salta el paso 2 (siempre busca en internet si no está en caché)
  RADIO_WEB_CACHE              ruta del archivo de caché (por defecto radio_cache_web.json junto a este archivo)
  RADIO_WEB_CACHE_DIAS=30      cuánto dura un dato encontrado
  RADIO_WEB_CACHE_NEG_DIAS=2   cuánto se recuerda que NO se encontró nada
  RADIO_WEB_CACHE_MAX=1500     máximo de entradas guardadas (se borran las más viejas)
  RADIO_WEB_EXITOS_HORAS=12    cuánto dura la lista de éxitos del país
Para vaciar la caché basta con borrar el archivo radio_cache_web.json.
"""
import asyncio
import json
import logging
import os
import re
import time
import unicodedata

import aiohttp

from .ia import GEMINI_MODEL, GEMINI_URL, _idx, claves_gemini

logger = logging.getLogger(__name__)


def _num(nombre: str, defecto) -> float:
    try:
        return float(os.getenv(nombre, defecto))
    except ValueError:
        return float(defecto)


WEB_ACTIVA = os.getenv("RADIO_IA_WEB", "1") != "0"
WEB_DATOS = os.getenv("RADIO_IA_WEB_DATOS", "1") != "0"
WEB_MODELO = os.getenv("RADIO_GEMINI_WEB_MODEL", GEMINI_MODEL)
CONOCIMIENTO_PRIMERO = os.getenv("RADIO_IA_CONOCIMIENTO", "1") != "0"

CACHE_ARCHIVO = os.getenv("RADIO_WEB_CACHE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "radio_cache_web.json")
CACHE_TTL_S = _num("RADIO_WEB_CACHE_DIAS", 30) * 86400
CACHE_NEG_TTL_S = _num("RADIO_WEB_CACHE_NEG_DIAS", 2) * 86400
CACHE_MAX = int(_num("RADIO_WEB_CACHE_MAX", 1500))
EXITOS_TTL_S = _num("RADIO_WEB_EXITOS_HORAS", 12) * 3600

_mixin = None
_cache = None                 # clave -> {"t": timestamp, "txt": str|None, "via": "conocimiento"|"web"}
_pendientes = {}              # clave -> tarea en curso (si piden lo mismo a la vez, se comparte la búsqueda)
_guardando = False            # hay un guardado en disco en curso
_sucio = False                # llegaron datos nuevos mientras se guardaba: repetir el guardado al terminar
_tareas_guardado = set()


# ───────────────────────── Caché ─────────────────────────

def _norm(texto: str) -> str:
    """Minúsculas, sin acentos ni signos: 'Bad Bunny - Tití Me Preguntó' y 'bad bunny titi me pregunto' son la misma clave."""
    t = unicodedata.normalize("NFKD", (texto or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", t).split())


def _cargar() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(CACHE_ARCHIVO, "r", encoding="utf-8") as f:
                datos = json.load(f)
            _cache = datos if isinstance(datos, dict) else {}
        except Exception:
            _cache = {}
    return _cache


def _escribir(datos: dict):
    tmp = CACHE_ARCHIVO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False)
    os.replace(tmp, CACHE_ARCHIVO)          # reemplazo atómico: nunca queda un archivo a medias


async def _guardar():
    """Guarda en disco SIN hacer esperar a nadie (corre en segundo plano). Si el disco se cuelga, se rinde a los 10 s;
    si ya hay un guardado en curso, solo lo marca para repetirlo al terminar."""
    global _guardando, _sucio
    if _guardando:
        _sucio = True
        return
    _guardando = True
    try:
        while True:
            _sucio = False
            try:
                await asyncio.wait_for(asyncio.to_thread(_escribir, dict(_cache)), timeout=10)
            except Exception as e:
                logger.warning(f"[radio] no pude guardar la caché web (se sigue sin guardar): {e!r}")
            if not _sucio:
                break
    finally:
        _guardando = False


async def _poner(clave: str, entrada: dict):
    c = _cargar()
    entrada["t"] = time.time()
    c[clave] = entrada
    if len(c) > CACHE_MAX:                  # poda: fuera las más viejas (y un 10% extra para no podar cada vez)
        sobran = len(c) - CACHE_MAX + max(1, CACHE_MAX // 10)
        for k, _ in sorted(c.items(), key=lambda kv: kv[1].get("t", 0))[:sobran]:
            c.pop(k, None)
    t = asyncio.ensure_future(_guardar())      # en segundo plano: buscar_info NO espera al disco
    _tareas_guardado.add(t)
    t.add_done_callback(_tareas_guardado.discard)


def _leer(clave: str, ttl_dias: float = None):
    """(encontrado_en_caché, texto). El texto puede ser None si se recuerda que no había nada.
    `ttl_dias` solo puede ACORTAR la vigencia normal (nunca alargarla)."""
    e = _cargar().get(clave)
    if not e:
        return False, None
    ttl = CACHE_TTL_S if e.get("txt") else CACHE_NEG_TTL_S
    if ttl_dias is not None:
        ttl = min(ttl, ttl_dias * 86400)
    if time.time() - e.get("t", 0) < ttl:
        return True, e.get("txt")
    return False, None


# ───────────────────────── Fuentes ─────────────────────────

def _obtener_mixin():
    """Instancia de tu BusquedaWebMixin (donde la tengas), o None si no se encuentra."""
    global _mixin
    if _mixin is not None:
        return _mixin or None
    for ruta in ("ia_core.busqueda_web_mixin", "busqueda_web_mixin", "ia_core.busqueda_web"):
        try:
            modulo = __import__(ruta, fromlist=["BusquedaWebMixin"])
            _mixin = modulo.BusquedaWebMixin()
            return _mixin
        except Exception:
            continue
    logger.info("[radio] no encontré BusquedaWebMixin; la búsqueda usará Gemini directo")
    _mixin = False
    return None


async def _post_gemini(body: dict, timeout: int):
    """Prueba las llaves en rotación. Devuelve (respondio, texto): respondio=False si NINGUNA llave contestó 200
    (error de red/cuota), así se distingue 'no hay nada' de 'falló'."""
    llaves = claves_gemini()
    if not llaves:
        return False, None
    respondio = False
    for i in range(len(llaves)):
        k = (_idx.get("gemini", 0) + i) % len(llaves)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
                async with s.post(GEMINI_URL.format(modelo=WEB_MODELO), json=body,
                                  headers={"x-goog-api-key": llaves[k]}) as r:
                    data = await r.json(content_type=None)
                    if r.status == 200:
                        respondio = True
                        partes = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts")) or []
                        txt = " ".join("".join(p.get("text", "") for p in partes).split())
                        if txt:
                            return True, txt
        except Exception as e:
            logger.debug(f"[radio] gemini#{k + 1}: {e!r}")
    return respondio, None


async def _conocimiento(consulta: str, timeout: int = 20):
    """Paso 2: responde SOLO con lo que Gemini sabe de memoria (sin Google Search). None si no sabe."""
    body = {"contents": [{"role": "user", "parts": [{"text":
            "Responde en español, en máximo 120 palabras, SOLO con datos que sepas con seguridad. "
            "No inventes ni adivines fechas, cifras, nombres o anécdotas. "
            "Si no conoces bien el tema, o es algo muy reciente o poco conocido, responde exactamente: NO_SE. "
            "Consulta: " + consulta}]}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 400}}
    respondio, txt = await _post_gemini(body, timeout)
    if not txt or "NO_SE" in txt.upper():
        return respondio, None
    return respondio, txt


async def _gemini_directo(consulta: str, timeout: int = 30):
    """Paso 3 (respaldo): Gemini con Google Search. Devuelve (respondio, texto)."""
    body = {"contents": [{"role": "user", "parts": [{"text":
            "Busca en internet y responde en español, en máximo 120 palabras, con datos concretos. "
            "Si no encuentras nada confiable, dilo. Consulta: " + consulta}]}],
            "tools": [{"google_search": {}}], "generationConfig": {"temperature": 0.2, "maxOutputTokens": 600}}
    return await _post_gemini(body, timeout)


async def _buscar_web(consulta: str, timeout: int):
    """Paso 3. Devuelve (texto, cachear). cachear=False cuando hubo un error, para no recordar un fallo."""
    try:
        m = _obtener_mixin()
        if m is not None:
            res = await asyncio.wait_for(m.search_duckduckgo(consulta), timeout=timeout)
            if res and res.startswith("Error en búsqueda web"):
                return None, False
            return (res or None), True
        respondio, txt = await asyncio.wait_for(_gemini_directo(consulta), timeout=timeout)
        return txt, respondio
    except Exception as e:
        logger.debug(f"[radio] búsqueda web falló: {e!r}")
        return None, False


async def _resolver(clave: str, consulta: str, timeout: int, conocimiento: bool = True):
    if CONOCIMIENTO_PRIMERO and conocimiento:
        try:
            _, txt = await asyncio.wait_for(_conocimiento(consulta), timeout=min(timeout, 20))
            if txt:
                logger.debug(f"[radio] '{clave}' resuelto con conocimiento propio (sin buscar en internet)")
                await _poner(clave, {"txt": txt, "via": "conocimiento"})
                return txt
        except Exception as e:
            logger.debug(f"[radio] conocimiento propio falló: {e!r}")
    txt, cachear = await _buscar_web(consulta, timeout)
    if cachear:
        await _poner(clave, {"txt": txt, "via": "web"})
    return txt


# ───────────────────────── API pública ─────────────────────────

async def buscar_info(consulta: str, timeout: int = 45, clave: str = None, ttl_dias: float = None,
                      conocimiento: bool = True):
    """Texto con lo que se sabe/encontró sobre `consulta`, o None (apagado, sin resultados o error).
    `clave` (opcional) agrupa consultas parecidas bajo una misma entrada de caché (p. ej. el nombre del artista).
    `ttl_dias` (opcional) acorta la vigencia de la caché para esta llamada. `conocimiento=False` fuerza internet."""
    if not WEB_ACTIVA or not consulta:
        return None
    k = _norm(clave or consulta)
    if not k:
        return None

    hit, txt = _leer(k, ttl_dias)
    if hit:
        logger.debug(f"[radio] caché web: '{k}'")
        return txt

    tarea = _pendientes.get(k)
    if tarea is None:                        # si ya hay una búsqueda igual en curso, se comparte
        tarea = asyncio.ensure_future(_resolver(k, consulta, timeout, conocimiento))
        _pendientes[k] = tarea
        tarea.add_done_callback(lambda _t, _k=k: _pendientes.pop(_k, None))
    try:
        return await asyncio.shield(tarea)
    except Exception as e:
        logger.debug(f"[radio] búsqueda web falló: {e!r}")
        return None


async def exitos_del_pais(pais: str = "mx", cantidad: int = 25) -> list:
    """Lo más escuchado en Apple Music del país (si tu mixin está disponible). [] si no. Se guarda en caché."""
    m = _obtener_mixin()
    if m is None or not WEB_ACTIVA:
        return []
    clave = f"__exitos__{pais}_{cantidad}"
    e = _cargar().get(clave)
    if e and e.get("lista") and time.time() - e.get("t", 0) < EXITOS_TTL_S:
        return e["lista"]
    try:
        lista = [{"artista": x["artista"], "titulo": x["titulo"]} for x in await m.obtener_exitos(pais, cantidad)]
    except Exception:
        return e["lista"] if e and e.get("lista") else []     # si falla, mejor la lista vieja que nada
    if lista:
        await _poner(clave, {"lista": lista, "via": "web"})
    return lista