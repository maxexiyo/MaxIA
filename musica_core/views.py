import discord
import math
import time

from musica_core.utils import get_user_display_name
from musica_core.nodos import conectar_player
from musica_core.fuentes import YTMusicSource

from database import db


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
            voice_client = await conectar_player(user_channel)
        elif voice_client.channel != user_channel:
            await voice_client.move_to(user_channel)

        was_playing = self.cog.is_active(guild_id, voice_client)

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
            await conectar_player(user_channel)
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
        was_playing = self.cog.is_active(guild_id, voice_client)

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
    def __init__(self, cog, guild_id=None):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id  # solo informativo: cada botón usa interaction.guild_id (así la vista es persistente)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        voice_client = interaction.guild.voice_client
        if not voice_client or not voice_client.channel:
            await interaction.response.send_message("❌ El bot no está en ningún canal de voz.", ephemeral=True)
            return False
        if not interaction.user.voice or interaction.user.voice.channel != voice_client.channel:
            await interaction.response.send_message("❌ Debes estar en el mismo canal de voz que el bot para usar los controles.", ephemeral=True)
            return False
        return True

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, custom_id="maxmusic:prev")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        current = self.cog.current.get(interaction.guild_id)
        if not self.cog.has_control_permission(interaction.guild_id, interaction.user, current, interaction.guild.voice_client):
            await interaction.response.send_message(f"❌ {get_user_display_name(interaction.user)}, no tienes permisos para regresar la canción.", ephemeral=True)
            return
        
        await interaction.response.defer()
        ctx = await self.cog.bot.get_context(interaction.message)
        ctx.author = interaction.user
        await self.cog.previous_track(ctx)

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.primary, custom_id="maxmusic:playpause")
    async def pause_play_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        current = self.cog.current.get(interaction.guild_id)
        if not self.cog.has_control_permission(interaction.guild_id, interaction.user, current, interaction.guild.voice_client):
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

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="maxmusic:next")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        current = self.cog.current.get(interaction.guild_id)
        has_general_perm = self.cog.has_control_permission(interaction.guild_id, interaction.user, current, interaction.guild.voice_client)
        is_own_song = current and current.get('requester') == interaction.user

        if not has_general_perm and not is_own_song:
            await interaction.response.send_message(f"❌ {get_user_display_name(interaction.user)}, no puedes omitir esta canción.", ephemeral=True)
            return

        await interaction.response.defer()
        ctx = await self.cog.bot.get_context(interaction.message)
        ctx.author = interaction.user
        await self.cog.next_track(ctx)

    @discord.ui.button(label="Ver Fila", emoji="📜", style=discord.ButtonStyle.success, custom_id="maxmusic:fila")
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        ctx = await self.cog.bot.get_context(interaction.message)
        ctx.author = interaction.user
        await self.cog.mfila(ctx)

    @discord.ui.button(label="Buscar", emoji="🔍", style=discord.ButtonStyle.blurple, custom_id="maxmusic:buscar")
    async def search_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = await self.cog.bot.get_context(interaction.message)
        ctx.author = interaction.user
        await interaction.response.send_modal(SearchModal(self.cog, ctx))

    @discord.ui.button(label="Permisos", emoji="⚙️", style=discord.ButtonStyle.grey, custom_id="maxmusic:permisos")
    async def perms_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = PermissionsConfigView(self.cog, interaction.guild_id)
        current_state = self.cog.get_temp_permissions(interaction.guild_id)
        status = "🌐 Libres (Todos en llamada)" if current_state else "🔒 Restringidos (Personalizado / Anfitrión)"
        host = self.cog.hosts.get(interaction.guild_id)
        host_obj = interaction.guild.get_member(host) if host else None
        host_name = get_user_display_name(host_obj) if host_obj else "Ninguno"
        
        await interaction.response.send_message(
            f"⚙️ **Configuración de Permisos Temporales**\n• Estado general: {status}\n• Anfitrión: **{host_name}**\n\nSelecciona usuarios, roles o cambia el modo global abajo:",
            view=view,
            ephemeral=True
        )
