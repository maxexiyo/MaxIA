"""
Radio Maxia (versión Lavalink) — cog de Discord.

Las canciones salen por tus nodos de Lavalink (nodos.py: búsqueda con fallback, salud, MusicPlayer).
Los segmentos hablados (intro + voz de Maxia + fondo) se mezclan en tu equipo con ffmpeg, se suben a un canal
privado de Discord y Lavalink los reproduce por URL como cualquier otro track.

Comandos:
  !radio          → prende/apaga la radio en tu canal de voz
  !msjrmaxia ...  → saludo/petición (con o sin imagen) para que Maxia lo lea al aire
  !radiodiag      → (solo dueño del bot) revisa que todo lo necesario funcione
"""
import asyncio
import colorsys
import io
import json
import logging
import hashlib
import shutil
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional

import aiohttp
import discord
import wavelink
from discord.ext import commands, tasks

from musica_core.nodos import (buscar_tracks_con_fallback, conectar_player, get_nodos_conectados, limpiar_voz,
                               es_nodo_prioritario, marcar_fallo_prioritario, limpiar_fallo_prioritario, prioritario_enfriando)
from radio_core import config as C
from radio_core.busqueda_web import WEB_ACTIVA, buscar_info, exitos_del_pais
from radio_core.director import Director
from radio_core.emisora import Emisora
from radio_core.fuentes import elegir_mejor
from radio_core.programas import PROGRAMAS
from radio_core.ia import OPENROUTER_MODEL, _con_openrouter, claves_openrouter
from radio_core.voz import LOCAL_PRIMERO, MODELO_OR_DEFECTO, OR_TTS_MODELO, OR_TTS_VOZ, VOZ, VOZ_GEMINI_DEFECTO, limpiar_para_voz, sintetizar, sintetizar_openrouter, sintetizar_local

logger = logging.getLogger(__name__)
BOLITA = "🔘"
# Imagen del embed cuando NO suena una canción (voz de Maxia, noticiero, anuncios). Pega la URL de la imagen de cada programa
# (clave = la de programas.py; "noticiero" = durante las noticias). Vacío = se usa el logo (C.LOGO_URL) como miniatura.
IMAGENES_PROGRAMA: dict = {
    "general": "",
    "noticiero": "",
    "leyendas_del_rock": "",
    "virales_2016": "",
    "sonidos_de_america": "",
    "cine_y_soundtracks": "",
    "madrugada": "",
    "videojuegos_ost": "",
    "cumbias": "",
    "romanticas_de_siempre": "",
}
MAX_NODOS_POR_SEGMENTO = 3   # nodos distintos (contando el original) que se prueban con un segmento que falla


@dataclass
class Sesion:
    guild_id: int
    vc: wavelink.Player
    canal_texto: discord.abc.Messageable
    mensaje: Optional[discord.Message] = None
    ultimo: str = ""


def _portada_de(t):
    """Portada del track: la que da Lavalink; si no trae, la miniatura de YouTube a partir del id del video."""
    art = getattr(t, "artwork", None)
    if art:
        return art
    ident = getattr(t, "identifier", None)
    if ident and "youtube" in str(getattr(t, "source", "") or "").lower():
        return f"https://i.ytimg.com/vi/{ident}/hqdefault.jpg"
    return None


def color_dominante(datos: bytes):
    """(r, g, b) del color más representativo de una imagen: el tono con más presencia entre los colores vivos
    (ignora negros y grises), con saturación/brillo ajustados para que se vea bien en el embed. Necesita Pillow."""
    from PIL import Image
    img = Image.open(io.BytesIO(datos)).convert("RGB")
    img.thumbnail((64, 64))
    cubos, suma = {}, [0, 0, 0, 0]
    for r, g, b in img.getdata():
        suma[0] += r; suma[1] += g; suma[2] += b; suma[3] += 1
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if v < 0.2 or s < 0.18:
            continue
        peso = s * (0.4 + v)
        c = cubos.setdefault(int(h * 18) % 18, [0.0, 0.0, 0.0, 0.0])
        c[0] += r * peso; c[1] += g * peso; c[2] += b * peso; c[3] += peso
    if cubos:
        c = max(cubos.values(), key=lambda x: x[3])
        r, g, b = c[0] / c[3], c[1] / c[3], c[2] / c[3]
    else:                                   # portada en blanco y negro: el promedio
        n = max(1, suma[3])
        r, g, b = suma[0] / n, suma[1] / n, suma[2] / n
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    if cubos:
        s = max(s, 0.45)
    v = min(max(v, 0.55), 0.95)
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


def barra_progreso(pos: float, dur: float) -> str:
    if not dur or dur <= 0:
        return "🔴 ▬▬▬▬▬▬▬▬▬▬▬▬▬ [EN VIVO]"
    barras = 13
    lleno = int(barras * min(max(pos / dur, 0), 1))
    vacio = max(0, barras - lleno - 1)
    m1, s1 = divmod(int(pos), 60)
    m2, s2 = divmod(int(dur), 60)
    return f"{'▬' * lleno}{BOLITA}{'▬' * vacio}\n`{m1}:{s1:02d} / {m2}:{s2:02d}`"


def _lista(res) -> list:
    if not res:
        return []
    return list(res.tracks) if isinstance(res, wavelink.Playlist) else list(res)


