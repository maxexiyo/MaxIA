"""Voz de Maxia: Gemini TTS directo (tus GEMINI_API_KEYS, con cadena de modelos de respaldo), TTS por OpenRouter
(OPENROUTER_KEY) y edge-tts (es-MX, gratis). Si prefieres tu TTS de Gemini, pásale tu función al Director (tts=...):
async def mi_tts(texto) -> ruta_de_audio | None."""
import asyncio
import base64
import hashlib
import io
import logging
import os
import re
import sys
import threading
import time
import wave

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

# ── Gemini TTS DIRECTO (con tus GEMINI_API_KEYS, sin pasar por OpenRouter) ─────────────────────────────────
# Usa la misma voz, estilo y cadena de modelos que tu cog TTS (mismas variables de entorno y mismos valores por
# defecto). Si un modelo se queda sin cuota (429), pasa al siguiente; si todos fallan devuelve None y sintetizar()
# sigue con OpenRouter → edge-tts → locales.
# Para forzar otra cosa solo en la radio: RADIO_GEMINI_MODELOS="modelo1,modelo2", RADIO_GEMINI_VOZ, RADIO_GEMINI_ESTILO.
GEMINI_VOZ = os.getenv("RADIO_GEMINI_VOZ") or os.getenv("GEMINI_TTS_VOICE", "Kore")
GEMINI_ESTILO = os.getenv("RADIO_GEMINI_ESTILO") or os.getenv(
    "GEMINI_TTS_STYLE", "estilo mexicano, con entonacion, mete emocion")
GEMINI_TIMEOUT = float(os.getenv("RADIO_GEMINI_TIMEOUT", "60"))
GEMINI_COOLDOWN = float(os.getenv("GEMINI_TTS_COOLDOWN", "30"))
_modelos_env = os.getenv("RADIO_GEMINI_MODELOS") or ",".join([
    os.getenv("GEMINI_TTS_MODEL", "gemini-3.8-flash-lite-tts"),
    os.getenv("GEMINI_TTS_FALLBACKS",
              "gemini-3.8-flash-tts,gemini-3.1-flash-tts-preview,gemini-2.5-flash-preview-tts"),
])
GEMINI_MODELOS: list = []
for _m in _modelos_env.split(","):
    _m = _m.strip()
    if _m and _m not in GEMINI_MODELOS:
        GEMINI_MODELOS.append(_m)

_gem = {"idx": 0, "cool": {}, "racha": {}, "clientes": {}}      # estado por (key, modelo)
_gem_sem = asyncio.Semaphore(2)                                  # máx. 2 peticiones a Gemini al mismo tiempo


def _claves_gemini() -> list:
    """Las mismas keys que usa el cog TTS (GEMINI_API_KEYS de config.py); si no se puede importar, usa el entorno."""
    keys = os.getenv("GEMINI_API_KEYS", "")
    try:
        from config import GEMINI_API_KEYS as _k
        keys = _k
    except Exception:
        pass
    if isinstance(keys, str):
        keys = keys.split(",")
    return [k.strip() for k in (keys or []) if k and k.strip()]


def _es_limite(e: Exception) -> bool:
    if getattr(e, "code", None) == 429 or getattr(e, "status_code", None) == 429:
        return True
    t = f"{type(e).__name__} {e}".lower()
    return "429" in t or "resource_exhausted" in t or "too many" in t or "quota" in t


def _pcm_a_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(pcm)
    return buf.getvalue()


def _gemini_wav_sync(key: str, modelo: str, texto: str) -> bytes:
    """Una petición a Gemini TTS (síncrona, se corre en un hilo). Devuelve un WAV completo."""
    from google import genai
    cliente = _gem["clientes"].get(key)
    if cliente is None:
        try:
            cliente = genai.Client(api_key=key, http_options={"timeout": int(GEMINI_TIMEOUT * 1000)})
        except Exception:
            cliente = genai.Client(api_key=key)
        _gem["clientes"][key] = cliente

    if modelo.startswith("gemini-3.8"):                      # Interactions API (ya devuelve WAV)
        if not hasattr(cliente, "interactions"):
            raise RuntimeError("google-genai muy viejo, actualiza con: pip install -U google-genai")
        contenido = {"type": "text", "text": texto}
        if GEMINI_ESTILO:
            contenido["annotations"] = [{"type": "speech_metadata", "style": GEMINI_ESTILO}]
        it = cliente.interactions.create(
            model=modelo,
            input=[{"type": "user_input", "content": [contenido]}],
            response_format={"type": "audio"},
            generation_config={"speech_config": [{"voice": GEMINI_VOZ}]},
        )
        return base64.b64decode(it.output_audio.data)

    from google.genai import types                           # modelos 3.1 / 2.5: PCM crudo → hay que envolverlo en WAV
    r = cliente.models.generate_content(
        model=modelo,
        contents=f"{GEMINI_ESTILO}: {texto}" if GEMINI_ESTILO else texto,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=GEMINI_VOZ))),
        ),
    )
    return _pcm_a_wav(r.candidates[0].content.parts[0].inline_data.data)


