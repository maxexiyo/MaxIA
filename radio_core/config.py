"""Configuración central de Radio Maxia (versión Lavalink). Casi todo lo que quieras tocar está aquí."""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Carpetas ────────────────────────────────────────────────────────────────────
RAIZ = Path(__file__).resolve().parent.parent
DIR_RADIO = Path(os.getenv("RADIO_DIR", str(RAIZ / "radio_data")))
DIR_JINGLES = DIR_RADIO / "audio" / "jingles"       # Radio_Maxia.*, Radio_Mazia.*, Three-Note_Motif.*
DIR_PROGRAMAS = DIR_RADIO / "audio" / "programas"   # una carpeta por programa (intro, fondo, cortinilla)
DIR_FONDOS = DIR_RADIO / "audio" / "fondos"         # fondos instrumentales genéricos
DIR_CACHE = DIR_RADIO / "cache"                     # voces TTS y segmentos mezclados (se limpia sola)
for _d in (DIR_JINGLES, DIR_PROGRAMAS, DIR_FONDOS, DIR_CACHE):
    _d.mkdir(parents=True, exist_ok=True)

FFMPEG = os.getenv("FFMPEG_PATH", "ffmpeg")
FFPROBE = os.getenv("FFPROBE_PATH", "ffprobe")

# ── Dónde se "suben" los segmentos hablados para que Lavalink los lea ────────────
# ID de un canal de texto PRIVADO donde el bot pueda subir archivos (solo para esto).
CANAL_ARCHIVOS_ID = int(os.getenv("RADIO_CANAL_ARCHIVOS", "0") or 0)
BORRAR_ARCHIVOS_TRAS_S = 2 * 3600

# ── Reproducción ────────────────────────────────────────────────────────────────
VOLUMEN = int(os.getenv("RADIO_VOLUMEN", "70"))      # volumen del player de Lavalink (0-100)
# Cuántos segundos ANTES de que termine el segmento actual se manda el siguiente (cubre lo que tarda
# Lavalink en abrir el stream, para que no quede un hueco).
ADELANTO_S = {"voz": 1.0, "cancion": 0.6}
PLAY_TIMEOUT = 15
# La radio pasa al siguiente segmento cuando Lavalink avisa que el actual REALMENTE terminó (TrackEnd). Si ese aviso
# no llega (nodo caído, evento perdido), pasa solo cuando se cumple la duración + esta gracia.
GRACIA_FIN_S = 12.0

# ── Voz de Maxia ────────────────────────────────────────────────────────────────
# "auto" = Gemini TTS de tu cog TTS (con sus keys y cupos) → modelo TTS de OpenRouter (OPENROUTER_KEY) → edge-tts;
# "gemini" = Gemini → edge-tts; "openrouter" = OpenRouter → edge-tts;
# "edge" = siempre edge-tts (gratis, y la voz nunca cambia a media transmisión).
TTS_MOTOR = os.getenv("RADIO_TTS_MOTOR", "auto")
TTS_ESPERAS_CUPO = 3               # veces que espera a que Gemini tenga cupo antes de usar edge-tts
TTS_ESPERA_S = 12                  # segundos entre esas esperas (la radio genera con tiempo de sobra)

# ── Mezcla de los segmentos hablados ────────────────────────────────────────────
GAN_FONDO = 0.55          # volumen del fondo cuando nadie habla
GAN_INICIO = 0.9          # volumen de intro/jingle/cortinilla
VOZ_LUFS = -16            # loudness al que se normaliza la voz de Maxia
COLA_FONDO_S = 1.8        # el fondo sigue sonando este tiempo después de que Maxia termina (tapa la carga de la canción)
INTRO_SOLO_MAX_S = 3.0    # cuánto suena la intro/jingle SOLO antes de que Maxia hable encima

# ── Estación ────────────────────────────────────────────────────────────────────
NOMBRE = "Radio Maxia"
LOCUTORA = "Maxia"
ESLOGAN = "La radio que nunca duerme 📻"
LOGO_URL = os.getenv("RADIO_LOGO_URL", "")           # URL pública de la imagen del logo (para el embed)
COLOR_EMBED = (184, 134, 11)
ZONA_HORARIA = "America/Mexico_City"
# False = la radio solo corre mientras haya oyentes (ahorra CPU, subidas a Discord y límites de las APIs).
# True  = 24/7 de verdad, aunque no haya nadie escuchando.
SIEMPRE_AL_AIRE = os.getenv("RADIO_SIEMPRE_AL_AIRE", "0") == "1"

# ── Programación ────────────────────────────────────────────────────────────────
CANCIONES_ENTRE_HORA = (3, 4)      # Maxia dice la hora cada 3 o 4 canciones
CANCIONES_ENTRE_NOTICIERO = 8
CANCIONES_ENTRE_ID = 6             # jingle de la estación cada N canciones
PROB_COMENTARIO_CANCION = 0.35     # prob. de que Maxia comente una canción (programa general)
PROB_CHARLA = 0.10                 # prob. de que Maxia "se entretenga" platicando con fondo musical
COLA_OBJETIVO = 3                  # cuántos segmentos por delante tiene que tener la cola
MIN_DUR_CANCION = 60               # segundos; más corto que esto se descarta (intros, clips)
MAX_DUR_CANCION = 600              # segundos; más largo que esto se descarta
MAX_MENSAJES_POR_BLOQUE = 3
# Segundos reservados para armar el cierre + intro del programa nuevo (IA, voz, mezcla y subida)
MARGEN_TRANSICION_S = 25


def ahora_local() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(ZONA_HORARIA))
    except Exception:
        return datetime.now(timezone(timedelta(hours=-6)))