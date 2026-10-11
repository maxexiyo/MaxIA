"""
AccionesMixin: la IA puede usar los comandos REALES del bot (música, transcripción, reaction
roles) a nombre de quien se lo pide, tipo asistente.

Cómo funciona: no se reimplementa nada. Se arma un mensaje "falso" (copia del mensaje original
con otro texto, ej. "!max play despacito") y se pasa por bot.invoke(). Así corre el comando de
verdad con TODOS sus checks de permisos, igual que si la persona lo hubiera escrito ella misma:
la IA nunca puede hacer algo que quien se lo pide no podría hacer por su cuenta.

Para agregar otra acción: define su tool en tools_schema.py (ACCIONES_TOOLS_DEFINITIONS) y agrega
su rama en ejecutar_accion_bot().
"""
import asyncio
import copy
import re
import time
import unicodedata
import discord
from discord.ext import commands

# Segundos que una petición queda esperando a que la persona entre a un canal de voz
PENDIENTE_TTL = 300

# acción de la tool 'controlar_musica' -> comando real del cog de música
ACCIONES_MUSICA = {
    "pausar": "pause",
    "reanudar": "reanude",
    "saltar": "next",
    "anterior": "previous",
    "detener": "stop",
    "fila": "mfila",
    "radio": "radio",
}


def _log_tarea(tarea: asyncio.Task):
    if not tarea.cancelled() and tarea.exception():
        print(f"⚠️ [Acción] Falló un comando lanzado en segundo plano: {tarea.exception()!r}")


class PlaylistConfirmView(discord.ui.View):
    """Botones para confirmar o cancelar una playlist propuesta por la IA (solo quien la pidió)."""
    def __init__(self, mixin, origen, miembro, nombre, canciones):
        super().__init__(timeout=180)
        self.mixin, self.origen, self.miembro = mixin, origen, miembro
        self.nombre, self.canciones = nombre, canciones
        self.mensaje = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.miembro.id:
            await interaction.response.send_message("❌ Esta propuesta es de otra persona.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Añadir a la fila", emoji="➕", style=discord.ButtonStyle.success)
    async def añadir(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="⏳ Buscando y añadiendo las canciones...", view=None)
        try:
            n = await self.mixin._agregar_playlist(self.origen, interaction.user, self.canciones)
        except Exception as e:
            print(f"⚠️ [Acción] Error añadiendo playlist: {type(e).__name__}: {e}")
            n = -2
        if n == -1:
            texto = "❌ Tienes que estar en un canal de voz para añadirla."
        elif n < 0:
            texto = "❌ No pude añadir la playlist, algo falló."
        else:
            texto = f"✅ **{self.nombre}**: añadí {n}/{len(self.canciones)} canciones a la fila."
        await interaction.edit_original_response(content=texto, view=None)

    @discord.ui.button(label="Cancelar", emoji="❌", style=discord.ButtonStyle.danger)
    async def cancelar(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="🗑️ Playlist descartada.", embed=None, view=None)

    async def on_timeout(self):
        if self.mensaje:
            try:
                await self.mensaje.edit(view=None)
            except Exception:
                pass


