import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import io
import logging
import math
import time
import json
import os
import re
import aiohttp
import wavelink

from musica_core.utils import get_user_display_name
from musica_core.player import _SinCanalDeVoz
from musica_core.nodos import (
    LAVALINK_NODES_CONFIG,
    PLAY_TIMEOUT,
    WATCHDOG_ESPERA,
    PROGRESS_UPDATE_INTERVAL,
    _buscar_en_nodos,
    _clave_nodo,
    _error_bloquea_nodo,
    _nodos_bloqueados,
    buscar_tracks_con_fallback,
    get_nodo_conectado,
    get_nodos_conectados,
    marcar_nodo_enfermo,
    conectar_player,
    limpiar_voz,
)
from musica_core.fuentes import YTMusicSource
from musica_core.bromas import broma_fallo_ia
from musica_core.salud_nodos import iniciar_monitor, detener_monitor
from musica_core.views import (
    SearchSelectView,
    PlaylistView,
    QueuePaginationView,
    SimilarSongsView,
    MusicControlView,
)

from database import db

logger = logging.getLogger(__name__)

EMOJI_CARGANDO = "<a:cargando:1556817864520900668>"   # emoji animado del bot que sale mientras carga la canción
EMOJI_REPRODUCIENDO = "<:disco:1557542659210354728>"  # emoji ESTÁTICO del título cuando ya está sonando (uno animado se reinicia en cada edición)
BOLITA_PROGRESO = "🔘"   # la bolita de la barra de progreso
COLOR_EMBED_DEFAULT =(184, 134, 11)   # color de la barrita si no se pudo sacar de la portada
INTENTOS_CANCION = 3        # veces que se reintenta TODA la carga de una canción antes de saltarla
MAX_REINTENTOS_TRACK = 6     # reintentos en otros nodos cuando la canción falla ya sonando
TIMEOUT_INICIO = 12           # seg. máximos esperando a que Lavalink confirme que la canción empezó
NODOS_POR_INTENTO = 5        # cuántos nodos distintos se prueban en cada reintento


