"""Programas de Radio Maxia y la parrilla (qué programa va a qué hora)."""
from datetime import timedelta
from pathlib import Path

from . import config as C

EXTENSIONES = (".mp3", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".flac", ".webm")

PROGRAMAS = {
    "general": {
        "nombre": "Radio Maxia Variada",
        "tema": "Programación musical variada, ambiente relajado y buena onda. Música para acompañar el día.",
        "generos": "éxitos de todas las épocas en español e inglés: pop, rock, baladas, música latina, clásicos y algo actual",
        "charla": False,       # False = solo comenta algunas canciones
        "noticias": True,
    },
    "leyendas_del_rock": {
        "nombre": "Leyendas del Rock",
        "tema": ("Programa de rock clásico, un homenaje al estilo de los programas de leyendas del rock: "
                 "biografías, datos curiosos y anécdotas de los grupos e intérpretes que escribieron la historia del rock. "
                 "Antes de cada canción Maxia cuenta algo del grupo o de la canción."),
        "generos": "rock clásico de los años 60, 70, 80 y 90 (en inglés), grupos y canciones que hicieron época",
        "charla": True,        # True = antes de cada canción cuenta una biografía/anécdota
        "noticias": False,     # durante este programa no hay noticiero
        # Rutas SIN extensión, relativas a radio_data/audio/programas/
        "intro": "leyendas_del_rock/intro",            # The Ides of March - Vehicle (instrumental)
        "intro_max_seg": 25,
        "fondos": ["leyendas_del_rock/fondo"],          # Booker T. & the MG's - Green Onions
        "cortinilla": "leyendas_del_rock/cortinilla",   # The Ventures - Pipeline (solo el inicio)
        "cortinilla_max_seg": 9,
    },
"virales_2016": {
        "nombre": "Virales del 2016",
        "tema": ("Programa nostálgico con los temas que sonaron en todos lados en 2016: "
                 "EDM, future bass, tropical house y pop electrónico. Maxia comenta con buena onda "
                 "datos curiosos y recuerdos de esa época, sin alargarse demasiado."),
        "generos": ("EDM y pop electrónico de 2016: future bass, tropical house, trap EDM, big room y electropop "
                    "(estilo Marshmello, The Chainsmokers, Kygo, Alan Walker, Zedd, Flume, Martin Garrix, Porter Robinson)"),
        "charla": True,       # solo comenta algunas canciones
        "noticias": False,     # sin noticiero durante el programa
    },
}

# (días 0=lunes … 6=domingo, o None = todos | inicio "HH:MM" | fin "HH:MM" | programa)
PARRILLA = [
    (None, "18:00", "23:00", "leyendas_del_rock"),
]


def programa_para(dt) -> str:
    minutos = dt.hour * 60 + dt.minute
    for dias, ini, fin, clave in PARRILLA:
        if dias is not None and dt.weekday() not in dias:
            continue
        h1, m1 = map(int, ini.split(":"))
        h2, m2 = map(int, fin.split(":"))
        if h1 * 60 + m1 <= minutos < h2 * 60 + m2:
            return clave
    return "general"


def proximo_cambio(dt):
    """Momento exacto (datetime) en que cambia el programa, o None si no hay cambio en 48 h."""
    actual = programa_para(dt)
    for m in range(1, 48 * 60 + 1):
        t = dt + timedelta(minutes=m)
        if programa_para(t) != actual:
            return t.replace(second=0, microsecond=0)
    return None


def buscar_audio(carpeta: Path, nombre: str):
    """Busca <carpeta>/<nombre>.<cualquier extensión de audio>. Devuelve la ruta o None."""
    base = Path(carpeta) / nombre
    for ext in EXTENSIONES:
        p = Path(str(base) + ext)
        if p.exists():
            return str(p)
    return None


def fondos_locales(clave: str) -> dict:
    """{nombre_visible: ruta} de los fondos instrumentales que hay en disco para este programa."""
    res = {}
    for nombre in PROGRAMAS.get(clave, {}).get("fondos", []):
        ruta = buscar_audio(C.DIR_PROGRAMAS, nombre)
        if ruta:
            res[Path(ruta).stem] = ruta
    if res:             # el programa tiene sus propios fondos: no se mezclan con los de la programación normal
        return res
    if C.DIR_FONDOS.exists():
        for p in sorted(C.DIR_FONDOS.iterdir()):
            if p.suffix.lower() in EXTENSIONES:
                res[p.stem] = str(p)
    return res