import discord
from discord.ext import commands
from database import db
from config import GEMINI_API_KEYS
import logging
import edge_tts
import asyncio
import base64
import io
import os
import re
import sys
import threading
import time
import wave
from array import array
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)

TTS_FOLDER = Path(__file__).parent.parent / 'tts_audios'
TTS_FOLDER.mkdir(exist_ok=True)

# ---------------- Configuración ----------------
MAX_CHARS = 750            # máximo de caracteres que lee por mensaje
MAX_QUEUE = 30             # si hay más mensajes en cola, los nuevos se ignoran (anti-spam)
EDGE_RATE = os.getenv('TTS_RATE', '+10%')   # velocidad de edge-tts (ej: '+0%', '+15%', '-10%')
EDGE_TIMEOUT = float(os.getenv('TTS_EDGE_TIMEOUT', '8'))   # segundos máximos para que edge-tts genere el audio

# Motor con IA (Gemini TTS). Usa las mismas GEMINI_API_KEYS de config.py. Necesita: pip install -U google-genai


def _load_gemini_keys():
    """Toma las keys de config.py (GEMINI_API_KEYS), igual que el módulo IA."""
    keys = GEMINI_API_KEYS
    if isinstance(keys, str):
        keys = keys.split(',')
    return [k.strip() for k in (keys or []) if k and k.strip()]


GEMINI_KEYS = _load_gemini_keys()
# gemini-3.8-flash-lite-tts = rápido (recomendado). gemini-3.8-flash-tts = más expresivo pero más lento.
# Requiere: pip install -U google-genai (>= 2.25.0)
GEMINI_TTS_MODEL = os.getenv('GEMINI_TTS_MODEL', 'gemini-3.8-flash-lite-tts')
GEMINI_TTS_VOICE = os.getenv('GEMINI_TTS_VOICE', 'Kore')
# Motor por defecto: gemini si hay API key, si no edge
DEFAULT_ENGINE = 'gemini' if GEMINI_KEYS else 'edge'
# Estilo opcional corto (ej: 'casual, friendly'). Vacío = más rápido y es lo que recomienda Google.
GEMINI_STYLE = os.getenv('GEMINI_TTS_STYLE', 'estilo mexicano, con entonacion, mete emocion')
CHUNK_CHARS = 160          # (solo sin streaming) mensajes más largos se parten en pedazos

# Streaming: el audio empieza a sonar mientras Gemini lo genera (solo modelos gemini-3.8-*).
# Para apagarlo: export GEMINI_TTS_STREAM=0
GEMINI_STREAM = os.getenv('GEMINI_TTS_STREAM', '1') != '0'
USE_STREAM = GEMINI_STREAM and GEMINI_TTS_MODEL.startswith('gemini-3.8')
PREBUFFER_SECS = 0.35      # cuánto audio junta antes de empezar a sonar (evita cortes al inicio)

# Anti "too many requests" (429)
GEMINI_CONCURRENCY = int(os.getenv('GEMINI_TTS_CONCURRENCY', '2'))   # streams a Gemini al mismo tiempo
KEY_COOLDOWN = float(os.getenv('GEMINI_TTS_COOLDOWN', '30'))         # segundos que "descansa" una key tras un 429
# Cupo LOCAL por key: el bot se frena SOLO antes de que Google responda 429 (los mensajes que no caben
# se leen con edge-tts). Ajústalo a lo que diga tu página de límites en AI Studio.
# El plan gratis de los modelos TTS es MUY bajo (en el modelo 2.5 TTS eran 3 por minuto y 15 por día).
GEMINI_RPM_PER_KEY = int(os.getenv('GEMINI_TTS_RPM', '3'))        # peticiones por minuto por key (0 = sin límite)
GEMINI_DAILY_PER_KEY = int(os.getenv('GEMINI_TTS_DAILY', '0'))    # peticiones por día por key (0 = sin límite)
# Si Gemini no manda NADA de audio en este tiempo, se usa edge-tts para ese mensaje (en vez de quedarse mudo)
GEMINI_FIRST_AUDIO_TIMEOUT = float(os.getenv('GEMINI_TTS_FIRST_AUDIO_TIMEOUT', '10'))
# Si todos los lugares de Gemini están ocupados, espera máximo esto y se va directo a edge-tts
GEMINI_SLOT_WAIT = float(os.getenv('GEMINI_TTS_SLOT_WAIT', '3'))
GEMINI_HTTP_TIMEOUT_MS = 40000   # corta peticiones colgadas a nivel de red

