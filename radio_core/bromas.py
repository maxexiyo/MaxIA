"""Chistes para cuando una canción no se puede reproducir (musica_core/bromas.py).
Los usan el cog de música y el de radio: así el chat nunca muestra errores técnicos de Lavalink."""
import asyncio
import logging
import random

logger = logging.getLogger(__name__)

BROMAS_FALLO = [
    "🎻 **{titulo}** se negó a sonar. Dice que hoy no tiene ganas de trabajar.",
    "🔇 **{titulo}** se hizo la sorda. Qué carácter.",
    "🕵️ **{titulo}** desapareció del servidor sin dejar rastro. Caso cerrado.",
    "🚫 YouTube dijo que **{titulo}** \"no está disponible\". Así me dicen a mí en las fiestas.",
    "💀 **{titulo}** murió en el intento. Un minuto de silencio (que, de hecho, es lo que está pasando).",
    "🎤 **{titulo}** le dio miedo escénico y se escondió detrás de un nodo.",
    "🔌 Le pedí **{titulo}** al nodo y me respondió con un suspiro.",
    "🙈 **{titulo}** vio la fila y le dio pereza cargar.",
    "📻 Error 404: **{titulo}** salió por cigarros y no ha vuelto.",
    "🧙 **{titulo}** lanzó \"invisibilidad\" y le salió demasiado bien.",
]

_ultima = None


def broma_fallo(titulo: str) -> str:
    """Devuelve un chiste con el título metido (sin repetir el último que salió)."""
    global _ultima
    opciones = [b for b in BROMAS_FALLO if b != _ultima] or BROMAS_FALLO
    _ultima = random.choice(opciones)
    return _ultima.format(titulo=(titulo or "la canción")[:80])


# ── Chistes hechos por la IA (usa el mismo cliente de la radio: Groq → Gemini → OpenRouter con tus keys) ──
TIMEOUT_IA = 7    # seg. máximos esperando a la IA; si tarda o falla se usa un chiste de la lista de arriba
ESTILOS = ["sarcástico", "dramático como telenovela", "como narrador de fútbol", "como si el bot estuviera ofendido",
           "como horóscopo", "como noticiero de última hora", "como mesero que no encuentra el pedido",
           "con humor absurdo", "como si la canción tuviera vida propia"]
SISTEMA_BROMA = ("Eres MaxIA, un bot de música de Discord con humor ligero y juguetón, en español mexicano casual. "
                 "Respondes SOLO con el chiste: una sola frase corta, sin comillas, sin explicaciones.")


async def broma_fallo_ia(titulo: str, artista: str = "") -> str:
    """Chiste generado por la IA sobre la canción que no se pudo reproducir. Si la IA no responde a tiempo
    (o no hay keys), devuelve uno de la lista fija: nunca lanza excepciones ni deja al bot sin mensaje."""
    titulo = (titulo or "la canción")[:80]
    try:
        from radio_core.ia import pedir_ia       # import tardío: si la radio no está, simplemente se usa la lista
        prompt = (f"No se pudo reproducir la canción «{titulo}»" + (f" de {artista[:60]}" if artista else "") + ". "
                  f"Escribe UN chiste corto (máximo 150 caracteres), estilo {random.choice(ESTILOS)}, sobre esa falla. "
                  f"Empieza con un emoji, pon el título en **negritas** y no menciones errores técnicos, nodos ni servidores. "
                  f"Nada ofensivo.")
        txt = await asyncio.wait_for(pedir_ia(prompt, SISTEMA_BROMA, max_tokens=120), timeout=TIMEOUT_IA)
        txt = " ".join((txt or "").split()).strip(' "«»')
        txt = txt.replace("@", "@\u200b")        # nada de menciones (@everyone, @usuario)
        if 8 <= len(txt) <= 300:
            return txt
    except Exception as e:
        logger.debug(f"La IA no pudo hacer el chiste ({type(e).__name__}); se usa uno de la lista")
    return broma_fallo(titulo)