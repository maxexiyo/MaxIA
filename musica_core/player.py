import asyncio
import logging
import wavelink

logger = logging.getLogger(__name__)

_bg_tasks = set()


def _fire(coro):
    """create_task que NO se traga los errores en silencio (antes pause/resume/stop fallaban sin avisar).
    Devuelve la tarea: quien quiera esperar el resultado puede hacer `await`, y quien no (código viejo) la ignora."""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)

    def _done(t):
        _bg_tasks.discard(t)
        if not t.cancelled() and t.exception():
            logger.error(f"Tarea en segundo plano falló: {t.exception()!r}")

    task.add_done_callback(_done)
    return task


class _SinCanalDeVoz(Exception):
    """El bot ya no está en un canal de voz (lo desconectaron a media carga)."""


class MusicPlayer(wavelink.Player):
    """Extiende wavelink.Player para que se comporte igual que el discord.VoiceClient de antes
    (is_playing(), is_paused(), pause(), resume(), stop() como métodos síncronos), así casi todo
    el código de comandos/vistas que ya tenías (permisos, botones, colas) no tuvo que tocarse.

    pause/resume/stop se pueden usar de las DOS formas:
      vc.pause()          -> como antes: se dispara en segundo plano y no hay que esperar nada
      await vc.pause(x)   -> como lo llama wavelink por dentro (switch_node hace `await self.pause(self.paused)`)
    Por eso devuelven la tarea (awaitable) en vez de None."""

    def is_playing(self) -> bool:
        return bool(self.playing) and not self.paused

    def is_paused(self) -> bool:
        return bool(self.paused)

    def pause(self, value: bool = True, /):
        # sin argumento = pausar (uso viejo); con argumento = el valor que manda wavelink (True pausa, False reanuda)
        return _fire(super().pause(value))

    def resume(self):
        return _fire(super().pause(False))

    def stop(self, *, force: bool = True):
        return _fire(super().stop(force=force))