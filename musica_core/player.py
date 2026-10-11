import asyncio
import logging
import wavelink

logger = logging.getLogger(__name__)

_bg_tasks = set()


def _fire(coro):
    """create_task que NO se traga los errores en silencio (antes pause/resume/stop fallaban sin avisar)."""
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
    el código de comandos/vistas que ya tenías (permisos, botones, colas) no tuvo que tocarse."""

    def is_playing(self) -> bool:
        return bool(self.playing) and not self.paused

    def is_paused(self) -> bool:
        return bool(self.paused)

    def pause(self):
        _fire(super().pause(True))

    def resume(self):
        _fire(super().pause(False))

    def stop(self):
        _fire(super().stop())
