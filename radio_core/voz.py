"""Voz de Maxia: edge-tts (es-MX, gratis) y TTS por OpenRouter (modelos de IA que generan voz, con tus keys de
OPENROUTER_KEY). Si prefieres tu TTS de Gemini, pásale tu función al Director (tts=...):
async def mi_tts(texto) -> ruta_de_audio | None."""
import asyncio
import hashlib
import logging
import os
import re
import sys
import threading

import aiohttp

from . import config as C
from .ia import _idx, claves_openrouter

logger = logging.getLogger(__name__)

VOZ = os.getenv("RADIO_VOZ", "es-MX-DaliaNeural")
VELOCIDAD = os.getenv("RADIO_VOZ_VELOCIDAD", "+4%")
# Orden de los respaldos cuando Gemini no está disponible. Por defecto (0): voces en la nube primero (OpenRouter/Gemini →
# edge-tts, que suenan más naturales) y Piper/Kokoro solo como último recurso (sin internet o todo caído).
# RADIO_TTS_LOCAL_PRIMERO=1 restaura el orden anterior (Piper → Kokoro antes que edge-tts).
LOCAL_PRIMERO = os.getenv("RADIO_TTS_LOCAL_PRIMERO", "0") == "1"

# ── TTS por OpenRouter (POST /api/v1/audio/speech, compatible con OpenAI) ──────────────────────────────────
# Respaldo de Gemini: el cog toma de tu cog TTS el modelo (GEMINI_TTS_MODEL), la voz (GEMINI_TTS_VOICE) y el estilo
# (GEMINI_STYLE) y los manda a OpenRouter (google/<modelo>), así Maxia suena igual en ambos motores.
# Estas variables solo sirven para FORZAR otra cosa; vacías = se copia lo de tu cog TTS.
OR_TTS_URL = "https://openrouter.ai/api/v1/audio/speech"
OR_TTS_MODELO = os.getenv("RADIO_OR_TTS_MODELO", "")             # slug completo, p. ej. google/gemini-3.8-flash-lite-tts
OR_TTS_VOZ = os.getenv("RADIO_OR_TTS_VOZ", "")
OR_TTS_INSTRUCCIONES = os.getenv("RADIO_OR_TTS_INSTRUCCIONES", "")
# Si no se encuentra nada en tu cog TTS:
MODELO_OR_DEFECTO = "google/gemini-3.8-flash-lite-tts"
VOZ_GEMINI_DEFECTO = "Kore"
ESTILO_DEFECTO = "estilo mexicano, con entonacion, mete emocion"

# Si la voz pronuncia mal algo, corrígelo aquí: {"como se escribe": "como quieres que suene"}
PRONUNCIACIONES: dict = {}

_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]+", flags=re.UNICODE)


def limpiar_para_voz(texto: str) -> str:
    t = _EMOJI.sub("", texto or "")
    t = re.sub(r"[*_`#>~|]+", "", t)
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"\([^)]{0,80}\)", "", t) if re.search(r"\((risas|pausa|música|suspira)", t, re.I) else t
    for a, b in PRONUNCIACIONES.items():
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()[:5000]


# ── Piper (local, gratis, sin keys ni cupo). Descarga la voz una vez:
#   python -m piper.download_voices es_MX-claude-high --data-dir radio_data/piper
# y listo: si el archivo .onnx existe, se usa como respaldo antes de edge-tts. ──
PIPER_MODELO = os.getenv("RADIO_PIPER_MODELO", str(C.DIR_RADIO / "piper" / "es_MX-claude-high.onnx"))
PIPER_VELOCIDAD = os.getenv("RADIO_PIPER_VELOCIDAD", "1.0")      # length_scale: menor = más rápido
PIPER_VARIACION = os.getenv("RADIO_PIPER_VARIACION", "2.0")      # noise_scale: más alto = más expresivo/menos plano
PIPER_VARIACION_RITMO = os.getenv("RADIO_PIPER_VARIACION_RITMO", "1.0")   # noise_w_scale: variación del ritmo
PIPER_PAUSA = os.getenv("RADIO_PIPER_PAUSA", "0.25")             # seg. de pausa entre frases


