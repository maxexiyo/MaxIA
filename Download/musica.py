import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import logging
import math
import time
import json
import os
import subprocess
from pathlib import Path
from database import db
import yt_dlp
#import youtube_login
import wavelink

logger = logging.getLogger(__name__)

# 🔍 Logging detallado de wavelink para diagnosticar fallos de conexión a los nodos
logging.getLogger('wavelink').setLevel(logging.DEBUG)

# ✅ Varios nodos de Lavalink en rotación (mismo patrón que usaste para las API keys de Groq en
# MaxIA): se conecta el bot a TODOS, y al buscar una canción se prueba el nodo activo primero;
# si falla o está caído, prueba el siguiente automático y se queda ahí mientras siga funcionando.
# Lista sacada de https://lavalink.darrennathanael.com/ (nodos públicos gratuitos mantenidos por
# voluntarios — pueden caerse o cambiar; si alguno ya no sirve, revisa esa página por otro).
LAVALINK_NODES_CONFIG = [
    # ✅ Tu nodo local (Lavalink.jar corriendo junto al bot) va PRIMERO en la lista — así se
    # prueba primero que los públicos, ya que es el más confiable al ser tuyo.
    {"host": "localhost", "port": 2333, "password": "TUVIEJAJIJIJIJA", "secure": False},
    {"host": "lavalinkv4.serenetia.com", "port": 443, "password": "https://seretia.link/discord", "secure": True},
    {"host": "lavalink.jirayu.net", "port": 443, "password": "youshallnotpass", "secure": True},
    {"host": "lava-v4.millohost.my.id", "port": 443, "password": "https://discord.gg/mjS5J2K3ep", "secure": True},
    {"host": "lavalink-v4.triniumhost.com", "port": 443, "password": "free", "secure": True},
    {"host": "sg1-nodelink.nyxbot.app", "port": 3000, "password": "nyxbot.app/support", "secure": False},
    {"host": "sg2-nodelink.nyxbot.app", "port": 3000, "password": "nyxbot.app/support", "secure": False},
    {"host": "lava.g3v.co.uk", "port": 9008, "password": "lavalinklol", "secure": False},
]

# ✅ Puedes agregar tus propios nodos (o los tuyos privados) sin tocar código, con la variable de
# entorno LAVALINK_EXTRA_NODES, formato: "host:puerto:password:secure,host2:puerto2:password2:secure2"
_extra_nodes_env = os.environ.get("LAVALINK_EXTRA_NODES", "").strip()
if _extra_nodes_env:
    for _raw_node in _extra_nodes_env.split(","):
        try:
            _h, _p, _pw, _sec = _raw_node.strip().split(":", 3)
            LAVALINK_NODES_CONFIG.append({
                "host": _h, "port": int(_p), "password": _pw,
                "secure": _sec.strip().lower() in ("1", "true", "yes")
            })
        except Exception:
            logger.warning(f"⚠️ No se pudo interpretar el nodo extra de Lavalink: {_raw_node!r}")

_lavalink_active_idx = 0  # ✅ "sticky": se queda en el nodo que funcionó mientras siga funcionando


def _build_wavelink_nodes():
    """Arma los objetos wavelink.Node a partir de LAVALINK_NODES_CONFIG, para conectarlos todos
    de una vez en setup()."""
    nodos = []
    for i, cfg in enumerate(LAVALINK_NODES_CONFIG):
        uri = f"{'https' if cfg['secure'] else 'http'}://{cfg['host']}:{cfg['port']}"
        nodos.append(wavelink.Node(identifier=f"node{i}", uri=uri, password=cfg['password']))
    return nodos


async def buscar_tracks_con_fallback(query: str):
    """Busca una canción probando los nodos de Lavalink en rotación: si el nodo activo falla
    (caído, sobrecargado, error de red), prueba el siguiente automáticamente y se queda en el
    que sí funcionó. Devuelve lo mismo que wavelink.Pool.fetch_tracks() (lista de Playable o
    Playlist), o None si absolutamente ningún nodo pudo resolver la búsqueda."""
    global _lavalink_active_idx
    num_nodes = len(LAVALINK_NODES_CONFIG)
    if num_nodes == 0:
        return None

    orden_intentos = [(_lavalink_active_idx + i) % num_nodes for i in range(num_nodes)]

    for idx in orden_intentos:
        identifier = f"node{idx}"
        node = wavelink.Pool.nodes.get(identifier)
        if not node or node.status != wavelink.NodeStatus.CONNECTED:
            logger.warning(f"⚠️ Lavalink {identifier} ({LAVALINK_NODES_CONFIG[idx]['host']}) no disponible, probando el siguiente...")
            continue
        try:
            resultado = await wavelink.Pool.fetch_tracks(query, node=node)
            if resultado:
                _lavalink_active_idx = idx  # ✅ se queda en este nodo mientras funcione
                return resultado
            else:
                logger.warning(f"⚠️ Lavalink {identifier} ({LAVALINK_NODES_CONFIG[idx]['host']}) respondió sin resultados para '{query}', probando el siguiente...")
        except Exception as e:
            logger.warning(f"⚠️ Lavalink {identifier} ({LAVALINK_NODES_CONFIG[idx]['host']}) falló buscando '{query}': {e}. Probando el siguiente...")
            continue

    logger.error(f"❌ Ningún nodo de Lavalink pudo resolver: {query}")
    return None


def get_nodo_conectado():
    """Devuelve un wavelink.Node que esté realmente CONNECTED ahorita mismo (empezando por el
    nodo 'activo' que buscar_tracks_con_fallback ya validó, y si no, cualquier otro conectado).
    Se usa al unirse a un canal de voz para no dejar que wavelink elija automáticamente un nodo
    que solo APARENTA estar disponible pero no completó el handshake (bug que causaba
    ChannelTimeoutException al reproducir). Devuelve None si ninguno está conectado."""
    num_nodes = len(LAVALINK_NODES_CONFIG)
    if num_nodes == 0:
        return None

    orden_intentos = [(_lavalink_active_idx + i) % num_nodes for i in range(num_nodes)]
    for idx in orden_intentos:
        node = wavelink.Pool.nodes.get(f"node{idx}")
        if node and node.status == wavelink.NodeStatus.CONNECTED:
            return node
    return None


class MusicPlayer(wavelink.Player):
    """Extiende wavelink.Player para que se comporte igual que el discord.VoiceClient de antes
    (is_playing(), is_paused(), pause(), resume(), stop() como métodos síncronos), así casi todo
    el código de comandos/vistas que ya tenías (permisos, botones, colas) no tuvo que tocarse."""

    def is_playing(self) -> bool:
        return bool(self.playing) and not self.paused

    def is_paused(self) -> bool:
        return bool(self.paused)

    def pause(self):
        asyncio.create_task(super().pause(True))

    def resume(self):
        asyncio.create_task(super().pause(False))

    def stop(self):
        asyncio.create_task(super().stop())

# Cargar las cabeceras de browser.json para compartirlas con yt-dlp
browser_headers = {}
if os.path.exists('browser.json'):
    try:
        with open('browser.json', 'r', encoding='utf-8') as f:
            browser_headers = json.load(f)
        logger.info("✅ browser.json cargado correctamente para yt-dlp y ytmusicapi")
    except Exception as e:
        logger.error(f"❌ Error al leer browser.json: {e}")

# Intentar importar ytmusicapi
try:
    from ytmusicapi import YTMusic
    YTMUSIC_AVAILABLE = True
except ImportError:
    YTMUSIC_AVAILABLE = False
    logger.warning("ytmusicapi no está instalado. Instala: pip install ytmusicapi --break-system-packages")

# Inicializar YTMusic con browser.json
yt_music = None
if YTMUSIC_AVAILABLE and browser_headers:
    try:
        yt_music = YTMusic('browser.json')
        logger.info("✅ YTMusic conectado con browser.json")
    except Exception as e:
        logger.error(f"❌ Error al conectar YTMusic: {e}")

ytdl_format_options = {
    'format': 'bestaudio/best',
        'extractaudio': True,
        'audioformat': 'mp3',
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames': True,
        'noplaylist': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'ytsearch',
        'source_address': '0.0.0.0',
    
}

# ✅ Si ya existe cookies.txt (generado con !ytlogin o con youtube_login.py), se lo pasamos
# a yt-dlp para que se autentique como si fueras tú navegando, en vez de una petición anónima
# que YouTube marca más fácil como bot.
COOKIES_FILE = "sexo.txt"
if os.path.exists(COOKIES_FILE):
    ytdl_format_options['cookiefile'] = COOKIES_FILE
    logger.info(f"✅ '{COOKIES_FILE}' encontrado, yt-dlp lo usará para autenticarse.")