class AccionesMixin:
    # ───────────────────────── Mecánica: correr un comando real ─────────────────────────
    async def _prefijo_comandos(self, mensaje: discord.Message) -> str:
        pref = await self.bot.get_prefix(mensaje)
        if isinstance(pref, str):
            return pref
        # Si hay varios prefijos, mejor uno normal que la mención (<@id> )
        normales = [p for p in pref if not p.startswith("<@")]
        return (normales or list(pref))[0]

    async def _invocar_comando(self, origen: discord.Message, texto_comando: str,
                               autor: discord.Member = None, esperar: bool = True) -> bool:
        """Corre un comando real del bot como si 'autor' (o el autor de 'origen') lo hubiera
        escrito. Devuelve False si el comando no existe (ej. el cog no está cargado).
        esperar=False lo lanza en segundo plano (para comandos interactivos largos)."""
        falso = copy.copy(origen)
        falso.content = f"{await self._prefijo_comandos(origen)}{texto_comando}"
        if autor is not None:
            falso.author = autor
        ctx = await self.bot.get_context(falso)
        if not ctx.valid:
            return False
        if esperar:
            await self.bot.invoke(ctx)
        else:
            asyncio.create_task(self.bot.invoke(ctx)).add_done_callback(_log_tarea)
        return True

    async def _es_admin_o_owner(self, miembro: discord.Member) -> bool:
        if miembro.id == miembro.guild.owner_id or miembro.guild_permissions.administrator:
            return True
        return await self.bot.is_owner(miembro)

    @staticmethod
    def _en_voz(miembro: discord.Member) -> bool:
        """True si la persona está en un canal de voz. No confía solo en miembro.voice (puede venir
        vacío si el objeto Member es una copia vieja/sin caché): también busca su id en los canales."""
        if miembro.voice and miembro.voice.channel:
            return True
        guild = miembro.guild
        actual = guild.get_member(miembro.id)
        if actual and actual.voice and actual.voice.channel:
            return True
        canales = list(guild.voice_channels) + list(guild.stage_channels)
        return any(miembro.id in c.voice_states for c in canales)

    # ───────────────────────── Punto de entrada desde las tools ─────────────────────────
    async def ejecutar_accion_bot(self, tool_name: str, tool_input: dict, origen: discord.Message) -> str:
        """Ejecuta una acción pedida por el modelo y devuelve el resultado en texto (que el modelo
        usa para redactar su respuesta). NUNCA lanza excepciones: si lo hiciera, el motor pensaría
        que falló la key de la API y repetiría la acción con otra key (música doble, etc.)."""
        try:
            if origen.guild is None or not isinstance(origen.author, discord.Member):
                return "Esa acción solo funciona dentro de un servidor, no por mensaje directo."
            if tool_name == "poner_musica":
                return await self._accion_poner_musica(
                    origen, tool_input.get("consulta", ""),
                    saltar_actual=bool(tool_input.get("saltar_actual")), forzar=bool(tool_input.get("forzar")))
            if tool_name == "ver_fila_musica":
                return self._accion_ver_fila(origen)
            if tool_name == "controlar_musica":
                return await self._accion_controlar_musica(origen, tool_input.get("accion", ""))
            if tool_name == "proponer_playlist":
                return await self._accion_proponer_playlist(origen, tool_input.get("nombre", ""), tool_input.get("canciones", []))
            if tool_name == "transcribir_audio":
                return await self._accion_transcribir(origen)
            if tool_name == "crear_panel_roles":
                return await self._accion_panel_roles(origen)
            return f"No conozco la acción '{tool_name}'."
        except Exception as e:
            print(f"⚠️ [Acción] Error ejecutando '{tool_name}': {type(e).__name__}: {e}")
            return f"No se pudo completar la acción ({type(e).__name__}). Dile que algo falló."

    # ───────────────────────── Música ─────────────────────────
    # ── Fila: lectura y detección de repetidas ──
    @staticmethod
    def _tokens(texto: str) -> set:
        t = unicodedata.normalize("NFKD", texto.lower())
        t = "".join(c for c in t if not unicodedata.combining(c))
        return {w for w in re.findall(r"[a-z0-9]+", t) if len(w) > 1}

    def _pistas_en_fila(self, guild_id: int):
        """Lista [(etiqueta_posicion, track_dict)]: 'sonando' y luego #1, #2... de la fila."""
        cog = self.bot.get_cog("Music")
        if cog is None:
            return []
        pistas = []
        actual = cog.current.get(guild_id)
        if actual:
            pistas.append(("sonando ahora", actual))
        for i, t in enumerate(cog.queues.get(guild_id, []), 1):
            pistas.append((f"#{i} en la fila", t))
        return pistas

    def _buscar_repetida(self, guild_id: int, consulta: str):
        """Devuelve (etiqueta, track) si la consulta ya está sonando/en la fila, o None."""
        q = consulta.strip()
        es_link = q.startswith("http://") or q.startswith("https://")
        qt = self._tokens(q)
        for etiqueta, t in self._pistas_en_fila(guild_id):
            if es_link:
                if q == (t.get("url") or "") or (t.get("query") or "") == q:
                    return etiqueta, t
            elif qt and qt <= self._tokens(f"{t.get('title', '')} {t.get('uploader', '')}"):
                return etiqueta, t
        return None

    def _accion_ver_fila(self, origen: discord.Message) -> str:
        cog = self.bot.get_cog("Music")
        if cog is None:
            return "El módulo de música no está cargado ahorita."
        gid = origen.guild.id
        actual = cog.current.get(gid)
        fila = cog.queues.get(gid, [])
        hist = cog.history.get(gid, [])[-10:]
        fmt = lambda t: f"{t.get('title', '?')} — {t.get('uploader', '?')}"
        lineas = [f"Sonando ahora: {fmt(actual)}" if actual else "Ahorita no suena nada."]
        if fila:
            lineas.append(f"Fila ({len(fila)}):")
            lineas += [f"  {i}. {fmt(t)}" for i, t in enumerate(fila[:25], 1)]
            if len(fila) > 25:
                lineas.append(f"  ...y {len(fila) - 25} más")
        else:
            lineas.append("La fila está vacía.")
        if hist:
            lineas.append("Ya sonaron hace poco: " + "; ".join(fmt(t) for t in hist))
        return "\n".join(lineas)

    async def _accion_poner_musica(self, origen: discord.Message, consulta: str,
                                   saltar_actual: bool = False, forzar: bool = False) -> str:
        consulta = (consulta or "").strip()
        miembro = origen.author
        if not consulta:
            return "No me quedó claro qué canción o artista poner, pregúntale cuál quiere."

        if not self._en_voz(miembro):
            self._limpiar_pendientes()
            self.acciones_pendientes[(origen.guild.id, miembro.id)] = {
                "consulta": consulta,
                "mensaje": origen,
                "expira": time.monotonic() + PENDIENTE_TTL,
            }
            print(f"⏳ [Acción] Música pendiente para {miembro.display_name}: '{consulta}'")
            return (
                f"{miembro.display_name} NO está en ningún canal de voz, así que todavía NO se puso nada. "
                f"Quedó PENDIENTE '{consulta}': en cuanto se conecte a un canal de voz (tiene {PENDIENTE_TTL // 60} min) "
                f"la pongo sola. Díselo: que se conecte al canal que quiera y yo la pongo."
            )

        if not forzar:
            rep = self._buscar_repetida(origen.guild.id, consulta)
            if rep:
                etiqueta, t = rep
                return (f"NO se añadió: '{t.get('title')}' ya está {etiqueta}. Pregúntale a {miembro.display_name} si la quiere "
                        f"añadir de todos modos; si dice que sí, vuelve a llamar poner_musica con forzar=true.")

        cog = self.bot.get_cog("Music")
        gid = origen.guild.id
        saltar = False
        nota_permiso = ""
        if saltar_actual and cog is not None:
            vc = origen.guild.voice_client
            if vc and (vc.is_playing() or vc.is_paused()):
                actual = cog.current.get(gid)
                if cog.has_control_permission(gid, miembro, actual, vc) or (actual and actual.get("requester") == miembro):
                    saltar = True
                else:
                    nota_permiso = " (No tiene permiso para saltar la canción actual, así que solo se añadió a la fila; díselo.)"
        antes = len(cog.queues.get(gid, [])) if cog else 0

        if not await self._invocar_comando(origen, f"play {consulta}"):
            return "El módulo de música no está cargado ahorita, no puedo poner nada."

        if saltar:
            cola = cog.queues.get(gid, [])
            if len(cola) > antes:
                cola.insert(0, cola.pop())     # la nueva pasa al frente
                cog.save_queue_to_db(gid)
                await self._invocar_comando(origen, "next")
                return (f"Cambié la canción: '{consulta}' pasa a sonar ya (saltando la anterior). El propio comando avisa en el chat. "
                        f"Confirma MUY corto.")
        return (
            f"Ejecuté el comando de música con '{consulta}'. El propio comando avisa en el chat si la puso, "
            f"la encoló o si falló. Confirma MUY corto y sin afirmar que ya está sonando.{nota_permiso}"
        )

    async def _accion_controlar_musica(self, origen: discord.Message, accion: str) -> str:
        accion = (accion or "").strip().lower()
        comando = ACCIONES_MUSICA.get(accion)
        if not comando:
            return f"Esa acción de música no existe. Las válidas: {', '.join(ACCIONES_MUSICA)}."
        miembro = origen.author
        if accion != "fila" and not self._en_voz(miembro):
            return (f"{miembro.display_name} no está en un canal de voz, y para controlar la música tiene que "
                    f"estar en el mismo canal que yo. No se ejecutó nada, díselo.")
        if not await self._invocar_comando(origen, comando):
            return "El módulo de música no está cargado ahorita."
        return (
            f"Ejecuté '{accion}'. El propio comando avisa en el chat el resultado (si no tenía permisos, él lo dice). "
            f"Confirma MUY corto, sin inventar que funcionó."
        )

    # ───────────────────────── Playlists propuestas por la IA ─────────────────────────
    async def _accion_proponer_playlist(self, origen: discord.Message, nombre: str, canciones) -> str:
        if isinstance(canciones, str):
            txt = canciones.strip()
            try:                                   # por si el modelo mandó un JSON de lista como texto
                import json
                parsed = json.loads(txt)
                canciones = parsed if isinstance(parsed, list) else txt.splitlines()
            except Exception:
                canciones = txt.replace(";", "\n").splitlines()
        canciones = [re.sub(r"^\s*(\d+[.)]|[-•*])\s*", "", str(c)).strip() for c in (canciones or [])]
        canciones = [c for c in canciones if c][:15]
        nombre = (nombre or "Playlist de MaxIA").strip()[:80]
        if len(canciones) < 1:
            return "No armaste ninguna canción; vuelve a intentarlo con una lista."
        miembro = origen.author
        repetidas = [c for c in canciones if self._buscar_repetida(origen.guild.id, c)]
        canciones = [c for c in canciones if c not in repetidas]
        if not canciones:
            return "Todas esas canciones ya están en la fila o sonando; propón otras distintas (mira ver_fila_musica)."
        if not self._en_voz(miembro):
            return (f"{miembro.display_name} no está en un canal de voz, así que no se propuso nada. "
                    f"Dile que se conecte a uno y te lo vuelva a pedir.")
        if self.bot.get_cog("Music") is None:
            return "El módulo de música no está cargado ahorita."
        embed = discord.Embed(
            title=f"🎶 {nombre}",
            description="\n".join(f"`{i}.` {c}" for i, c in enumerate(canciones, 1)),
            color=discord.Color.from_rgb(184, 134, 11),
        )
        embed.set_footer(text=f"Propuesta para {miembro.display_name} · ¿La añado a la fila?")
        vista = PlaylistConfirmView(self, origen, miembro, nombre, canciones)
        vista.mensaje = await origen.channel.send(embed=embed, view=vista, silent=True,
                                                  allowed_mentions=self.SIN_PINGS_MASIVOS)
        self.ai_generated_messages.add(vista.mensaje.id)
        return (f"Ya mandé la playlist '{nombre}' ({len(canciones)} canciones) al chat con botones para que "
                f"{miembro.display_name} confirme. NO se ha puesto nada todavía. Coméntale brevemente qué onda "
                f"la selección (sin listar todas las canciones, ya salen en el mensaje) y que confirme con los botones."
                + (f" Quité {len(repetidas)} que ya estaban en la fila." if repetidas else ""))

    async def _agregar_playlist(self, origen: discord.Message, miembro: discord.Member, canciones: list) -> int:
        """Busca cada canción y la mete a la fila del cog de música; arranca si no suena nada."""
        from musica_core.fuentes import YTMusicSource
        from musica_core.nodos import conectar_player
        cog = self.bot.get_cog("Music")
        if cog is None or not self._en_voz(miembro):
            return -1
        falso = copy.copy(origen)
        falso.content = f"{await self._prefijo_comandos(origen)}play x"
        falso.author = miembro
        ctx = await self.bot.get_context(falso)
        gid = ctx.guild.id
        canal = miembro.voice.channel if miembro.voice else None
        if ctx.voice_client is None:
            if canal is None:
                return -1
            await conectar_player(canal)
        if gid not in cog.hosts or not ctx.guild.get_member(cog.hosts[gid]):
            cog.hosts[gid] = miembro.id
        cog.text_channels[gid] = ctx.channel
        if not cog.queues.get(gid):
            cog.queues[gid] = cog.load_queue_from_db(gid, ctx.guild)
        añadidas = 0
        for q in canciones:
            tracks = await YTMusicSource.create_sources_from_query(ctx, q, limit=1)
            if tracks:
                cog.queues[gid].append(tracks[0])
                añadidas += 1
        cog.save_queue_to_db(gid)
        if añadidas:
            await cog.play_next(ctx)   # solo arranca si no suena nada
        return añadidas

    # ───────────────────────── Transcripción ─────────────────────────
    async def _accion_transcribir(self, origen: discord.Message) -> str:
        if not await self._invocar_comando(origen, "transcribir"):
            return "El módulo de transcripción no está cargado ahorita, no puedo transcribir."
        return (
            "Ejecuté el comando de transcripción. Él mismo responde en el chat con el texto (o avisa si no "
            "encontró un audio). Confirma MUY corto, sin repetir ni inventar lo que dice el audio."
        )

    # ───────────────────────── Reaction roles ─────────────────────────
    async def _accion_panel_roles(self, origen: discord.Message) -> str:
        miembro = origen.author
        if not await self._es_admin_o_owner(miembro):
            return "No se puede: solo un administrador del servidor puede crear paneles de roles. No se ejecutó nada, díselo."
        # En segundo plano: el asistente de roles espera las respuestas de la persona por varios minutos
        if not await self._invocar_comando(origen, "rr make", esperar=False):
            return "El módulo de reaction roles no está cargado ahorita."
        return (
            "Arranqué el asistente interactivo de roles: el propio bot ya manda las preguntas paso a paso al chat "
            "y la persona las va contestando ahí. Solo avísale que siga los pasos, no hagas tú las preguntas."
        )

    # ───────────────────────── Peticiones pendientes (música sin estar en voz) ─────────────────────────
    def _limpiar_pendientes(self):
        ahora = time.monotonic()
        for clave in [k for k, v in self.acciones_pendientes.items() if v["expira"] < ahora]:
            self.acciones_pendientes.pop(clave, None)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """Si la persona tenía una canción pendiente y acaba de entrar a un canal de voz, la pone."""
        if member.bot or after.channel is None or after.channel == before.channel:
            return
        pendiente = self.acciones_pendientes.pop((member.guild.id, member.id), None)
        if not pendiente or time.monotonic() > pendiente["expira"]:
            return
        try:
            origen = pendiente["mensaje"]
            aviso = await origen.channel.send(
                f"🎶 {member.display_name}, ya te vi en el canal. Pongo: **{pendiente['consulta']}**",
                silent=True, allowed_mentions=self.SIN_PINGS_MASIVOS
            )
            self.ai_generated_messages.add(aviso.id)
            await self._invocar_comando(origen, f"play {pendiente['consulta']}", autor=member)
        except Exception as e:
            print(f"⚠️ [Acción] Error ejecutando la música pendiente de {member.display_name}: {e}")
