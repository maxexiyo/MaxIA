"""
Emisora de Radio Maxia: la línea de tiempo GLOBAL de la radio.

Hay una sola cola de segmentos (canciones y segmentos hablados). La emisora decide qué suena y cuándo;
cada servidor conectado (un player de Lavalink) solo obedece: cuando cambia el segmento, todos reciben
play(track) al mismo tiempo, y quien entra a media transmisión recibe play(track, start=segundos que lleva).
"""
import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from itertools import count
from typing import Optional

from . import config as C

logger = logging.getLogger(__name__)
_ids = count(1)


@dataclass
class Segmento:
    tipo: str                    # 'cancion' | 'voz'
    track: object                # wavelink.Playable (ya resuelto en Lavalink: así está "cargado" de antemano)
    duracion: float              # segundos
    meta: dict = field(default_factory=dict)   # titulo, artista, portada, url, pedida_por, noticia
    programa: Optional[str] = None
    etiqueta: str = ""
    id: int = field(default_factory=lambda: next(_ids))


class Emisora:
    def __init__(self, umbral_cola: int = C.COLA_OBJETIVO):
        self.cola: deque = deque()
        self.actual: Optional[Segmento] = None
        self.sesiones: dict = {}                 # guild_id -> player de Lavalink (algo con `async play(track, start=ms)`)
        self.umbral_cola = umbral_cola
        self.on_cola_baja = None                 # async fn(emisora): el director rellena la cola
        self.on_tick = None                      # fn(emisora): se llama en cada vuelta del bucle (revisiones rápidas)
        self.on_cambio = None                    # fn(segmento | None): para actualizar embeds
        self._t_inicio = 0.0
        self._corriendo = False
        self._tarea: Optional[asyncio.Task] = None
        self._tareas: set = set()
        self._rellenando = False
        self._no_antes_de = 0.0
        self._idle_avisado = False
        self._fin_real = False                   # TODOS los servidores confirmaron que el segmento actual terminó de sonar
        self._fines: set = set()                 # servidores que ya reportaron el fin del segmento actual

    # ── API ──
    @property
    def corriendo(self) -> bool:
        return self._corriendo

    def encolar(self, *segmentos) -> None:
        for s in segmentos:
            if s is not None:
                self.cola.append(s)

    def posicion(self) -> float:
        return max(0.0, time.monotonic() - self._t_inicio) if self.actual else 0.0

    async def iniciar(self) -> None:
        if self._corriendo:
            return
        self._corriendo = True
        self._tarea = asyncio.get_running_loop().create_task(self._bucle())
        logger.info("[emisora] al aire")

    async def detener(self) -> None:
        self._corriendo = False
        if self._tarea:
            self._tarea.cancel()
            try:
                await self._tarea
            except BaseException:
                pass
            self._tarea = None
        for gid, vc in list(self.sesiones.items()):
            try:
                await asyncio.wait_for(vc.stop(), C.PLAY_TIMEOUT)
            except Exception:
                pass
        self.actual = None
        self.cola.clear()
        if self._tareas:
            await asyncio.gather(*list(self._tareas), return_exceptions=True)
        logger.info("[emisora] fuera del aire")

    def notificar_fin(self, guild_id: int, encoded: str, razon: str) -> None:
        """Lo llama el cog con cada TrackEnd de Lavalink. Solo cuenta si es el track que suena ahora y terminó de
        verdad (finished): los 'replaced'/'stopped' son por nuestros propios play()/stop() y se ignoran."""
        a = self.actual
        if a is None or str(razon).lower() != "finished" or guild_id not in self.sesiones:
            return
        if getattr(a.track, "encoded", None) != encoded:
            return
        t = time.monotonic() - self._t_inicio
        if t < min(a.duracion * 0.5, 4.0):       # evento rezagado de un segmento anterior que usó el mismo track
            return
        # Se avanza hasta que TODOS los servidores terminaron: si un nodo va adelantado, no se corta al que va atrasado.
        # Si alguno nunca avisa (nodo caído), el respaldo por tiempo (duración + GRACIA_FIN_S) lo cubre.
        self._fines.add(guild_id)
        if self._fines >= set(self.sesiones):
            self._fin_real = True

    async def agregar_sesion(self, guild_id: int, vc) -> None:
        """Un servidor se conecta: entra a media transmisión, justo donde va la radio."""
        self.sesiones[guild_id] = vc
        if self.actual is not None:
            await self._tocar(guild_id, vc, self.actual, self.posicion())

    async def quitar_sesion(self, guild_id: int) -> None:
        self.sesiones.pop(guild_id, None)
        self._fines.discard(guild_id)
        # si el que se fue era el único que faltaba por terminar, ya se puede avanzar
        if self.actual is not None and self.sesiones and self._fines >= set(self.sesiones):
            self._fin_real = True

    def saltar_actual(self, seg=None, *, antes=None) -> None:
        """Da por terminado el segmento actual (p. ej. ningún nodo pudo tocarlo) para que la emisora pase al
        siguiente ya, sin esperar a que se cumpla su duración. Si se pasa `seg`, solo actúa si sigue siendo el actual.
        Si se pasa `antes` (un Segmento), ese suena primero (p. ej. la locutora comentando la falla)."""
        if self.actual is not None and (seg is None or seg is self.actual):
            if antes is not None:
                self.cola.appendleft(antes)
            self._fin_real = True

    async def resincronizar(self, guild_id: int) -> None:
        """Vuelve a poner a un servidor en lo que suena AHORA (después de un fallo de nodo, por ejemplo)."""
        vc = self.sesiones.get(guild_id)
        if vc is not None and self.actual is not None:
            await self._tocar(guild_id, vc, self.actual, self.posicion())

    def estado(self) -> dict:
        e = {"al_aire": self._corriendo, "tipo": "idle", "programa": None, "voz_activa": False,
             "titulo": None, "artista": None, "portada": None, "url": None, "pedida_por": None,
             "pos": 0.0, "dur": 0, "noticia": None, "siguiente": None, "oyentes": len(self.sesiones)}
        for s in self.cola:
            if s.tipo == "cancion":
                e["siguiente"] = f"{s.meta.get('titulo', '?')} — {s.meta.get('artista', '')}".strip(" —")
                break
        s = self.actual
        if s is None:
            return e
        e["programa"] = s.programa
        e["voz_activa"] = s.tipo == "voz"
        e["pos"], e["dur"] = self.posicion(), s.duracion
        if s.tipo == "cancion":
            m = s.meta
            e.update(tipo="cancion", titulo=m.get("titulo"), artista=m.get("artista"), portada=m.get("portada"),
                     url=m.get("url"), pedida_por=m.get("pedida_por"))
        elif s.meta.get("noticia"):
            e.update(tipo="noticia", noticia=s.meta["noticia"])
        else:
            e["tipo"] = "voz"
        return e

    # ── internos ──
    def _lanzar(self, coro):
        t = asyncio.get_running_loop().create_task(coro)
        self._tareas.add(t)
        t.add_done_callback(self._tareas.discard)
        return t

    def _avisar(self, seg) -> None:
        if self.on_cambio is None:
            return
        try:
            r = self.on_cambio(seg)
            if asyncio.iscoroutine(r):
                self._lanzar(r)
        except Exception:
            logger.exception("[emisora] error en on_cambio")

    async def _tocar(self, gid: int, vc, seg: Segmento, start_s: float) -> None:
        try:
            await asyncio.wait_for(vc.play(seg.track, start=int(max(0.0, start_s) * 1000), add_history=False), C.PLAY_TIMEOUT)
        except TypeError:        # players que no aceptan add_history
            await asyncio.wait_for(vc.play(seg.track, start=int(max(0.0, start_s) * 1000)), C.PLAY_TIMEOUT)
        except Exception as e:
            logger.warning(f"[emisora] servidor {gid}: no pude tocar «{seg.meta.get('titulo') or seg.etiqueta}»: {e!r}")

    async def _tocar_en_todos(self, seg: Segmento) -> None:
        await asyncio.gather(*(self._tocar(g, vc, seg, 0.0) for g, vc in list(self.sesiones.items())), return_exceptions=True)

    def _pedir_mas(self) -> None:
        if self.on_cola_baja is None or self._rellenando or time.monotonic() < self._no_antes_de:
            return
        self._rellenando = True

        async def _tarea():
            try:
                await self.on_cola_baja(self)
            except Exception:
                logger.exception("[emisora] error rellenando la cola")
            finally:
                self._rellenando = False
                self._no_antes_de = time.monotonic() + 2.0
        self._lanzar(_tarea())

    async def _avanzar(self) -> None:
        a = self.actual
        if a is not None:
            transcurrido = time.monotonic() - self._t_inicio
            # Termina cuando Lavalink lo confirma; si el aviso no llega, por tiempo (duración + gracia)
            limite = a.duracion + (C.GRACIA_FIN_S if self.sesiones else 0.0)
            if not self._fin_real and transcurrido < limite:
                return
            if not self.cola:                        # no hay nada más: que termine y quede en silencio
                if not self._idle_avisado:
                    self.actual, self._idle_avisado = None, True
                    self._avisar(None)
                return
        if self.cola:
            seg = self.cola.popleft()
            self.actual, self._t_inicio, self._idle_avisado = seg, time.monotonic(), False
            self._fin_real = False
            self._fines.clear()
            self._avisar(seg)
            await self._tocar_en_todos(seg)
            # el reloj arranca cuando YA se mandó a tocar (así cuenta lo que tarda Lavalink en abrir el stream)
            self._t_inicio = time.monotonic()
        elif not self._idle_avisado and a is None:
            self._idle_avisado = True
            self._avisar(None)

    async def _bucle(self):
        while self._corriendo:
            try:
                await self._avanzar()
                if self.on_tick is not None:
                    self.on_tick(self)
                if len(self.cola) < self.umbral_cola:
                    self._pedir_mas()
            except Exception:
                logger.exception("[emisora] error en el bucle")
            await asyncio.sleep(0.2)