"""vigilante.py — evita que un bloqueo (p. ej. un nodo de Lavalink que se cuelga) deje TODO el bot sin responder.

Qué hace
  1. LATIDO: una tarea del bot marca "sigo vivo" cada segundo.
  2. VIGILANTE (hilo aparte, no depende del loop del bot): si el latido se detiene más de AVISO_S segundos escribe en
     el log EXACTAMENTE qué línea de código está ejecutando el bot en ese momento (así se encuentra al culpable).
     Si pasan LIMITE_S segundos, guarda un volcado en vigilante_volcado.txt y cierra el proceso con código 3.
  3. DISCORD: si la conexión con Discord lleva más de GATEWAY_S segundos sin latido válido (bot.latency inválida),
     escribe las tareas pendientes y cierra el proceso con código 4.
  El cierre solo sirve si algo vuelve a arrancar el bot: usa run_bot.sh (o pm2/systemd).

Uso (una sola vez, dentro del loop; es idempotente):
    import vigilante
    ...
    async def setup_hook(self):          # o en on_ready
        await vigilante.iniciar(self)

Variables de entorno (opcionales):
  VIGILANTE_AVISO_S=8        a los cuántos segundos congelado avisa y muestra dónde está trabado
  VIGILANTE_LIMITE_S=60      a los cuántos segundos congelado cierra el proceso (para que lo reinicien)
  VIGILANTE_GATEWAY_S=180    segundos sin conexión válida con Discord antes de reiniciar (0 = no vigilar)
  VIGILANTE_REINICIAR=0      solo avisar y volcar, sin cerrar el proceso
  VIGILANTE_VOLCADO=ruta     archivo del volcado (por defecto vigilante_volcado.txt)
  VIGILANTE_DEBUG_LOOP=1     DIAGNÓSTICO: asyncio avisa de cada llamada que bloquee más de 1 s (consume más CPU)
"""
import asyncio
import faulthandler
import logging
import os
import sys
import threading
import time
import traceback

logger = logging.getLogger(__name__)

AVISO_S = float(os.getenv("VIGILANTE_AVISO_S", "8"))
LIMITE_S = float(os.getenv("VIGILANTE_LIMITE_S", "60"))
GATEWAY_S = float(os.getenv("VIGILANTE_GATEWAY_S", "180"))
REINICIAR = os.getenv("VIGILANTE_REINICIAR", "1") != "0"
ARCHIVO_VOLCADO = os.getenv("VIGILANTE_VOLCADO", "vigilante_volcado.txt")
DEBUG_LOOP = os.getenv("VIGILANTE_DEBUG_LOOP", "0") == "1"

_ultimo = time.monotonic()
_hilo_loop = None
_iniciado = False
_tarea = None


def _stack_loop() -> str:
    """Pila de llamadas del hilo del bot AHORA MISMO (sirve aunque el loop esté bloqueado)."""
    marco = sys._current_frames().get(_hilo_loop)
    return "".join(traceback.format_stack(marco)) if marco else "(no pude leer el hilo del bot)"


def volcar_tareas(limite: int = 40) -> str:
    """Tareas asyncio pendientes y dónde está cada una. Debe llamarse DENTRO del loop."""
    filas = []
    for t in list(asyncio.all_tasks())[:limite]:
        marco = t.get_stack(limit=1)
        donde = f"{os.path.basename(marco[0].f_code.co_filename)}:{marco[0].f_lineno}" if marco else "?"
        filas.append(f"  - {t.get_name()} -> {donde}")
    return "\n".join(filas) or "  (ninguna)"


def _volcar_a_archivo(motivo: str):
    try:
        with open(ARCHIVO_VOLCADO, "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {motivo} =====\n")
            f.flush()
            faulthandler.dump_traceback(file=f, all_threads=True)
    except Exception as e:
        logger.error(f"[vigilante] no pude escribir el volcado: {e!r}")


def _cerrar(codigo: int, motivo: str):
    _volcar_a_archivo(motivo)
    if REINICIAR:
        logger.critical(f"[vigilante] cerrando el proceso (código {codigo}) para que se reinicie. Volcado en {ARCHIVO_VOLCADO}")
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass
        os._exit(codigo)      # a propósito: un proceso congelado no puede cerrarse "bonito"


def _vigilar():
    avisado = volcado = False
    while True:
        time.sleep(2)
        parado = time.monotonic() - _ultimo
        if parado <= AVISO_S:
            avisado = volcado = False
            continue
        if not avisado:
            avisado = True
            logger.error(f"[vigilante] el bot lleva {parado:.0f}s sin responder. Está ejecutando esto AHORA:\n{_stack_loop()}")
        if parado > LIMITE_S and not volcado:
            volcado = True
            logger.critical(f"[vigilante] el bot lleva {parado:.0f}s congelado")
            _cerrar(3, f"bot congelado {parado:.0f}s")


async def _latido(bot):
    global _ultimo
    sin_discord = 0.0
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(1)
        ahora = time.monotonic()
        _ultimo = ahora
        retraso = ahora - t0 - 1
        if retraso > 2:
            logger.warning(f"[vigilante] el bot se trabó {retraso:.1f}s y ya volvió")
        if bot is not None and GATEWAY_S > 0:
            try:
                valida = bot.is_ready() is False or bot.latency < 120      # nan / inf / enorme = sin latido de Discord
            except Exception:
                valida = True
            sin_discord = 0.0 if valida else sin_discord + (ahora - t0)
            if sin_discord > GATEWAY_S:
                logger.critical(f"[vigilante] {sin_discord:.0f}s sin latido válido de Discord. Tareas pendientes:\n{volcar_tareas()}")
                _cerrar(4, f"sin latido de Discord {sin_discord:.0f}s\n{volcar_tareas()}")
                sin_discord = 0.0


async def iniciar(bot=None):
    """Arranca el latido y el hilo vigilante. Llamar una sola vez desde dentro del loop del bot."""
    global _hilo_loop, _iniciado, _ultimo, _tarea
    if _iniciado:
        return
    _iniciado = True
    loop = asyncio.get_running_loop()
    _hilo_loop = threading.get_ident()
    _ultimo = time.monotonic()
    if DEBUG_LOOP:
        loop.set_debug(True)
        loop.slow_callback_duration = 1.0
    _tarea = asyncio.ensure_future(_latido(bot))
    threading.Thread(target=_vigilar, name="vigilante", daemon=True).start()
    logger.info(f"[vigilante] activo: aviso a {AVISO_S:.0f}s, reinicio a {LIMITE_S:.0f}s, discord {GATEWAY_S:.0f}s, "
                f"reiniciar={'sí' if REINICIAR else 'no'}")