class Radio(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Los comandos sueltos (!radio, !msjrmaxia) solo se registran si ese nombre está libre en tu bot. Si ya existe
        # (otro cog, una radio anterior...), se omiten sin error y se usan colgados de tu grupo: !max radio / !max msjrmaxia.
        libres = []
        for cmd in self.__cog_commands__:
            nombres = [cmd.name, *cmd.aliases]
            if any(bot.get_command(n) is not None for n in nombres):
                logger.warning(f"[radio] ya existe un comando «{cmd.name}» en el bot; no registro el mío "
                               f"(úsalo con tu grupo: !max {cmd.name}, ver README sección 7)")
            else:
                libres.append(cmd)
        self.__cog_commands__ = tuple(libres)
        self.emisora = Emisora()
        # La voz usa tu cog TTS (Gemini con sus keys y cupos, y edge-tts de respaldo). Si quieres usar TU módulo de IA
        # para los textos: Director(..., ia=mi_funcion_ia)  con  ia(prompt, sistema, *, json_mode=False, max_tokens=900) -> str
        self.director = Director(self.emisora, buscar_cancion=self._buscar_cancion, publicar=self._publicar, tts=self._tts,
                                 buscar_web=self._buscar_web, exitos=self._exitos)
        self.emisora.on_cola_baja = self.director.rellenar
        self.emisora.on_tick = self.director.revisar_programa      # cambio de programa: tira lo que ya estaba en cola
        self.emisora.on_cambio = self._on_cambio
        self.sesiones: dict[int, Sesion] = {}
        self._subidos: deque = deque()          # (mensaje, timestamp) de los archivos subidos
        self._vacio: dict[int, asyncio.Task] = {}
        self._refrescando = False
        self._recuperando: set = set()          # servidores cuya voz se está reconstruyendo ahora mismo
        self._voz_mala: dict = {}               # gid -> revisiones seguidas con la voz muerta
        self._ult_recup: dict = {}              # gid -> time.monotonic() de la última recuperación
        self._sucio = False                     # hubo un cambio mientras se refrescaba: repetir para no perderlo
        self._colores: dict = {}                # url de imagen -> (r, g, b) | None
        self._colores_en_curso: dict = {}
        self._info_nodos: dict = {}             # identifier -> (timestamp, acepta_http)
        self._resync_visto: dict = {}
        self._fallas_vistas: set = set()        # ids de segmentos fallidos ya comentados al aire (no repetir el chiste)
        self._aviso_canal = False
        self._vigilante: Optional[asyncio.Task] = None

    async def cog_load(self):
        self._refrescar.start()
        self._vigilante = asyncio.get_running_loop().create_task(self._vigilar_loop())
        if C.SIEMPRE_AL_AIRE:
            await self.emisora.iniciar()

    async def cog_unload(self):
        self._refrescar.cancel()
        if self._vigilante:
            self._vigilante.cancel()
        for gid in list(self.sesiones):
            await self.desactivar(gid, aviso=False)
        await self.emisora.detener()

    async def _vigilar_loop(self):
        """Diagnóstico de lag: avisa en el log cuando el event loop del bot se queda 'dormido' más de 0.3 s (algo
        síncrono lo bloquea). Si ese aviso sale justo cuando se traba el audio, el problema es el bot y no el nodo."""
        n = 0
        while True:
            t = time.monotonic()
            await asyncio.sleep(1)
            retraso = time.monotonic() - t - 1
            if retraso > 0.3:
                logger.warning(f"[radio] el event loop se atrasó {retraso:.2f}s")
            n += 1
            if n % 10 == 0:
                try:
                    self._revisar_voz()
                except Exception:
                    logger.exception("[radio] error revisando la conexión de voz")

    # ═══ API pública (para colgarla de !max) ═══════════════════════════════════════
    def activa_en(self, guild_id: int) -> bool:
        return guild_id in self.sesiones

    async def toggle_radio(self, ctx: commands.Context):
        if self.activa_en(ctx.guild.id):
            await self.desactivar(ctx.guild.id)
            return await ctx.send("📻 Radio apagada. ¡Hasta la próxima!")
        await self.activar(ctx)

    async def enviar_mensaje(self, ctx: commands.Context, texto: str):
        if not self.emisora.corriendo:
            return await ctx.send("📻 La radio no está al aire ahorita. Prende `!radio` en un canal de voz primero.")
        img = next((a.url for a in ctx.message.attachments if (a.content_type or "").startswith("image/")), None)
        if not texto.strip() and not img:
            return await ctx.send("Escribe tu mensaje o petición, por ejemplo: `!msjrmaxia saludos a mi banda, ponme algo de Soda Stereo`")
        await self.director.agregar_mensaje(ctx.author.display_name, texto, img)
        await ctx.message.add_reaction("📻")
        await ctx.send(f"✅ Listo {ctx.author.mention}, Maxia lo leerá al aire en cuanto pueda.", delete_after=15)

    # ═══ comandos sueltos ══════════════════════════════════════════════════════════
    @commands.command(name="radio")
    @commands.guild_only()
    async def cmd_radio(self, ctx):
        await self.toggle_radio(ctx)

    @commands.command(name="msjrmaxia", aliases=["mensajemaxia"])
    @commands.guild_only()
    @commands.cooldown(1, 45, commands.BucketType.user)
    async def cmd_msj(self, ctx, *, texto: str = ""):
        await self.enviar_mensaje(ctx, texto)

    @commands.command(name="radiodiag")
    @commands.is_owner()
    async def cmd_diag(self, ctx):
        await self.diagnostico(ctx)

    @commands.command(name="radionodo")
    @commands.is_owner()
    async def cmd_nodo(self, ctx):
        """Muestra en qué nodo está cada servidor de la radio y cómo anda cada nodo conectado."""
        lineas = []
        for gid, ses in self.sesiones.items():
            vc = getattr(ses, "vc", None)
            nodo = getattr(vc, "node", None)
            lineas.append(f"• servidor `{gid}` → nodo **{getattr(nodo, 'identifier', '?')}**")
        if not lineas:
            lineas.append("• no hay servidores con la radio prendida")
        nodos = list(get_nodos_conectados())
        medidas = await asyncio.gather(*(self._medir_nodo(n) for n in nodos)) if nodos else []
        lineas.append("")
        for n, (pen, resumen) in sorted(zip(nodos, medidas), key=lambda x: x[1][0]):
            lineas.append(f"`{n.identifier}` ({getattr(n, 'uri', '?')}) — {resumen}")
        await ctx.send("\n".join(lineas)[:1900])

    # ═══ IA con búsqueda web (la de tu bot) ═══════════════════════════════════════
    def _motor_web(self):
        """El cog de tu bot que trae BusquedaWebMixin (search_duckduckgo), para compartir su caché, SearXNG y keys."""
        for cog in self.bot.cogs.values():
            if cog is not self and hasattr(cog, "search_duckduckgo"):
                return cog
        return None

    async def _buscar_web(self, consulta: str):
        """Texto con lo encontrado en internet sobre `consulta`, o None. Usa la búsqueda de tu IA; si no la encuentra, la propia."""
        if not WEB_ACTIVA or not consulta:
            return None
        motor = self._motor_web()
        if motor is None:
            return await buscar_info(consulta)
        try:
            res = await asyncio.wait_for(motor.search_duckduckgo(consulta), 45)
        except Exception as e:
            logger.debug(f"[radio] búsqueda web falló: {e!r}")
            return None
        return res if res and not str(res).startswith("Error en búsqueda web") else None

    async def _exitos(self, pais: str = "mx", cantidad: int = 25) -> list:
        motor = self._motor_web()
        if motor is None or not hasattr(motor, "obtener_exitos"):
            return await exitos_del_pais(pais, cantidad)
        try:
            return [{"artista": x["artista"], "titulo": x["titulo"]} for x in await motor.obtener_exitos(pais, cantidad)]
        except Exception:
            return []

    # ═══ Lavalink: canciones y segmentos hablados ═════════════════════════════════
    async def _buscar_cancion(self, artista: str, titulo: str):
        for prefijo in ("ytmsearch:", "ytsearch:"):
            try:
                res = await buscar_tracks_con_fallback(f"{prefijo}{artista} {titulo}")
            except Exception as e:
                logger.warning(f"[radio] búsqueda falló ({prefijo}): {e!r}")
                continue
            t = elegir_mejor(_lista(res), artista, titulo)
            if t is not None:
                return {"track": t, "titulo": titulo, "artista": artista, "duracion": (t.length or 0) / 1000,
                        "portada": _portada_de(t), "url": getattr(t, "uri", None)}
        return None

    def _canal_archivos(self):
        return self.bot.get_channel(C.CANAL_ARCHIVOS_ID) if C.CANAL_ARCHIVOS_ID else None

    async def _publicar(self, ruta: str):
        """Sube el mp3 al canal privado y lo resuelve en Lavalink por su URL. Devuelve el track o None."""
        canal = self._canal_archivos()
        if canal is None:
            if not self._aviso_canal:
                self._aviso_canal = True
                logger.error("[radio] Falta RADIO_CANAL_ARCHIVOS (ID de un canal privado para subir los audios).")
            return None
        try:
            msg = await canal.send(file=discord.File(ruta))
        except Exception as e:
            logger.warning(f"[radio] no pude subir {ruta}: {e!r}")
            return None
        self._subidos.append((msg, time.time()))
        return await self._resolver_url(msg.attachments[0].url)

    async def _resolver_url(self, url: str):
        """Pide a los nodos que aceptan http que lean el archivo. Se hace aquí (y no con buscar_tracks_con_fallback)
        para que un 403/429 del CDN de Discord no marque como 'bloqueados por YouTube' a tus nodos."""
        for nodo in get_nodos_conectados():
            if not await self._acepta_http(nodo):
                continue
            try:
                res = await asyncio.wait_for(wavelink.Pool.fetch_tracks(url, node=nodo), timeout=10)
            except Exception as e:
                logger.warning(f"[radio] el nodo {getattr(nodo, 'identifier', '?')} no pudo leer el archivo: {e!r}")
                continue
            pistas = _lista(res)
            if pistas:
                return pistas[0]
        return None

    # ═══ voz de Maxia: tu cog TTS ═════════════════════════════════════════════════
    def _cfg_gemini(self) -> dict:
        """Modelo, voz y estilo del Gemini TTS de tu cog TTS (GEMINI_TTS_MODEL / GEMINI_TTS_VOICE / GEMINI_STYLE), para que
        el respaldo de OpenRouter suene igual. Lo que no encuentre queda vacío y se usan los valores por defecto."""
        tts = self.bot.get_cog("TTS")
        mod = sys.modules.get(type(tts).__module__) if tts is not None else None

        def buscar(nombres):
            for obj in (mod, tts):
                for n in nombres:
                    v = getattr(obj, n, None) if obj is not None else None
                    if isinstance(v, str) and v.strip():
                        return v.strip()
            return ""
        modelo = buscar(("GEMINI_TTS_MODEL",))
        if modelo and "/" not in modelo:
            modelo = "google/" + modelo
        return {"voz": buscar(("GEMINI_TTS_VOICE", "GEMINI_VOICE")), "estilo": buscar(("GEMINI_STYLE", "GEMINI_TTS_STYLE")), "modelo": modelo}

    async def _tts(self, texto: str):
        """Texto → ruta de audio. Gemini TTS de tu cog (esperando cupo) → Gemini vía OpenRouter → edge-tts → Piper/Kokoro
        (los locales solo como último recurso; con RADIO_TTS_LOCAL_PRIMERO=1 van antes de OpenRouter y edge-tts)."""
        limpio = limpiar_para_voz(texto)
        if len(limpio) < 2:
            return None
        tts = self.bot.get_cog("TTS")
        if tts is None:
            return await sintetizar(texto)            # sin cog TTS: OpenRouter (Gemini) → Piper → Kokoro → edge-tts
        mod = sys.modules.get(type(tts).__module__)
        h = hashlib.sha1(f"{C.TTS_MOTOR}|{limpio}".encode()).hexdigest()[:16]
        if C.TTS_MOTOR in ("auto", "gemini") and getattr(mod, "GEMINI_KEYS", None):
            ruta = C.DIR_CACHE / f"voz_g_{h}.wav"
            if ruta.exists() and ruta.stat().st_size > 1000:
                return str(ruta)
            sin_cupo = getattr(mod, "GeminiNoCapacity", ())
            es_limite = getattr(mod, "_is_rate_limit", lambda e: False)
            for intento in range(C.TTS_ESPERAS_CUPO + 1):
                try:
                    await asyncio.wait_for(tts._gemini_tts(limpio, ruta), timeout=60)
                    if ruta.exists() and ruta.stat().st_size > 1000:
                        return str(ruta)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    ruta.unlink(missing_ok=True)
                    if (isinstance(e, sin_cupo) or es_limite(e)) and intento < C.TTS_ESPERAS_CUPO:
                        logger.info(f"[radio] Gemini TTS sin cupo; espero {C.TTS_ESPERA_S}s ({intento + 1}/{C.TTS_ESPERAS_CUPO})")
                        await asyncio.sleep(C.TTS_ESPERA_S)
                        continue
                    logger.warning(f"[radio] Gemini TTS falló ({type(e).__name__}: {e}); uso edge-tts")
                break
        if LOCAL_PRIMERO:
            ruta_l = await sintetizar_local(texto)    # orden anterior: Piper → Kokoro (locales, sin cupo)
            if ruta_l:
                return ruta_l
        if C.TTS_MOTOR in ("auto", "openrouter"):
            ruta_or = await sintetizar_openrouter(texto, **self._cfg_gemini())   # respaldo: mismo modelo/voz/estilo Gemini vía OpenRouter
            if ruta_or:
                return ruta_or
        ruta = C.DIR_CACHE / f"voz_e_{h}.mp3"
        if ruta.exists() and ruta.stat().st_size > 1000:
            return str(ruta)
        try:
            if await tts._edge_tts(limpio, VOZ, ruta):
                return str(ruta)
        except Exception as e:
            logger.warning(f"[radio] edge-tts del cog TTS falló: {e!r}")
        if not LOCAL_PRIMERO:                         # último recurso: voces locales (suenan más robóticas)
            ruta_l = await sintetizar_local(texto)
            if ruta_l:
                return ruta_l
        return await sintetizar(texto, openrouter=False, piper=False)

    async def _acepta_http(self, nodo) -> bool:
        c = self._info_nodos.get(nodo.identifier)
        if c and time.time() - c[0] < 600:
            return c[1]
        try:
            info = await asyncio.wait_for(nodo.fetch_info(), 6)
            ok = "http" in [s.lower() for s in info.source_managers]
        except Exception:
            ok = es_nodo_prioritario(nodo)      # kasawa: se intenta aunque no responda la info; solo un error real lo descarta
        self._info_nodos[nodo.identifier] = (time.time(), ok)
        return ok

    async def _medir_nodo(self, nodo):
        """(penalización, resumen) de un nodo: latencia de su API + carga + frames perdidos + oyentes. Menor = mejor."""
        t0 = time.monotonic()
        try:
            st = await asyncio.wait_for(nodo.fetch_stats(), 6)
        except Exception as e:
            return 1e9, f"sin stats ({type(e).__name__})"
        ms = (time.monotonic() - t0) * 1000
        cpu = getattr(getattr(st, "cpu", None), "lavalink_load", 0.0) or 0.0
        fs = getattr(st, "frame_stats", None)
        deficit = getattr(fs, "deficit", 0) or 0
        nulos = getattr(fs, "nulled", 0) or 0
        jugando = getattr(st, "playing_players", 0) or 0
        pen = ms + cpu * 200 + (deficit + nulos) * 2 + jugando * 5
        return pen, f"{ms:.0f} ms · CPU {cpu * 100:.0f}% · reproduciendo {jugando} · déficit {deficit} · nulos {nulos}"

    async def _nodo_con_http(self):
        """El MEJOR nodo conectado que acepta archivos por URL (menos latencia, carga y frames perdidos).
        Antes devolvía el primero de la lista, que podía ser justo el nodo saturado."""
        nodos = [n for n in get_nodos_conectados() if await self._acepta_http(n)]
        if not nodos:
            return None
        if es_nodo_prioritario(nodos[0]):        # kasawa siempre primero; los demás son respaldo
            return nodos[0]
        medidas = await asyncio.gather(*(self._medir_nodo(n) for n in nodos))
        return min(zip(nodos, medidas), key=lambda x: x[1][0])[0]

    # ═══ voz ═══════════════════════════════════════════════════════════════════════
    async def _preparar_player(self, ctx, canal: discord.VoiceChannel) -> wavelink.Player:
        guild = ctx.guild
        vc = guild.voice_client
        music = self.bot.get_cog("Music") or self.bot.get_cog("Musica")
        if music is not None:                      # la radio toma el canal: que la música normal se calle
            try:
                music._cancel_update_task(guild.id)
                music.current[guild.id] = None
            except Exception:
                pass
        if isinstance(vc, wavelink.Player):
            if vc.channel != canal:
                await vc.move_to(canal)
            try:
                await vc.stop()
            except Exception:
                pass
        else:
            if vc is not None:
                await limpiar_voz(guild)
            vc = await conectar_player(canal)
        nodo = await self._nodo_con_http()
        if nodo is None:
            raise RuntimeError("Ningún nodo de Lavalink conectado acepta archivos por URL (fuente http).")
        if vc.node != nodo:
            try:
                await vc.switch_node(nodo)
            except Exception as e:
                marcar_fallo_prioritario(nodo)
                logger.warning(f"[radio] no pude usar {nodo.identifier}: {e!r}")
                alt = [n for n in get_nodos_conectados() if n != nodo and n != vc.node and await self._acepta_http(n)]
                if alt:
                    await vc.switch_node(alt[0])
        await vc.set_volume(C.VOLUMEN)
        return vc

    async def activar(self, ctx: commands.Context):
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send("🎧 Métete primero a un canal de voz para prender la radio.")
        if shutil.which(C.FFMPEG) is None or shutil.which(C.FFPROBE) is None:
            return await ctx.send("❌ No encuentro ffmpeg/ffprobe en el sistema.")
        if self._canal_archivos() is None:
            return await ctx.send("❌ Falta configurar `RADIO_CANAL_ARCHIVOS` (ID de un canal privado donde el bot sube los audios). Corre `!radiodiag`.")
        canal = ctx.author.voice.channel
        permisos = canal.permissions_for(ctx.guild.me)
        if not (permisos.connect and permisos.speak):
            return await ctx.send("❌ No tengo permiso para conectarme o hablar en ese canal.")
        lector = self.bot.get_cog("TTS")
        if lector is not None and ctx.guild.id in getattr(lector, "vc_readers", {}):
            return await ctx.send("🗣️ Tengo activo el lector de mensajes por voz en este servidor. Apágalo con `!dejardeleer` y vuelve a prender la radio.")

        gid = ctx.guild.id
        async with ctx.typing():
            try:
                self.sesiones[gid] = Sesion(gid, None, ctx.channel)   # registrada ya: los eventos de la música normal se ignoran
                vc = await self._preparar_player(ctx, canal)
            except Exception as e:
                self.sesiones.pop(gid, None)
                logger.exception("[radio] no pude preparar el player")
                return await ctx.send(f"❌ No pude conectarme: `{e}`")
            ses = self.sesiones[gid]
            ses.vc = vc
            if not self.emisora.corriendo:
                self.director.reiniciar()
                await self.emisora.iniciar()
            await self.emisora.agregar_sesion(gid, vc)
        ses.mensaje = await ctx.send(embed=self._embed(self.emisora.estado()))

    async def desactivar(self, guild_id: int, *, aviso: bool = True):
        ses = self.sesiones.pop(guild_id, None)
        await self.emisora.quitar_sesion(guild_id)
        t = self._vacio.pop(guild_id, None)
        if t:
            t.cancel()
        if ses is not None and ses.vc is not None:
            try:
                await ses.vc.stop()
            except Exception:
                pass
            try:
                await ses.vc.disconnect()
            except Exception:
                pass
            if ses.mensaje and aviso:
                try:
                    await ses.mensaje.edit(embed=discord.Embed(title=f"📻 {C.NOMBRE}", description="Fuera del aire en este servidor. ¡Gracias por escuchar!",
                                                               color=discord.Color.from_rgb(*C.COLOR_EMBED)))
                except Exception:
                    pass
        if not self.sesiones and not C.SIEMPRE_AL_AIRE:
            await self.emisora.detener()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        gid = member.guild.id
        ses = self.sesiones.get(gid)
        if ses is None or ses.vc is None:
            return
        if member.id == self.bot.user.id and before.channel is not None and after.channel is None:
            if gid in self._recuperando:            # nos desconectamos nosotros para reconectar: no apagar la radio
                return
            await self.desactivar(gid, aviso=False)
            return
        canal = getattr(ses.vc, "channel", None)
        if canal and not [m for m in canal.members if not m.bot]:
            if gid not in self._vacio:
                self._vacio[gid] = asyncio.create_task(self._salir_si_vacio(gid))
        else:
            t = self._vacio.pop(gid, None)
            if t:
                t.cancel()

    async def _salir_si_vacio(self, gid: int):
        try:
            await asyncio.sleep(45)
            ses = self.sesiones.get(gid)
            canal = getattr(ses.vc, "channel", None) if ses else None
            if ses and canal and not [m for m in canal.members if not m.bot]:
                try:
                    await ses.canal_texto.send("📻 Me quedé solo en el canal, apago la radio por aquí. ¡Vuelve cuando quieras!")
                except Exception:
                    pass
                await self.desactivar(gid)
        except asyncio.CancelledError:
            pass
        finally:
            self._vacio.pop(gid, None)

    # ═══ eventos de Lavalink (solo en servidores con radio) ════════════════════════
    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload):
        """Lavalink avisa que un track terminó: la emisora pasa al siguiente segmento solo cuando esto llega."""
        player = payload.player
        if not player or not player.guild or player.guild.id not in self.sesiones:
            return
        track = getattr(payload, "track", None)
        prio = None
        try:   # terminó bien: si está en un nodo de respaldo y kasawa ya está disponible, volver a kasawa
            if not es_nodo_prioritario(player.node) and "exception" not in str(getattr(payload, "reason", "")).lower():
                prio = next((n for n in get_nodos_conectados() if es_nodo_prioritario(n)), None)
                if prio is not None and not prioritario_enfriando(prio):
                    await player.switch_node(prio)
        except Exception as e:
            if prio is not None:
                marcar_fallo_prioritario(prio)
            logger.debug(f"[radio] no volví a kasawa: {e!r}")
        self.emisora.notificar_fin(player.guild.id, getattr(track, "encoded", None), str(getattr(payload, "reason", "")))

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload):
        player = payload.player
        if not player or not player.guild or player.guild.id not in self.sesiones:
            return
        gid = player.guild.id
        seg = self.emisora.actual
        logger.warning(f"[radio] servidor {gid}: Lavalink no pudo reproducir «{(seg.meta.get('titulo') if seg else '') or (seg.etiqueta if seg else '?')}»: {getattr(payload, 'exception', '')}")
        if seg is None:
            return
        titulo = seg.meta.get('titulo') or seg.etiqueta or '?'
        # Nodos ya probados con ESTE segmento (se reinicia solo cuando la emisora pasa a otro segmento).
        # Antes se reintentaba en "el mejor nodo con http", que casi siempre era el MISMO donde ya había fallado.
        visto = self._resync_visto.get(gid)
        if visto is None or visto[0] != seg.id:
            visto = (seg.id, set())
            self._resync_visto[gid] = visto
        visto[1].add(player.node.identifier)
        marcar_fallo_prioritario(player.node)     # si falló kasawa DE VERAS, pasa a respaldo
        for nodo in get_nodos_conectados():
            if len(visto[1]) >= MAX_NODOS_POR_SEGMENTO:
                break
            if nodo.identifier in visto[1] or not await self._acepta_http(nodo):
                continue
            visto[1].add(nodo.identifier)
            try:
                await player.switch_node(nodo)
                await self.emisora.resincronizar(gid)
                logger.info(f"[radio] servidor {gid}: «{titulo}» reintentada en {nodo.identifier}")
                return
            except Exception as e:
                logger.warning(f"[radio] no pude pasar a {nodo.identifier}: {e!r}")
        await self._saltar_fallida(gid, seg, titulo)

    async def _saltar_fallida(self, gid: int, seg, titulo: str):
        """Ningún nodo pudo tocar este segmento. Si era una canción, la locutora lo comenta AL AIRE con un chiste
        (generado por la IA y dicho en voz alta, nada de chat) y la emisora pasa al siguiente en vez de quedarse en
        silencio hasta que se acabe el tiempo de la canción."""
        if seg.id in self._fallas_vistas:
            return
        self._fallas_vistas.add(seg.id)
        if len(self._fallas_vistas) > 50:
            self._fallas_vistas = {seg.id}
        solo = len(self.sesiones) <= 1            # la emisora es una sola para todos: con varios servidores no se corta a los demás
        voz = None
        if seg.tipo == "cancion":
            try:
                voz = await asyncio.wait_for(self.director.segmento_falla(titulo, seg.meta.get('artista') or ''), 90)
            except Exception as e:
                logger.warning(f"[radio] no pude armar el chiste de la falla: {e!r}")
        if self.emisora.actual is not seg:        # mientras se armaba la voz la emisora ya siguió sola
            return
        if solo:
            self.emisora.saltar_actual(seg, antes=voz)
        elif voz is not None:                     # varios servidores: el chiste suena solo en el que falló
            vc = self.emisora.sesiones.get(gid)
            if vc is not None:
                await self.emisora._tocar(gid, vc, voz, 0.0)

    @commands.Cog.listener()
    async def on_wavelink_track_stuck(self, payload):
        """El track se trabó (nodo saturado o sin buffer: justo el lag que se oye como atraso/corte). Se trata como una
        falla del nodo: se prueba en otro y se vuelve a poner donde va la radio; si ninguno puede, se salta el segmento."""
        player = payload.player
        if not player or not player.guild or player.guild.id not in self.sesiones:
            return
        logger.warning(f"[radio] servidor {player.guild.id}: track trabado en {player.node.identifier} "
                       f"(umbral {getattr(payload, 'threshold', '?')} ms)")
        await self.on_wavelink_track_exception(payload)

    @commands.Cog.listener()
    async def on_wavelink_websocket_closed(self, payload):
        """Discord cerró la conexión de voz de este servidor. Solo se registra (el código dice el motivo: 4014 = lo
        sacaron del canal, 4006/4009 = sesión inválida o caducada) para diagnosticar cortes."""
        player = getattr(payload, "player", None)
        if not player or not player.guild or player.guild.id not in self.sesiones:
            return
        gid = player.guild.id
        codigo = getattr(payload, "code", None)
        logger.warning(f"[radio] servidor {gid}: websocket de voz cerrado "
                       f"(código {codigo}: {getattr(payload, 'reason', '')})")
        if codigo in (4014, 4004):      # 4014 = nos sacaron del canal (lo maneja on_voice_state_update); 4004 = auth
            return
        asyncio.get_running_loop().create_task(self._tras_cierre_voz(gid, codigo))

    async def _tras_cierre_voz(self, gid: int, codigo):
        """Discord cerró el websocket de voz. Sin esto, Lavalink seguía «reproduciendo» (el embed avanzaba) pero nadie lo
        oía hasta salir y volver a entrar. 4006/4009/4015 = sesión inválida/caducada/servidor caído: se reconstruye la
        conexión. Con otros códigos solo si tras unos segundos la voz sigue muerta."""
        await asyncio.sleep(3 if codigo in (4006, 4009, 4015) else 6)
        ses = self.sesiones.get(gid)
        if ses is None or ses.vc is None:
            return
        if codigo in (4006, 4009, 4015) or not self._voz_ok(ses.vc):
            await self._recuperar_voz(gid, f"websocket de voz cerrado ({codigo})")

    @staticmethod
    def _voz_ok(vc) -> bool:
        """¿La conexión de voz del player está viva? (wavelink: `connected` y `ping` = -1 cuando no hay voz)."""
        if vc is None or not getattr(vc, "connected", True):
            return False
        ping = getattr(vc, "ping", None)
        return not (isinstance(ping, (int, float)) and ping < 0)

    def _revisar_voz(self):
        """Cada ~10 s: si la voz de un servidor lleva 2 revisiones seguidas muerta, se reconstruye sola."""
        for gid, ses in list(self.sesiones.items()):
            vc = ses.vc
            if vc is None or gid in self._recuperando:
                continue
            if self._voz_ok(vc):
                self._voz_mala.pop(gid, None)
                continue
            self._voz_mala[gid] = self._voz_mala.get(gid, 0) + 1
            if self._voz_mala[gid] >= 2:
                asyncio.get_running_loop().create_task(self._recuperar_voz(gid, "conexión de voz muerta"))

    async def _recuperar_voz(self, gid: int, motivo: str):
        """Hace lo mismo que salir y volver a entrar con !radio, pero solo: reconecta el player al canal de voz y lo
        vuelve a sincronizar con la transmisión (el mensaje/embed se conserva)."""
        ses = self.sesiones.get(gid)
        guild = self.bot.get_guild(gid)
        if ses is None or guild is None or gid in self._recuperando:
            return
        if time.monotonic() - self._ult_recup.get(gid, 0.0) < 45:      # no entrar en bucle de reconexiones
            return
        canal = getattr(ses.vc, "channel", None) or (guild.me.voice.channel if guild.me and guild.me.voice else None)
        if canal is None:
            return
        self._recuperando.add(gid)
        self._ult_recup[gid] = time.monotonic()
        logger.warning(f"[radio] servidor {gid}: reconstruyo la conexión de voz ({motivo})")
        try:
            await self.emisora.quitar_sesion(gid)
            await limpiar_voz(guild)
            vc = await self._preparar_player(SimpleNamespace(guild=guild), canal)
            ses.vc = vc
            await self.emisora.agregar_sesion(gid, vc)
            self._voz_mala.pop(gid, None)
            logger.info(f"[radio] servidor {gid}: conexión de voz recuperada")
        except Exception as e:
            logger.warning(f"[radio] servidor {gid}: no pude recuperar la voz: {e!r} (reintento en ~1 min)")
        finally:
            self._recuperando.discard(gid)

    @commands.Cog.listener()
    async def on_wavelink_node_closed(self, node, disconnected):
        for player in disconnected:
            gid = player.guild.id if player and player.guild else None
            if gid in self.sesiones:
                try:
                    nuevo = await self._nodo_con_http()
                    if nuevo:
                        await player.switch_node(nuevo)
                        await self.emisora.resincronizar(gid)
                except Exception as e:
                    logger.warning(f"[radio] servidor {gid}: no pude pasar la radio a otro nodo: {e!r}")

    # ═══ embed ═════════════════════════════════════════════════════════════════════
    def _imagen_para(self, est: dict):
        """Portada de la canción que suena; si no hay canción (o no trae portada), la imagen del programa en el aire."""
        p = est.get("portada")
        if est["tipo"] == "cancion" and p and str(p).startswith("http"):
            return p
        if est["tipo"] == "noticia":
            clave = "noticiero"
        else:
            nombres = {v["nombre"].strip(): k for k, v in PROGRAMAS.items()}
            clave = nombres.get((est.get("programa") or "").strip(), "general")
        url = (IMAGENES_PROGRAMA.get(clave) or IMAGENES_PROGRAMA.get("general") or "").strip()
        return url if url.startswith("http") else None

    async def _color_para(self, url, espera: float = 1.5):
        """Color dominante de la imagen (con caché). Si tarda más de `espera`, sigue en segundo plano y el embed se
        refresca solo cuando esté listo; nunca bloquea nada."""
        if not url:
            return None
        if url in self._colores:
            return self._colores[url]
        tarea = self._colores_en_curso.get(url)
        if tarea is None:
            tarea = asyncio.get_running_loop().create_task(self._cargar_color(url))
            self._colores_en_curso[url] = tarea
        try:
            return await asyncio.wait_for(asyncio.shield(tarea), espera)
        except Exception:
            return None

    async def _cargar_color(self, url: str):
        rgb = None
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as s:
                async with s.get(url) as resp:
                    if resp.status == 200:
                        datos = await resp.read()
                        if len(datos) < 8_000_000:
                            rgb = await asyncio.to_thread(color_dominante, datos)
        except Exception as ex:
            logger.debug(f"[radio] no pude sacar el color de la portada: {ex!r}")
        finally:
            self._colores_en_curso.pop(url, None)
        if len(self._colores) > 300:
            self._colores.pop(next(iter(self._colores)))
        self._colores[url] = rgb
        if rgb:
            self._on_cambio(None)               # llegó tarde: refresca el embed con el color nuevo
        return rgb

    def _embed(self, est: dict) -> discord.Embed:
        imagen = self._imagen_para(est)
        e = discord.Embed(title=f"📻 {C.NOMBRE}",
                          color=discord.Color.from_rgb(*((self._colores.get(imagen) if imagen else None) or C.COLOR_EMBED)))
        tipo = est["tipo"]
        logo = C.LOGO_URL or None

        if tipo == "cancion":
            titulo = str(est["titulo"] or "Desconocido").replace("[", "(").replace("]", ")")
            e.description = f"🎵 **[{titulo}]({est['url']})**" if (est.get("url") or "").startswith("http") else f"🎵 **{titulo}**"
            e.add_field(name="🎤 Artista", value=est["artista"] or "Desconocido", inline=False)
            if est.get("pedida_por"):
                e.add_field(name="🙋 Pedida por", value=est["pedida_por"], inline=True)
            if est.get("siguiente"):
                e.add_field(name="⏭️ Sigue", value=est["siguiente"][:80], inline=True)
            e.add_field(name="Progreso", value=barra_progreso(est["pos"], est["dur"]), inline=False)
        elif tipo == "noticia" and est.get("noticia"):
            n = est["noticia"]
            e.description = f"🗞️ **{n['titulo'][:200]}**"
            e.add_field(name="📍 País", value=n["pais"], inline=True)
            e.add_field(name="📰 Fuente", value=f"[{n['fuente']}]({n['url']})" if n.get("url", "").startswith("http") else n["fuente"], inline=True)
            e.set_author(name="Noticiero de Radio Maxia")
        else:
            e.description = f"🎙️ **{C.LOCUTORA} al aire**\n{C.ESLOGAN}" if tipo == "voz" else f"**{C.ESLOGAN}**"
            if est.get("siguiente") and tipo == "voz":
                e.add_field(name="⏭️ Sigue", value=est["siguiente"][:80], inline=False)

        if est.get("programa") and tipo != "noticia":
            e.add_field(name="📡 Programa", value=est["programa"], inline=False)
        if imagen:
            e.set_image(url=imagen)             # portada del álbum (o imagen del programa si no suena canción)
        elif logo:
            e.set_thumbnail(url=logo)
        e.set_footer(text=f"{C.NOMBRE} • {est['oyentes']} oyente(s)")
        return e

    def _on_cambio(self, seg):
        """Llamado por la emisora (síncrono, no bloquea): el embed se actualiza en su propia tarea."""
        self._sucio = True
        if not self._refrescando:
            asyncio.get_running_loop().create_task(self._refrescar_ya())

    async def _refrescar_ya(self):
        self._refrescando = True
        try:
            await asyncio.sleep(0.3)            # deja que la emisora termine de actualizar su estado
            while self._sucio:
                self._sucio = False
                await self._actualizar_embeds()
        finally:
            self._refrescando = False

    @tasks.loop(seconds=7)
    async def _refrescar(self):
        await self._actualizar_embeds()
        while self._subidos and time.time() - self._subidos[0][1] > C.BORRAR_ARCHIVOS_TRAS_S:
            msg, _ = self._subidos.popleft()
            try:
                await msg.delete()
            except Exception:
                pass

    @_refrescar.before_loop
    async def _antes(self):
        await self.bot.wait_until_ready()

    async def _actualizar_embeds(self):
        if not self.sesiones:
            return
        est = self.emisora.estado()
        await self._color_para(self._imagen_para(est))
        embed = self._embed(est)
        firma = json.dumps(embed.to_dict(), sort_keys=True, default=str)
        # todos los servidores a la vez: uno lento no atrasa a los demás
        await asyncio.gather(*(self._editar_embed(ses, embed, firma) for ses in list(self.sesiones.values())),
                             return_exceptions=True)

    async def _editar_embed(self, ses, embed, firma):
        if ses.mensaje is None or firma == ses.ultimo:
            return
        try:
            await asyncio.wait_for(ses.mensaje.edit(embed=embed), 10)
            ses.ultimo = firma
        except discord.NotFound:
            try:
                ses.mensaje = await ses.canal_texto.send(embed=embed)
                ses.ultimo = firma
            except Exception:
                pass
        except discord.HTTPException as ex:
            logger.warning(f"[radio] no pude editar el embed: {ex}")

    # ═══ diagnóstico ═══════════════════════════════════════════════════════════════
    async def _reporte_nodos(self) -> list:
        """Una línea por nodo de tu nodos.py: si el monitor de salud lo aprueba, por qué lo descarta, y si la radio puede usarlo
        (conectado + fuente http). Así ves por qué la radio solo toma 1 nodo."""
        try:
            from musica_core import nodos as N
            cfgs = list(getattr(N, "LAVALINK_NODES_CONFIG", []))
            estado = getattr(N, "ESTADO_NODOS", {})
        except Exception:
            return []
        if not cfgs:
            return []
        if not estado:
            return ["⏳ El monitor de salud aún no ha hecho su primera revisión (espera unos segundos y repite `!radiodiag`)."]
        import wavelink as _wl
        filas, sanos, usables = [], 0, 0
        for i, cfg in enumerate(cfgs):
            e = estado.get(i, {})
            sano = bool(e.get("sano"))
            sanos += sano
            extra = ""
            nodo = _wl.Pool.nodes.get(f"node{i}")
            if nodo is not None and nodo.status == _wl.NodeStatus.CONNECTED:
                http = await self._acepta_http(nodo)
                usables += bool(sano and http)
                extra = " · http ✅" if http else " · http ❌ (la radio no lo puede usar)"
            filas.append(f"{'✅' if sano else '❌'} `{cfg['host']}:{cfg['port']}` {str(e.get('motivo', 'sin revisar'))[:80]}{extra}")
        return [f"**Nodos: {sanos}/{len(cfgs)} sanos · {usables} usables por la radio**"] + filas

    async def diagnostico(self, ctx: commands.Context):
        lineas = []
        ok = lambda b: "✅" if b else "❌"
        lineas.append(f"{ok(shutil.which(C.FFMPEG))} ffmpeg  {ok(shutil.which(C.FFPROBE))} ffprobe")
        canal = self._canal_archivos()
        if canal is None:
            lineas.append("❌ `RADIO_CANAL_ARCHIVOS` no está configurado (o el bot no ve ese canal)")
        else:
            p = canal.permissions_for(canal.guild.me)
            lineas.append(f"{ok(p.send_messages and p.attach_files)} canal de archivos {canal.mention}: enviar mensajes y adjuntar archivos")
        nodos = get_nodos_conectados()
        con_http = [n for n in nodos if await self._acepta_http(n)]
        lineas.append(f"{ok(con_http)} nodos conectados: {len(nodos)} · aceptan archivos por URL (http): {len(con_http)}")
        msg = await ctx.send("🔎 Revisando…\n" + "\n".join(lineas))

        # Prueba real: tono de 2 s → ffmpeg → subir → que Lavalink lo lea
        if canal is not None and shutil.which(C.FFMPEG):
            try:
                ruta = tempfile.mktemp(suffix=".mp3")
                p = await asyncio.create_subprocess_exec(
                    C.FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "libmp3lame", ruta,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await p.wait()
                t0 = time.time()
                track = await self._publicar(ruta)
                lineas.append(f"{ok(track)} prueba de subida + lectura en Lavalink" + (f" ({time.time() - t0:.1f} s, dura {track.length / 1000:.1f} s)" if track else " (falló: mira la consola)"))
            except Exception as e:
                lineas.append(f"❌ prueba de subida: `{e}`")
        cancion = await self._buscar_cancion("Queen", "Under Pressure")
        lineas.append(f"{ok(cancion)} búsqueda de canciones en Lavalink" + (f": {cancion['track'].title[:50]}" if cancion else ""))

        # OpenRouter: keys, modelo de texto y modelo de voz
        n_or = len(claves_openrouter())
        lineas.append(f"{ok(n_or)} keys de OpenRouter (`OPENROUTER_KEY`): {n_or}")
        if n_or:
            errs = []
            txt = await _con_openrouter("Responde solo: ok", "Eres un asistente breve.", False, 20, aiohttp.ClientTimeout(total=45), errs)
            lineas.append(f"{ok(txt)} OpenRouter texto (`{OPENROUTER_MODEL}`)" + ("" if txt else f": {(errs[-1] if errs else '')[:120]}"))
            cfg = self._cfg_gemini()
            voz_or, modelo_or = OR_TTS_VOZ or cfg["voz"] or VOZ_GEMINI_DEFECTO, OR_TTS_MODELO or cfg["modelo"] or MODELO_OR_DEFECTO
            v = await sintetizar_openrouter("Hola, soy Maxia, y esta es una prueba de voz.", **cfg)
            lineas.append(f"{ok(v)} OpenRouter voz (`{modelo_or}`, voz `{voz_or}`)" + ("" if v else " (falló: mira la consola)"))
        await msg.edit(content="📻 **Diagnóstico de Radio Maxia**\n" + "\n".join(lineas))
        # Detalle de cada nodo (en mensajes aparte para no pasar el límite de 2000 caracteres de Discord)
        trozo = ""
        for fila in await self._reporte_nodos():
            if len(trozo) + len(fila) + 1 > 1800:
                await ctx.send(trozo)
                trozo = ""
            trozo += fila + "\n"
        if trozo:
            await ctx.send(trozo)


async def setup(bot: commands.Bot):
    await bot.add_cog(Radio(bot))