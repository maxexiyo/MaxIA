"""Elegir el mejor resultado de una búsqueda de Lavalink para una canción (versión original, no covers ni en vivo)."""
import re

from . import config as C

_NO_DESEADOS = ("cover", "live", "en vivo", "karaoke", "remix", "8d", "slowed", "reverb", "nightcore", "sped up",
                "instrumental", "tutorial", "reaction", "lyrics video", "letra", "bass boosted", "concert", "acoustic")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (s or "").lower())).strip()


def elegir_mejor(resultados, artista: str, titulo: str):
    """Devuelve el mejor candidato (objetos con .title .author .length en ms) o None.
    Descarta clips muy cortos/largos, covers, en vivo, remixes... salvo que la petición los pida."""
    pedido = _norm(f"{artista} {titulo}")
    pistas = _norm(titulo).split()
    mejor, mejor_p = None, -1e9
    for t in resultados or []:
        seg = (getattr(t, "length", 0) or 0) / 1000
        if getattr(t, "is_stream", False) or not (C.MIN_DUR_CANCION <= seg <= C.MAX_DUR_CANCION):
            continue
        texto = _norm(f"{getattr(t, 'title', '')} {getattr(t, 'author', '')}")
        p = 0.0
        for mala in _NO_DESEADOS:
            if mala in texto and mala not in pedido:
                p -= 10
        p += 3 * sum(1 for w in pistas if w in texto) / max(1, len(pistas))        # el título coincide
        if any(w in texto for w in _norm(artista).split()):                         # el artista coincide
            p += 3
        if "topic" in _norm(getattr(t, "author", "")) or "official" in texto:        # audio oficial
            p += 1
        if p > mejor_p:
            mejor, mejor_p = t, p
    return mejor if mejor_p > -5 else None
