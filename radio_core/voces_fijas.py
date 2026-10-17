"""Voces fijas de Radio Maxia (radio_core/voces_fijas.py).

Frases que se repiten (la hora, la bienvenida del noticiero) cuyo audio se guarda PARA SIEMPRE en
radio_data/voces_fijas/. No es caché: nada lo borra. Reglas:
  · Solo se guardan audios hechos con la voz de Gemini (los que `tts` nombra voz_g_* o voz_o_*). Si Gemini falla y suena
    otra voz (edge/Piper), esa vez suena pero NO se guarda; la próxima vez se vuelve a intentar con Gemini.
  · Si ya hay alguna variante guardada suena al instante (sin gastar cupo) y las que faltan se crean solas en segundo
    plano, de una en una, solo con Gemini (sin caer a edge/Piper) y con descanso si no hay cupo.
  · El nombre lleva un hash corto del texto + VERSION_VOZ: si cambias una frase o la voz, se generan audios nuevos
    en vez de reusar los viejos (los viejos quedan en la carpeta; bórralos a mano si quieres).
"""
import asyncio
import hashlib
import logging
import random
import time
from pathlib import Path

from . import config as C

logger = logging.getLogger(__name__)

DIR_FIJAS = C.DIR_RADIO / "voces_fijas"
DIR_FIJAS.mkdir(parents=True, exist_ok=True)

VERSION_VOZ = "gemini-kore-1"        # cámbialo si cambias de voz/estilo: así no se mezclan voces distintas en la carpeta
DESCANSO_SIN_CUPO = 600              # seg. sin reintentar el relleno en segundo plano tras un fallo
_PREFIJOS_GEMINI = ("voz_g_", "voz_o_")   # cog TTS (Gemini) y Gemini vía OpenRouter

# ── Textos ───────────────────────────────────────────────────────────────────────────────────────────────────
# Sin "buenos días/tardes": el noticiero sale a distintas horas.
BIENVENIDA_NOTICIERO = [
    "Y ahora, lo más importante de Latinoamérica. Este es el noticiero de Radio Maxia.",
    "Atención, atención. Arranca el noticiero de Radio Maxia con lo que está pasando en toda Latinoamérica.",
    "Radio Maxia, tu estación. Es momento de las noticias de Latinoamérica.",
    "Prepárate, porque comienza el noticiero de Radio Maxia con las noticias de todo el continente.",
]
PLANTILLAS_HORA = [
    "{Es} {hora} {periodo}.",
    "Radio Maxia te lo recuerda: {es} {hora} {periodo}.",
]

_UNIDADES = ["cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve", "diez", "once", "doce",
             "trece", "catorce", "quince", "dieciséis", "diecisiete", "dieciocho", "diecinueve", "veinte", "veintiuno",
             "veintidós", "veintitrés", "veinticuatro", "veinticinco", "veintiséis", "veintisiete", "veintiocho",
             "veintinueve"]
_DECENAS = {3: "treinta", 4: "cuarenta", 5: "cincuenta"}


def _n(n: int) -> str:
    """0-59 en palabras."""
    if n < 30:
        return _UNIDADES[n]
    d, u = divmod(n, 10)
    return _DECENAS[d] + (f" y {_UNIDADES[u]}" if u else "")


def _periodo(h: int, m: int) -> str:
    if h == 12:
        return "del día"
    if h == 0 and m == 0:
        return "de la noche"
    if h < 6:
        return "de la madrugada"
    if h < 12:
        return "de la mañana"
    return "de la tarde" if h < 19 else "de la noche"


def _hora_en_palabras(h: int, m: int):
    h12 = h % 12 or 12
    una = h12 == 1
    hora = "la una" if una else f"las {_n(h12)}"
    if m == 0:
        hora += " en punto"
    elif m == 1:
        hora += " con un minuto"
    else:                                              # 21, 31, 41, 51 → "veintiún / treinta y un minutos"
        hora += " con " + _n(m).replace("veintiuno", "veintiún").replace("y uno", "y un") + " minutos"
    return ("es" if una else "son"), hora


def _clave(base: str, texto: str) -> str:
    return f"{base}_{hashlib.sha1(f'{VERSION_VOZ}|{texto}'.encode()).hexdigest()[:6]}"


def variantes_hora(h: int, m: int) -> list:
    """Hora EXACTA (sin redondear) → [(clave, texto), ...] con las variantes de la frase."""
    es, hora = _hora_en_palabras(h, m)
    per = _periodo(h, m)
    out = []
    for i, p in enumerate(PLANTILLAS_HORA):
        texto = p.format(Es=es.capitalize(), es=es, hora=hora, periodo=per)
        out.append((_clave(f"hora_{h:02d}{m:02d}_v{i + 1}", texto), texto))
    return out


def variantes(base: str, textos: list) -> list:
    """Variantes con tus propios textos: variantes("hora_1637", [texto1, texto2]) → [(clave, texto), ...]."""
    return [(_clave(f"{base}_v{i + 1}", t), t) for i, t in enumerate(textos)]


def variantes_bienvenida_noticiero() -> list:
    return [(_clave(f"noticiero_bienvenida_{i + 1}", t), t) for i, t in enumerate(BIENVENIDA_NOTICIERO)]