async def sintetizar_piper(texto: str):
    """Texto → ruta de un .wav hecho con Piper en tu equipo, o None si no está instalado/falla."""
    if not os.path.exists(PIPER_MODELO):
        return None
    texto = limpiar_para_voz(texto)
    if len(texto) < 2:
        return None
    h = hashlib.sha1(f"piper|{PIPER_MODELO}|{PIPER_VELOCIDAD}|{texto}".encode()).hexdigest()[:16]
    ruta = C.DIR_CACHE / f"voz_p_{h}.wav"
    if ruta.exists() and ruta.stat().st_size > 1000:
        return str(ruta)
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "piper", "-m", PIPER_MODELO, "-f", str(ruta), "--length-scale", PIPER_VELOCIDAD,
            "--noise-scale", PIPER_VARIACION, "--noise-w-scale", PIPER_VARIACION_RITMO, "--sentence-silence", PIPER_PAUSA,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(proc.communicate(texto.encode("utf-8")), timeout=90)
        if proc.returncode == 0 and ruta.exists() and ruta.stat().st_size > 1000:
            return str(ruta)
        logger.warning(f"[radio] Piper falló: {err.decode(errors='ignore')[-200:]}")
    except Exception as e:
        logger.warning(f"[radio] Piper falló: {e!r}")
    try:
        ruta.unlink(missing_ok=True)
    except Exception:
        pass
    return None


# ── Kokoro (local, gratis, más expresivo que Piper). Archivos en radio_data/kokoro/ (ver README/instrucciones):
#   kokoro-v1.0.int8.onnx  y  voices-v1.0.bin   · voces en español: ef_dora (mujer), em_alex, em_santa (hombres)
KOKORO_MODELO = os.getenv("RADIO_KOKORO_MODELO", str(C.DIR_RADIO / "kokoro" / "kokoro-v1.0.int8.onnx"))
KOKORO_VOCES = os.getenv("RADIO_KOKORO_VOCES", str(C.DIR_RADIO / "kokoro" / "voices-v1.0.bin"))
KOKORO_VOZ = os.getenv("RADIO_KOKORO_VOZ", "ef_dora")
KOKORO_VELOCIDAD = float(os.getenv("RADIO_KOKORO_VELOCIDAD", "1.0"))
_kokoro = None
_kokoro_lock = threading.Lock()


def _kokoro_sintetizar_sync(texto: str, ruta: str) -> bool:
    global _kokoro
    import soundfile as sf
    with _kokoro_lock:                       # el modelo se carga una vez y se usa de uno en uno
        if _kokoro is None:
            from kokoro_onnx import Kokoro
            _kokoro = Kokoro(KOKORO_MODELO, KOKORO_VOCES)
        muestras, sr = _kokoro.create(texto, voice=KOKORO_VOZ, speed=KOKORO_VELOCIDAD, lang="es",
                                      sentence_pause=0.3, clause_pause=0.12)
    sf.write(ruta, muestras, sr)
    return True


async def sintetizar_kokoro(texto: str):
    """Texto → ruta de un .wav hecho con Kokoro en tu equipo, o None si no está instalado/falla."""
    if not (os.path.exists(KOKORO_MODELO) and os.path.exists(KOKORO_VOCES)):
        return None
    texto = limpiar_para_voz(texto)
    if len(texto) < 2:
        return None
    h = hashlib.sha1(f"kokoro|{KOKORO_VOZ}|{KOKORO_VELOCIDAD}|{texto}".encode()).hexdigest()[:16]
    ruta = C.DIR_CACHE / f"voz_k_{h}.wav"
    if ruta.exists() and ruta.stat().st_size > 1000:
        return str(ruta)
    try:
        await asyncio.wait_for(asyncio.to_thread(_kokoro_sintetizar_sync, texto, str(ruta)), timeout=120)
        if ruta.exists() and ruta.stat().st_size > 1000:
            return str(ruta)
    except Exception as e:
        logger.warning(f"[radio] Kokoro falló: {e!r}")
    try:
        ruta.unlink(missing_ok=True)
    except Exception:
        pass
    return None


async def sintetizar_local(texto: str):
    """Voces locales gratis, en orden: Piper → Kokoro. None si ninguna está instalada o falla."""
    return await sintetizar_piper(texto) or await sintetizar_kokoro(texto)