async def sintetizar_gemini(texto: str):
    """Texto → ruta de un .wav generado directo con Gemini TTS (o None si no hay keys / todos los modelos fallan).
    Prueba la cadena de modelos en orden y rota keys; un 429 hace "descansar" esa combinación key+modelo."""
    texto = limpiar_para_voz(texto)
    if len(texto) < 2:
        return None
    llaves = _claves_gemini()
    if not llaves or not GEMINI_MODELOS:
        return None
    h = hashlib.sha1(f"gem|{GEMINI_VOZ}|{GEMINI_ESTILO}|{texto}".encode()).hexdigest()[:16]
    ruta = C.DIR_CACHE / f"voz_g_{h}.wav"
    if ruta.exists() and ruta.stat().st_size > 1000:
        return str(ruta)

    n = len(llaves)
    ahora = time.monotonic()
    orden_keys = [(_gem["idx"] + j) % n for j in range(n)]
    combos = [(i, m) for m in GEMINI_MODELOS for i in orden_keys if _gem["cool"].get((i, m), 0) <= ahora]
    if not combos:
        logger.info("[radio] Gemini TTS: todas las combinaciones key+modelo están descansando")
        return None

    async with _gem_sem:
        for i, m in combos:
            try:
                datos = await asyncio.wait_for(asyncio.to_thread(_gemini_wav_sync, llaves[i], m, texto),
                                               timeout=GEMINI_TIMEOUT)
                if datos and len(datos) > 1000:
                    ruta.write_bytes(datos)
                    _gem["idx"] = i
                    _gem["racha"][(i, m)] = 0
                    logger.info(f"[radio] Gemini TTS OK con {m} (key#{i + 1})")
                    return str(ruta)
                logger.warning(f"[radio] Gemini TTS {m} key#{i + 1} no devolvió audio")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[radio] Gemini TTS {m} key#{i + 1} falló: {type(e).__name__}: {str(e)[:150]}")
                if _es_limite(e):                            # el descanso se duplica con cada 429 seguido (máx. 1 h)
                    racha = _gem["racha"].get((i, m), 0) + 1
                    _gem["racha"][(i, m)] = racha
                    descanso = min(GEMINI_COOLDOWN * (2 ** (racha - 1)), 3600)
                    if any(s in str(e).lower() for s in ("perday", "per day", "daily")):
                        descanso = max(descanso, 3600)       # cuota del día agotada
                    _gem["cool"][(i, m)] = time.monotonic() + descanso
                elif isinstance(e, asyncio.TimeoutError):
                    _gem["cool"][(i, m)] = time.monotonic() + GEMINI_COOLDOWN
    try:
        ruta.unlink(missing_ok=True)
    except Exception:
        pass
    return None


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


async def precalentar_chatterbox() -> None:
    """Ya no hace nada (Chatterbox se quitó). Se deja vacía por si alguna parte del bot todavía la llama."""
    return None


async def sintetizar_local(texto: str):
    """Voces locales gratis, en orden: Piper → Kokoro. None si ninguna está instalada o falla."""
    return await sintetizar_piper(texto) or await sintetizar_kokoro(texto)


async def sintetizar(texto: str, openrouter: bool = True, piper: bool = True, gemini=None, chatterbox: bool = False):
    """Texto → ruta de audio en la caché (o None si falla).
    Orden: Gemini TTS directo → OpenRouter → edge-tts → Piper → Kokoro (los locales al final;
    RADIO_TTS_LOCAL_PRIMERO=1 los pone antes de edge-tts).
    Según RADIO_TTS_MOTOR: auto/gemini prueban Gemini directo y luego OpenRouter; openrouter lo invierte;
    edge se salta la nube. `gemini=None` hereda el valor de `openrouter` (así los llamados viejos siguen igual).
    `chatterbox` se ignora (se deja solo para no romper llamados viejos)."""
    if gemini is None:
        gemini = openrouter
    if C.TTS_MOTOR in ("auto", "gemini", "chatterbox"):               # "chatterbox" = valor viejo de RADIO_TTS_MOTOR, se trata como auto
        nube = [("gemini", gemini), ("openrouter", openrouter)]
    elif C.TTS_MOTOR == "openrouter":
        nube = [("openrouter", openrouter), ("gemini", gemini)]
    else:
        nube = []
    for nombre, activo in nube:
        if not activo:
            continue
        ruta_n = await (sintetizar_gemini(texto) if nombre == "gemini" else sintetizar_openrouter(texto))
        if ruta_n:
            return ruta_n
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