class Music(commands.Cog):
    """Cog de música avanzado con persistencia de cola en DB y reproducción limpia"""
    
    def __init__(self, bot):
        self.bot = bot
        self.queues = {}          
        self.history = {}         
        self.current = {}         
        self.skip_votes = {}      
        self.start_times = {}     
        self.update_tasks = {}    
        self.current_messages = {}
        self.last_update = {}
        self.hosts = {}
        self._node_disconnect_messages = {} # Para trackear mensajes de desconexión de nodo                   
        self.temp_allowed_users = {}      
        self.temp_allowed_roles = {}      
        self.text_channels = {}  # guild_id -> último canal de texto usado, para avisar cuando Wavelink termine una canción sola
        self._play_locks = {}      # guild_id -> asyncio.Lock (un solo play_next a la vez por servidor)
        self._advancing = set()    # guild_ids que están cargando la siguiente canción ahora mismo
        self._pos_ref = {}         # guild_id -> ancla local para calcular el progreso de forma robusta
        self._reintentos = {}      # guild_id -> cuántas veces se reintentó la canción actual en otro nodo
        self._nodos_probados = {}  # guild_id -> identifiers de los nodos ya probados con la canción actual
        self._reintentando = set() # guild_ids con un reintento por TrackException en curso
        self._ult_migracion = {}   # guild_id -> timestamp de la última migración por lag (para no migrar sin parar)
        self._prefetch = {}        # guild_id -> (url, resultado, nodo, timestamp) de la SIGUIENTE canción ya resuelta
        self._confirmado = set()   # guild_ids cuya canción actual YA se confirmó sonando (TrackStart recibido)
        self._stall = {}           # guild_id -> {'pos': ms, 'n': updates seguidos sin avanzar}
        self._track_start = {}     # guild_id -> asyncio.Event que se activa cuando Lavalink confirma que la canción EMPEZÓ
        self._colores = {}         # url de portada -> (r, g, b) dominante, para el color de la barrita del embed
        self._aviso_carga = {}     # guild_id -> mensaje "Cargando canción" que luego se edita al embed de reproducción
        self._resume_ms = {}       # guild_id -> (url, posición en ms, ¿en pausa?) para reanudar justo donde iba tras un reinicio

    async def cog_load(self):
        # Si el vigilante reinicia el bot, la música vuelve sola al canal y a la posición donde iba
        try:
            import vigilante
            vigilante.registrar_estado('musica', self._estado_para_vigilante, self._restaurar_estado)
        except Exception as e:
            logger.info(f"Música: sin restauración tras reinicios del vigilante ({e!r})")

    async def cog_unload(self):
        detener_monitor()
        try:
            import vigilante
            vigilante.quitar_estado('musica')
        except Exception:
            pass

    # ───────────────────────── Reanudar tras un reinicio del vigilante ─────────────────────────
    @staticmethod
    def _track_a_json(track) -> dict:
        req = track.get('requester')
        req_id = req.id if isinstance(req, (discord.Member, discord.User)) else track.get('requester_id', 0)
        return {
            'title': track.get('title'),
            'uploader': track.get('uploader'),
            'duration': track.get('duration', 0),
            'thumbnail': track.get('thumbnail'),
            'url': track.get('url'),
            'query': track.get('query', ''),
            'requester_id': req_id,
        }

    def _estado_para_vigilante(self):
        """Qué suena en cada servidor (canal de voz, canción actual con su posición, fila). El vigilante lo guarda cada
        pocos segundos; tras un reinicio se usa para volver a entrar y seguir justo donde iba."""
        sesiones = []
        for vc in list(self.bot.voice_clients):
            if not isinstance(vc, wavelink.Player) or not getattr(vc, 'channel', None):
                continue
            gid = vc.guild.id
            if self._radio_activa(gid):          # en modo radio manda el cog Radio
                continue
            actual = self.current.get(gid)
            cola = self.queues.get(gid) or []
            if not actual and not cola:
                continue
            try:
                posicion_ms = int(self._elapsed_seconds(gid, vc) * 1000) if actual else 0
            except Exception:
                posicion_ms = 0
            canal_txt = self.text_channels.get(gid)
            sesiones.append({
                'guild_id': gid,
                'voice_channel_id': vc.channel.id,
                'text_channel_id': getattr(canal_txt, 'id', None),
                'host_id': self.hosts.get(gid),
                'actual': self._track_a_json(actual) if actual else None,
                'posicion_ms': posicion_ms,
                'pausado': bool(vc.is_paused()),
                'cola': [self._track_a_json(t) for t in cola][:100],
            })
        return {'sesiones': sesiones} if sesiones else None

    async def _restaurar_estado(self, datos):
        """Tras un reinicio: reconecta al canal de voz, reconstruye la fila y reanuda la canción en su posición.
        Idempotente: el vigilante la reintenta si algo falla, y los servidores ya restaurados se saltan."""
        errores = []
        for d in datos.get('sesiones', []):
            gid = d['guild_id']
            try:
                guild = self.bot.get_guild(gid)
                canal = guild.get_channel(d['voice_channel_id']) if guild else None
                if canal is None:
                    logger.warning(f"Música: no pude restaurar {gid}, ya no existe el canal de voz")
                    continue
                vc = guild.voice_client
                if vc is not None and (self.is_active(gid, vc) or self.current.get(gid)):
                    continue                                   # ya restaurado (reintento) o alguien ya puso música
                if not [m for m in canal.members if not m.bot]:
                    logger.info(f"Música: {guild.name} / {canal.name} quedó sin oyentes durante el reinicio; no restauro")
                    continue
                if self._radio_activa(gid):
                    continue

                if vc is None:
                    if not await self.get_nodo_conectado_con_reintento():
                        raise RuntimeError("ningún nodo de Lavalink disponible todavía")
                    await conectar_player(canal)
                elif vc.channel != canal:
                    await vc.move_to(canal)

                cola = []
                items = ([d['actual']] if d.get('actual') else []) + list(d.get('cola') or [])
                for item in items:
                    item = dict(item)
                    item['requester'] = guild.get_member(item.get('requester_id', 0)) or guild.me
                    cola.append(item)
                if not cola:
                    continue
                self.queues[gid] = cola
                if d.get('actual'):
                    self._resume_ms[gid] = (d['actual'].get('url'), int(d.get('posicion_ms') or 0), bool(d.get('pausado')))
                host = d.get('host_id')
                if host and guild.get_member(host):
                    self.hosts[gid] = host
                canal_txt = guild.get_channel(d.get('text_channel_id') or 0) or canal
                self.text_channels[gid] = canal_txt
                ctx = self._CtxLike(guild, canal_txt, guild.voice_client)
                try:
                    await canal_txt.send("🔄 Me reinicié por una falla técnica, ya regresé con la música.", silent=True)
                except Exception:
                    pass
                await self.play_next(ctx)
                logger.info(f"Música restaurada en {guild.name} / {canal.name} ({len(cola)} canciones)")
            except Exception as e:
                logger.warning(f"Música: no pude restaurar el servidor {gid}: {type(e).__name__}: {e}")
                errores.append(e)
        if errores:
            raise errores[0]          # el vigilante reintenta; los servidores ya restaurados se saltan

    def save_queue_to_db(self, guild_id: int):
        """Guarda la cola actual del servidor en la BD en formato JSON"""
        try:
            queue_data = []
            for track in self.queues.get(guild_id, []):
                queue_data.append(self._track_a_json(track))
            db.set_config(guild_id, 'music_session_queue', json.dumps(queue_data))
        except Exception as e:
            logger.error(f"Error al guardar cola en DB: {e}")

    def load_queue_from_db(self, guild_id: int, guild: discord.Guild):
        """Carga la cola guardada desde la BD"""
        try:
            raw_data = db.get_config(guild_id, 'music_session_queue', '[]')
            queue_data = json.loads(raw_data)
            loaded_queue = []
            for item in queue_data:
                req_id = item.get('requester_id', 0)
                member = guild.get_member(req_id) if guild else None
                item['requester'] = member or guild.me
                loaded_queue.append(item)
            return loaded_queue
        except Exception as e:
            logger.error(f"Error al cargar cola desde DB: {e}")
            return []

    def get_temp_permissions(self, guild_id: int) -> bool:
        val = db.get_config(guild_id, 'music_temp_public_mode', 'false')
        return val == 'true'

    def set_temp_permissions(self, guild_id: int, status: bool):
        db.set_config(guild_id, 'music_temp_public_mode', 'true' if status else 'false')

    def is_host_or_mod(self, guild_id: int, member: discord.Member) -> bool:
        if member.guild_permissions.administrator or member.id == member.guild.owner_id:
            return True
        if guild_id in self.hosts and self.hosts[guild_id] == member.id:
            return True
        return False

    def _reasignar_anfitrion(self, guild: discord.Guild, canal: discord.VoiceChannel):
        """Si el anfitrión salió de la llamada, el control pasa a otra persona que siga en ella
        (prioriza admins/dueño). Si no queda nadie, se libera el puesto."""
        humanos = [m for m in canal.members if not m.bot]
        if not humanos:
            self.hosts.pop(guild.id, None)
            return None
        humanos.sort(key=lambda m: not (m.guild_permissions.administrator or m.id == guild.owner_id))
        nuevo = humanos[0]
        self.hosts[guild.id] = nuevo.id
        return nuevo

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        # 👤 El anfitrión salió de la llamada del bot -> relevo
        if member.id != self.bot.user.id and before.channel is not None and before.channel != after.channel:
            vc_bot = member.guild.voice_client
            if vc_bot and vc_bot.channel == before.channel and self.hosts.get(member.guild.id) == member.id:
                nuevo = self._reasignar_anfitrion(member.guild, before.channel)
                canal_txt = self.text_channels.get(member.guild.id)
                if nuevo and canal_txt:
                    try:
                        await canal_txt.send(f"👑 {get_user_display_name(member)} salió de la llamada, ahora el anfitrión es **{get_user_display_name(nuevo)}**.", silent=True)
                    except Exception:
                        pass

        # 🔌 Desconectaron al bot (inactividad, lo sacaron, Discord tiró la conexión): limpiar todo
        # para que la próxima vez que pongan música conecte limpio, sin quedar un voice client zombi.
        if member.id == self.bot.user.id and before.channel is not None and after.channel is None:
            gid = member.guild.id
            self._cancel_update_task(gid)
            self.current[gid] = None
            self._pos_ref.pop(gid, None)
            self._node_disconnect_messages.pop(gid, None)
            self.hosts.pop(gid, None)
            self._reintentando.discard(gid)
            self._confirmado.discard(gid)
            if member.guild.voice_client is not None:
                await limpiar_voz(member.guild)

        if member.id == self.bot.user.id:
            voice_client = member.guild.voice_client
            if voice_client:
                is_muted = after.self_mute or after.mute
                was_muted = before.self_mute or before.mute
                if not was_muted and is_muted:
                    if voice_client.is_playing():
                        voice_client.pause()
                elif was_muted and not is_muted:
                    if voice_client.is_paused():
                        voice_client.resume()

    def get_required_votes(self, total_users: int) -> int:
        if total_users <= 2:
            return 1
        elif total_users <= 5:
            return math.ceil(total_users * 0.6)
        else:
            return math.ceil(total_users * 0.7)

    def has_control_permission(self, guild_id: int, member: discord.Member, current_track, voice_client) -> bool:
        if not voice_client or not voice_client.channel:
            return False
        if not member.voice or member.voice.channel != voice_client.channel:
            return False
            
        if member.guild_permissions.administrator or member.id == member.guild.owner_id:
            return True
        if guild_id in self.hosts and self.hosts[guild_id] == member.id:
            return True
        if current_track and current_track.get('requester') == member:
            return True
            
        if self.get_temp_permissions(guild_id):
            return True

        allowed_users = self.temp_allowed_users.get(guild_id, set())
        if member.id in allowed_users:
            return True

        allowed_roles = self.temp_allowed_roles.get(guild_id, set())
        if any(role.id in allowed_roles for role in member.roles):
            return True

        allowed_djs = db.get_config(guild_id, 'music_djs', '')
        allowed_list = [int(uid) for uid in allowed_djs.split(',') if uid.isdigit()]
        return member.id in allowed_list

    def generate_progress_bar(self, current_sec, total_sec):
        if not total_sec or total_sec <= 0:
            return "🔴 ▬▬▬▬▬▬▬▬▬▬▬▬▬ [EN VIVO]"

        bars = 13
        progress = min(max(current_sec / total_sec, 0), 1)
        filled = int(bars * progress)
        empty = max(0, bars - filled - 1)
        bar = "▬" * filled + BOLITA_PROGRESO + "▬" * empty

        mins_current, secs_current = divmod(int(current_sec), 60)
        mins_total, secs_total = divmod(int(total_sec), 60)

        return f"{bar}\n`{mins_current}:{secs_current:02d} / {mins_total}:{secs_total:02d}`"

    # ── Color de la barrita del embed = color dominante de la portada ──
    @staticmethod
    def _color_dominante(datos: bytes):
        """(r, g, b) más 'vistoso' de la imagen, ignorando casi-negro/casi-blanco (las miniaturas de
        YouTube traen barras negras). Devuelve None si no se pudo. Requiere Pillow (pip install pillow)."""
        try:
            from PIL import Image
            from collections import Counter
            im = Image.open(io.BytesIO(datos)).convert("RGB")
            im.thumbnail((64, 64))
            w, h = im.size
            im = im.crop((w // 8, h // 4, w - w // 8, h - h // 4))   # centro: esquiva las barras negras
            cont, suma = Counter(), {}
            total = coloridos = 0
            brillo = 0
            for r, g, b in im.getdata():
                total += 1
                brillo += (r + g + b) / 3
                mx, mn = max(r, g, b), min(r, g, b)
                if mx < 50 or (mx - mn) / mx < 0.2:      # casi negro o grisáceo: no cuenta como "color"
                    continue
                coloridos += 1
                clave = (r // 32, g // 32, b // 32)
                cont[clave] += 0.2 + (mx - mn) / mx          # pesa más lo saturado que lo grisáceo
                s = suma.setdefault(clave, [0, 0, 0, 0])
                s[0] += r; s[1] += g; s[2] += b; s[3] += 1
            if not total:
                return None
            # Portada casi sin color (blanco y negro / escala de grises): la barrita sale negra, blanca o gris
            if coloridos < total * 0.10 or not cont:
                g = int(min(max(brillo / total, 12), 245))
                return (g, g, g)
            s = suma[cont.most_common(1)[0][0]]
            return (s[0] // s[3], s[1] // s[3], s[2] // s[3])
        except Exception:
            return None

    async def _cargar_color(self, url):
        """Descarga la portada y guarda su color dominante en self._colores (no lanza excepciones)."""
        if not url or url in self._colores:
            return
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
                async with s.get(url) as r:
                    if r.status != 200:
                        return
                    datos = await r.read()
            color = await asyncio.get_running_loop().run_in_executor(None, self._color_dominante, datos)
            if color:
                if len(self._colores) > 300:
                    self._colores.clear()
                self._colores[url] = color
        except Exception as e:
            logger.debug(f"No se pudo sacar el color de la portada: {e}")

    def _color_portada(self, track) -> "discord.Color":
        return discord.Color.from_rgb(*self._colores.get(track.get('thumbnail'), COLOR_EMBED_DEFAULT))

    @staticmethod
    def _titulo_link(track) -> str:
        """Título en negritas; si la canción tiene URL de YouTube, es un link clicable."""
        titulo = str(track.get('title', 'Desconocido')).replace('[', '(').replace(']', ')')
        url = track.get('url') or ''
        if url.startswith('http'):
            return f"**[{titulo}]({url})**"
        return f"**{titulo}**"

    def create_loading_embed(self, track):
        embed = discord.Embed(
            title=f"{EMOJI_CARGANDO} Cargando canción",
            description=f"**{track.get('title', 'la canción')}**",
            color=self._color_portada(track)
        )
        if track.get('thumbnail'):
            embed.set_thumbnail(url=track['thumbnail'])
        return embed

    def create_music_embed(self, ctx, track, progress_bar, queue_len=0, queue_position=1):
        embed = discord.Embed(
            title=f"{EMOJI_REPRODUCIENDO} Reproduciendo:",
            description=self._titulo_link(track),
            color=self._color_portada(track)
        )

        embed.add_field(name="🎤 Artista", value=track.get('uploader', 'Desconocido'), inline=False)
        duration_str = f"`{int(track.get('duration', 0) // 60)}:{int(track.get('duration', 0) % 60):02d}`"
        embed.add_field(name="⏱️ Duración", value=duration_str, inline=True)
        embed.add_field(name="📑 Total en Fila", value=f"`{queue_len}` canciones", inline=True)

        # ⏭️ Siguiente en fila (se lee de la fila del servidor en cada actualización del embed)
        try:
            fila = self.queues.get(ctx.guild.id) or []
        except Exception:
            fila = []
        if fila:
            sig = fila[0]
            texto_sig = f"**{str(sig.get('title', 'Desconocido'))[:60]}** — {str(sig.get('uploader', ''))[:30]}".rstrip(" —")
        else:
            texto_sig = "Nada en fila"
        embed.add_field(name="⏭️ Siguiente", value=texto_sig, inline=False)
        embed.add_field(name="Progreso", value=progress_bar, inline=False)
        
        requester = track.get('requester')
        req_name = get_user_display_name(requester)
        req_avatar = requester.display_avatar.url if isinstance(requester, discord.Member) else None
        
        embed.add_field(name="👤 Pedida por", value=f"{req_name} ({requester.mention if isinstance(requester, discord.Member) else ''})", inline=False)
        
        if req_avatar:
            embed.set_author(name=f"Reproducido por {req_name}", icon_url=req_avatar)
        
        if track.get('thumbnail'):
            embed.set_thumbnail(url=track['thumbnail'])
        
        embed.set_footer(text="Conectado vía YouTube Music API")
        return embed

    def create_added_embed(self, track, position):
        embed = discord.Embed(
            title="➕ Canción Agregada a la Fila",
            description=f"**{track['title']}**",
            color=discord.Color.from_rgb(46, 139, 87)
        )
        embed.add_field(name="🎤 Artista", value=track.get('uploader', 'Desconocido'), inline=False)
        duration_str = f"`{int(track.get('duration', 0) // 60)}:{int(track.get('duration', 0) % 60):02d}`"
        embed.add_field(name="⏱️ Duración", value=duration_str, inline=True)
        embed.add_field(name="📊 Posición en Fila", value=f"`#{position}`", inline=True)
        
        requester = track.get('requester')
        req_name = get_user_display_name(requester)
        embed.add_field(name="👤 Pedida por", value=f"{req_name}", inline=False)
        
        if track.get('thumbnail'):
            embed.set_thumbnail(url=track['thumbnail'])
            
        embed.set_footer(text="YouTube Music API")
        return embed

    async def fetch_similar_tracks(self, track, offset=0, limit=5):
        """Busca canciones similares usando Wavelink/Lavalink (sin ytmusicapi)"""
        loop = self.bot.loop
        try:
            # ✅ Buscamos la canción actual en YouTube Music y usamos el watch playlist
            # que Lavalink regresa para obtener recomendaciones similares.
            query = f"ytmsearch:{track['title']} {track.get('uploader', '')}".strip()
            wl_results = await buscar_tracks_con_fallback(query)
            if not wl_results:
                return []

            entries = wl_results.tracks if isinstance(wl_results, wavelink.Playlist) else wl_results
            similar = []
            seen_ids = set()

            # Saltamos la primera canción que es la misma que se está reproduciendo
            for entry in entries[1:]:
                uri = entry.uri or ''
                vid = uri.split('v=')[-1].split('&')[0] if 'v=' in uri else uri
                if not vid or vid in seen_ids:
                    continue
                seen_ids.add(vid)

                similar.append({
                    'title': entry.title or 'Desconocido',
                    'uploader': entry.author or 'Artista Desconocido',
                    'duration': (entry.length or 0) // 1000,
                    'thumbnail': entry.artwork,
                    'url': entry.uri or f"https://www.youtube.com/watch?v={vid}",
                    'query': f"https://www.youtube.com/watch?v={vid}",
                })

            return similar[offset:offset+limit]
        except Exception as e:
            logger.error(f"Error obteniendo canciones similares: {e}")
            return []

    # ───────────────────────── Estado / utilidades de reproducción ─────────────────────────
    def _cancel_update_task(self, guild_id: int):
        """Cancela el loop de la barra de progreso (sin cancelarse a sí mismo si lo llaman desde él)."""
        task = self.update_tasks.get(guild_id)
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def is_active(self, guild_id: int, vc) -> bool:
        """True si hay algo sonando/pausado O si ya se está cargando la siguiente canción.
        Antes solo se miraba is_playing()/is_paused(): en el hueco entre que termina una canción y
        empieza la otra (buscando en Lavalink) un /play creaba un 2º play_next en paralelo."""
        if guild_id in self._advancing:
            return True
        return bool(vc and (vc.is_playing() or vc.is_paused()))

    def _elapsed_seconds(self, guild_id: int, vc) -> float:
        """Segundos reproducidos. Usa player.position cuando Wavelink lo va actualizando; si se queda
        congelado (nodo que no manda playerUpdate, nodo cambiado, etc.) sigue contando con reloj local
        para que la barra nunca se quede quieta."""
        if guild_id not in self._confirmado:
            return 0.0   # todavía cargando / sin audio confirmado: la barra NO avanza
        now = time.monotonic()
        try:
            pos = int(vc.position or 0)
        except Exception:
            pos = 0

        ref = self._pos_ref.get(guild_id)
        if ref is None:
            ref = self._pos_ref[guild_id] = {'obs': pos, 'pos': pos, 'wall': now}

        if vc.is_paused():
            # Congelado: re-anclamos para que al reanudar no "salte"
            ref['pos'] = max(pos, ref['pos'])
            ref['obs'] = pos
            ref['wall'] = now
            return ref['pos'] / 1000

        if pos != ref['obs']:
            # Wavelink dio un valor nuevo: lo tomamos como bueno
            ref['obs'] = pos
            ref['pos'] = pos
            ref['wall'] = now
            return pos / 1000

        # Wavelink no se movió: seguimos con el reloj local desde la última ancla
        return (ref['pos'] + (now - ref['wall']) * 1000) / 1000

    # ───────────────────────── Avanzar a la siguiente canción ─────────────────────────
    async def play_next(self, ctx, only_if_idle: bool = True):
        """Reproduce la siguiente canción de la fila.
        - Un Lock por servidor: nunca corren dos play_next a la vez (antes, dos a la vez se pisaban,
          Lavalink mandaba TrackEnd 'replaced' y el bot se saltaba canciones en cascada).
        - only_if_idle=True: si ya suena algo no hace nada. El evento TrackEnd de Wavelink lo llama con
          False porque ahí sabemos que la canción anterior ya terminó."""
        if self._radio_activa(ctx.guild.id):   # en modo radio, el cog Radio maneja la reproducción
            return
        guild_id = ctx.guild.id
        lock = self._play_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            self._advancing.add(guild_id)
            try:
                await self._play_next_locked(ctx, only_if_idle)
            finally:
                self._advancing.discard(guild_id)

    async def _safe_send(self, ctx, *args, **kwargs):
        # Con la radio al aire en el servidor, los avisos de la música ("ya no hay más en la fila", "no pude reproducir...")
        # no van al chat: ahí manda el cog Radio.
        try:
            if self._radio_activa(ctx.guild.id):
                logger.debug(f"Aviso de música omitido (radio al aire en guild {ctx.guild.id}): {str(args[0])[:60] if args else ''}")
                return None
        except Exception:
            pass
        try:
            return await ctx.send(*args, **kwargs)
        except Exception as e:
            logger.warning(f"No se pudo enviar mensaje al canal: {e}")
            return None

    @staticmethod
    def _es_bloqueo_youtube(msg: str) -> bool:
        # Antes era `"bot" in msg` y como el mensaje incluye el TÍTULO, canciones como "Both Sides",
        # "Robot Rock" o "Bottle" desconectaban al bot creyendo que YouTube lo había bloqueado.
        return bool(re.search(r"sign in to confirm|not a bot", msg.lower()))

    async def _play_next_locked(self, ctx, only_if_idle: bool):
        guild_id = ctx.guild.id
        vc = ctx.voice_client

        if vc is None:
            logger.warning(f"play_next en guild {guild_id} sin canal de voz; no toco la fila.")
            return

        if only_if_idle and (vc.is_playing() or vc.is_paused()):
            return  # ya hay algo sonando

        self._cancel_update_task(guild_id)
        # Nueva canción: el aviso de "nodo caído" de la anterior ya no aplica (si se quedaba
        # puesto, update_embed_loop dejaba la barra congelada para SIEMPRE).
        self._node_disconnect_messages.pop(guild_id, None)
        self.text_channels[guild_id] = ctx.channel

        # La canción que acaba de terminar pasa al historial (una sola vez)
        prev = self.current.get(guild_id)
        if prev:
            hist = self.history.setdefault(guild_id, [])
            hist.append(prev)
            del hist[:-50]
        self.current[guild_id] = None

        while True:
            queue = self.queues.get(guild_id)
            if not queue:
                self.current[guild_id] = None
                self._pos_ref.pop(guild_id, None)
                self.save_queue_to_db(guild_id)
                old_msg = self.current_messages.get(guild_id)
                if old_msg:
                    try:
                        embed = old_msg.embeds[0]
                        embed.color = discord.Color.dark_grey()
                        embed.set_footer(text="Reproducción finalizada.")
                        await old_msg.edit(embed=embed, view=None)
                    except Exception:
                        pass
                await self._safe_send(ctx, "✅ Ya no hay más canciones en la fila.", silent=True)
                return

            track = queue.pop(0)
            self.current[guild_id] = track
            self.save_queue_to_db(guild_id)

            try:
                for intento in range(INTENTOS_CANCION):
                    try:
                        await self._reproducir_track(ctx, track)
                        return
                    except _SinCanalDeVoz:
                        raise
                    except Exception as e0:
                        if intento == INTENTOS_CANCION - 1:
                            raise
                        logger.debug(f"Intento {intento + 1}/{INTENTOS_CANCION} falló con '{track.get('title')}': {e0}")
                        await asyncio.sleep(2)
            except _SinCanalDeVoz:
                # Nos desconectaron: la canción vuelve a la fila, no se pierde
                queue.insert(0, track)
                self.current[guild_id] = None
                self.save_queue_to_db(guild_id)
                return
            except Exception as e:
                error_message = str(e) or repr(e)
                # ✅ logger.exception guarda el traceback completo en consola
                logger.exception(f"Error en play_next reproduciendo '{track.get('title')}': {error_message}")
                self.current[guild_id] = None   # la que falló no cuenta como "actual" (ni va al historial)

                # Los RuntimeError son los míos (no cargó / timeout) y llevan el título dentro: no cuentan
                if not isinstance(e, RuntimeError) and self._es_bloqueo_youtube(error_message):
                    try:
                        if ctx.voice_client:
                            await ctx.voice_client.disconnect()
                    except Exception:
                        pass
                    await self._safe_send(
                        ctx,
                        "Estoy experimentando un bloqueo temporal por parte de youtube :,c Espera de 5 a 10 minutos y vuelve a intentarlo.",
                        silent=False
                    )
                    return

                await self._safe_send(
                    ctx,
                    f"❌ No pude reproducir **{track.get('title', 'esa canción')}** ({error_message[:150]}). "
                    f"Pasando a la siguiente..."
                )
                # y el while sigue con la siguiente (ya no es recursión)

    async def _reproducir_track(self, ctx, track):
        """Muestra un aviso de 'cargando' mientras se resuelve y arranca la canción (y lo borra al terminar)."""
        aviso = None
        vc = ctx.voice_client
        destino = getattr(vc, 'channel', None) or ctx
        embed_carga = self.create_loading_embed(track)
        try:
            aviso = await destino.send(embed=embed_carga, silent=True)
        except Exception:
            try:
                aviso = await ctx.send(embed=embed_carga, silent=True)
            except Exception:
                aviso = None
        gid = ctx.guild.id
        if aviso is not None:
            self._aviso_carga[gid] = aviso      # el embed de reproducción edita ESTE mensaje al empezar
        try:
            await self._reproducir_track_interno(ctx, track)
        finally:
            # Si el aviso no se convirtió en el embed de reproducción (falló/reintento), se borra
            if aviso is not None and self._aviso_carga.get(gid) is aviso:
                self._aviso_carga.pop(gid, None)
                try:
                    await aviso.delete()
                except Exception:
                    pass

    async def _reproducir_track_interno(self, ctx, track):
        """Resuelve el track en Lavalink y lo reproduce. Lanza excepción si no se pudo."""
        guild_id = ctx.guild.id
        vc = ctx.voice_client
        if vc is None:
            raise _SinCanalDeVoz()

        # ⏩ Tras un reinicio del vigilante: la canción que sonaba se reanuda en su posición (solo si es esa canción)
        resume = self._resume_ms.get(guild_id)
        if resume and resume[0] != track.get('url'):
            self._resume_ms.pop(guild_id, None)
            resume = None
        start_ms, pausado = (resume[1], resume[2]) if resume else (0, False)

        # 🎨 El color de la portada se saca en paralelo mientras carga la canción
        tarea_color = asyncio.create_task(self._cargar_color(track.get('thumbnail')))

        # ✅ Buscamos primero en el MISMO nodo donde va a sonar (antes se resolvía en un nodo y
        # se reproducía en otro distinto, y algunos nodos rechazan tracks de otros).
        preferido = getattr(vc, 'node', None)
        pre = self._prefetch.pop(guild_id, None)
        if pre and pre[0] == track['url'] and time.time() - pre[3] < 900:
            wl_results, nodo = pre[1], pre[2]      # ya estaba resuelta de antemano: arranca sin esperar la búsqueda
        else:
            wl_results, nodo = await _buscar_en_nodos(track['url'], preferred_node=preferido)

        # 🔁 Fallback: si el link exacto fue bloqueado/restringido (Content ID/región), reintenta
        # con una búsqueda por texto.
        if not wl_results:
            fallback_query = f"ytmsearch:{track['title']} {track.get('uploader','')}".strip()
            logger.debug(f"'{track['url']}' no cargó en Lavalink, reintentando con búsqueda: {fallback_query}")
            wl_results, nodo = await _buscar_en_nodos(fallback_query, preferred_node=preferido)

        if not wl_results:
            raise RuntimeError(
                f"Lavalink no pudo cargar '{track['title']}' (probablemente bloqueada por "
                f"región/Content ID en los nodos públicos, o el video ya no está disponible)."
            )
        wl_track = wl_results.tracks[0] if isinstance(wl_results, wavelink.Playlist) else wl_results[0]

        vc = ctx.voice_client  # pudo cambiar mientras buscábamos
        if vc is None:
            raise _SinCanalDeVoz()

        self._reintentos[guild_id] = 0
        self._nodos_probados.pop(guild_id, None)
        self._confirmado.discard(guild_id)
        self._stall.pop(guild_id, None)
        inicio = asyncio.Event()
        self._track_start[guild_id] = inicio
        try:
            await self._play_en_player(vc, wl_track, nodo, start_ms, pausado)
            # play() solo significa "Lavalink aceptó la orden". La canción realmente empezó cuando llega
            # el evento TrackStart; mientras tanto se sigue mostrando el aviso de "cargando".
            try:
                await asyncio.wait_for(inicio.wait(), timeout=TIMEOUT_INICIO)
            except asyncio.TimeoutError:
                marcar_nodo_enfermo(vc.node, "no arrancó la canción")
                raise RuntimeError(f"La canción no arrancó en el nodo (nodo lento o con problemas).")
        finally:
            self._track_start.pop(guild_id, None)
        self._resume_ms.pop(guild_id, None)       # ya arrancó: la reanudación se cumplió

        # ── A partir de aquí la canción YA está sonando; que falle el mensaje no debe reintentar ──
        self.start_times[guild_id] = time.time()
        self._pos_ref[guild_id] = {'obs': start_ms, 'pos': start_ms, 'wall': time.monotonic()}

        bar_text = self.generate_progress_bar(start_ms / 1000, track.get('duration', 0))
        queue_len = len(self.queues.get(guild_id, []))
        try:
            await asyncio.wait_for(asyncio.shield(tarea_color), timeout=3)
        except Exception:
            pass
        try:
            embed = self.create_music_embed(ctx, track, bar_text, queue_len=queue_len)
            view = MusicControlView(self, guild_id)
            msg = None
            # ✨ El mensaje "Cargando canción" se convierte en el embed de reproducción (sin borrar/enviar otro)
            aviso = self._aviso_carga.pop(guild_id, None)
            if aviso is not None:
                try:
                    # edit() devuelve el mensaje ACTUALIZADO; el objeto `aviso` se queda con el embed viejo
                    msg = await aviso.edit(embed=embed, view=view)
                except Exception as e:
                    logger.debug(f"No se pudo editar el aviso de carga, se envía un mensaje nuevo: {e}")
                    try:
                        await aviso.delete()
                    except Exception:
                        pass
            # 🔊 El embed va al chat del canal de voz donde suena (si no hay permisos, al canal del comando)
            canal_voz = getattr(vc, 'channel', None)
            if msg is None and canal_voz is not None:
                try:
                    msg = await canal_voz.send(embed=embed, view=view, silent=True)
                except Exception as e:
                    logger.debug(f"No se pudo enviar el embed al chat del canal de voz: {e}")
            if msg is None:
                msg = await ctx.send(embed=embed, view=view, silent=True)
        except Exception as e:
            # Antes esto caía al except de play_next y se "saltaba" una canción que sí estaba sonando
            logger.warning(f"No se pudo enviar el embed de reproducción: {e}")
            return

        self.current_messages[guild_id] = msg
        self.last_update[guild_id] = time.time()
        self.update_tasks[guild_id] = self.bot.loop.create_task(self.update_embed_loop(ctx, guild_id))
        self.bot.loop.create_task(self._precargar_siguiente(guild_id))

    async def _migrar_a_mejor_nodo(self, player, guild_id: int, motivo: str) -> bool:
        """Mueve el player en vivo (misma canción, misma posición) a otro nodo y castiga al que daba lag."""
        ahora = time.time()
        if ahora - self._ult_migracion.get(guild_id, 0) < 60:
            return False
        marcar_nodo_enfermo(player.node, motivo)
        otros = [n for n in get_nodos_conectados() if n != player.node]
        if not otros:
            return False
        self._ult_migracion[guild_id] = ahora
        for nodo in otros[:3]:
            try:
                await asyncio.wait_for(player.switch_node(nodo), timeout=15)
                logger.debug(f"Guild {guild_id}: movido a {nodo.identifier} ({motivo})")
                return True
            except Exception as e:
                marcar_nodo_enfermo(nodo, "falló al recibir el player")
                logger.debug(f"No se pudo mover a {nodo.identifier}: {e}")
        return False

    async def _precargar_siguiente(self, guild_id: int):
        """Mientras suena la canción actual, ya deja resuelta la siguiente de la fila (búsqueda en Lavalink),
        así el cambio de canción no espera la búsqueda ni depende de un nodo lento en ese momento."""
        try:
            await asyncio.sleep(5)
            cola = self.queues.get(guild_id) or []
            if not cola:
                return
            siguiente = cola[0]
            guild = self.bot.get_guild(guild_id)
            vc = guild.voice_client if guild else None
            res, nodo = await _buscar_en_nodos(siguiente['url'], preferred_node=getattr(vc, 'node', None))
            if res:
                self._prefetch[guild_id] = (siguiente['url'], res, nodo, time.time())
        except Exception as e:
            logger.debug(f"No se pudo precargar la siguiente canción: {e}")

    async def _play_en_player(self, vc, wl_track, nodo_origen, start_ms: int = 0, pausado: bool = False):
        """vc.play() con timeout, y un reintento en otro nodo si el del player falla/se cuelga."""
        # Si el track lo resolvió OTRO nodo (el del player falló), movemos el player a ese nodo
        if nodo_origen is not None and vc.node != nodo_origen and nodo_origen.status == wavelink.NodeStatus.CONNECTED:
            try:
                await vc.switch_node(nodo_origen)
            except Exception as e:
                logger.debug(f"No se pudo mover el player al nodo {nodo_origen.identifier}: {e}")

        async def _play():
            try:
                # paused=False: Wavelink arranca la canción nueva PAUSADA si el player estaba en pausa
                await asyncio.wait_for(vc.play(wl_track, start=start_ms, paused=pausado), timeout=PLAY_TIMEOUT)
            except asyncio.TimeoutError:
                raise RuntimeError("Lavalink tardó demasiado en responder (nodo lento o caído).")

        try:
            await _play()
            return
        except Exception as e:
            ultimo_error = e
            marcar_nodo_enfermo(vc.node, f"play() falló: {type(e).__name__}")
            logger.debug(f"play() falló en {vc.node.identifier} ({e})")

        # Probar en OTROS nodos (hasta 3), marcando como enfermo cada uno que falle
        for otro in [n for n in get_nodos_conectados() if n != vc.node][:NODOS_POR_INTENTO + 1]:
            try:
                await vc.switch_node(otro)
                await _play()
                return
            except Exception as e:
                ultimo_error = e
                marcar_nodo_enfermo(otro, f"play() falló: {type(e).__name__}")
                logger.debug(f"play() falló también en {otro.identifier} ({e})")
        raise ultimo_error

    async def _watchdog_avanzar(self, ctx, guild_id: int, track_ref):
        """Red de seguridad: si la canción terminó (o la saltaron) y NADIE avanzó a la siguiente
        (evento TrackEnd perdido, nodo que se cayó, etc.), avanza solo. Esto evita el estado
        'pegado' donde ya no suena nada y la fila se queda esperando."""
        await asyncio.sleep(WATCHDOG_ESPERA)  # darle chance al evento normal de TrackEnd
        try:
            if self.current.get(guild_id) is not track_ref:
                return  # ya avanzó
            if guild_id in self._advancing or guild_id in self._node_disconnect_messages:
                return
            vc = ctx.voice_client
            if not vc or vc.is_playing() or vc.is_paused():
                return
            logger.warning(f"🛟 Guild {guild_id}: la canción terminó y no se avanzó sola; avanzando con el watchdog.")
            await self.play_next(ctx, only_if_idle=True)
        except Exception:
            logger.exception("Error en el watchdog de reproducción")

    class _CtxLike:
        """Imita lo mínimo de un ctx de comando (guild, channel, voice_client, send) para poder
        reutilizar play_next() cuando una canción termina sola vía evento de Wavelink, en vez de
        venir de un comando con ctx real."""
        def __init__(self, guild, channel, voice_client):
            self.guild = guild
            self.channel = channel
            self.voice_client = voice_client

        async def send(self, *args, **kwargs):
            return await self.channel.send(*args, **kwargs)

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: "wavelink.TrackStartEventPayload"):
        """Lavalink confirma que la canción de verdad empezó a sonar."""
        player = payload.player
        if player and player.guild:
            gid = player.guild.id
            self._confirmado.add(gid)
            self._stall.pop(gid, None)
            base = (self._resume_ms.get(gid) or (None, 0))[1]      # 0, o la posición si se está reanudando tras un reinicio
            self._pos_ref[gid] = {'obs': base, 'pos': base, 'wall': time.monotonic()}
            ev = self._track_start.get(gid)
            if ev:
                ev.set()

    @commands.Cog.listener()
    async def on_wavelink_player_update(self, payload: "wavelink.PlayerUpdateEventPayload"):
        """Lavalink manda el estado REAL del player cada ~5 s. Si la canción debería estar sonando pero la
        posición no avanza (o Lavalink perdió la conexión de voz) durante ~10 s, se considera trabada y se
        reintenta en otro nodo, en vez de dejar la barra corriendo sobre silencio."""
        player = payload.player
        if not player or not player.guild:
            return
        gid = player.guild.id
        if gid not in self._confirmado or gid in self._advancing or gid in self._reintentando:
            self._stall.pop(gid, None)
            return
        if not player.playing or player.paused or not player.current:
            self._stall.pop(gid, None)
            return
        est = self._stall.get(gid)
        sin_avance = (not payload.connected) or (est is not None and payload.position == est['pos'])
        n = (est['n'] + 1) if (est is not None and sin_avance) else 0

        # 🐌 Lag: el audio avanza, pero más lento que el reloj real (se entrecorta) o el ping a Discord es alto
        lag = 0
        if est is not None and not sin_avance:
            dt = payload.time - est['t']
            dp = payload.position - est['pos']
            if (dt >= 3000 and 0 < dp < dt * 0.75) or payload.ping > 500:
                lag = est.get('lag', 0) + 1
        self._stall[gid] = {'pos': payload.position, 't': payload.time, 'n': n, 'lag': lag}
        if lag >= 2:
            self._stall.pop(gid, None)
            if await self._migrar_a_mejor_nodo(player, gid, "lag durante la reproducción"):
                return
        if n >= 2:
            self._stall.pop(gid, None)
            self._confirmado.discard(gid)
            logger.debug(f"Guild {gid}: la canción no avanza realmente; reintentando en otro nodo.")
            try:
                if not await self._reintentar_en_otro_nodo(player, gid, player.current, "sin audio"):
                    player.stop()   # genera TrackEnd y se pasa a la siguiente
            except Exception:
                logger.exception("Error reintentando una canción sin audio")

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload):
        """Se dispara cuando Lavalink no pudo cargar/streamear el track actual (ej. video con
        restricción de edad, bloqueo regional, Content ID, etc). Sin esto, la canción se saltaba
        en silencio porque Lavalink manda un TrackEnd(LOAD_FAILED) justo después de esto, y
        on_wavelink_track_end la pasaba por alto sin avisar nada."""
        player = payload.player
        if not player or not player.guild:
            return
        guild_id = player.guild.id
        channel = self.text_channels.get(guild_id) or getattr(player, 'channel', None)

        exc = payload.exception
        if isinstance(exc, dict):
            reason = exc.get('message') or exc.get('cause') or 'motivo desconocido'
        else:
            reason = getattr(exc, 'message', None) or str(exc) or 'motivo desconocido'

        track = getattr(payload, 'track', None)
        track_title = getattr(track, 'title', None) or (self.current.get(guild_id) or {}).get('title', 'la canción actual')

        logger.warning(f"TrackException en guild {guild_id} reproduciendo '{track_title}': {reason}")
        self._confirmado.discard(guild_id)

        # En modo radio el que maneja los fallos es el cog Radio (cambia de nodo y salta al siguiente segmento).
        # Antes los dos actuaban a la vez: este mandaba su mensaje técnico y hasta ponía la canción "cruda" encima.
        if self._radio_activa(guild_id):
            return

        # 🔁 Antes de rendirse: probar la misma canción en OTRO nodo (restricción de edad/región suele
        # depender del nodo), y luego una búsqueda por texto en otro más.
        if track is not None and await self._reintentar_en_otro_nodo(player, guild_id, track, reason):
            return

        # Ya se probó en otros nodos y en otras versiones. Se avisa con un chiste (el error técnico solo va al log)
        # y se pasa a la siguiente; si la fila está vacía, se busca una parecida para no quedarse callado.
        cola_vacia = not self.queues.get(guild_id)
        if channel is not None and cola_vacia:
            # Antes de cualquier await: que el TrackEnd(loadFailed) de detrás no avance solo mientras se busca otra
            self._reintentando.add(guild_id)
        try:
            if channel is not None:
                texto = await broma_fallo_ia(track_title, getattr(track, 'author', '') or '')
                texto += "\n🔎 Déjame buscarte otra parecida..." if cola_vacia else "\n⏭️ Paso a la siguiente."
                try:
                    await channel.send(texto, silent=True)
                except Exception as e:
                    logger.debug(f"No se pudo enviar el aviso de fallo: {e}")
                if cola_vacia:
                    await self._poner_parecida(guild_id, track_title, track)
        finally:
            if channel is not None and cola_vacia:
                self._reintentando.discard(guild_id)
        if channel is not None and cola_vacia:
            try:
                await self.play_next(self._CtxLike(player.guild, channel, player), only_if_idle=True)
            except Exception:
                logger.exception(f"Error poniendo una canción parecida tras el fallo (guild {guild_id})")

    async def _poner_parecida(self, guild_id: int, track_title: str, track):
        """La canción falló sin remedio y la fila está vacía: deja en la fila una canción parecida."""
        try:
            base = self.current.get(guild_id) or {'title': track_title, 'uploader': getattr(track, 'author', '') or ''}
            fallida = getattr(track, 'uri', None) or base.get('url')
            for t in await self.fetch_similar_tracks(base, offset=0, limit=5):
                if t.get('url') and t['url'] != fallida:
                    t = t.copy()
                    t['requester'] = base.get('requester')
                    self.queues.setdefault(guild_id, []).append(t)
                    self.save_queue_to_db(guild_id)
                    return
        except Exception:
            logger.exception("No se pudo buscar una canción parecida tras el fallo")

    def _radio_activa(self, guild_id: int) -> bool:
        radio = self.bot.get_cog("Radio")
        try:
            return bool(radio and radio.activa_en(guild_id))
        except Exception:
            return False

    @staticmethod
    def _error_de_video(msg: str) -> bool:
        """True si el error es del VIDEO (no disponible, privado, región, edad, copyright) y no del nodo."""
        return bool(re.search(r"not available|unavailable|private video|has been removed|removed by the uploader|"
                              r"copyright|blocked it|in your country|age.restrict|inappropriate for some",
                              (msg or "").lower()))

    async def _reintentar_en_otro_nodo(self, player, guild_id: int, track, reason: str) -> bool:
        """Reintenta la canción que falló en otro nodo. True si quedó sonando."""
        if _error_bloquea_nodo(reason):
            nodo_malo = player.node
            for cfg in LAVALINK_NODES_CONFIG:
                if f"{cfg['host']}:{cfg['port']}" in (nodo_malo.uri or ""):
                    _nodos_bloqueados[_clave_nodo(cfg)] = time.time()
        intento = self._reintentos.get(guild_id, 0)
        if intento >= MAX_REINTENTOS_TRACK:
            return False
        # Un video "no disponible"/restringido NO es culpa del nodo: antes cada video así castigaba al nodo 15 min
        # y con unos cuantos se acababan los nodos "sanos" a los que cambiar (y ya no se cambiaba de nodo).
        if not self._error_de_video(reason):
            marcar_nodo_enfermo(player.node, reason[:40])
        probados = self._nodos_probados.setdefault(guild_id, set())
        probados.add(player.node.identifier)
        otros = [n for n in get_nodos_conectados() if n != player.node]
        if not otros:      # si todos están castigados, igual se prueba cualquier otro que esté conectado
            otros = [n for n in wavelink.Pool.nodes.values()
                     if n.status == wavelink.NodeStatus.CONNECTED and n != player.node]
        otros.sort(key=lambda n: n.identifier in probados)    # primero los que aún no se probaron con esta canción
        if not otros:
            return False
        self._reintentos[guild_id] = intento + 1
        self._reintentando.add(guild_id)
        try:
            # Primeros intentos: el mismo link en otros nodos. Los últimos: otras versiones de la canción
            # (YouTube Music y luego YouTube normal), por si ese video en particular es el que falla.
            titulo = f"{track.title} {getattr(track, 'author', '')}".strip()
            if intento < MAX_REINTENTOS_TRACK - 2 and getattr(track, 'uri', None):
                query, pos = track.uri, 0
            elif intento == MAX_REINTENTOS_TRACK - 2:
                query, pos = f"ytmsearch:{titulo}", 1      # 2º resultado: suele ser otra subida distinta
            else:
                query, pos = f"ytsearch:{titulo}", 0
            for nodo in otros[:NODOS_POR_INTENTO]:
                try:
                    res = await asyncio.wait_for(wavelink.Pool.fetch_tracks(query, node=nodo), timeout=8)
                    if not res:
                        continue
                    lista = res.tracks if isinstance(res, wavelink.Playlist) else res
                    wl_track = lista[pos] if len(lista) > pos else lista[0]
                    await player.switch_node(nodo)
                    await asyncio.wait_for(player.play(wl_track, paused=False), timeout=PLAY_TIMEOUT)
                    self._pos_ref[guild_id] = {'obs': 0, 'pos': 0, 'wall': time.monotonic()}
                    logger.debug(f"Guild {guild_id}: '{track.title}' recuperada en {nodo.identifier}")
                    return True
                except Exception as e:
                    logger.debug(f"Reintento en {nodo.identifier} falló: {e}")
            return False
        finally:
            self._reintentando.discard(guild_id)

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload):
        """Reemplaza el 'after=' callback de discord.py: Wavelink avisa por evento cuando una
        canción termina, y aquí seguimos igual que antes con la siguiente de la fila."""
        player = payload.player
        if not player or not player.guild:
            return

        # ✅ Lavalink manda TrackEnd también cuando un track fue REEMPLAZADO por otro (play() encima
        # de uno que sonaba) o cuando se destruye el player. Antes esos eventos también llamaban a
        # play_next y el bot se saltaba canciones en cascada / avisaba "ya no hay más canciones" de más.
        reason = str(getattr(payload, 'reason', '') or '').lower()
        if reason in ('loadfailed', 'load_failed') and (player.guild.id in self._reintentando
                                                       or player.guild.id in self._advancing):
            return  # se está reintentando / cargando ya; el propio reintento decide si saltar
        if reason in ('replaced', 'cleanup'):
            logger.debug(f"TrackEnd ignorado (reason={reason}) en guild {player.guild.id}")
            return
        if not getattr(player, 'connected', True):
            return

        guild_id = player.guild.id
        self._confirmado.discard(guild_id)
        self._stall.pop(guild_id, None)
        channel = self.text_channels.get(guild_id) or player.channel
        if not channel:
            return
        fake_ctx = self._CtxLike(player.guild, channel, player)
        try:
            # Aquí SÍ sabemos que la canción anterior terminó, por eso only_if_idle=False
            await self.play_next(fake_ctx, only_if_idle=False)
        except Exception:
            logger.exception(f"Error avanzando a la siguiente canción tras TrackEnd (guild {guild_id})")

    @commands.Cog.listener()
    async def on_wavelink_track_stuck(self, payload: "wavelink.TrackStuckEventPayload"):
        """La canción se trabó (sin audio por varios segundos). Se manda un stop para que Lavalink
        emita TrackEnd y el bot continúe con la siguiente, en vez de quedarse pegado."""
        player = payload.player
        if not player or not player.guild:
            return
        guild_id = player.guild.id
        if self._radio_activa(guild_id):      # en modo radio, el que maneja las trabas es el cog Radio
            return
        channel = self.text_channels.get(guild_id) or getattr(player, 'channel', None)
        logger.debug(f"TrackStuck en guild {guild_id}.")
        track = getattr(payload, 'track', None)
        if track is not None and await self._reintentar_en_otro_nodo(player, guild_id, track, "canción trabada"):
            return
        if channel:
            try:
                await channel.send("⚠️ La canción se trabó, paso a la siguiente...", silent=True)
            except Exception:
                pass
        player.stop()  # MusicPlayer.stop() es síncrono (dispara la tarea); luego llega TrackEnd

    @commands.Cog.listener()
    async def on_wavelink_node_closed(self, node: wavelink.Node, disconnected: list[wavelink.Player]):
        """Se dispara cuando un nodo de Lavalink se cierra o desconecta.
        Intentamos reconectar automáticamente cada player que estaba en ese nodo."""
        logger.debug(f"Nodo Lavalink {node.identifier} ({node.uri}) desconectado. "
                     f"Afectando a {len(disconnected)} reproductores.")

        for player in disconnected:
            if not player or not player.guild:
                continue

            guild_id = player.guild.id
            if self._radio_activa(guild_id):  # en modo radio, el cog Radio cambia de nodo por su cuenta (y sin avisos de música)
                continue
            channel = self.text_channels.get(guild_id) or getattr(player, 'channel', None)

            # 📌 Guardamos canción y posición ANTES de mover el player (después pueden resetearse)
            track_actual = player.current
            try:
                posicion = int(player.position or 0)
            except Exception:
                posicion = 0

            # Notificar en el embed si existe
            original_msg = self.current_messages.get(guild_id)
            if original_msg:
                try:
                    alert_embed = discord.Embed(
                        description=f"⚠️ **Nodo caído:** {node.uri}\nReconectando a otro servidor de música...",
                        color=discord.Color.orange()
                    )
                    await original_msg.edit(embed=alert_embed)
                    self._node_disconnect_messages[guild_id] = time.time()
                except Exception as e:
                    logger.debug(f"No se pudo actualizar embed de reconexión para {guild_id}: {e}")

            # Intentar failover a un nuevo nodo
            recuperado = False
            try:
                new_node = next((n for n in get_nodos_conectados() if n != node), None)
                if new_node:
                    logger.debug(f"Guild {guild_id}: cambiando al nodo {new_node.identifier} ({new_node.uri})...")
                    await player.switch_node(new_node)

                    if track_actual:
                        # Reanudar desde la posición donde se quedó
                        await player.play(track_actual, start=posicion, paused=False)
                        self._pos_ref[guild_id] = {'obs': posicion, 'pos': posicion, 'wall': time.monotonic()}
                        logger.debug(f"Guild {guild_id}: reproducción reanudada en {new_node.uri}")
                    recuperado = True
                else:
                    logger.debug(f"Guild {guild_id}: no hay nodos disponibles para failover.")
                    if channel:
                        await channel.send("❌ Se perdió la conexión con el servidor de música y no hay otros disponibles. La música se detuvo.", silent=True)
            except Exception as e:
                logger.error(f"❌ Error en failover para guild {guild_id}: {e}")
                if channel:
                    await channel.send(f"❌ Error al intentar cambiar de servidor: {e}", silent=True)
            finally:
                # ✅ Pase lo que pase, el aviso se quita. Antes solo se quitaba si el failover salía
                # perfecto y con player.current; si no, el flag se quedaba y update_embed_loop dejaba
                # la barra de progreso congelada para TODAS las canciones siguientes de ese servidor.
                self._node_disconnect_messages.pop(guild_id, None)

            if recuperado and original_msg:
                current_track = self.current.get(guild_id)
                if current_track:
                    try:
                        bar_text = self.generate_progress_bar(self._elapsed_seconds(guild_id, player), current_track.get('duration', 0))
                        queue_len = len(self.queues.get(guild_id, []))
                        embed = self.create_music_embed(self._CtxLike(player.guild, channel, player), current_track, bar_text, queue_len=queue_len)
                        await original_msg.edit(embed=embed)
                    except Exception:
                        pass

    async def update_embed_loop(self, ctx, guild_id):
        """Actualiza la barra de progreso mientras suene la canción.
        - Una falla suelta (HTTPException, etc.) ya no mata el loop para el resto de la canción.
        - Si borran el mensaje (NotFound) deja de editar, pero sigue vivo para armar el watchdog.
        - Si el player deja de reproducir sin que se avance a la siguiente, el watchdog avanza."""
        track_ref = self.current.get(guild_id)   # la canción a la que pertenece este loop
        editar = True
        errores_seguidos = 0
        try:
            while True:
                vc = ctx.voice_client
                if not vc or not (vc.is_playing() or vc.is_paused()):
                    break
                if self.current.get(guild_id) is not track_ref:
                    return  # otra canción tomó el control

                aviso = self._node_disconnect_messages.get(guild_id)
                if aviso:
                    if time.time() - aviso < 60:
                        # Nodo caído: no sobreescribimos el aviso... pero máximo 60 s
                        await asyncio.sleep(0.5)
                        continue
                    self._node_disconnect_messages.pop(guild_id, None)

                msg = self.current_messages.get(guild_id)
                if (editar and msg and track_ref
                        and time.time() - self.last_update.get(guild_id, 0) >= PROGRESS_UPDATE_INTERVAL):
                    try:
                        total_duration = track_ref.get('duration', 0)
                        elapsed = self._elapsed_seconds(guild_id, vc)
                        if total_duration:
                            elapsed = min(elapsed, total_duration)
                        progress = self.generate_progress_bar(elapsed, total_duration)
                        queue_len = len(self.queues.get(guild_id, []))
                        embed = self.create_music_embed(ctx, track_ref, progress, queue_len=queue_len)
                        nuevo_msg = await msg.edit(embed=embed)
                        if nuevo_msg is not None and self.current_messages.get(guild_id) is msg:
                            self.current_messages[guild_id] = nuevo_msg   # así "finalizada" parte del último embed, no de uno viejo
                        self.last_update[guild_id] = time.time()
                        errores_seguidos = 0
                    except discord.errors.NotFound:
                        logger.info(f"El mensaje de reproducción de guild {guild_id} fue borrado; dejo de actualizarlo.")
                        editar = False
                    except Exception as e:
                        errores_seguidos += 1
                        self.last_update[guild_id] = time.time()  # no reintentar cada 0.2 s
                        logger.warning(f"Error actualizando la barra de progreso ({errores_seguidos}): {e}")
                        if errores_seguidos >= 8:
                            logger.error("Demasiados errores seguidos actualizando la barra; la dejo de actualizar.")
                            editar = False

                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            return

        # Salimos porque el player ya no está reproduciendo. Si nadie avanzó, el watchdog lo hace.
        if track_ref is not None and self.current.get(guild_id) is track_ref:
            self.bot.loop.create_task(self._watchdog_avanzar(ctx, guild_id, track_ref))

    async def get_nodo_conectado_con_reintento(self):
        """Intenta obtener un nodo conectado, si no encuentra, espera un poco y reintenta."""
        for _ in range(8):   # el monitor puede estar terminando su primera revisión
            nodo = get_nodo_conectado()
            if nodo:
                return nodo
            logger.debug("No hay nodos conectados, esperando a que Wavelink reconecte...")
            await asyncio.sleep(2)
        return None

    @commands.hybrid_command(name='play', aliases=['p'])
    @app_commands.describe(query='Canción, artista o link')
    async def play(self, ctx, *, query: str):
        if not ctx.author.voice:
            await ctx.send("❌ ¡Perdón! pero debes estar en un canal de voz!")
            return

        guild_id = ctx.guild.id
        channel = ctx.author.voice.channel

        # Voice client muerto/zombi (desconexión por inactividad, timeout previo...): se limpia antes
        if ctx.voice_client is not None and not getattr(ctx.voice_client, 'connected', True):
            await limpiar_voz(ctx.guild)

        if ctx.voice_client is None:
            nodo = await self.get_nodo_conectado_con_reintento()
            if not nodo:
                await ctx.send("❌ Ningún servidor de música está disponible ahorita. Intenta de nuevo en unos segundos.")
                return
            try:
                # En Wavelink 3.x no existe el argumento 'node' en connect().
                # Wavelink usa su propia lógica interna para asignar el nodo al reproductor.
                # Para asegurar que use el nodo que queremos, confiamos en que Pool.connect ya los registró.
                voice_client = await conectar_player(channel)
            except Exception as e:
                logger.error(f"❌ Error crítico al conectar al canal de voz: {e}")
                await ctx.send(f"❌ Error al conectar al canal de voz: {str(e)[:100]}")
                return

        elif ctx.voice_client.channel != channel:
            try:
                await ctx.voice_client.move_to(channel)
            except Exception as e:
                logger.debug(f"move_to falló ({e}); reconectando desde cero.")
                await limpiar_voz(ctx.guild)
                try:
                    await conectar_player(channel)
                except Exception as e2:
                    await ctx.send(f"❌ Error al conectar al canal de voz: {str(e2)[:100]}")
                    return

        if guild_id not in self.hosts or not ctx.guild.get_member(self.hosts[guild_id]):
            self.hosts[guild_id] = ctx.author.id

        self.text_channels[guild_id] = ctx.channel

        async with ctx.typing():
            # ✅ Soporte de playlists: si la query es un link de playlist (YouTube, YT Music,
            # Spotify si hay plugin, etc), en vez de agregar todo directo se pregunta qué hacer.
            is_url = query.startswith('http://') or query.startswith('https://')
            if is_url:
                playlist_result = await YTMusicSource.resolve_playlist(ctx, query, loop=self.bot.loop)
                if playlist_result:
                    playlist_name, playlist_tracks = playlist_result
                    if guild_id not in self.queues or not self.queues[guild_id]:
                        self.queues[guild_id] = self.load_queue_from_db(guild_id, ctx.guild)

                    view = PlaylistView(self, ctx, playlist_name, playlist_tracks, author_id=ctx.author.id)
                    embed = view.create_embed()
                    await ctx.send(embed=embed, view=view, silent=True)
                    return

            tracks = await YTMusicSource.create_sources_from_query(ctx, query, loop=self.bot.loop, limit=1)
            if not tracks:
                await ctx.send("❌ No encontré nada :c.")
                return

            if guild_id not in self.queues or not self.queues[guild_id]:
                self.queues[guild_id] = self.load_queue_from_db(guild_id, ctx.guild)

            was_playing = self.is_active(guild_id, ctx.voice_client)

            if not was_playing:
                first_track = tracks[0]
                self.queues[guild_id].append(first_track)
                self.save_queue_to_db(guild_id)
                await self.play_next(ctx)
            else:
                track = tracks[0]
                self.queues[guild_id].append(track)
                self.save_queue_to_db(guild_id)
                pos = len(self.queues[guild_id])
                embed = self.create_added_embed(track, pos)
                await ctx.send(embed=embed, silent=True)

    @commands.hybrid_command(name='buscarm', aliases=['bsearch', 'b'], description="Busca una canción de forma privada")
    @app_commands.describe(query='Canción o artista a buscar')
    async def buscarm(self, ctx, *, query: str):
        if not ctx.author.voice:
            await ctx.send("❌ Primero debes estar en un canal de voz!", ephemeral=True)
            return

        is_interaction = ctx.interaction is not None

        if is_interaction:
            await ctx.defer(ephemeral=True)
        else:
            await ctx.typing()

        # ✅ Soporte de playlists también aquí: si pegan un link de playlist, se pregunta qué
        # hacer en vez de mostrarla como si fuera una sola canción.
        is_url = query.startswith('http://') or query.startswith('https://')
        if is_url:
            playlist_result = await YTMusicSource.resolve_playlist(ctx, query, loop=self.bot.loop)
            if playlist_result:
                playlist_name, playlist_tracks = playlist_result
                view = PlaylistView(self, ctx, playlist_name, playlist_tracks, author_id=ctx.author.id)
                embed = view.create_embed()
                if is_interaction:
                    await ctx.send(embed=embed, view=view, ephemeral=True)
                else:
                    await ctx.send(embed=embed, view=view, silent=True)
                return

        tracks = await YTMusicSource.create_sources_from_query(ctx, query, loop=self.bot.loop, limit=40)
        if not tracks:
            if is_interaction:
                await ctx.send("❌ No se encontraron resultados para tu búsqueda.", ephemeral=True)
            else:
                await ctx.send("❌ No se encontraron resultados para tu búsqueda.")
            return

        view = SearchSelectView(self, ctx, query, tracks, author_id=ctx.author.id, page=0, per_page=10)
        embed = view.create_embed()

        if is_interaction:
            await ctx.send(embed=embed, view=view, ephemeral=True)
        else:
            await ctx.send(embed=embed, view=view, silent=True)

    @commands.hybrid_command(name='helpm', aliases=['ayudam'])
    async def helpm(self, ctx):
        guild_id = ctx.guild.id
        perms_active = self.get_temp_permissions(guild_id)
        has_perm = self.has_control_permission(guild_id, ctx.author, self.current.get(guild_id), ctx.voice_client)
        
        embed = discord.Embed(
            title="🎶 Ayuda de Música - Comandos & Permisos",
            description="Lista de comandos disponibles para el sistema de música de Max:",
            color=discord.Color.gold()
        )
        
        commands_list = (
            "`!max play <canción/url>` - reproducir musica (de preferencia usar url de youtube o ytmusic). Si es un link de playlist, te preguntará qué hacer con ella.\n"
            "`!max buscarm <búsqueda>` o `/buscarm` - Busca una canción (o pega un link de playlist) con resultados privados.\n"
            "`!max mfila` - Muestra la cola de reproducción actual con paginación.\n"
            "`!max next` - Pasa a la siguiente canción.\n"
            "`!max previous` - Regresa a la canción anterior.\n"
            "`!max pause` / `!max reanude` - Pausa o reanuda la reproducción.\n"
            "`!max skip` - Votación para saltar canción actual.\n"
         
            "`!max stop` - Desconecta el bot y limpia la cola."
            
        )
        embed.add_field(name="📜 Comandos", value=commands_list, inline=False)
        
        status_text = "🌐 Libres (Todos en llamada)" if perms_active else "🔒 Restringidos (Personalizado / Anfitrión)"
        user_status = "✅ Sí (Tienes acceso concedido)" if has_perm else "❌ No (Solo puedes omitir tus propias canciones)"
        
        embed.add_field(name="⚙️ Estado de Permisos para omitir canciones", value=status_text, inline=False)
        embed.add_field(name="👤 Tus Permisos", value=user_status, inline=False)
        embed.set_footer(text=f"Solicitado por {get_user_display_name(ctx.author)}")
        await ctx.send(embed=embed, silent=True)

    @commands.hybrid_command(name='mfila', aliases=['queue', 'q', 'cola'])
    async def mfila(self, ctx):
        guild_id = ctx.guild.id
        current = self.current.get(guild_id)
        queue = self.queues.get(guild_id, [])

        if not queue and ctx.guild:
            queue = self.load_queue_from_db(guild_id, ctx.guild)
            self.queues[guild_id] = queue

        if not current and not queue:
            await ctx.send("📭 La fila está vacía.")
            return

        view = QueuePaginationView(self, ctx, current, queue, page=0, per_page=10)
        embed = view.create_embed()
        await ctx.send(embed=embed, view=view, silent=True)

    @commands.hybrid_command(name='next', aliases=['n', 'skip-direct'])
    async def next_track(self, ctx):
        guild_id = ctx.guild.id
        vc = ctx.voice_client
        if not vc:
            await ctx.send("❌ El bot no está en un canal de voz.")
            return

        current = self.current.get(guild_id)
        has_general = self.has_control_permission(guild_id, ctx.author, current, vc)
        is_own = current and current.get('requester') == ctx.author

        # 🛟 Estado "pegado": no suena nada. Antes aquí solo decía "No hay nada reproduciéndose"
        # y te dejaba tirado aunque hubiera canciones en la fila.
        if not vc.is_playing() and not vc.is_paused():
            if guild_id in self._advancing:
                await ctx.send("⏳ Ya estoy cargando la siguiente canción, dame un momento...", silent=True)
                return
            if self.queues.get(guild_id):
                if not has_general:
                    await ctx.send(f"❌ {get_user_display_name(ctx.author)}, no tienes permisos para arrancar la fila.")
                    return
                await ctx.send("🔄 No había nada sonando pero sí canciones en la fila, arrancando la siguiente...", silent=True)
                await self.play_next(ctx)
                return
            await ctx.send("❌ No hay nada reproduciéndose.")
            return

        if not has_general and not is_own:
            await ctx.send(f"❌ {get_user_display_name(ctx.author)}, no tienes permisos para omitir esta canción.")
            return

        self._cancel_update_task(guild_id)

        await ctx.send(f"⏭️ Canción omitida por {get_user_display_name(ctx.author)}.", silent=True)
        vc.stop()
        # Red de seguridad: si Wavelink no avisa que terminó, avanzamos solos
        self.bot.loop.create_task(self._watchdog_avanzar(ctx, guild_id, current))

    @commands.hybrid_command(name='previous', aliases=['prev', 'back', 'previus'])
    async def previous_track(self, ctx):
        guild_id = ctx.guild.id
        if not ctx.voice_client:
            await ctx.send("❌ El bot no está en un canal de voz.")
            return

        current = self.current.get(guild_id)
        if not self.has_control_permission(guild_id, ctx.author, current, ctx.voice_client):
            await ctx.send(f"❌ {get_user_display_name(ctx.author)}, no tienes permisos para regresar la canción.")
            return

        if guild_id not in self.history or not self.history[guild_id]:
            await ctx.send("❌ No hay historial de canciones anteriores.")
            return

        prev_track = self.history[guild_id].pop()
        
        if guild_id not in self.queues:
            self.queues[guild_id] = []
            
        if current:
            self.queues[guild_id].insert(0, current)
            
        self.queues[guild_id].insert(0, prev_track)
        self.current[guild_id] = None
        self.save_queue_to_db(guild_id)

        if guild_id in self.update_tasks and self.update_tasks[guild_id]:
            self.update_tasks[guild_id].cancel()

        await ctx.send(f"⏮️ Regresando a: **{prev_track['title']}** (Acción por {get_user_display_name(ctx.author)})", silent=True)
        
        if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
            ctx.voice_client.stop()
            # Red de seguridad: current quedó en None; si nadie avanza en unos segundos, lo hacemos
            self.bot.loop.create_task(self._watchdog_avanzar(ctx, guild_id, None))
        else:
            await self.play_next(ctx)

    @commands.hybrid_command(name='skip')
    async def skip(self, ctx):
        guild_id = ctx.guild.id
        if not ctx.voice_client or not ctx.voice_client.is_playing():
            await ctx.send("❌ No hay nada reproduciéndose.")
            return

        if not ctx.author.voice or ctx.author.voice.channel != ctx.voice_client.channel:
            await ctx.send("❌ Debes estar en el mismo canal de voz para votar.")
            return

        if guild_id not in self.skip_votes:
            self.skip_votes[guild_id] = set()

        if ctx.author.id in self.skip_votes[guild_id]:
            await ctx.send("⚠️ No puedes volver a votar :v.")
            return

        self.skip_votes[guild_id].add(ctx.author.id)

        voice_channel = ctx.voice_client.channel
        listeners = [m for m in voice_channel.members if not m.bot]
        total_listeners = len(listeners)
        required_votes = self.get_required_votes(total_listeners)
        current_votes = len(self.skip_votes[guild_id])

        if current_votes >= required_votes:
            await ctx.send(f"✅ Voto aprobado ({current_votes}/{required_votes} votos). Saltando canción...", silent=True)
            self._cancel_update_task(guild_id)
            track_saltado = self.current.get(guild_id)
            ctx.voice_client.stop()
            self.skip_votes[guild_id] = set()
            # Red de seguridad: si Wavelink no avisa que terminó, avanzamos solos
            self.bot.loop.create_task(self._watchdog_avanzar(ctx, guild_id, track_saltado))
        else:
            await ctx.send(f"🗳️ Voto registrado (`{current_votes}/{required_votes}` necesarios).")

    @commands.hybrid_command(name='pause')
    async def pause(self, ctx):
        guild_id = ctx.guild.id
        current = self.current.get(guild_id)
        if not self.has_control_permission(guild_id, ctx.author, current, ctx.voice_client):
            await ctx.send(f"❌ {get_user_display_name(ctx.author)}, los permisos de pausa están desactivados.")
            return
        if not ctx.voice_client or not ctx.voice_client.is_playing():
            await ctx.send("❌ No hay nada reproduciéndose.")
            return
            
        ctx.voice_client.pause()
        await ctx.send(f"⏸️ Música pausada por {get_user_display_name(ctx.author)}.", silent=True)

    @commands.hybrid_command(name='reanude', aliases=['reanudar'])
    async def reanude(self, ctx):
        guild_id = ctx.guild.id
        current = self.current.get(guild_id)
        if not self.has_control_permission(guild_id, ctx.author, current, ctx.voice_client):
            await ctx.send(f"❌ {get_user_display_name(ctx.author)}, los permisos de reanudar están desactivados.")
            return
        if not ctx.voice_client or not ctx.voice_client.is_paused():
            await ctx.send("❌ La música no está pausada.")
            return
            
        ctx.voice_client.resume()
        await ctx.send(f"▶️ Música reanudada por {get_user_display_name(ctx.author)}.", silent=True)

    @commands.hybrid_command(name='stop', aliases=['leave', 'dc'])
    async def stop(self, ctx):
        guild_id = ctx.guild.id
        if guild_id in self.update_tasks and self.update_tasks[guild_id]:
            self.update_tasks[guild_id].cancel()
        if guild_id in self.queues:
            self.queues[guild_id].clear()
        if guild_id in self.current:
            self.current[guild_id] = None
        self._pos_ref.pop(guild_id, None)
        self._node_disconnect_messages.pop(guild_id, None)
            
        self.save_queue_to_db(guild_id)
        
        if guild_id in self.hosts:
            self.hosts.pop(guild_id, None)
        if guild_id in self.temp_allowed_users:
            self.temp_allowed_users.pop(guild_id, None)
        if guild_id in self.temp_allowed_roles:
            self.temp_allowed_roles.pop(guild_id, None)
            
        if ctx.voice_client:
            await ctx.voice_client.disconnect()
            await ctx.send(f"Bye {get_user_display_name(ctx.author)}, 👋 Ahí se ven xd.", silent=True)
        else:
            await ctx.send("❌ Aún no estoy en ningún canal de voz :v", silent=True)

    async def agregar_desde_ia(self, miembro, message, canciones) -> str:
        """Lo llama la IA (tool 'agregar_canciones_musica' en ia_core/motor_ia_mixin.py).
        Busca cada canción, la mete a la fila y arranca la música si no suena nada.
        Devuelve un texto con lo que pasó (la IA se lo cuenta a la persona)."""
        import copy
        canciones = [c.strip() for c in (canciones or []) if isinstance(c, str) and c.strip()][:20]
        if not canciones:
            return "No me mandaron ninguna canción, no puse nada."
        if not miembro.voice or not miembro.voice.channel:
            return (f"{miembro.display_name} no está en un canal de voz, no puse nada. "
                    f"Dile que se conecte a uno y me lo pida otra vez.")

        # Mensaje falso con prefijo por mención para poder armar un ctx válido
        falso = copy.copy(message)
        falso.content = f"<@{self.bot.user.id}> play x"
        falso.author = miembro
        ctx = await self.bot.get_context(falso)
        guild_id = ctx.guild.id
        channel = miembro.voice.channel

        # Misma lógica de conexión que el comando 'play'
        if ctx.voice_client is not None and not getattr(ctx.voice_client, 'connected', True):
            await limpiar_voz(ctx.guild)
        try:
            if ctx.voice_client is None:
                nodo = await self.get_nodo_conectado_con_reintento()
                if not nodo:
                    return "Ningún servidor de música está disponible ahorita, no pude poner nada. Intenta en unos segundos."
                await conectar_player(channel)
            elif ctx.voice_client.channel != channel:
                try:
                    await ctx.voice_client.move_to(channel)
                except Exception:
                    await limpiar_voz(ctx.guild)
                    await conectar_player(channel)
        except Exception as e:
            logger.error(f"❌ [Música IA] Error conectando al canal de voz: {e}")
            return f"No pude conectarme al canal de voz ({type(e).__name__}), no puse nada."
        ctx = await self.bot.get_context(falso)  # se vuelve a armar ya con el voice_client conectado

        if guild_id not in self.hosts or not ctx.guild.get_member(self.hosts[guild_id]):
            self.hosts[guild_id] = miembro.id
        self.text_channels[guild_id] = ctx.channel
        if guild_id not in self.queues or not self.queues[guild_id]:
            self.queues[guild_id] = self.load_queue_from_db(guild_id, ctx.guild)

        añadidas, fallidas = [], []
        for q in canciones:
            try:
                tracks = await YTMusicSource.create_sources_from_query(ctx, q, loop=self.bot.loop, limit=1)
            except Exception as e:
                logger.error(f"⚠️ [Música IA] Error buscando '{q}': {e!r}")
                tracks = None
            if tracks:
                self.queues[guild_id].append(tracks[0])
                añadidas.append(q)
            else:
                fallidas.append(q)

        self.save_queue_to_db(guild_id)
        if añadidas:
            await self.play_next(ctx)  # solo arranca si no suena nada (only_if_idle=True)

        if not añadidas:
            return f"No encontré ninguna de esas canciones ({', '.join(fallidas)}), no se agregó nada."
        texto = f"Agregué {len(añadidas)} canción(es) a la fila y la música ya va."
        if fallidas:
            texto += f" Estas no las encontré: {', '.join(fallidas)}."
        return texto


async def setup(bot):
    """Carga el cog. Los nodos NO se conectan aquí: el monitor de salud (musica_core/salud_nodos.py)
    prueba todos en segundo plano y conecta solo los que funcionan, repitiéndolo cada 5 minutos."""
    # 🔇 Wavelink loguea cada intento de reconexión de cada nodo público: solo dejamos errores graves.
    logging.getLogger("wavelink").setLevel(logging.ERROR)

    cog = Music(bot)
    bot.add_view(MusicControlView(cog))  # botones del reproductor siguen vivos tras reiniciar/reconectar
    await bot.add_cog(cog)
    iniciar_monitor(bot)