async def sintetizar(texto: str, openrouter: bool = True, piper: bool = True):
    """Texto → ruta de audio en la caché (o None si falla). Orden: OpenRouter (Gemini TTS) → edge-tts → Piper → Kokoro (los locales al final; RADIO_TTS_LOCAL_PRIMERO=1 los pone antes de edge-tts).
    Según RADIO_TTS_MOTOR: auto/openrouter prueban primero OpenRouter (si hay keys); edge/gemini lo saltan."""
    if openrouter and C.TTS_MOTOR in ("auto", "openrouter"):      # Gemini (vía OpenRouter) primero
        ruta_or = await sintetizar_openrouter(texto)
        if ruta_or:
            return ruta_or
    if piper and LOCAL_PRIMERO:                       # (piper=False = "ya probé las voces locales")
        ruta_l = await sintetizar_local(texto)        # Piper → Kokoro
        if ruta_l:
            return ruta_l
    ruta_e = await _sintetizar_edge(texto)
    if ruta_e:
        return ruta_e
    if piper and not LOCAL_PRIMERO:                   # último recurso: voces locales
        return await sintetizar_local(texto)
    return None


async def _sintetizar_edge(texto: str):
    """edge-tts (voz neural de Microsoft, gratis) → ruta del mp3 en la caché, o None."""
    texto = limpiar_para_voz(texto)
    if len(texto) < 2:
        return None
    h = hashlib.sha1(f"{VOZ}|{VELOCIDAD}|{texto}".encode()).hexdigest()[:16]
    ruta = C.DIR_CACHE / f"voz_{h}.mp3"
    if ruta.exists() and ruta.stat().st_size > 1000:
        return str(ruta)
    try:
        import edge_tts
        await asyncio.wait_for(edge_tts.Communicate(texto, VOZ, rate=VELOCIDAD).save(str(ruta)), timeout=40)
        if ruta.exists() and ruta.stat().st_size > 1000:
            return str(ruta)
    except Exception as e:
        logger.warning(f"[radio] TTS falló: {e!r}")
    try:
        ruta.unlink(missing_ok=True)
    except Exception:
        pass
    return None


async def sintetizar_openrouter(texto: str, voz: str = "", estilo: str = "", modelo: str = ""):
    """Texto → ruta de un mp3 generado por un modelo TTS de OpenRouter (o None si no hay keys / todas fallan).
    Rota entre las keys de OPENROUTER_KEY cuando una da error o se queda sin cupo/saldo."""
    texto = limpiar_para_voz(texto)
    if len(texto) < 2:
        return None
    voz = OR_TTS_VOZ or voz or VOZ_GEMINI_DEFECTO
    estilo = OR_TTS_INSTRUCCIONES or estilo or ESTILO_DEFECTO
    modelo = OR_TTS_MODELO or modelo or MODELO_OR_DEFECTO
    llaves = claves_openrouter()
    if not llaves:
        return None
    h = hashlib.sha1(f"or|{modelo}|{voz}|{estilo}|{texto}".encode()).hexdigest()[:16]
    ruta = C.DIR_CACHE / f"voz_o_{h}.mp3"
    if ruta.exists() and ruta.stat().st_size > 1000:
        return str(ruta)
    body = {"model": modelo, "input": texto, "voice": voz, "response_format": "mp3"}
    if estilo:
        body["instructions"] = estilo          # OpenRouter lo manda a Gemini como speech_metadata.style (igual que tu cog)
    for i in range(len(llaves)):
        k = (_idx["openrouter_tts"] + i) % len(llaves)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
                async with s.post(OR_TTS_URL, json=body, headers={"Authorization": f"Bearer {llaves[k]}",
                                                                  "X-Title": "Radio Maxia"}) as r:
                    if r.status == 200 and "json" not in (r.headers.get("Content-Type") or ""):
                        datos = await r.read()
                        if len(datos) > 1000:
                            ruta.write_bytes(datos)
                            _idx["openrouter_tts"] = k
                            return str(ruta)
                    else:
                        logger.warning(f"[radio] OpenRouter TTS key#{k + 1} → {r.status}: {(await r.text())[:150]}")
        except Exception as e:
            logger.warning(f"[radio] OpenRouter TTS key#{k + 1} falló: {e!r}")
    return None