else:
    logger.warning(f"⚠️ No se encontró '{COOKIES_FILE}'. Usa el comando !ytlogin para generarlo (login automático desde terminal, sin ventana).")

# ✅ Proxy opcional para las peticiones de yt-dlp (ej. un SOCKS5 saliendo por datos móviles vía
# Termux+SSH, para esquivar el bloqueo de IP de YouTube en el servidor/PC donde corre el bot).
# Se configura con la variable de entorno PROXY_URL, ej: socks5://127.0.0.1:1080
PROXY_URL = os.environ.get("PROXY_URL", "").strip()
if PROXY_URL:
    ytdl_format_options['proxy'] = PROXY_URL
    logger.info(f"✅ Usando proxy para yt-dlp: {PROXY_URL}")
else:
    logger.info("ℹ️ Sin proxy configurado (PROXY_URL vacío). yt-dlp saldrá con la IP normal del bot.")

ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)


def rebuild_ytdl():
    """Reconstruye el objeto ytdl global tomando en cuenta si cookies.txt ya existe o cambió,
    y si hay un proxy configurado en PROXY_URL. Se llama después de generar cookies nuevas con
    !ytlogin, así no hace falta reiniciar el bot."""
    global ytdl
    if os.path.exists(COOKIES_FILE):
        ytdl_format_options['cookiefile'] = COOKIES_FILE
    proxy_actual = os.environ.get("PROXY_URL", "").strip()
    if proxy_actual:
        ytdl_format_options['proxy'] = proxy_actual
    else:
        ytdl_format_options.pop('proxy', None)
    ytdl = yt_dlp.YoutubeDL(ytdl_format_options)
    return ytdl


def get_user_display_name(member: discord.Member) -> str:
    """Obtiene el nombre grande del perfil (global_name) o el nombre de usuario por defecto"""
    if not isinstance(member, (discord.Member, discord.User)):
        return "Desconocido"
    return member.global_name or member.name