# ── Almacén ──────────────────────────────────────────────────────────────────────────────────────────────────
def _existente(clave: str):
    for ext in (".mp3", ".wav"):
        r = DIR_FIJAS / f"{clave}{ext}"
        if r.exists() and r.stat().st_size > 1000:
            return r
    return None


def _es_gemini(ruta) -> bool:
    return Path(ruta).name.startswith(_PREFIJOS_GEMINI)


async def _guardar(clave: str, origen: Path) -> Path:
    """Copia el audio a la carpeta fija. Lo comprime a mp3 con ffmpeg (un wav de 5 s pesa ~240 KB, un mp3 ~25 KB);
    si ffmpeg falla, guarda el original tal cual."""
    destino = DIR_FIJAS / f"{clave}.mp3"
    tmp = DIR_FIJAS / f"{clave}.part.mp3"          # se escribe aparte y se renombra: nunca queda un archivo a medias
    try:
        proc = await asyncio.create_subprocess_exec(
            getattr(C, "FFMPEG", "ffmpeg"), "-y", "-loglevel", "error", "-i", str(origen), "-codec:a", "libmp3lame", "-q:a", "4", str(tmp),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(proc.wait(), 30)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 1000:
            tmp.replace(destino)
            return destino
    except Exception as e:
        logger.warning(f"[voces_fijas] no pude comprimir {clave}: {e!r}; guardo el original")
    tmp.unlink(missing_ok=True)
    crudo = DIR_FIJAS / f"{clave}.part{origen.suffix}"
    crudo.write_bytes(origen.read_bytes())
    final = DIR_FIJAS / f"{clave}{origen.suffix}"
    crudo.replace(final)
    return final


def migrar_voces_hora(carpeta_vieja: Path, hash_viejo, anuncios) -> int:
    """Copia las voces de la hora que ya tenías (radio_data/voces_hora/hora_<hash>.*) a voces_fijas/ con su nombre nuevo,
    para no volver a gastar cupo en ellas. No borra nada. hash_viejo(texto) → hash que usaba el Director;
    anuncios(h, m) → lista de textos de la hora h:m. Devuelve cuántas copió."""
    viejos = {p.stem: p for p in carpeta_vieja.glob("hora_*.*") if p.is_file() and p.stat().st_size > 1000} \
        if carpeta_vieja.is_dir() else {}
    n = 0
    for h in range(24):
        for m in range(60):
            for clave, texto in variantes(f"hora_{h:02d}{m:02d}", anuncios(h, m)):
                p = viejos.get(f"hora_{hash_viejo(texto)}")
                if p is None or _existente(clave):
                    continue
                tmp = DIR_FIJAS / f"{clave}.part{p.suffix}"
                tmp.write_bytes(p.read_bytes())
                tmp.replace(DIR_FIJAS / f"{clave}{p.suffix}")
                n += 1
    return n


# ── Uso ──────────────────────────────────────────────────────────────────────────────────────────────────────
_ultima: dict = {}            # grupo -> última clave usada (para no repetir la misma dos veces seguidas)
_en_curso: set = set()        # claves que se están rellenando (solo una a la vez)
_tareas: set = set()
_proximo_intento = 0.0


async def hablar_fijo(tts, grupo: str, variantes: list):
    """variantes = [(clave, texto), ...]. Devuelve la ruta del audio (o None). `tts` es la función de voz de la radio
    (Radio._tts): tts(texto) y tts(texto, solo_gemini=True)."""
    hechas = [c for c, _ in variantes if _existente(c)]
    faltan = [(c, t) for c, t in variantes if not _existente(c)]
    if hechas:
        opciones = [c for c in hechas if c != _ultima.get(grupo)] or hechas
        clave = random.choice(opciones)
        _ultima[grupo] = clave
        if faltan:
            _rellenar_en_segundo_plano(tts, *faltan[0])
        return str(_existente(clave))
    clave, texto = random.choice(variantes)           # no hay ninguna guardada: se genera ahora, como siempre
    _ultima[grupo] = clave
    ruta = await tts(texto)
    if ruta and _es_gemini(ruta):
        try:
            return str(await _guardar(clave, Path(ruta)))
        except Exception as e:
            logger.warning(f"[voces_fijas] no pude guardar {clave}: {e!r}")
    return ruta                                        # si sonó otra voz: suena esta vez, pero no se guarda


def _rellenar_en_segundo_plano(tts, clave: str, texto: str):
    if _en_curso or time.monotonic() < _proximo_intento:
        return
    _en_curso.add(clave)
    tarea = asyncio.get_running_loop().create_task(_rellenar(tts, clave, texto))
    _tareas.add(tarea)
    tarea.add_done_callback(_tareas.discard)


async def _rellenar(tts, clave: str, texto: str):
    global _proximo_intento
    try:
        ruta = await asyncio.wait_for(tts(texto, solo_gemini=True), 150)
        if ruta and _es_gemini(ruta):
            await _guardar(clave, Path(ruta))
            logger.info(f"[voces_fijas] guardada {clave}")
        else:
            _proximo_intento = time.monotonic() + DESCANSO_SIN_CUPO
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"[voces_fijas] relleno de {clave} falló: {e!r}")
        _proximo_intento = time.monotonic() + DESCANSO_SIN_CUPO
    finally:
        _en_curso.discard(clave)