# ✅ FIX: antes SYNTH_TIMEOUT era 5s, MENOR que el tiempo que Gemini puede tardar antes de rendirse (10s).
# El worker cancelaba la tarea a los 5s y edge-tts NUNCA llegaba a ejecutarse (el bot se quedaba mudo).
# Ahora el timeout total siempre es mayor que: espera de lugar + primer audio de Gemini + edge-tts.
SYNTH_TIMEOUT = float(os.getenv(
    'TTS_SYNTH_TIMEOUT', str(GEMINI_SLOT_WAIT + GEMINI_FIRST_AUDIO_TIMEOUT + EDGE_TIMEOUT + 2)
))

# Formato del audio que devuelve Gemini en streaming: PCM 16-bit, 24 kHz, mono
SRC_BYTES_PER_SEC = 24000 * 2
PREBUFFER_BYTES = int(SRC_BYTES_PER_SEC * PREBUFFER_SECS)
FRAME_IN = 960             # 20 ms de audio de Gemini (24kHz mono 16-bit = 480 muestras)
FRAME_OUT = 3840           # 20 ms de audio para Discord (48kHz estéreo 16-bit)

# ---------------- Limpieza de texto ----------------
CODE_BLOCK_RE = re.compile(r'```.*?```', re.DOTALL)
INLINE_CODE_RE = re.compile(r'`([^`]*)`')
SPOILER_RE = re.compile(r'\|\|.*?\|\|', re.DOTALL)
URL_RE = re.compile(r'https?://\S+')
CUSTOM_EMOJI_RE = re.compile(r'<a?:\w+:\d+>')
EMOJI_RE = re.compile('[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u200d]')
MD_RE = re.compile(r'[*_~>#]+')


def clean_text(text: str) -> str:
    """Deja el texto listo para que suene natural al leerlo en voz alta."""
    text = CODE_BLOCK_RE.sub(' bloque de código ', text)
    text = SPOILER_RE.sub(' spoiler ', text)
    text = URL_RE.sub(' enlace ', text)
    text = CUSTOM_EMOJI_RE.sub(' ', text)
    text = EMOJI_RE.sub(' ', text)
    text = INLINE_CODE_RE.sub(r'\1', text)
    text = text.replace('@', '')

    text = MD_RE.sub(' ', text)
    text = re.sub(r'\s+', ' ', text).strip()

    if not re.search(r'\w', text):
        return ''

    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS].rsplit(' ', 1)[0] + '... y más'
    return text


def split_chunks(text: str, limit: int = CHUNK_CHARS):
    """Parte un texto largo en pedazos (por oraciones). Solo se usa cuando NO hay streaming."""
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ''
    for part in re.split(r'(?<=[.!?…])\s+', text):
        if cur and len(cur) + 1 + len(part) > limit:
            chunks.append(cur)
            cur = part
        else:
            cur = f'{cur} {part}'.strip()
    if cur:
        chunks.append(cur)
    final = []
    for c in chunks:
        while len(c) > limit * 1.5:
            cut = c.rfind(' ', 0, limit)
            if cut <= 0:
                cut = limit
            final.append(c[:cut].strip())
            c = c[cut:].strip()
        if c:
            final.append(c)
    return final


class GeminiNoCapacity(RuntimeError):
    """No hay ninguna key de Gemini disponible ahora mismo (descansando o sin cupo local)."""


def _is_rate_limit(e: Exception) -> bool:
    """¿El error es un límite de peticiones / cuota (429)?"""
    if getattr(e, 'code', None) == 429 or getattr(e, 'status_code', None) == 429:
        return True
    t = f'{type(e).__name__} {e}'.lower()
    return '429' in t or 'resource_exhausted' in t or 'too many' in t or 'quota' in t


def _tts_request(text: str) -> dict:
    """Parámetros de la petición a Gemini 3.8 TTS (Interactions API)."""
    content = {'type': 'text', 'text': text}
    if GEMINI_STYLE:
        content['annotations'] = [{'type': 'speech_metadata', 'style': GEMINI_STYLE}]
    return dict(
        model=GEMINI_TTS_MODEL,
        input=[{'type': 'user_input', 'content': [content]}],
        response_format={'type': 'audio'},
        generation_config={'speech_config': [{'voice': GEMINI_TTS_VOICE}]},
    )


def _upsample(raw: bytes) -> bytes:
    """PCM 24kHz mono -> 48kHz estéreo (cada muestra se repite 4 veces: 2 canales x 2 en el tiempo)."""
    a = array('h')
    a.frombytes(raw)
    if sys.byteorder == 'big':
        a.byteswap()
    out = array('h', bytes(len(a) * 8))
    out[0::4] = a
    out[1::4] = a
    out[2::4] = a
    out[3::4] = a
    if sys.byteorder == 'big':
        out.byteswap()
    return out.tobytes()


