"""Saltar intros largas habladas (skits) de canciones, p. ej. «Giorgio by Moroder» de Daft Punk (la canción empieza ~1:56).

  await inicio_cancion(artista, titulo, duracion_s, url, ia=..., buscar_web=...) -> segundos desde donde empezar (0.0 = normal)

Orden de decisión:
  1. Tabla manual  radio_data/saltos_intro.json   {"daft punk|giorgio by moroder": 116, ...}  (la que tú quieras editar)
  2. Automático (por defecto; RADIO_SALTO_AUTO=0 lo apaga): la IA con búsqueda en internet dice si hay intro hablada y en qué
     segundo empieza la música (solo se usa si dice estar segura; se descarta si cae fuera de límites). No descarga nada.
     Opcional aparte, RADIO_SALTO_YTDLP=1 (necesita yt-dlp, ffmpeg y numpy):
       a) la IA confirma que la canción tiene una intro larga hablada/narrada (si no, NO se salta nada: así no se
          recortan intros musicales normales),
       b) se baja el inicio del audio (primeros 4 min) y se busca el punto donde entran el bajo y la percusión
          y se quedan (la voz sola tiene muy poco bajo).
     Lo detectado se guarda en el mismo json con el valor para que no se repita el análisis.
Lavalink solo sabe empezar en un segundo dado (start=ms); el análisis lo hace este módulo.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import unicodedata
from pathlib import Path

from . import config as C

logger = logging.getLogger(__name__)

AUTO = os.getenv("RADIO_SALTO_AUTO", "1") != "0"          # análisis automático con IA + internet (sin descargar nada)
YTDLP = os.getenv("RADIO_SALTO_YTDLP", "0") == "1"       # extra opcional: bajar el audio con yt-dlp y medirlo
MIN_SALTO_S = 20.0           # intros más cortas que esto no se saltan
MAX_FRACCION = 0.45          # nunca se salta más del 45 % de la canción
SALTOS_BASE = {"daft punk|giorgio by moroder": 116.0}
_cache = None


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", "", t).strip()


def _clave(artista: str, titulo: str) -> str:
    return f"{_norm(artista)}|{_norm(titulo)}"


def _ruta():
    return Path(C.DIR_RADIO) / "saltos_intro.json"


def _tabla() -> dict:
    global _cache
    if _cache is None:
        _cache = dict(SALTOS_BASE)
        try:
            for k, v in json.loads(_ruta().read_text(encoding="utf-8")).items():
                _cache[_norm(k.split("|")[0]) + "|" + _norm(k.split("|")[-1])] = float(v)
        except Exception:
            pass
    return _cache


def _guardar(clave: str, seg: float) -> None:
    _tabla()[clave] = seg
    try:
        extra = {}
        try:
            extra = json.loads(_ruta().read_text(encoding="utf-8"))
        except Exception:
            pass
        extra[clave] = seg
        _ruta().write_text(json.dumps(extra, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        logger.debug(f"[radio] no se pudo guardar saltos_intro.json: {e!r}")


def detectar_inicio(muestras, sr: int):
    """Segundo en que entran el bajo y la percusión y se quedan, o None. `muestras` = np.ndarray mono float.
    Pura (se prueba sin red). Voz sola ≈ casi nada de energía bajo 150 Hz; la canción con ritmo, mucha."""
    import numpy as np
    paso = int(sr * 0.5)
    n = len(muestras) // paso
    if n < 60:
        return None
    graves = np.empty(n)
    total = np.empty(n)
    for i in range(n):
        seg = muestras[i * paso:(i + 1) * paso] * np.hanning(paso)
        esp = np.abs(np.fft.rfft(seg)) ** 2
        f = np.fft.rfftfreq(paso, 1 / sr)
        graves[i] = esp[(f >= 35) & (f <= 150)].sum()
        total[i] = esp.sum() + 1e-9
    razon = graves / total                       # proporción de graves en cada medio segundo
    umbral_alto = max(np.percentile(razon, 80) * 0.5, 1e-4)
    # ventana de 10 s (20 pasos): la canción empieza donde TODA la ventana siguiente tiene graves y la anterior (≥20 s) casi no
    v = 20
    for i in range(int(MIN_SALTO_S * 2), n - v):
        antes = razon[max(0, i - 40):i]
        despues = razon[i:i + v]
        if despues.min() >= umbral_alto * 0.6 and np.median(antes) < umbral_alto * 0.25 and np.median(despues) > np.median(antes) * 6:
            # afinar: primer paso donde ya hay graves sostenidos
            j = i
            while j > 0 and razon[j - 1] >= umbral_alto * 0.6:
                j -= 1
            return j * 0.5
    return None


async def _audio_inicio(url: str, segundos: int = 240):
    """Baja y decodifica los primeros `segundos` del audio. (muestras, sr) o None."""
    if not shutil.which("yt-dlp") or not shutil.which(C.FFMPEG):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        base = str(Path(tmp) / "a.%(ext)s")
        p = await asyncio.create_subprocess_exec(
            "yt-dlp", "-q", "--no-playlist", "-f", "bestaudio", "--download-sections", f"*0-{segundos}",
            "-o", base, url, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(p.wait(), 60)
        except asyncio.TimeoutError:
            p.kill()
            return None
        archivos = list(Path(tmp).glob("a.*"))
        if not archivos:
            return None
        f = await asyncio.create_subprocess_exec(
            C.FFMPEG, "-v", "error", "-i", str(archivos[0]), "-t", str(segundos), "-ac", "1", "-ar", "16000",
            "-f", "s16le", "-", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        crudo, _ = await asyncio.wait_for(f.communicate(), 60)
    import numpy as np
    return np.frombuffer(crudo, dtype=np.int16).astype("float32") / 32768.0, 16000


async def _ia_confirma(artista, titulo, ia, buscar_web) -> bool:
    info = ""
    if buscar_web:
        try:
            info = await buscar_web(f"{artista} {titulo} canción introducción hablada narración intro larga") or ""
        except Exception:
            info = ""
    try:
        r = await ia(f"¿La canción «{titulo}» de {artista} (versión de estudio/álbum) tiene una introducción LARGA (más de 20 s) "
                     f"hablada, narrada o un skit antes de que empiece la música en sí? Info de internet: {info}\n"
                     'Responde SOLO JSON: {"intro_hablada":true|false}', "Respondes solo JSON.", json_mode=True, max_tokens=100)
        return bool(json.loads(re.search(r"\{.*\}", r, re.S).group(0)).get("intro_hablada"))
    except Exception:
        return False


async def _ia_tiempo(artista, titulo, duracion_s, ia, buscar_web):
    """Pregunta a la IA (con búsqueda en internet) si la canción tiene intro hablada larga y en qué segundo empieza la música.
    Solo devuelve un valor si la IA dice estar segura; si no, None."""
    info = ""
    if buscar_web:
        try:
            info = await buscar_web(f"{artista} {titulo} en qué minuto empieza la canción después de la introducción hablada") or ""
        except Exception:
            info = ""
    try:
        r = await ia(
            f"Canción «{titulo}» de {artista}, versión de estudio/álbum, duración {int(duracion_s)} s. ¿Tiene una introducción LARGA "
            f"(más de 20 s) hablada, narrada o un skit antes de que empiece la música en sí? Si es así, ¿en qué segundo empieza "
            f"la canción de verdad? Solo responde con número si lo sabes con certeza (por datos de internet o porque es muy conocido); "
            f"si dudas, pon seguro=false. Info de internet: {info}\n"
            'Responde SOLO JSON: {"intro_hablada":true|false,"empieza_en_seg":numero|null,"seguro":true|false}',
            "Respondes solo JSON.", json_mode=True, max_tokens=150)
        d = json.loads(re.search(r"\{.*\}", r, re.S).group(0))
        if d.get("intro_hablada") and d.get("seguro") and d.get("empieza_en_seg") is not None:
            return float(d["empieza_en_seg"])
    except Exception:
        pass
    return None


async def inicio_cancion(artista: str, titulo: str, duracion_s: float, url: str = "", ia=None, buscar_web=None) -> float:
    clave = _clave(artista, titulo)
    tabla = _tabla()
    if clave in tabla:
        s = tabla[clave]
    elif AUTO and ia is not None:
        s = 0.0
        try:
            if YTDLP and url and await _ia_confirma(artista, titulo, ia, buscar_web):
                audio = await _audio_inicio(url)
                if audio is not None:
                    s = detectar_inicio(*audio) or 0.0
            if not s:
                s = await _ia_tiempo(artista, titulo, duracion_s, ia, buscar_web) or 0.0
            if s:
                logger.info(f"[radio] intro hablada de «{titulo}»: la música empieza en {s:.0f} s")
        except Exception as e:
            logger.debug(f"[radio] detección de intro falló: {e!r}")
        _guardar(clave, s)                      # 0.0 = "ya analizada, no saltar"
    else:
        return 0.0
    if s < MIN_SALTO_S or (duracion_s and s > duracion_s * MAX_FRACCION):
        return 0.0
    return float(s)