class SearchSelectView(discord.ui.View):
    """Vista con menú desplegable y paginación para resultados de búsqueda efímeros"""
    def __init__(self, cog, ctx, query, tracks, author_id: int, page=0, per_page=10, max_display_pages=4):
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = ctx
        self.query = query
        self.tracks = tracks
        self.author_id = author_id
        self.page = page
        self.per_page = per_page
        # ✅ Máximo 4 páginas de 10 (40 resultados) mostrados/seleccionables
        self.max_pages = max(1, min(max_display_pages, math.ceil(len(tracks) / per_page)))
        self.update_components()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Garantiza que solo la persona que buscó pueda interactuar con los elementos"""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Solo la persona que hizo esta búsqueda puede interactuar con el menú.", ephemeral=True)
            return False
        return True

    def update_components(self):
        self.clear_items()
        if not self.tracks:
            return

        start_idx = self.page * self.per_page
        end_idx = start_idx + self.per_page
        page_tracks = self.tracks[start_idx:end_idx]

        options = []
        for i, track in enumerate(page_tracks, start_idx):
            label = f"{i+1}. {track['title']}"[:100]
            desc = f"Art: {track['uploader']} | Dur: {int(track.get('duration',0)//60)}:{int(track.get('duration',0)%60):02d}"[:100]
            options.append(discord.SelectOption(label=label, description=desc, value=str(i)))

        if options:
            select = discord.ui.Select(placeholder=f"Página {self.page+1}/{self.max_pages} - Selecciona canción...", min_values=1, max_values=1, options=options, row=0)
            select.callback = self.select_callback
            self.add_item(select)

        prev_btn = discord.ui.Button(label="Anterior", emoji="◀️", style=discord.ButtonStyle.secondary, row=1, disabled=(self.page <= 0))
        prev_btn.callback = self.prev_callback
        self.add_item(prev_btn)

        page_btn = discord.ui.Button(label=f"Pág {self.page+1}/{self.max_pages}", style=discord.ButtonStyle.grey, row=1, disabled=True)
        self.add_item(page_btn)

        next_btn = discord.ui.Button(label="Siguiente", emoji="▶️", style=discord.ButtonStyle.secondary, row=1, disabled=(self.page >= self.max_pages - 1))
        next_btn.callback = self.next_callback
        self.add_item(next_btn)

        cancel_btn = discord.ui.Button(label="Cancelar", emoji="❌", style=discord.ButtonStyle.danger, row=1)
        cancel_btn.callback = self.cancel_callback
        self.add_item(cancel_btn)

    def create_embed(self):
        embed = discord.Embed(
            title=f"🔎 Resultados para: {self.query}",
            description=f"Página **{self.page + 1} de {self.max_pages}**\nSelecciona la canción que deseas agregar usando el menú desplegable:",
            color=discord.Color.blue()
        )
        start_idx = self.page * self.per_page
        end_idx = start_idx + self.per_page
        page_tracks = self.tracks[start_idx:end_idx]
        
        desc_text = ""
        for i, track in enumerate(page_tracks, start_idx + 1):
            dur = f"{int(track.get('duration', 0)//60)}:{int(track.get('duration', 0)%60):02d}"
            desc_text += f"`{i}.` **{track['title']}** — *{track['uploader']}* (`{dur}`)\n"
            
        embed.add_field(name="Opciones en esta página", value=desc_text or "No hay resultados.", inline=False)
        return embed

    async def select_callback(self, interaction: discord.Interaction):
        # 1. Verificar si el usuario está en un canal de voz
        if not interaction.user.voice:
            await interaction.response.send_message("❌ Debes estar en un canal de voz para reproducir la canción.", ephemeral=True)
            return

        idx = int(interaction.data['values'][0])
        track = self.tracks[idx]
        track['requester'] = interaction.user

        guild_id = interaction.guild.id
        if guild_id not in self.cog.queues:
            self.cog.queues[guild_id] = []

        # 2. Conectar al canal de voz si no está conectado o mover si es necesario
        voice_client = interaction.guild.voice_client
        user_channel = interaction.user.voice.channel

        if voice_client is None:
            nodo = get_nodo_conectado()
            voice_client = await user_channel.connect(cls=MusicPlayer)
            if nodo and voice_client.node != nodo:
                try:
                    await voice_client.switch_node(nodo)
                except Exception as e:
                    logger.warning(f"⚠️ No se pudo cambiar al nodo {nodo.identifier}: {e}")
        elif voice_client.channel != user_channel:
            await voice_client.move_to(user_channel)

        was_playing = voice_client.is_playing() or voice_client.is_paused()

        if not was_playing:
            self.cog.queues[guild_id].append(track)
            self.cog.save_queue_to_db(guild_id)
            if guild_id not in self.cog.hosts:
                self.cog.hosts[guild_id] = interaction.user.id
            
            await interaction.response.edit_message(content=f"✅ Reproduciendo ahora: **{track['title']}**", embed=None, view=None)
            await self.cog.play_next(self.ctx)
        else:
            self.cog.queues[guild_id].append(track)
            self.cog.save_queue_to_db(guild_id)
            pos = len(self.cog.queues[guild_id])
            embed = self.cog.create_added_embed(track, pos)
            
            await interaction.response.edit_message(content=f"✅ Añadido a la fila en la posición `#{pos}`", embed=None, view=None)
            await interaction.channel.send(embed=embed, silent=True)
        self.stop()

    async def prev_callback(self, interaction: discord.Interaction):
        if self.page > 0:
            self.page -= 1
            self.update_components()
            await interaction.response.edit_message(embed=self.create_embed(), view=self)
        else:
            await interaction.response.defer()

    async def next_callback(self, interaction: discord.Interaction):
        if self.page < self.max_pages - 1:
            self.page += 1
            self.update_components()
            await interaction.response.edit_message(embed=self.create_embed(), view=self)
        else:
            await interaction.response.defer()

    async def cancel_callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="❌ Búsqueda cancelada.", embed=None, view=None)
        self.stop()


class PlaylistView(discord.ui.View):
    """Vista que aparece al detectar que el link/consulta es una PLAYLIST completa (YouTube,
    YT Music, Spotify vía plugin de Lavalink, etc). En vez de agregar todo directo a la fila,
    le pregunta al usuario qué quiere hacer: ver las canciones (10 por página, máx. 4 páginas =
    40 canciones visibles/seleccionables), cancelar, seleccionar canciones puntuales de la
    playlist, o agregar la playlist completa (esto último siempre agrega TODAS las canciones,
    aunque la playlist tenga más de 40)."""

    def __init__(self, cog, ctx, playlist_name, tracks, author_id: int, page=0, per_page=10, max_display_pages=4):
        super().__init__(timeout=90)
        self.cog = cog
        self.ctx = ctx
        self.playlist_name = playlist_name or "Playlist sin nombre"
        self.tracks = tracks
        self.author_id = author_id
        self.page = page
        self.per_page = per_page
        self.max_pages = max(1, min(max_display_pages, math.ceil(len(tracks) / per_page)))
        self.mode = "browse"  # "browse" o "select"
        self.update_components()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Solo la persona que agregó esta playlist puede interactuar con el menú.", ephemeral=True)
            return False
        return True

    def get_page_tracks(self):
        start_idx = self.page * self.per_page
        end_idx = start_idx + self.per_page
        return self.tracks[start_idx:end_idx], start_idx

    def update_components(self):
        self.clear_items()
        if not self.tracks:
            return

        page_tracks, start_idx = self.get_page_tracks()

        if self.mode == "select":
            options = []
            for i, track in enumerate(page_tracks, start_idx):
                label = f"{i+1}. {track['title']}"[:100]
                desc = f"Art: {track['uploader']} | Dur: {int(track.get('duration',0)//60)}:{int(track.get('duration',0)%60):02d}"[:100]
                options.append(discord.SelectOption(label=label, description=desc, value=str(i)))

            if options:
                select = discord.ui.Select(
                    placeholder=f"Selecciona canciones de la página {self.page+1}...",
                    min_values=1, max_values=len(options), options=options, row=0
                )
                select.callback = self.select_callback
                self.add_item(select)

        prev_btn = discord.ui.Button(label="Anterior", emoji="◀️", style=discord.ButtonStyle.secondary, row=1, disabled=(self.page <= 0))
        prev_btn.callback = self.prev_callback
        self.add_item(prev_btn)

        page_btn = discord.ui.Button(label=f"Pág {self.page+1}/{self.max_pages}", style=discord.ButtonStyle.grey, row=1, disabled=True)
        self.add_item(page_btn)

        next_btn = discord.ui.Button(label="Siguiente", emoji="▶️", style=discord.ButtonStyle.secondary, row=1, disabled=(self.page >= self.max_pages - 1))
        next_btn.callback = self.next_callback
        self.add_item(next_btn)

        cancel_btn = discord.ui.Button(label="Cancelar", emoji="❌", style=discord.ButtonStyle.danger, row=2)
        cancel_btn.callback = self.cancel_callback
        self.add_item(cancel_btn)

        if self.mode == "browse":
            select_btn = discord.ui.Button(label="Seleccionar Canciones", emoji="🎯", style=discord.ButtonStyle.primary, row=2)
            select_btn.callback = self.enter_select_mode
            self.add_item(select_btn)

            add_all_btn = discord.ui.Button(label=f"Agregar Todo ({len(self.tracks)})", emoji="➕", style=discord.ButtonStyle.success, row=2)
            add_all_btn.callback = self.add_all_callback
            self.add_item(add_all_btn)
        else:
            back_btn = discord.ui.Button(label="Volver", emoji="↩️", style=discord.ButtonStyle.secondary, row=2)
            back_btn.callback = self.back_to_browse
            self.add_item(back_btn)

    def create_embed(self):
        embed = discord.Embed(
            title=f"🎶 Playlist Detectada: {self.playlist_name}",
            description=(
                f"Esta playlist tiene **{len(self.tracks)}** canciones.\n"
                f"¿Qué quieres hacer con ella?\n\n"
                f"Página **{self.page + 1} de {self.max_pages}**"
            ),
            color=discord.Color.from_rgb(138, 43, 226)
        )
        page_tracks, start_idx = self.get_page_tracks()
        desc_text = ""
        for i, track in enumerate(page_tracks, start_idx + 1):
            dur = f"{int(track.get('duration', 0)//60)}:{int(track.get('duration', 0)%60):02d}"
            desc_text += f"`{i}.` **{track['title']}** — *{track['uploader']}* (`{dur}`)\n"
        embed.add_field(name="Canciones en esta página", value=desc_text or "No hay canciones.", inline=False)
        if self.mode == "select":
            embed.set_footer(text="Modo selección: elige canciones del menú para agregarlas a la fila.")
        else:
            embed.set_footer(text="Usa los botones para navegar, seleccionar canciones puntuales, o agregar toda la playlist.")
        return embed

    async def _ensure_voice(self, interaction: discord.Interaction) -> bool:
        """Conecta el bot al canal de voz del usuario si hace falta. Devuelve False (y avisa)
        si el usuario no está en un canal de voz."""
        if not interaction.user.voice:
            await interaction.response.send_message("❌ Debes estar en un canal de voz para agregar canciones.", ephemeral=True)
            return False
        voice_client = interaction.guild.voice_client
        user_channel = interaction.user.voice.channel
        if voice_client is None:
            nodo = get_nodo_conectado()
            vc_nuevo = await user_channel.connect(cls=MusicPlayer)
            if nodo and vc_nuevo.node != nodo:
                try:
                    await vc_nuevo.switch_node(nodo)
                except Exception as e:
                    logger.warning(f"⚠️ No se pudo cambiar al nodo {nodo.identifier}: {e}")
        elif voice_client.channel != user_channel:
            await voice_client.move_to(user_channel)
        return True

    async def _add_tracks(self, interaction: discord.Interaction, tracks_to_add):
        guild_id = interaction.guild.id
        if guild_id not in self.cog.queues:
            self.cog.queues[guild_id] = []
        if guild_id not in self.cog.hosts:
            self.cog.hosts[guild_id] = interaction.user.id

        voice_client = interaction.guild.voice_client
        was_playing = voice_client.is_playing() or voice_client.is_paused()

        for t in tracks_to_add:
            t = t.copy()
            t['requester'] = interaction.user
            self.cog.queues[guild_id].append(t)
        self.cog.save_queue_to_db(guild_id)

        if not was_playing:
            await self.cog.play_next(self.ctx)

    async def select_callback(self, interaction: discord.Interaction):
        if not await self._ensure_voice(interaction):
            return
        selected_indices = [int(v) for v in interaction.data['values']]
        selected_tracks = [self.tracks[i] for i in selected_indices if i < len(self.tracks)]
        await interaction.response.edit_message(
            content=f"✅ Se agregaron **{len(selected_tracks)}** canciones seleccionadas de la playlist a la fila.",
            embed=None, view=None
        )
        await self._add_tracks(interaction, selected_tracks)
        self.stop()

    async def add_all_callback(self, interaction: discord.Interaction):
        if not await self._ensure_voice(interaction):
            return
        await interaction.response.edit_message(
            content=f"✅ Se agregó toda la playlist **{self.playlist_name}** ({len(self.tracks)} canciones) a la fila.",
            embed=None, view=None
        )
        await self._add_tracks(interaction, self.tracks)
        self.stop()

    async def enter_select_mode(self, interaction: discord.Interaction):
        self.mode = "select"
        self.update_components()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    async def back_to_browse(self, interaction: discord.Interaction):
        self.mode = "browse"
        self.update_components()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    async def prev_callback(self, interaction: discord.Interaction):
        if self.page > 0:
            self.page -= 1
            self.update_components()
            await interaction.response.edit_message(embed=self.create_embed(), view=self)
        else:
            await interaction.response.defer()

    async def next_callback(self, interaction: discord.Interaction):
        if self.page < self.max_pages - 1:
            self.page += 1
            self.update_components()
            await interaction.response.edit_message(embed=self.create_embed(), view=self)
        else:
            await interaction.response.defer()

    async def cancel_callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="❌ Se canceló la playlist, no se agregó nada.", embed=None, view=None)
        self.stop()


class SearchModal(discord.ui.Modal, title="Buscar Canción"):
    query_input = discord.ui.TextInput(
        label="¿Qué deseas buscar?",
        placeholder="Escribe el nombre de la canción o artista...",
        style=discord.TextStyle.short,
        required=True,
        max_length=100
    )

    def __init__(self, cog, ctx):
        super().__init__()
        self.cog = cog
        self.ctx = ctx

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        query = self.query_input.value
        tracks = await YTMusicSource.create_sources_from_query(self.ctx, query, loop=self.cog.bot.loop, limit=40)
        if not tracks:
            await interaction.followup.send("❌ No se encontraron resultados para tu búsqueda.", ephemeral=True)
            return

        view = SearchSelectView(self.cog, self.ctx, query, tracks, author_id=interaction.user.id, page=0, per_page=10)
        embed = view.create_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class QueuePaginationView(discord.ui.View):
    """Vista interactiva para paginar ÚNICAMENTE la canción actual y las próximas en fila"""
    def __init__(self, cog, ctx, current, queue, page=0, per_page=10):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.current = current
        self.queue = queue
        self.page = page
        self.per_page = per_page
        self.max_pages = max(1, math.ceil(len(queue) / per_page)) if queue else 1
        self.update_components()

    def update_components(self):
        self.clear_items()
        if not self.queue:
            return

        prev_btn = discord.ui.Button(label="Anterior", emoji="◀️", style=discord.ButtonStyle.secondary, disabled=(self.page <= 0))
        prev_btn.callback = self.prev_callback
        self.add_item(prev_btn)

        page_btn = discord.ui.Button(label=f"Pág {self.page+1}/{self.max_pages}", style=discord.ButtonStyle.grey, disabled=True)
        self.add_item(page_btn)

        next_btn = discord.ui.Button(label="Siguiente", emoji="▶️", style=discord.ButtonStyle.secondary, disabled=(self.page >= self.max_pages - 1))
        next_btn.callback = self.next_callback
        self.add_item(next_btn)

    def create_embed(self):
        embed = discord.Embed(title="📜 Fila de Reproducción", color=discord.Color.from_rgb(184, 134, 11))

        if self.current:
            elapsed = (self.ctx.voice_client.position / 1000) if self.ctx.voice_client else 0
            duration = self.current.get('duration', 0)
            mins_c, secs_c = divmod(int(elapsed), 60)
            mins_t, secs_t = divmod(int(duration), 60)
            req_name = get_user_display_name(self.current.get('requester'))
            
            embed.add_field(
                name="▶️ Reproduciendo Ahora",
                value=f"**{self.current['title']}**\n`{mins_c}:{secs_c:02d} / {mins_t}:{secs_t:02d}`\n👤 {req_name}",
                inline=False
            )
        
        if self.queue:
            start_idx = self.page * self.per_page
            end_idx = start_idx + self.per_page
            page_tracks = self.queue[start_idx:end_idx]
            
            queue_text = ""
            for i, track in enumerate(page_tracks, start_idx + 1):
                r_name = get_user_display_name(track.get('requester'))
                queue_text += f"`{i}.` **{track['title']}** - *{r_name}*\n"
                
            embed.add_field(name=f"⏭️ Próximas en la Fila (Pág. {self.page + 1}/{self.max_pages})", value=queue_text, inline=False)
        else:
            embed.add_field(name="⏭️ Próximas en la Fila", value="No hay más canciones en la fila.", inline=False)

        embed.set_footer(text=f"Total de canciones por reproducir: {len(self.queue)}")
        return embed

    async def prev_callback(self, interaction: discord.Interaction):
        if self.page > 0:
            self.page -= 1
            self.update_components()
            await interaction.response.edit_message(embed=self.create_embed(), view=self)
        else:
            await interaction.response.defer()

    async def next_callback(self, interaction: discord.Interaction):
        if self.page < self.max_pages - 1:
            self.page += 1
            self.update_components()
            await interaction.response.edit_message(embed=self.create_embed(), view=self)
        else:
            await interaction.response.defer()


class PermissionsConfigView(discord.ui.View):
    """Interfaz visual avanzada para configurar permisos"""
    def __init__(self, cog, guild_id):
        super().__init__(timeout=120)
        self.cog = cog
        self.guild_id = guild_id
        self.update_components()

    def update_components(self):
        self.clear_items()
        
        user_select = discord.ui.UserSelect(placeholder="👤 Seleccionar usuarios específicos...", min_values=0, max_values=10, row=0)
        user_select.callback = self.user_select_callback
        self.add_item(user_select)

        role_select = discord.ui.RoleSelect(placeholder="🛡️ Seleccionar roles específicos...", min_values=0, max_values=10, row=1)
        role_select.callback = self.role_select_callback
        self.add_item(role_select)

        all_btn = discord.ui.Button(label="Dar a todos en llamada", emoji="🌐", style=discord.ButtonStyle.success, row=2)
        all_btn.callback = self.all_btn_callback
        self.add_item(all_btn)

        restrict_btn = discord.ui.Button(label="Restringir permisos", emoji="🔒", style=discord.ButtonStyle.danger, row=2)
        restrict_btn.callback = self.restrict_btn_callback
        self.add_item(restrict_btn)

    async def user_select_callback(self, interaction: discord.Interaction):
        if not self.cog.is_host_or_mod(self.guild_id, interaction.user):
            await interaction.response.send_message("❌ Solo el anfitrión o moderador puede cambiar estos permisos.", ephemeral=True)
            return
        
        selected_values = interaction.data.get('values', [])
        if self.guild_id not in self.cog.temp_allowed_users:
            self.cog.temp_allowed_users[self.guild_id] = set()
            
        added_names = []
        for uid_str in selected_values:
            uid = int(uid_str)
            self.cog.temp_allowed_users[self.guild_id].add(uid)
            member = interaction.guild.get_member(uid)
            if member:
                added_names.append(get_user_display_name(member))

        names_str = ", ".join(added_names) if added_names else "Usuarios"
        db.set_config(self.guild_id, f'music_perms_log_{int(time.time())}', f"Anfitrión/Mod {interaction.user} dio permisos a: {names_str}")
        await interaction.response.send_message(f"✅ Permisos de música concedidos temporalmente a: **{names_str}**", ephemeral=True)

    async def role_select_callback(self, interaction: discord.Interaction):
        if not self.cog.is_host_or_mod(self.guild_id, interaction.user):
            await interaction.response.send_message("❌ Solo el anfitrión o moderador puede cambiar estos permisos.", ephemeral=True)
            return
        
        selected_values = interaction.data.get('values', [])
        if self.guild_id not in self.cog.temp_allowed_roles:
            self.cog.temp_allowed_roles[self.guild_id] = set()
            
        added_roles = []
        for rid_str in selected_values:
            rid = int(rid_str)
            self.cog.temp_allowed_roles[self.guild_id].add(rid)
            role = interaction.guild.get_role(rid)
            if role:
                added_roles.append(role.name)

        roles_str = ", ".join(added_roles) if added_roles else "Roles"
        db.set_config(self.guild_id, f'music_perms_log_{int(time.time())}', f"Anfitrión/Mod {interaction.user} dio permisos a roles: {roles_str}")
        await interaction.response.send_message(f"✅ Permisos de música concedidos a los roles: **{roles_str}**", ephemeral=True)

    async def all_btn_callback(self, interaction: discord.Interaction):
        if not self.cog.is_host_or_mod(self.guild_id, interaction.user):
            await interaction.response.send_message("❌ Solo el anfitrión o moderador puede cambiar estos permisos.", ephemeral=True)
            return
        
        self.cog.set_temp_permissions(self.guild_id, True)
        db.set_config(self.guild_id, f'music_perms_log_{int(time.time())}', f"Anfitrión/Mod {interaction.user} activó permisos para todos.")
        await interaction.response.edit_message(content="🌐 **Permisos actualizados:** Acceso libre concedido a **todos en la llamada**.", view=self)

    async def restrict_btn_callback(self, interaction: discord.Interaction):
        if not self.cog.is_host_or_mod(self.guild_id, interaction.user):
            await interaction.response.send_message("❌ Solo el anfitrión o moderador puede cambiar estos permisos.", ephemeral=True)
            return
        
        self.cog.set_temp_permissions(self.guild_id, False)
        if self.guild_id in self.cog.temp_allowed_users:
            self.cog.temp_allowed_users[self.guild_id].clear()
        if self.guild_id in self.cog.temp_allowed_roles:
            self.cog.temp_allowed_roles[self.guild_id].clear()
            
        db.set_config(self.guild_id, f'music_perms_log_{int(time.time())}', f"Anfitrión/Mod {interaction.user} restringió todos los permisos.")
        await interaction.response.edit_message(content="🔒 **Permisos actualizados:** Restringido (Solo anfitrión, mods y canciones propias).", view=self)


class YTMusicSource:
    """Fuente de audio que prioriza ytmusicapi"""
    def __init__(self, *, data, volume=0.5):
        self.data = data
        self.title = data.get('title', 'Desconocido')
        self.url = data.get('url', '')
        self.duration = data.get('duration', 0)
        self.thumbnail = data.get('thumbnail', None)
        self.uploader = data.get('uploader', 'Artista Desconocido')

    @classmethod
    async def resolve_playlist(cls, ctx, query: str, loop=None):
        """Revisa si la query/link es una PLAYLIST completa (vía Lavalink: YouTube, YT Music,
        Spotify si hay plugin, etc). Si lo es, devuelve (nombre_playlist, lista_de_tracks) con
        TODAS las canciones de la playlist. Si no es una playlist (o no se pudo resolver),
        devuelve None y el llamador debe seguir con la búsqueda normal."""
        loop = loop or asyncio.get_event_loop()
        try:
            wl_results = await buscar_tracks_con_fallback(query)
        except Exception as e:
            logger.warning(f"Error revisando si '{query}' es una playlist: {e}")
            return None

        if not wl_results or not isinstance(wl_results, wavelink.Playlist):
            return None

        playlist_name = wl_results.name or "Playlist sin nombre"
        tracks = []
        for entry in wl_results.tracks:
            tracks.append({
                'title': entry.title or 'Desconocido',
                'uploader': entry.author or 'Artista Desconocido',
                'duration': (entry.length or 0) // 1000,
                'thumbnail': entry.artwork,
                'url': entry.uri or query,
                'query': query,
                'requester': ctx.author
            })

        if not tracks:
            return None

        return playlist_name, tracks

    @classmethod
    async def create_sources_from_query(cls, ctx, query: str, loop=None, limit=15):
        loop = loop or asyncio.get_event_loop()
        tracks = []
        is_url = query.startswith('http://') or query.startswith('https://')
        original_query = query

        try:
            if YTMUSIC_AVAILABLE and yt_music and not is_url:
                try:
                    search_results = await loop.run_in_executor(None, lambda: yt_music.search(query, filter="songs", limit=limit))
                    if not search_results and limit == 1:
                        search_results = await loop.run_in_executor(None, lambda: yt_music.search(query, limit=10))
                    
                    if search_results:
                        for res in search_results:
                            vid = res.get('videoId')
                            if vid:
                                title = res.get('title', 'Desconocido')
                                artists = res.get('artists', [{'name': 'Desconocido'}])
                                uploader = artists[0]['name'] if artists else 'Desconocido'
                                duration = res.get('duration_seconds', 0) or 0
                                thumbnails = res.get('thumbnails', [])
                                thumbnail = thumbnails[-1]['url'] if thumbnails else None
                                
                                tracks.append({
                                    'title': title,
                                    'uploader': uploader,
                                    'duration': duration,
                                    'thumbnail': thumbnail,
                                    'url': f"https://www.youtube.com/watch?v={vid}",
                                    'query': original_query,
                                    'requester': ctx.author
                                })
                        if tracks:
                            if limit == 1:
                                return tracks[:1]
                            return tracks

                    video_id = None
                    if search_results:
                        for res in search_results:
                            if 'videoId' in res and res['videoId']:
                                video_id = res['videoId']
                                break
                    
                    if video_id:
                        query = f"https://music.youtube.com/watch?v={video_id}"
                    else:
                        query = f"ytmsearch:{original_query}"
                except Exception as e:
                    logger.warning(f"Error en ytmusicapi search, usando Wavelink/Lavalink: {e}")
                    query = f"ytmsearch:{original_query}"

            # ✅ Failsafe: si por cualquier motivo query no es una URL ni ya trae un prefijo de
            # búsqueda (ytmsearch:, spsearch:, etc.), Lavalink no sabe qué hacer con texto plano.
            # Esto pasaba cuando YTMUSIC_AVAILABLE es False o is_url es True pero el bloque de
            # arriba no llegó a poner el prefijo — causaba búsquedas silenciosamente vacías.
            # Usamos ytmsearch: (YouTube Music) en vez de ytsearch: (YouTube normal) para
            # priorizar la pista de audio limpia en vez del video musical oficial.
            if not is_url and ':' not in query.split(' ')[0]:
                query = f"ytmsearch:{query}"

            # ✅ Fallback con Wavelink/Lavalink en vez de yt-dlp directo: el nodo de Lavalink es
            # el que le pide el audio a YouTube, no el bot, así se evita el bloqueo de IP.
            wl_results = await buscar_tracks_con_fallback(query)
            if not wl_results:
                return tracks

            if isinstance(wl_results, wavelink.Playlist):
                entries = wl_results.tracks
            else:
                entries = wl_results

            for entry in entries:
                tracks.append({
                    'title': entry.title or 'Desconocido',
                    'uploader': entry.author or 'Artista Desconocido',
                    'duration': (entry.length or 0) // 1000,
                    'thumbnail': entry.artwork,
                    'url': entry.uri or f"ytsearch:{original_query}",
                    'query': original_query,
                    'requester': ctx.author
                })
        except Exception as e:
            logger.error(f"Error al extraer fuentes: {e}")

        if limit == 1 and len(tracks) > 1:
            return tracks[:1]

        return tracks


class SimilarSongsView(discord.ui.View):
    """Vista interactiva para seleccionar canciones similares de la radio"""
    def __init__(self, cog, ctx, current_track, similar_tracks, offset=0):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.current_track = current_track
        self.similar_tracks = similar_tracks
        self.offset = offset
        self.update_components()

    def update_components(self):
        self.clear_items()
        if not self.similar_tracks:
            return

        options = []
        for i, track in enumerate(self.similar_tracks[:5]):
            label = f"{i+1}. {track['title']}"[:100]
            desc = f"Art: {track['uploader']}"[:100]
            options.append(discord.SelectOption(label=label, description=desc, value=str(i)))

        select = discord.ui.Select(placeholder="Selecciona canciones para añadir...", min_values=1, max_values=len(options), options=options)
        select.callback = self.select_callback
        self.add_item(select)

        add_all_btn = discord.ui.Button(label="Añadir las 5", emoji="➕", style=discord.ButtonStyle.success)
        add_all_btn.callback = self.add_all_callback
        self.add_item(add_all_btn)

        next_batch_btn = discord.ui.Button(label="Siguientes 5", emoji="🔄", style=discord.ButtonStyle.primary)
        next_batch_btn.callback = self.next_batch_callback
        self.add_item(next_batch_btn)

        cancel_btn = discord.ui.Button(label="Cancelar", emoji="❌", style=discord.ButtonStyle.danger)
        cancel_btn.callback = self.cancel_callback
        self.add_item(cancel_btn)

    def create_embed(self):
        embed = discord.Embed(
            title="🎶 Canciones Similares (Radio)",
            description=f"Basado en: **{self.current_track['title']}**\nSelecciona las canciones que deseas añadir a la fila:",
            color=discord.Color.from_rgb(138, 43, 226)
        )
        desc_text = ""
        for i, track in enumerate(self.similar_tracks[:5], 1):
            dur = f"{int(track.get('duration', 0)//60)}:{int(track.get('duration', 0)%60):02d}"
            desc_text += f"`{i}.` **{track['title']}** — *{track['uploader']}* (`{dur}`)\n"
            
        embed.add_field(name="Sugerencias actuales", value=desc_text or "No hay sugerencias.", inline=False)
        embed.set_footer(text="Usa el menú desplegable o los botones de abajo.")
        return embed

    async def select_callback(self, interaction: discord.Interaction):
        guild_id = self.ctx.guild.id
        if not self.cog.has_control_permission(guild_id, interaction.user, self.current_track, self.ctx.voice_client):
            await interaction.response.send_message("❌ No tienes permisos para usar estos controles.", ephemeral=True)
            return

        if guild_id not in self.cog.queues:
            self.cog.queues[guild_id] = []

        selected_indices = [int(val) for val in interaction.data['values']]
        added_count = 0
        
        for idx in selected_indices:
            if idx < len(self.similar_tracks):
                track = self.similar_tracks[idx].copy()
                track['requester'] = interaction.user
                self.cog.queues[guild_id].append(track)
                added_count += 1
                pos = len(self.cog.queues[guild_id])
                embed = self.cog.create_added_embed(track, pos)
                await self.ctx.send(embed=embed, silent=True)

        self.cog.save_queue_to_db(guild_id)
        await interaction.response.edit_message(content=f"✅ Se han añadido **{added_count}** canciones seleccionadas a la fila.", embed=None, view=None)
        self.stop()

    async def add_all_callback(self, interaction: discord.Interaction):
        guild_id = self.ctx.guild.id
        if not self.cog.has_control_permission(guild_id, interaction.user, self.current_track, self.ctx.voice_client):
            await interaction.response.send_message("❌ No tienes permisos para usar estos controles.", ephemeral=True)
            return

        if guild_id not in self.cog.queues:
            self.cog.queues[guild_id] = []

        added_count = 0
        for track_info in self.similar_tracks[:5]:
            track = track_info.copy()
            track['requester'] = interaction.user
            self.cog.queues[guild_id].append(track)
            added_count += 1
            pos = len(self.cog.queues[guild_id])
            embed = self.cog.create_added_embed(track, pos)
            await self.ctx.send(embed=embed, silent=True)

        self.cog.save_queue_to_db(guild_id)
        await interaction.response.edit_message(content=f"✅ Se han añadido las **{added_count}** canciones sugeridas a la fila.", embed=None, view=None)
        self.stop()

    async def next_batch_callback(self, interaction: discord.Interaction):
        guild_id = self.ctx.guild.id
        if not self.cog.has_control_permission(guild_id, interaction.user, self.current_track, self.ctx.voice_client):
            await interaction.response.send_message("❌ No tienes permisos para usar estos controles.", ephemeral=True)
            return

        await interaction.response.defer()
        new_offset = self.offset + 5
        next_tracks = await self.cog.fetch_similar_tracks(self.current_track, offset=new_offset, limit=5)
        
        if not next_tracks:
            await interaction.followup.send("⚠️ No hay más canciones similares disponibles en este momento.", ephemeral=True)
            return

        self.offset = new_offset
        self.similar_tracks = next_tracks
        self.update_components()
        embed = self.create_embed()
        await interaction.edit_original_response(embed=embed, view=self)

    async def cancel_callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="❌ Búsqueda de canciones similares cancelada.", embed=None, view=None)
        self.stop()


class MusicControlView(discord.ui.View):
    """Botones interactivos sin spam de mensajes efímeros"""
    def __init__(self, cog, guild_id):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        voice_client = interaction.guild.voice_client
        if not voice_client or not voice_client.channel:
            await interaction.response.send_message("❌ El bot no está en ningún canal de voz.", ephemeral=True)
            return False
        if not interaction.user.voice or interaction.user.voice.channel != voice_client.channel:
            await interaction.response.send_message("❌ Debes estar en el mismo canal de voz que el bot para usar los controles.", ephemeral=True)
            return False
        return True

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        current = self.cog.current.get(self.guild_id)
        if not self.cog.has_control_permission(self.guild_id, interaction.user, current, interaction.guild.voice_client):
            await interaction.response.send_message(f"❌ {get_user_display_name(interaction.user)}, no tienes permisos para regresar la canción.", ephemeral=True)
            return
        
        await interaction.response.defer()
        ctx = await self.cog.bot.get_context(interaction.message)
        ctx.author = interaction.user
        await self.cog.previous_track(ctx)

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.primary)
    async def pause_play_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        current = self.cog.current.get(self.guild_id)
        if not self.cog.has_control_permission(self.guild_id, interaction.user, current, interaction.guild.voice_client):
            await interaction.response.send_message(f"❌ {get_user_display_name(interaction.user)}, los permisos de pausa están desactivados.", ephemeral=True)
            return

        voice_client = interaction.guild.voice_client
        if voice_client:
            await interaction.response.defer()
            display_name = get_user_display_name(interaction.user)
            if voice_client.is_playing():
                voice_client.pause()
                await interaction.channel.send(f"⏸️ Música pausada por {display_name}.", silent=True)
            elif voice_client.is_paused():
                voice_client.resume()
                await interaction.channel.send(f"▶️ Música reanudada por {display_name}.", silent=True)
        else:
            await interaction.response.send_message("❌ El bot no está en un canal de voz.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        current = self.cog.current.get(self.guild_id)
        has_general_perm = self.cog.has_control_permission(self.guild_id, interaction.user, current, interaction.guild.voice_client)
        is_own_song = current and current.get('requester') == interaction.user

        if not has_general_perm and not is_own_song:
            await interaction.response.send_message(f"❌ {get_user_display_name(interaction.user)}, no puedes omitir esta canción.", ephemeral=True)
            return

        await interaction.response.defer()
        ctx = await self.cog.bot.get_context(interaction.message)
        ctx.author = interaction.user
        await self.cog.next_track(ctx)

    @discord.ui.button(label="Ver Fila", emoji="📜", style=discord.ButtonStyle.success)
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        ctx = await self.cog.bot.get_context(interaction.message)
        ctx.author = interaction.user
        await self.cog.mfila(ctx)

    @discord.ui.button(label="Buscar", emoji="🔍", style=discord.ButtonStyle.blurple)
    async def search_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = await self.cog.bot.get_context(interaction.message)
        ctx.author = interaction.user
        await interaction.response.send_modal(SearchModal(self.cog, ctx))

    @discord.ui.button(label="Permisos", emoji="⚙️", style=discord.ButtonStyle.grey)
    async def perms_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = PermissionsConfigView(self.cog, self.guild_id)
        current_state = self.cog.get_temp_permissions(self.guild_id)
        status = "🌐 Libres (Todos en llamada)" if current_state else "🔒 Restringidos (Personalizado / Anfitrión)"
        host = self.cog.hosts.get(self.guild_id)
        host_obj = interaction.guild.get_member(host) if host else None
        host_name = get_user_display_name(host_obj) if host_obj else "Ninguno"
        
        await interaction.response.send_message(
            f"⚙️ **Configuración de Permisos Temporales**\n• Estado general: {status}\n• Anfitrión: **{host_name}**\n\nSelecciona usuarios, roles o cambia el modo global abajo:",
            view=view,
            ephemeral=True
        )


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
        self.temp_allowed_users = {}      
        self.temp_allowed_roles = {}      
        self.text_channels = {}  # guild_id -> último canal de texto usado, para avisar cuando Wavelink termine una canción sola

    def save_queue_to_db(self, guild_id: int):
        """Guarda la cola actual del servidor en la BD en formato JSON"""
        try:
            queue_data = []
            for track in self.queues.get(guild_id, []):
                req = track.get('requester')
                req_id = req.id if isinstance(req, (discord.Member, discord.User)) else track.get('requester_id', 0)
                queue_data.append({
                    'title': track.get('title'),
                    'uploader': track.get('uploader'),
                    'duration': track.get('duration', 0),
                    'thumbnail': track.get('thumbnail'),
                    'url': track.get('url'),
                    'query': track.get('query', ''),
                    'requester_id': req_id
                })
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

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
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
            return "🔴 ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬ [EN VIVO]"
        
        bars = 15
        progress = min(max(current_sec / total_sec, 0), 1)
        filled = int(bars * progress)
        bar = "▬" * filled + "🔘" + "▬" * (bars - filled - 1)
        
        mins_current, secs_current = divmod(int(current_sec), 60)
        mins_total, secs_total = divmod(int(total_sec), 60)
        
        return f"{bar}\n`{mins_current}:{secs_current:02d} / {mins_total}:{secs_total:02d}`"

    def create_music_embed(self, ctx, track, progress_bar, queue_len=0, queue_position=1):
        embed = discord.Embed(
            title="🎵 Reproduciendo Música",
            description=f"**{track['title']}**",
            color=discord.Color.from_rgb(184, 134, 11)
        )
        
        embed.add_field(name="🎤 Artista", value=track.get('uploader', 'Desconocido'), inline=False)
        duration_str = f"`{int(track.get('duration', 0) // 60)}:{int(track.get('duration', 0) % 60):02d}`"
        embed.add_field(name="⏱️ Duración", value=duration_str, inline=True)
        embed.add_field(name="📊 Posición en Fila", value=f"`#{queue_position}`", inline=True)
        embed.add_field(name="📑 Total en Fila", value=f"`{queue_len}` canciones", inline=True)
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
        if not YTMUSIC_AVAILABLE or not yt_music:
            return []
        
        loop = self.bot.loop
        try:
            video_id = None
            url = track.get('url', '')
            if 'v=' in url:
                try:
                    video_id = url.split('v=')[1].split('&')[0]
                except:
                    pass
            
            if not video_id:
                search_query = f"{track['title']} {track.get('uploader', '')}"
                res = await loop.run_in_executor(None, lambda: yt_music.search(search_query, filter="songs", limit=1))
                if res and 'videoId' in res[0]:
                    video_id = res[0]['videoId']
            
            if not video_id:
                return []
                
            watch_playlist = await loop.run_in_executor(None, lambda: yt_music.get_watch_playlist(videoId=video_id))
            raw_tracks = watch_playlist.get('tracks', [])
            
            similar = []
            seen_ids = set()
            if 'v=' in url:
                seen_ids.add(url.split('v=')[1].split('&')[0])

            for t in raw_tracks:
                vid = t.get('videoId')
                if not vid or vid in seen_ids:
                    continue
                seen_ids.add(vid)
                
                title = t.get('title', 'Desconocido')
                artists = t.get('artists', [{'name': 'Desconocido'}])
                uploader = artists[0]['name'] if artists else 'Desconocido'
                duration = t.get('duration_seconds', 0) or 0
                thumbnails = t.get('thumbnail', [])
                thumbnail = thumbnails[-1]['url'] if thumbnails else None
                
                similar.append({
                    'title': title,
                    'uploader': uploader,
                    'duration': duration,
                    'thumbnail': thumbnail,
                    'url': f"https://www.youtube.com/watch?v={vid}",
                    'query': f"https://www.youtube.com/watch?v={vid}",
                })
                
            return similar[offset:offset+limit]
        except Exception as e:
            logger.error(f"Error obteniendo canciones similares: {e}")
            return []

    async def play_next(self, ctx):
        guild_id = ctx.guild.id
        
        if guild_id in self.update_tasks and self.update_tasks[guild_id]:
            try:
                self.update_tasks[guild_id].cancel()
            except:
                pass

        if guild_id not in self.queues or not self.queues[guild_id]:
            self.current[guild_id] = None
            self.save_queue_to_db(guild_id)
            if guild_id in self.current_messages:
                try:
                    old_msg = self.current_messages[guild_id]
                    embed = old_msg.embeds[0]
                    embed.color = discord.Color.dark_grey()
                    embed.set_footer(text="Reproducción finalizada.")
                    await old_msg.edit(embed=embed, view=None)
                except:
                    pass
            await ctx.send("✅ Ya no hay más canciones en la fila.", silent=True)
            return

        if guild_id in self.current and self.current[guild_id]:
            if guild_id not in self.history:
                self.history[guild_id] = []
            self.history[guild_id].append(self.current[guild_id])

        track = self.queues[guild_id].pop(0)
        self.current[guild_id] = track
        self.save_queue_to_db(guild_id)
        self.start_times[guild_id] = time.time()
        self.text_channels[guild_id] = ctx.channel  # para poder avisar cuando el track termine solo (evento de Wavelink)

        try:
            # ✅ El nodo de Lavalink es el que le pide el audio a YouTube/SoundCloud, no el bot,
            # así se evita el bloqueo de IP que tenías con yt-dlp directo.
            wl_results = await buscar_tracks_con_fallback(track['url'])

            # 🔁 Fallback: si el link exacto (video ID) fue bloqueado/restringido por Lavalink
            # (típico en canciones de sellos grandes por Content ID/región), reintenta con una
            # búsqueda por texto en vez del link directo — a veces resuelve otro video válido
            # de la misma canción.
            if not wl_results:
                fallback_query = f"ytmsearch:{track['title']} {track.get('uploader','')}".strip()
                logger.warning(f"⚠️ '{track['url']}' no cargó en Lavalink, reintentando con búsqueda: {fallback_query}")
                wl_results = await buscar_tracks_con_fallback(fallback_query)

            if not wl_results:
                raise RuntimeError(
                    f"Lavalink no pudo cargar '{track['title']}' (probablemente bloqueada por "
                    f"región/Content ID en los nodos públicos, o el video ya no está disponible)."
                )
            wl_track = wl_results.tracks[0] if isinstance(wl_results, wavelink.Playlist) else wl_results[0]

            await ctx.voice_client.play(wl_track)

            bar_text = self.generate_progress_bar(0, track.get('duration', 0))
            queue_len = len(self.queues.get(guild_id, []))
            embed = self.create_music_embed(ctx, track, bar_text, queue_len=queue_len)
            msg = await ctx.send(embed=embed, view=MusicControlView(self, guild_id), silent=True)
            self.current_messages[guild_id] = msg
            self.last_update[guild_id] = time.time()

            self.update_tasks[guild_id] = self.bot.loop.create_task(self.update_embed_loop(ctx, guild_id))
            
        except Exception as e:
            error_message = str(e)
            # ✅ logger.exception guarda el traceback completo en consola, así se ve la razón
            # REAL que dio Lavalink/wavelink (age-restricted, región, video no disponible, etc)
            # en vez de solo "sin resultados".
            logger.exception(f"Error en play_next reproduciendo '{track.get('title')}': {error_message}")
            if "bot" in error_message.lower() or "sign in" in error_message.lower():
                self.current[guild_id] = None
                if ctx.voice_client:
                    await ctx.voice_client.disconnect()
                await ctx.send(
                    "Estoy experimentando un bloqueo temporal por parte de youtube :,c Espera de 5 a 10 minutos y vuelve a intentarlo.",
                    silent=False
                )
                return
            else:
                await ctx.send(
                    f"❌ No pude reproducir **{track.get('title', 'esa canción')}** ({error_message[:150]}). "
                    f"Pasando a la siguiente..."
                )
                await self.play_next(ctx)

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

        logger.error(f"⚠️ TrackException en guild {guild_id} reproduciendo '{track_title}': {reason}")

        if channel:
            await channel.send(
                f"⚠️ No se pudo reproducir **{track_title}** ({reason[:200]}). "
                f"Seguramente está bloqueada/restringida en el nodo actual (edad, región, Content ID). "
                f"Pasando a la siguiente...",
                silent=True
            )

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload):
        """Reemplaza el 'after=' callback de discord.py: Wavelink avisa por evento cuando una
        canción termina, y aquí seguimos igual que antes con la siguiente de la fila."""
        player = payload.player
        if not player or not player.guild:
            return
        guild_id = player.guild.id
        channel = self.text_channels.get(guild_id) or player.channel
        if not channel:
            return
        fake_ctx = self._CtxLike(player.guild, channel, player)
        await self.play_next(fake_ctx)

    async def update_embed_loop(self, ctx, guild_id):
        try:
            while ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
                if guild_id in self.current_messages and guild_id in self.start_times:
                    msg = self.current_messages[guild_id]
                    track = self.current[guild_id]
                    
                    elapsed = ctx.voice_client.position / 1000
                        
                    total_duration = track.get('duration', 0)
                    time_since_last = time.time() - self.last_update.get(guild_id, 0)
                    
                    if time_since_last >= 0.75:
                        progress = self.generate_progress_bar(elapsed, total_duration)
                        queue_len = len(self.queues.get(guild_id, []))
                        embed = self.create_music_embed(ctx, track, progress, queue_len=queue_len)
                        
                        try:
                            await msg.edit(embed=embed)
                            self.last_update[guild_id] = time.time()
                        except discord.errors.NotFound:
                            pass
                
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error en update_embed_loop: {e}")

    @commands.command(name='play', aliases=['p'])
    async def play(self, ctx, *, query: str):
        if not ctx.author.voice:
            await ctx.send("❌ ¡Perdón! pero debes estar en un canal de voz!")
            return

        guild_id = ctx.guild.id
        channel = ctx.author.voice.channel
        
        if ctx.voice_client is None:
            nodo = get_nodo_conectado()
            if not nodo:
                await ctx.send("❌ Ningún servidor de música está disponible ahorita. Intenta de nuevo en unos segundos.")
                return
            voice_client = await channel.connect(cls=MusicPlayer)
            if voice_client.node != nodo:
                try:
                    await voice_client.switch_node(nodo)
                except Exception as e:
                    logger.warning(f"⚠️ No se pudo cambiar al nodo {nodo.identifier}: {e}")
        elif ctx.voice_client.channel != channel:
            await ctx.voice_client.move_to(channel)

        if guild_id not in self.hosts or not ctx.guild.get_member(self.hosts[guild_id]):
            self.hosts[guild_id] = ctx.author.id

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

            was_playing = ctx.voice_client.is_playing() or ctx.voice_client.is_paused()

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

    @commands.command(name='helpm', aliases=['ayudam'])
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
            "`!max radio` - Muestra recomendaciones similares.\n"
            "`!max stop` - Desconecta el bot y limpia la cola."
            
        )
        embed.add_field(name="📜 Comandos", value=commands_list, inline=False)
        
        status_text = "🌐 Libres (Todos en llamada)" if perms_active else "🔒 Restringidos (Personalizado / Anfitrión)"
        user_status = "✅ Sí (Tienes acceso concedido)" if has_perm else "❌ No (Solo puedes omitir tus propias canciones)"
        
        embed.add_field(name="⚙️ Estado de Permisos para omitir canciones", value=status_text, inline=False)
        embed.add_field(name="👤 Tus Permisos", value=user_status, inline=False)
        embed.set_footer(text=f"Solicitado por {get_user_display_name(ctx.author)}")
        await ctx.send(embed=embed, silent=True)

    @commands.command(name='mfila', aliases=['queue', 'q', 'cola'])
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

    @commands.command(name='next', aliases=['n', 'skip-direct'])
    async def next_track(self, ctx):
        guild_id = ctx.guild.id
        if not ctx.voice_client or (not ctx.voice_client.is_playing() and not ctx.voice_client.is_paused()):
            await ctx.send("❌ No hay nada reproduciéndose.")
            return

        current = self.current.get(guild_id)
        has_general = self.has_control_permission(guild_id, ctx.author, current, ctx.voice_client)
        is_own = current and current.get('requester') == ctx.author

        if not has_general and not is_own:
            await ctx.send(f"❌ {get_user_display_name(ctx.author)}, no tienes permisos para omitir esta canción.")
            return

        if guild_id in self.update_tasks:
            self.update_tasks[guild_id].cancel()

        await ctx.send(f"⏭️ Canción omitida por {get_user_display_name(ctx.author)}.", silent=True)
        ctx.voice_client.stop()

    @commands.command(name='previous', aliases=['prev', 'back', 'previus'])
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
        else:
            await self.play_next(ctx)

    @commands.command(name='skip')
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
            if guild_id in self.update_tasks:
                self.update_tasks[guild_id].cancel()
            ctx.voice_client.stop()
            self.skip_votes[guild_id] = set()
        else:
            await ctx.send(f"🗳️ Voto registrado (`{current_votes}/{required_votes}` necesarios).")

    @commands.command(name='pause')
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

    @commands.command(name='reanude', aliases=['reanudar'])
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

    @commands.command(name='radio', aliases=['recomendadas', 'similares'])
    async def show_similar_songs_cmd(self, ctx):
        guild_id = ctx.guild.id
        if not ctx.voice_client or not ctx.voice_client.channel:
            await ctx.send("❌ El bot no está en ningún canal de voz.")
            return
        if not ctx.author.voice or ctx.author.voice.channel != ctx.voice_client.channel:
            await ctx.send("❌ Debes estar en el mismo canal de voz que el bot para ver recomendaciones.")
            return

        current = self.current.get(guild_id)
        if not current:
            await ctx.send("❌ No hay ninguna canción reproduciéndose para basar las recomendaciones.")
            return

        async with ctx.typing():
            tracks = await self.fetch_similar_tracks(current, offset=0, limit=5)
            if not tracks:
                await ctx.send("❌ No se pudieron encontrar canciones similares en este momento.")
                return

            view = SimilarSongsView(self, ctx, current, tracks, offset=0)
            embed = view.create_embed()
            await ctx.send(embed=embed, view=view, silent=True)

    @commands.command(name='stop', aliases=['leave', 'dc'])
    async def stop(self, ctx):
        guild_id = ctx.guild.id
        if guild_id in self.update_tasks and self.update_tasks[guild_id]:
            self.update_tasks[guild_id].cancel()
        if guild_id in self.queues:
            self.queues[guild_id].clear()
        if guild_id in self.current:
            self.current[guild_id] = None
            
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

    @commands.command(name='ytlogin', aliases=['login_youtube', 'cookies'])
    @commands.is_owner()
    async def ytlogin(self, ctx, espera_minutos: int = 1):
        """Inicia sesión 100% automática (sin ventana, corre en terminal/headless) en la
        cuenta de YouTube/Google dedicada al bot, usando las credenciales guardadas en
        youtube_login.py. Al terminar, genera cookies.txt y yt-dlp las usa de inmediato
        (sin reiniciar el bot). Solo el dueño del bot puede usarlo.
        Uso: !ytlogin  (o !ytlogin 2 para darle 2 minutos de margen en vez de 1)."""
        await ctx.send(
            f"🤖 Iniciando sesión automática desde terminal (sin ventana), dale hasta "
            f"{espera_minutos} min..."
        )
        loop = self.bot.loop
        try:
            exito = await loop.run_in_executor(
                None, lambda: youtube_login.generar_cookies_youtube(espera_minutos * 60)
            )
        except RuntimeError as e:
            await ctx.send(f"❌ {e}")
            return
        except Exception as e:
            logger.error(f"Error en !ytlogin: {e}")
            await ctx.send(f"❌ Ocurrió un error iniciando sesión: {e}")
            return

        if exito:
            rebuild_ytdl()
            await ctx.send("✅ Cookies generadas y aplicadas. Ya puedes reproducir sin reiniciar el bot.")
        else:
            await ctx.send(
                f"⛔ No se pudo iniciar sesión en {espera_minutos} min. Revisa la consola del bot "
                f"(ahí dice el motivo exacto: correo/contraseña mal, o que Google pidió verificación "
                f"extra que un login sin ventana no puede resolver) y la captura 'youtube_login_debug.png'."
            )


async def _conectar_nodo_lavalink(bot, nodo: "wavelink.Node", resultados: list):
    """Conecta UN nodo de Lavalink en su propia tarea, corriendo en paralelo con los demás.
    NO se espera (await) en setup() — así el bot no se bloquea ni se dispara antes de que
    bot.user esté listo (login completo), que es lo que wavelink necesita para el handshake.
    Guarda su resultado (True/False) en la lista compartida `resultados` para el conteo final."""
    try:
        await wavelink.Pool.connect(nodes=[nodo], client=bot)
        logger.debug(f"✅ Nodo Lavalink conectado: {nodo.uri}")
        resultados.append(True)
    except Exception as e:
        logger.debug(f"❌ No se pudo conectar el nodo Lavalink {nodo.uri}: {type(e).__name__}: {e}", exc_info=True)
        resultados.append(False)


async def _resumen_conexion_lavalink(bot, total_nodos: int, resultados: list):
    """Espera a que el bot esté listo (bot.wait_until_ready) y unos segundos extra para dar
    tiempo a que los nodos intenten conectar, luego loguea un solo resumen del total."""
    await bot.wait_until_ready()
    await asyncio.sleep(8)  # margen para que los nodos lentos alcancen a responder
    exitosos = sum(1 for r in resultados if r is True)
    logger.info(f"🎵 Lavalink: {exitosos}/{total_nodos} nodo(s) conectados correctamente al arrancar.")


# ✅ Carpeta donde vive Lavalink.jar + application.yml. Ajusta esta ruta si los pusiste en otro
# lado — por defecto asume una carpeta "lavalink" al lado de la carpeta del bot.
LAVALINK_SERVER_DIR = Path(__file__).parent.parent / "lavalink"
LAVALINK_JAR_PATH = LAVALINK_SERVER_DIR / "Lavalink.jar"

_lavalink_process = None  # referencia global al proceso, para poder cerrarlo si el bot se apaga


def _iniciar_servidor_lavalink_local():
    """Lanza 'java -jar Lavalink.jar' como subproceso, SOLO si el .jar existe en la ruta
    esperada (así este código no rompe nada para quien no use un Lavalink local, solo usa
    nodos públicos). El proceso corre en segundo plano; su output se manda a lavalink.log
    dentro de la misma carpeta, para no ensuciar la consola del bot."""
    global _lavalink_process

    if not LAVALINK_JAR_PATH.exists():
        logger.info("ℹ️ No se encontró Lavalink.jar local — se usarán solo los nodos configurados en LAVALINK_NODES_CONFIG.")
        return

    log_path = LAVALINK_SERVER_DIR / "lavalink.log"
    try:
        log_file = open(log_path, "a", encoding="utf-8")
        _lavalink_process = subprocess.Popen(
            ["java", "-jar", "Lavalink.jar"],
            cwd=str(LAVALINK_SERVER_DIR),
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        logger.info(f"🎶 Servidor Lavalink local iniciado (PID {_lavalink_process.pid}). Logs en: {log_path}")
    except FileNotFoundError:
        logger.error("❌ No se encontró el comando 'java'. Instala Java (apt install openjdk-21-jre-headless) para usar Lavalink local.")
    except Exception as e:
        logger.error(f"❌ No se pudo iniciar Lavalink local: {type(e).__name__}: {e}")


async def setup(bot):
    # ✅ Si hay un Lavalink.jar local, lo levantamos primero y le damos unos segundos para que
    # termine de arrancar (carga plugins, etc.) antes de que wavelink intente conectarse a él.
    _iniciar_servidor_lavalink_local()
    if _lavalink_process is not None:
        await asyncio.sleep(10)  # margen para que el server local termine de levantar

    # ✅ Cada nodo se conecta en su propia tarea de fondo (fire-and-forget, sin await aquí), igual
    # que antes — esto es importante porque bot.user todavía puede no estar listo si se espera
    # (await) demasiado pronto en setup(), lo cual rompe el handshake de wavelink. El resumen se
    # calcula aparte, en segundo plano, dando tiempo a que el bot termine de loguearse.
    nodos = _build_wavelink_nodes()
    resultados = []
    for nodo in nodos:
        asyncio.create_task(_conectar_nodo_lavalink(bot, nodo, resultados))
    asyncio.create_task(_resumen_conexion_lavalink(bot, len(nodos), resultados))
    logger.info(f"🔄 Conectando en paralelo a {len(nodos)} nodo(s) de Lavalink en segundo plano...")

    await bot.add_cog(Music(bot))