class PCMStreamSource(discord.AudioSource):
    """Fuente de audio que se va llenando mientras Gemini genera el audio.

    Un hilo mete el PCM con feed() y el reproductor de Discord lo va leyendo con read()
    cada 20 ms. Si el audio llega más lento de lo que suena, se rellena con silencio.
    """

    def __init__(self, loop):
        self._loop = loop
        self._buf = bytearray()
        self._carry = b''
        self._lock = threading.Lock()
        self._finished = False
        self._ready_sent = False
        self.cancelled = False
        self.error = None
        self.total_bytes = 0
        self.key_idx = None               # key que se está usando (para el descanso si se cuelga)
        self.slot_released = False        # ¿ya se liberó su lugar en el semáforo?
        self.ready = asyncio.Event()      # ya hay suficiente audio para empezar (o terminó / falló)
        self.done = threading.Event()     # terminó la generación

    def _signal_ready(self):
        if not self._ready_sent:
            self._ready_sent = True
            self._loop.call_soon_threadsafe(self.ready.set)

    # --- lado productor (hilo de Gemini) ---
    def feed(self, pcm: bytes):
        data = self._carry + pcm
        if len(data) % 2:
            self._carry = data[-1:]
            data = data[:-1]
        else:
            self._carry = b''
        if not data:
            return
        with self._lock:
            if self.cancelled:
                return
            self._buf += data
            self.total_bytes += len(data)
            if self.total_bytes >= PREBUFFER_BYTES:
                self._signal_ready()

    def finish(self, error=None):
        self.error = error
        self._finished = True
        self.done.set()
        self._signal_ready()

    # --- lado consumidor (reproductor de Discord) ---
    def read(self) -> bytes:
        with self._lock:
            if len(self._buf) >= FRAME_IN:
                raw = bytes(self._buf[:FRAME_IN])
                del self._buf[:FRAME_IN]
            elif self._finished:
                if not self._buf:
                    return b''           # fin del audio
                raw = bytes(self._buf) + b'\x00' * (FRAME_IN - len(self._buf))
                self._buf.clear()
            else:
                return b'\x00' * FRAME_OUT   # el audio llega más lento que la reproducción: silencio
        return _upsample(raw)

    def is_opus(self) -> bool:
        return False

    def cancel(self):
        with self._lock:
            self.cancelled = True
            self._finished = True
            self._buf.clear()
        self.done.set()
        self._signal_ready()

    def cleanup(self):
        self.cancel()


class TTS(commands.Cog):
    """Cog de text-to-speech: Gemini TTS (con streaming) por defecto, con respaldo a edge-tts"""

    def __init__(self, bot):
        self.bot = bot
        self.vc_readers = {}
        self._gemini_clients = {}   # key -> cliente
        self._gemini_idx = 0        # rotación pegajosa: se queda en la key que funciona
        self._key_cooldown = {}     # idx de key -> momento hasta el que descansa (por 429)
        self._key_streak = {}       # idx de key -> 429 seguidos (el descanso crece si se repite)
        self._key_calls = {}        # idx de key -> deque con la hora de sus últimas peticiones
        self._budget_lock = threading.Lock()
        self._gemini_sem = asyncio.Semaphore(GEMINI_CONCURRENCY)   # FIFO: respeta el orden de los mensajes
        # limpiar audios huérfanos de ejecuciones anteriores
        for old in TTS_FOLDER.glob('tts_*'):
            old.unlink(missing_ok=True)

    def cog_unload(self):
        for data in self.vc_readers.values():
            task = data.get('task')
            if task:
                task.cancel()

    # ---------------- Generación de audio ----------------
    def _get_client(self, key: str):
        from google import genai
        client = self._gemini_clients.get(key)
        if client is None:
            try:
                client = genai.Client(api_key=key, http_options={'timeout': GEMINI_HTTP_TIMEOUT_MS})
            except Exception:
                client = genai.Client(api_key=key)   # versiones que no aceptan http_options así
            self._gemini_clients[key] = client
        return client

    def _budget_ok_locked(self, idx: int, now: float) -> bool:
        """¿La key todavía tiene cupo local (por minuto / por día)? Llamar con _budget_lock tomado."""
        dq = self._key_calls.setdefault(idx, deque())
        horizonte = 86400 if GEMINI_DAILY_PER_KEY else 60
        while dq and now - dq[0] > horizonte:
            dq.popleft()
        if GEMINI_RPM_PER_KEY and sum(1 for t in dq if now - t < 60) >= GEMINI_RPM_PER_KEY:
            return False
        if GEMINI_DAILY_PER_KEY and len(dq) >= GEMINI_DAILY_PER_KEY:
            return False
        return True

    def _reserve(self, idx: int) -> bool:
        """Si hay cupo, apunta la petición y devuelve True."""
        now = time.time()
        with self._budget_lock:
            if not self._budget_ok_locked(idx, now):
                return False
            self._key_calls[idx].append(now)
            return True

    def _available_keys(self):
        """Índices de keys que NO están descansando y todavía tienen cupo local,
        empezando por la que funcionó la última vez."""
        now_m = time.monotonic()
        now_w = time.time()
        total = len(GEMINI_KEYS)
        if not total:
            return []
        orden = [(self._gemini_idx + i) % total for i in range(total)]
        libres = [i for i in orden if self._key_cooldown.get(i, 0) <= now_m]
        with self._budget_lock:
            return [i for i in libres if self._budget_ok_locked(i, now_w)]

    def _mark_success(self, idx: int):
        self._gemini_idx = idx
        self._key_streak[idx] = 0

    def _mark_failure(self, idx: int, e: Exception):
        if not _is_rate_limit(e):
            return
        # El descanso se duplica con cada 429 seguido (30s, 60s, 2min... hasta 1h) y se reinicia al funcionar
        racha = self._key_streak.get(idx, 0) + 1
        self._key_streak[idx] = racha
        descanso = min(KEY_COOLDOWN * (2 ** (racha - 1)), 3600)
        msg = str(e).lower()
        if 'perday' in msg or 'per day' in msg or 'daily' in msg:
            descanso = max(descanso, 3600)   # cuota del día agotada: no tiene caso insistir cada rato
        self._key_cooldown[idx] = time.monotonic() + descanso
        logger.warning(f'Key #{idx + 1} llegó al límite de peticiones: descansa {descanso:.0f}s')

    async def _acquire_slot(self):
        """Toma un lugar del semáforo, pero NO espera para siempre: si no hay lugar en
        GEMINI_SLOT_WAIT segundos, lanza GeminiNoCapacity y ese mensaje se lee con edge-tts."""
        try:
            await asyncio.wait_for(self._gemini_sem.acquire(), timeout=GEMINI_SLOT_WAIT)
        except asyncio.TimeoutError:
            raise GeminiNoCapacity('Gemini ocupado (sin lugar libre a tiempo)')

    def _stream_worker(self, text: str, source: PCMStreamSource):
        """Corre en un hilo: pide el audio a Gemini en streaming y lo mete en `source` conforme llega."""
        try:
            self._run_stream(text, source)
        finally:
            # libera el lugar en el semáforo (el siguiente mensaje en la fila puede arrancar)
            try:
                source._loop.call_soon_threadsafe(self._release_slot, source)
            except RuntimeError:
                pass  # el loop ya está cerrado (apagando el bot)

    def _release_slot(self, source: PCMStreamSource):
        """Libera el lugar del semáforo, una sola vez por stream. Siempre corre en el loop de asyncio."""
        if not source.slot_released:
            source.slot_released = True
            self._gemini_sem.release()

    def _run_stream(self, text: str, source: PCMStreamSource):
        candidatas = self._available_keys()
        if not candidatas:
            source.finish(error=GeminiNoCapacity('Sin keys disponibles (descansando o sin cupo)'))
            return

        total = len(GEMINI_KEYS)
        last_error = None
        for idx in candidatas:
            if source.cancelled:
                break
            if not self._reserve(idx):
                continue          # se acabó su cupo local justo ahora
            try:
                source.key_idx = idx
                client = self._get_client(GEMINI_KEYS[idx])
                if not hasattr(client, 'interactions'):
                    raise RuntimeError('google-genai muy viejo, actualiza con: pip install -U google-genai')

                stream = client.interactions.create(**_tts_request(text), stream=True)
                for event in stream:
                    if source.cancelled:
                        break
                    if getattr(event, 'event_type', None) != 'step.delta':
                        continue
                    delta = getattr(event, 'delta', None)
                    if delta is None or getattr(delta, 'type', None) != 'audio':
                        continue
                    data = getattr(delta, 'data', None)
                    if data:
                        source.feed(base64.b64decode(data))

                if source.cancelled:
                    source.finish()
                    return
                if source.total_bytes == 0:
                    raise RuntimeError('Gemini no devolvió audio')
                self._mark_success(idx)
                source.finish()
                return
            except Exception as e:
                last_error = e
                logger.warning(f'Gemini TTS (stream) key #{idx + 1}/{total} falló ({type(e).__name__}: {e})')
                self._mark_failure(idx, e)
                if source.total_bytes > 0:
                    # ya se mandó audio parcial: no se puede reintentar con otra key sin duplicarlo
                    source.finish(error=e)
                    return

        if source.cancelled:
            source.finish()
        else:
            source.finish(error=last_error or GeminiNoCapacity('Sin keys disponibles (sin cupo)'))

    async def _gemini_stream(self, text: str) -> PCMStreamSource:
        """Arranca el streaming y regresa en cuanto hay suficiente audio para empezar a sonar.
        Solo hay GEMINI_CONCURRENCY streams a la vez, en el mismo orden que los mensajes (evita los 429).
        Si Gemini no manda audio a tiempo, lanza error para que se use edge-tts."""
        if not self._available_keys():
            raise GeminiNoCapacity('Sin keys disponibles (descansando o sin cupo)')

        loop = asyncio.get_running_loop()
        await self._acquire_slot()                # espera su turno, pero con límite de tiempo
        source = PCMStreamSource(loop)
        try:
            loop.run_in_executor(None, self._stream_worker, text, source)
        except Exception:
            self._release_slot(source)
            raise

        try:
            await asyncio.wait_for(source.ready.wait(), timeout=GEMINI_FIRST_AUDIO_TIMEOUT)
        except asyncio.TimeoutError:
            # Gemini no mandó nada a tiempo: se suelta el lugar y esa key descansa un rato
            source.cancel()
            self._release_slot(source)
            if source.key_idx is not None:
                self._key_cooldown[source.key_idx] = time.monotonic() + KEY_COOLDOWN
                logger.warning(f'Key #{source.key_idx + 1} no dio audio en {GEMINI_FIRST_AUDIO_TIMEOUT:.0f}s: '
                               f'descansa {KEY_COOLDOWN:.0f}s')
            raise RuntimeError(f'Gemini no dio audio en {GEMINI_FIRST_AUDIO_TIMEOUT:.0f}s')
        except asyncio.CancelledError:
            source.cancel()
            self._release_slot(source)
            raise

        if source.total_bytes == 0:
            source.cancel()
            raise source.error or RuntimeError('Gemini no devolvió audio')
        return source

    def _gemini_call_38(self, client, text: str) -> bytes:
        """Gemini 3.8 TTS sin streaming (Interactions API). Síncrono, se corre en un hilo. Devuelve WAV."""
        if not hasattr(client, 'interactions'):
            raise RuntimeError('google-genai muy viejo, actualiza con: pip install -U google-genai')
        interaction = client.interactions.create(**_tts_request(text))
        return base64.b64decode(interaction.output_audio.data)  # ya trae header WAV

    async def _gemini_call_legacy(self, client, text: str) -> bytes:
        """Modelos viejos (2.5 / 3.1 preview): devuelven PCM crudo, hay que envolverlo en WAV."""
        from google.genai import types

        response = await client.aio.models.generate_content(
            model=GEMINI_TTS_MODEL,
            contents=(GEMINI_STYLE + ' ' + text) if GEMINI_STYLE else text,
            config=types.GenerateContentConfig(
                response_modalities=['AUDIO'],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=GEMINI_TTS_VOICE)
                    )
                ),
            ),
        )
        pcm = response.candidates[0].content.parts[0].inline_data.data
        buf = io.BytesIO()
        with wave.open(buf, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24000)
            wf.writeframes(pcm)
        return buf.getvalue()

    async def _gemini_tts(self, text: str, file_path: Path):
        """Genera el audio COMPLETO a un archivo (sin streaming), rotando keys si falla."""
        candidatas = self._available_keys()
        if not candidatas:
            raise GeminiNoCapacity('Sin keys disponibles (descansando o sin cupo)')

        total = len(GEMINI_KEYS)
        last_error = None
        await self._acquire_slot()
        try:
            for idx in candidatas:
                if not self._reserve(idx):
                    continue
                try:
                    client = self._get_client(GEMINI_KEYS[idx])

                    if GEMINI_TTS_MODEL.startswith('gemini-3.8'):
                        audio = await asyncio.to_thread(self._gemini_call_38, client, text)
                    else:
                        audio = await self._gemini_call_legacy(client, text)

                    file_path.write_bytes(audio)
                    self._mark_success(idx)
                    return
                except Exception as e:
                    last_error = e
                    logger.warning(f'Gemini TTS key #{idx + 1}/{total} falló ({type(e).__name__}: {e})')
                    self._mark_failure(idx, e)
        finally:
            self._gemini_sem.release()

        raise last_error or GeminiNoCapacity('Sin keys disponibles (sin cupo)')

    async def _edge_tts(self, text: str, voice: str, file_path: Path) -> bool:
        """Respaldo final: edge-tts (sin API key). Devuelve True si el archivo quedó listo."""
        try:
            await asyncio.wait_for(
                edge_tts.Communicate(text, voice, rate=EDGE_RATE).save(str(file_path)),
                timeout=EDGE_TIMEOUT,
            )
            if file_path.exists() and file_path.stat().st_size > 0:
                logger.info('Audio generado con edge-tts')
                return True
            logger.error('edge-tts no generó audio (archivo vacío)')
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.error(f'edge-tts tardó más de {EDGE_TIMEOUT:.0f}s')
        except Exception as e:
            logger.error(f'Error al generar edge-tts: {type(e).__name__}: {e}')
        file_path.unlink(missing_ok=True)
        return False

    async def _synthesize(self, text: str, voice: str, engine: str, file_path: Path):
        """Devuelve un PCMStreamSource (Gemini en streaming), un Path (archivo listo) o None si todo falló.
        Si Gemini falla (cuota, error, timeout, sin cupo), cae SIEMPRE a edge-tts."""
        if engine == 'gemini' and GEMINI_KEYS:
            try:
                t0 = time.monotonic()
                if USE_STREAM:
                    source = await self._gemini_stream(text)
                    logger.info(f'Gemini streaming: primer audio en {time.monotonic() - t0:.1f}s ({len(text)} chars)')
                    return source
                await self._gemini_tts(text, file_path)
                logger.info(f'Gemini: {len(text)} chars en {time.monotonic() - t0:.1f}s')
                return file_path
            except asyncio.CancelledError:
                raise
            except GeminiNoCapacity as e:
                logger.info(f'Gemini sin cupo ahora mismo ({e}), usando edge-tts para este mensaje')
            except Exception as e:
                logger.warning(f'Gemini TTS falló ({type(e).__name__}: {e}), usando edge-tts')
            file_path.unlink(missing_ok=True)   # por si Gemini dejó un archivo a medias

        if await self._edge_tts(text, voice, file_path):
            return file_path
        return None

    # ---------------- Reproducción ----------------
    async def _audio_worker(self, guild_id):
        """Reproduce los audios en el mismo orden en que llegaron los mensajes"""
        while guild_id in self.vc_readers and self.vc_readers[guild_id]['enabled']:
            try:
                queue = self.vc_readers[guild_id]['queue']
                file_path, synth_task = await queue.get()
                result = None

                try:
                    guild = self.bot.get_guild(guild_id)
                    voice_client = discord.utils.get(self.bot.voice_clients, guild=guild)

                    if not voice_client or not voice_client.is_connected():
                        synth_task.cancel()
                        continue

                    try:
                        result = await asyncio.wait_for(synth_task, timeout=SYNTH_TIMEOUT)
                    except asyncio.TimeoutError:
                        logger.warning(f'Timeout ({SYNTH_TIMEOUT:.0f}s) generando {file_path.name}. Saltando.')
                        continue

                    if result is None:
                        logger.warning('No se pudo generar audio para este mensaje (Gemini y edge-tts fallaron)')
                        continue

                    if isinstance(result, PCMStreamSource):
                        audio = result
                        logger.info('Reproduciendo (streaming)')
                    else:
                        if not file_path.exists() or file_path.stat().st_size == 0:
                            continue
                        audio = discord.FFmpegPCMAudio(str(file_path))
                        logger.info('Reproduciendo (archivo)')

                    loop = asyncio.get_running_loop()
                    done = asyncio.Event()

                    def after_playing(error):
                        if error:
                            logger.error(f'Error en reproducción de audio: {error}')
                        loop.call_soon_threadsafe(done.set)

                    voice_client.play(audio, after=after_playing)
                    await done.wait()
                finally:
                    queue.task_done()
                    if isinstance(result, PCMStreamSource):
                        result.cancel()
                    file_path.unlink(missing_ok=True)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f'Error en el worker de TTS: {type(e).__name__}: {e}', exc_info=True)
                await asyncio.sleep(1)

    def _drain_queue(self, guild_id):
        queue = self.vc_readers[guild_id].get('queue')
        if not queue:
            return
        while not queue.empty():
            try:
                file_path, synth_task = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            synth_task.cancel()
            if synth_task.done() and not synth_task.cancelled() and synth_task.exception() is None:
                res = synth_task.result()
                if isinstance(res, PCMStreamSource):
                    res.cancel()
            file_path.unlink(missing_ok=True)

    # ---------------- Comandos ----------------
    @commands.command(name='leervc')
    async def leervc(self, ctx):
        """Activar lectura en el canal de voz"""

        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send('❌ Debes estar en un canal de voz.')
            return

        channel = ctx.author.voice.channel

        if not ctx.voice_client:
            await channel.connect()
        elif ctx.voice_client.channel != channel:
            await ctx.voice_client.move_to(channel)

        guild_id = ctx.guild.id
        voice = db.get_config(guild_id, 'tts_voice', 'es-MX-DaliaNeural')
        engine = db.get_config(guild_id, 'tts_engine', DEFAULT_ENGINE)

        if guild_id not in self.vc_readers:
            queue = asyncio.Queue(maxsize=MAX_QUEUE)
            task = self.bot.loop.create_task(self._audio_worker(guild_id))
            self.vc_readers[guild_id] = {
                'enabled': True,
                'text_channel_id': ctx.channel.id,
                'voice': voice,
                'engine': engine,
                'last_author_id': None,
                'last_time': 0,
                'queue': queue,
                'task': task
            }
        else:
            reader = self.vc_readers[guild_id]
            reader['enabled'] = True
            reader['text_channel_id'] = ctx.channel.id
            reader['voice'] = voice
            reader['engine'] = engine
            reader['last_author_id'] = None
            reader['last_time'] = 0

            if 'task' not in reader or reader['task'].done():
                reader['queue'] = asyncio.Queue(maxsize=MAX_QUEUE)
                reader['task'] = self.bot.loop.create_task(self._audio_worker(guild_id))

        modo = 'streaming' if (engine == 'gemini' and USE_STREAM) else 'normal'
        await ctx.send(f'🔊 Conectado y leyendo en {channel.mention} (motor: `{engine}`, modo: `{modo}`)')
        logger.info(f'TTS activado en {ctx.guild.name} con motor {engine} ({modo})')

    @commands.command(name='dejardeleer')
    async def dejardeleer(self, ctx):
        """Desactivar lectura en el canal de voz"""

        guild_id = ctx.guild.id

        if guild_id in self.vc_readers:
            self.vc_readers[guild_id]['enabled'] = False

            if 'task' in self.vc_readers[guild_id]:
                self.vc_readers[guild_id]['task'].cancel()

            self._drain_queue(guild_id)

            if ctx.voice_client:
                await ctx.voice_client.disconnect()

            await ctx.send('🔇 Lectura desactivada y desconectado.')
            logger.info(f'TTS desactivado en {ctx.guild.name}')
        else:
            await ctx.send('❌ La lectura no está activada.')

    @commands.command(name='setvoz')
    @commands.has_permissions(administrator=True)
    async def set_voice(self, ctx, voice: str = None):
        """Establecer voz de edge-tts (ej: es-MX-DaliaNeural, es-MX-JorgeNeural, es-ES-AlvaroNeural)"""

        if voice is None:
            await ctx.send('❌ Ejemplo de voces: `es-MX-DaliaNeural`, `es-MX-JorgeNeural`, `es-ES-AlvaroNeural`')
            return

        guild_id = ctx.guild.id
        db.set_config(guild_id, 'tts_voice', voice)

        if guild_id in self.vc_readers:
            self.vc_readers[guild_id]['voice'] = voice

        await ctx.send(f'✅ Voz establecida a `{voice}`')

    @commands.command(name='setmotor')
    @commands.has_permissions(administrator=True)
    async def set_engine(self, ctx, motor: str = None):
        """Cambiar motor de voz: gemini (IA, más natural, default) o edge (rápido, ilimitado)"""

        if motor not in ('edge', 'gemini'):
            await ctx.send('❌ Usa `!setmotor edge` o `!setmotor gemini`')
            return

        if motor == 'gemini' and not GEMINI_KEYS:
            await ctx.send('❌ No hay keys en `GEMINI_API_KEYS` (config.py).')
            return

        guild_id = ctx.guild.id
        db.set_config(guild_id, 'tts_engine', motor)

        if guild_id in self.vc_readers:
            self.vc_readers[guild_id]['engine'] = motor

        await ctx.send(f'✅ Motor de voz: `{motor}`')

    @commands.command(name='ttsdiag')
    @commands.has_permissions(administrator=True)
    async def tts_diag(self, ctx):
        """Diagnóstico: prueba Gemini TTS y edge-tts, mide los tiempos y muestra el error exacto si falla"""

        engine = db.get_config(ctx.guild.id, 'tts_engine', DEFAULT_ENGINE)
        lines = [
            f'Motor guardado en este server: `{engine}` | Streaming: `{USE_STREAM}`',
            f'Keys en config: `{len(GEMINI_KEYS)}` | Modelo: `{GEMINI_TTS_MODEL}` | Voz: `{GEMINI_TTS_VOICE}`',
            f'Keys disponibles ahora: `{len(self._available_keys())}/{len(GEMINI_KEYS)}` '
            f'(descansando por 429 o sin cupo local) | Streams simultáneos máx: `{GEMINI_CONCURRENCY}`',
            f'Cupo local por key: `{GEMINI_RPM_PER_KEY or "sin límite"}`/min, '
            f'`{GEMINI_DAILY_PER_KEY or "sin límite"}`/día',
            f'Timeouts: lugar `{GEMINI_SLOT_WAIT:.0f}s` + primer audio `{GEMINI_FIRST_AUDIO_TIMEOUT:.0f}s` '
            f'+ edge `{EDGE_TIMEOUT:.0f}s` | Total máx por mensaje: `{SYNTH_TIMEOUT:.0f}s`',
        ]

        # --- Prueba del respaldo (edge-tts) ---
        t_edge = time.monotonic()
        edge_path = TTS_FOLDER / f'tts_diag_edge_{os.urandom(2).hex()}.audio'
        try:
            ok = await self._edge_tts('Prueba de voz', 'es-MX-DaliaNeural', edge_path)
            if ok:
                lines.append(f'✅ edge-tts (respaldo) OK en `{time.monotonic() - t_edge:.1f}s`')
            else:
                lines.append('❌ edge-tts (respaldo) FALLÓ: revisa los logs')
        finally:
            edge_path.unlink(missing_ok=True)

        try:
            import google.genai  # noqa: F401
        except ImportError:
            lines.append('❌ Falta la librería: `pip install -U google-genai`')
            await ctx.send('\n'.join(lines))
            return

        if not GEMINI_KEYS:
            lines.append('❌ `GEMINI_API_KEYS` está vacío en config.py')
            await ctx.send('\n'.join(lines))
            return

        t0 = time.monotonic()
        try:
            if USE_STREAM:
                source = await asyncio.wait_for(
                    self._gemini_stream('Hola, esta es una prueba de voz con streaming.'), timeout=30)
                t_first = time.monotonic() - t0
                await asyncio.to_thread(source.done.wait, 30)
                t_total = time.monotonic() - t0
                secs = source.total_bytes / SRC_BYTES_PER_SEC
                source.cancel()
                lines.append(
                    f'✅ Streaming OK: primer audio a los `{t_first:.1f}s`, '
                    f'audio de `{secs:.1f}s` generado completo en `{t_total:.1f}s`'
                )
            else:
                test_path = TTS_FOLDER / f'tts_diag_{os.urandom(2).hex()}.audio'
                try:
                    await asyncio.wait_for(self._gemini_tts('Prueba de voz', test_path), timeout=30)
                    lines.append(
                        f'✅ Gemini respondió en `{time.monotonic() - t0:.1f}s` '
                        f'({test_path.stat().st_size} bytes de audio)'
                    )
                finally:
                    test_path.unlink(missing_ok=True)
        except Exception as e:
            lines.append(f'❌ Gemini falló: `{type(e).__name__}: {str(e)[:300]}`')

        await ctx.send('\n'.join(lines))

    # ---------------- Lectura de mensajes ----------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Lee mensajes en VC (limpia el texto, mantiene el orden y limita la cola)"""

        if message.author.bot or message.guild is None:
            return

        guild_id = message.guild.id
        reader = self.vc_readers.get(guild_id)
        if not reader or not reader['enabled']:
            return

        voice_client = message.guild.voice_client
        if not voice_client or not voice_client.is_connected():
            return

        text_channel_id = reader.get('text_channel_id')
        if text_channel_id and message.channel.id != text_channel_id:
            return

        author_voice = getattr(message.author, 'voice', None)
        if not author_voice or author_voice.channel != voice_client.channel:
            return

        # Ignorar comandos del bot
        if message.content.startswith('!max'):
            return
        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return

        # clean_content convierte menciones (<@id>) a nombres legibles
        text = clean_text(message.clean_content)
        if not text:
            if message.attachments:
                text = 'mandó un archivo'
            else:
                return

        current_time = time.time()
        if reader.get('last_author_id') == message.author.id and (current_time - reader.get('last_time', 0)) < 60:
            text_to_read = text
        else:
            name = clean_text(message.author.display_name) or message.author.display_name
            text_to_read = f'{name} dice: {text}'

        reader['last_author_id'] = message.author.id
        reader['last_time'] = current_time

        queue = reader['queue']
        if queue.full():
            logger.warning(f'Cola de TTS llena en {message.guild.name}, mensaje ignorado')
            return

        voice = reader.get('voice', 'es-MX-DaliaNeural')
        engine = reader.get('engine', DEFAULT_ENGINE)
        # Con streaming no hace falta partir el mensaje: el primer audio llega rápido sin importar el largo
        partir = engine == 'gemini' and not USE_STREAM
        pedazos = split_chunks(text_to_read) if partir else [text_to_read]

        base = f'tts_{guild_id}_{int(time.time() * 1000)}_{os.urandom(2).hex()}'
        for i, pedazo in enumerate(pedazos):
            file_path = TTS_FOLDER / f'{base}_{i}.audio'
            # Se encola el task de inmediato: la generación arranca ya, pero suena en orden
            synth_task = asyncio.create_task(self._synthesize(pedazo, voice, engine, file_path))
            try:
                queue.put_nowait((file_path, synth_task))
            except asyncio.QueueFull:
                synth_task.cancel()
                logger.warning('Cola de TTS llena, se descartó el resto del mensaje')
                break


async def setup(bot):
    await bot.add_cog(TTS(bot))