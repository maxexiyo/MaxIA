"""
ComandosMixin: los comandos con prefijo (!remember_user, !apodo, !genero,
!personaje, !participar, !forget, !privado, !regla, !contexto) y el manejo
de errores de comandos (que redirige texto sin comando reconocido al chat
normal con la IA).
"""
import re
import discord
from discord import app_commands
from discord.ext import commands


class ComandosMixin:
    @commands.hybrid_command(name="contexto", aliases=["revisar", "entender"])
    @app_commands.describe(cantidad='Cuántos mensajes leer (máx. 100)', duda='Qué quieres saber o hacer')
    async def check_context(self, ctx, cantidad: int = 30, *, duda: str = "Entiende el contexto del chat y responde"):
        """Hace que la IA lea los últimos N mensajes del canal (hasta 100) antes de responder."""
        limite = max(1, min(cantidad, 100))
        if ctx.interaction is not None:
            # Con /slash no hay un mensaje real al que responder: se usa el adaptador (ver slash_adapter.py)
            from ia_core.slash_adapter import SlashMessage
            msg = SlashMessage.desde_ctx(ctx, duda)
            await self.process_ai_request(msg, duda, history_limit=limite)
            await msg.cerrar()
            return
        await self.process_ai_request(ctx.message, duda, history_limit=limite)

    @commands.hybrid_command(name="remember_user", aliases=["perfil_usuario"])
    @app_commands.describe(member='Usuario', profile_data='Dato a recordar (--privado al final = nota privada)')
    async def set_user_profile(self, ctx, member: discord.Member = None, *, profile_data: str = None):
        if not ctx.guild: return await ctx.send("❌ Este comando solo funciona dentro de servidores.")
        if not member or not profile_data:
            return await ctx.send("❌ Uso: `!remember_user @usuario datos` (agrega `--privado` al final para que sea una nota privada)")
        
        es_privado = "--privado" in profile_data.lower()
        if es_privado:
            profile_data = re.sub(r'--privado', '', profile_data, flags=re.IGNORECASE).strip()
        
        guardado = self.save_user_profile(member.id, member.display_name, profile_data, tipo="privado" if es_privado else "publico")
        if not guardado:
            return await ctx.send("🚫 No se guardó: ese dato es sexual, de salud o sobre orientación sexual/identidad de género - ninguna de esas categorías se guarda en ningún perfil, sin excepción.")
        etiqueta = "🔒 privada (solo la ve él/ella mismo en DM)" if es_privado else "🧠 pública"
        await ctx.send(f"{etiqueta} **Memoria GLOBAL actualizada para {member.display_name}** (la recordaré en cualquier servidor):\n`{profile_data}`")

    @commands.hybrid_command(name="apodo", aliases=["nickname", "alias"])
    @app_commands.describe(member='Usuario', nickname='Apodo')
    async def set_user_nickname(self, ctx, member: discord.Member = None, *, nickname: str = None):
        """Enseña al bot un apodo para un usuario. !apodo @usuario liko"""
        if not ctx.guild: return await ctx.send("❌ Solo funciona en servidores.")
        if not member or not nickname:
            return await ctx.send("❌ Uso: `!apodo @usuario apodo`\nEjemplo: `!apodo @Juan liki`")
        
        self.save_user_nickname(ctx.guild.id, member.display_name, nickname)
        existing_nicknames = self.get_user_nicknames(ctx.guild.id, member.display_name)
        nicknames_str = ", ".join(existing_nicknames) if existing_nicknames else nickname
        await ctx.send(f"✅ **Apodo guardado:** {member.display_name} también se le dice: `{nicknames_str}`")

    @commands.hybrid_command(name="genero", aliases=["pronombre"])
    @app_commands.describe(valor='Tu pronombre o género (ej: él, ella, elle)')
    async def set_own_gender(self, ctx, *, valor: str = None):
        """El usuario declara su propio género/pronombre (nunca se infiere solo). !genero ella"""
        if not valor:
            return await ctx.send("❌ Uso: `!genero <valor>` (ej: `!genero él`, `!genero ella`, `!genero elle`)\nSolo puedes ponerlo para ti mismo/a.")
        self.save_user_gender(ctx.author.id, ctx.author.display_name, valor.strip())
        await ctx.send(f"✅ Listo, ahora sé que te trato como: **{valor.strip()}**")

    @commands.hybrid_command(name="personaje", aliases=["personalidad", "character", "modo_personaje"])
    @app_commands.describe(modo='Personalidad del bot en este servidor')
    @app_commands.choices(modo=[app_commands.Choice(name='max', value='max'), app_commands.Choice(name='neko', value='neko'), app_commands.Choice(name='sexo', value='sexo'), app_commands.Choice(name='yuri', value='yuri')])
    async def set_personality(self, ctx, modo: str = None):
        """Cambia la personalidad del bot en este server. !max personaje max / !max personaje neko"""
        if not ctx.guild: return await ctx.send("❌ Solo funciona en servidores.")
        opciones = ", ".join(f"`{k}`" for k in self.PERSONALIDADES.keys())
        if not modo or modo.lower() not in self.PERSONALIDADES:
            actual = self.get_personality_mode(ctx.guild.id)
            return await ctx.send(
                f"❓ Uso: `!max personaje <modo>`\nModos disponibles: {opciones}\n"
                f"Modo actual: **{self.PERSONALIDADES[actual]['nombre']}** (`{actual}`)"
            )
        modo = modo.lower()

        self.set_personality_mode(ctx.guild.id, modo)
        nombre = self.PERSONALIDADES[modo]['nombre']
        await ctx.send(f"✅ **Personalidad cambiada a {nombre}** (`{modo}`) para todo este servidor.")

    @commands.hybrid_command(name="personajes", aliases=["pj", "elenco"])
    async def list_personajes(self, ctx):
        """Muestra los personajes del bot: su nombre, si tienen foto y cuál está activo."""
        if not ctx.guild: return await ctx.send("❌ Solo funciona en servidores.")
        activo = self.get_personality(ctx.guild.id, ctx.channel)["clave"]
        embed = discord.Embed(title="🎭 Personajes", color=discord.Color.purple(),
                              description=f"Modo loco: **{'ACTIVADO 🤪' if self.get_loco_mode(ctx.guild.id) else 'desactivado'}**")
        for clave in self.personajes_disponibles(ctx.channel):
            info = self.pj_info(ctx.guild.id, clave)
            nombre = self.nombre_historial(ctx.guild.id, clave) if clave == "max" else info["nombre"]
            foto = "con foto" if (info.get("avatar") or clave == "max") else "sin foto todavía"
            embed.add_field(name=f"{'⭐ ' if clave == activo else ''}{nombre}  (`{clave}`)", value=foto, inline=False)
        embed.set_footer(text="!max personaje <modo> · !max loco on/off · !max pjnombre / pjfoto (admins)")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="pjnombre")
    @commands.has_permissions(administrator=True)
    @app_commands.describe(modo='Personaje', nombre='Nombre nuevo (máx. 32 caracteres)')
    @app_commands.choices(modo=[app_commands.Choice(name='neko', value='neko'), app_commands.Choice(name='sexo', value='sexo'), app_commands.Choice(name='yuri', value='yuri')])
    async def set_pj_nombre(self, ctx, modo: str = None, *, nombre: str = None):
        """Cambia el nombre con el que sale un personaje. !max pjnombre sexo Maxia Fatal"""
        if not ctx.guild: return await ctx.send("❌ Solo funciona en servidores.")
        if not modo or modo.lower() not in self.PERSONALIDADES or not nombre:
            return await ctx.send("❓ Uso: `!max pjnombre <modo> <nombre>` — modos: " + ", ".join(f"`{k}`" for k in self.PERSONALIDADES))
        modo, nombre = modo.lower(), nombre.strip()[:32]
        if modo == "max":
            return await ctx.send("ℹ️ La personalidad normal habla con la cuenta del bot: su nombre se cambia en el portal de desarrolladores de Discord.")
        if any(x in nombre.lower() for x in ("discord", "clyde", "everyone", "here")):
            return await ctx.send("❌ Discord no permite ese nombre en un webhook, prueba con otro.")
        self.pj_set(ctx.guild.id, modo, "nombre", nombre)
        await ctx.send(f"✅ El personaje `{modo}` ahora se llama **{nombre}**.")

    @commands.hybrid_command(name="pjfoto")
    @commands.has_permissions(administrator=True)
    @app_commands.describe(modo='Personaje', url='Link PERMANENTE a la imagen (imgur, GitHub raw...)')
    @app_commands.choices(modo=[app_commands.Choice(name='neko', value='neko'), app_commands.Choice(name='sexo', value='sexo'), app_commands.Choice(name='yuri', value='yuri')])
    async def set_pj_foto(self, ctx, modo: str = None, url: str = None):
        """Pone la foto de un personaje (link directo a la imagen). !max pjfoto yuri https://..."""
        if not ctx.guild: return await ctx.send("❌ Solo funciona en servidores.")
        if not modo or modo.lower() not in self.PERSONALIDADES or not url:
            return await ctx.send("❓ Uso: `!max pjfoto <modo> <link de la imagen>` — modos: " + ", ".join(f"`{k}`" for k in self.PERSONALIDADES)
                                  + "\nUsa un link PERMANENTE (imgur, GitHub raw...): los links de archivos subidos a Discord caducan.")
        modo, url = modo.lower(), url.strip().strip("<>")
        if modo == "max":
            return await ctx.send("ℹ️ La personalidad normal habla con la cuenta del bot: su foto se cambia en el portal de desarrolladores de Discord.")
        if not url.startswith(("http://", "https://")) or len(url) > 400:
            return await ctx.send("❌ Eso no parece un link válido (tiene que empezar con http:// o https://).")
        self.pj_set(ctx.guild.id, modo, "avatar", url)
        await ctx.send(f"✅ Foto de `{modo}` guardada. Se verá en su próximo mensaje.")

    @commands.hybrid_command(name="loco", aliases=["modoloco"])
    @commands.has_permissions(administrator=True)
    @app_commands.describe(estado='Encender o apagar el modo loco')
    @app_commands.choices(estado=[app_commands.Choice(name='on', value='on'), app_commands.Choice(name='off', value='off')])
    async def toggle_loco(self, ctx, estado: str = None):
        """Modo loco: los personajes conviven en el chat como personas distintas (!loco on / off)"""
        if not ctx.guild: return await ctx.send("❌ Solo funciona en servidores.")
        if not estado or estado.lower() not in ["on", "off", "activar", "desactivar"]:
            actual = self.get_loco_mode(ctx.guild.id)
            return await ctx.send(
                f"❓ Uso: `!loco on` o `!loco off`\n"
                f"Estado actual: **{'ACTIVADO 🤪' if actual else 'DESACTIVADO (default)'}**\n"
                f"Requiere el modo ambiental (`!participar on`) para que los personajes se metan solos a la plática."
            )
        activar = estado.lower() in ["on", "activar"]
        if activar and not self.get_ambient_mode(ctx.guild.id):
            return await ctx.send("⚠️ Primero activa el modo ambiental con `!participar on` (sin él los personajes no se meten solos a la plática). Luego vuelve a usar `!loco on`.")
        self.set_loco_mode(ctx.guild.id, activar)
        if not activar:
            return await ctx.send("🤐 **Modo loco DESACTIVADO** — vuelve a hablar un solo personaje a la vez.")
        aviso = ""
        if not ctx.channel.permissions_for(ctx.guild.me).manage_webhooks:
            aviso = ("\n⚠️ No tengo el permiso **Gestionar webhooks** en este canal, así que los personajes saldrán con mi cuenta "
                     "y su nombre en negritas en vez de verse como usuarios distintos.")
        await ctx.send("🤪 **Modo loco ACTIVADO** — ahora los personajes conviven en el chat como si fueran personas distintas y pueden "
                       "contestarse entre ellos (con frenos para que no se queden en bucle)." + aviso)

    @commands.hybrid_command(name="participar", aliases=["ambient", "modo_ambiental"])
    @commands.has_permissions(administrator=True)
    @app_commands.describe(estado='Encender o apagar el modo ambiental')
    @app_commands.choices(estado=[app_commands.Choice(name='on', value='on'), app_commands.Choice(name='off', value='off')])
    async def toggle_ambient_mode(self, ctx, estado: str = None):
        """Activa/desactiva que el bot opine sin ser invocado. Desactivado por default.
        Uso: !participar on / !participar off"""
        if not ctx.guild: return await ctx.send("❌ Solo funciona en servidores.")
        if not estado or estado.lower() not in ["on", "off", "activar", "desactivar"]:
            actual = self.get_ambient_mode(ctx.guild.id)
            return await ctx.send(
                f"❓ Uso: `!participar on` o `!participar off`\n"
                f"Estado actual: **{'ACTIVADO 🗣️' if actual else 'DESACTIVADO 🤐 (default)'}**"
            )
        activar = estado.lower() in ["on", "activar"]
        self.set_ambient_mode(ctx.guild.id, activar)
        if activar:
            await ctx.send("🗣️ **Modo ambiental ACTIVADO** — de vez en cuando puedo meterme en la conversación sin que me invoquen, si de verdad tengo algo que aportar.")
        else:
            await ctx.send("🤐 **Modo ambiental DESACTIVADO** — solo hablaré si me invocan.")

    @commands.hybrid_command(name="forget", aliases=["olvida", "reset_ia"])
    @commands.has_permissions(administrator=True)
    @app_commands.describe(scope='Qué memoria borrar')
    @app_commands.choices(scope=[app_commands.Choice(name='este canal', value='channel'), app_commands.Choice(name='todo el servidor', value='all')])
    async def reset_memory(self, ctx, scope: str = "channel"):
        if not ctx.guild: return await ctx.send("❌ Solo en servidores.")
        if scope.lower() in ["all", "todo", "server"]:
            self.clear_guild_memory(ctx.guild.id)
            await ctx.send("🧹 **Memoria de chat de TODO el servidor formateada.**")
        else:
            self.clear_guild_memory(ctx.guild.id, ctx.channel.id)
            await ctx.send(f"🧹 **Memoria del canal #{ctx.channel.name} formateada.**")

    @commands.hybrid_command(name="privado", aliases=["ignorar", "no_recordar"])
    @commands.has_permissions(administrator=True)
    @app_commands.describe(action='Marcar o desmarcar este canal como privado')
    @app_commands.choices(action=[app_commands.Choice(name='marcar privado', value='add'), app_commands.Choice(name='quitar privado', value='remove')])
    async def ignore_channel(self, ctx, action: str = "add"):
        """Marca un canal como privado (ignorado). !privado add / !privado remove"""
        if not ctx.guild: return await ctx.send("❌ Solo en servidores.")
        
        action = action.lower()
        if action in ["add", "agregar", "marcar"]:
            self.add_ignored_channel(ctx.guild.id, ctx.channel.id)
            await ctx.send(f"🔒 **Canal #{ctx.channel.name} marcado como PRIVADO.**\n✅ Ya no se guardarán mensajes aquí.")
        elif action in ["remove", "quitar", "desmarcar"]:
            self.remove_ignored_channel(ctx.guild.id, ctx.channel.id)
            await ctx.send(f"🔓 **Canal #{ctx.channel.name} vuelto a PÚBLICO.**\n✅ Se volverán a guardar mensajes aquí.")
        else:
            await ctx.send("❌ Uso: `!privado add` o `!privado remove`")

    @commands.hybrid_command(name="regla", aliases=["reglas", "rule"])
    @commands.has_permissions(administrator=True)
    @app_commands.describe(accion='Qué hacer con las reglas', texto='Texto de la regla, o el número a borrar')
    @app_commands.choices(accion=[app_commands.Choice(name='agregar', value='agregar'), app_commands.Choice(name='lista', value='lista'), app_commands.Choice(name='borrar', value='borrar')])
    async def manage_server_rules(self, ctx, accion: str = None, *, texto: str = None):
        """Administra las reglas fijas de este servidor (ej: 'no digas @everyone').
        Uso: !regla agregar <texto> | !regla lista | !regla borrar <número>
        La IA también puede detectar y agregar/quitar reglas si un admin se lo pide hablando normal,
        no hace falta usar este comando siempre."""
        if not ctx.guild: return await ctx.send("❌ Solo funciona en servidores.")
        accion = (accion or "").lower()

        if accion in ["agregar", "add", "nueva", "crear"]:
            if not texto:
                return await ctx.send("❌ Uso: `!regla agregar <texto de la regla>`")
            rid = self.add_server_rule(ctx.guild.id, texto.strip(), ctx.author.display_name)
            await ctx.send(f"✅ **Regla #{rid} guardada:**\n> {texto.strip()}")
        elif accion in ["borrar", "quitar", "eliminar", "delete", "remove"]:
            if not texto or not texto.strip().isdigit():
                return await ctx.send("❌ Uso: `!regla borrar <número>` (mira los números con `!regla lista`)")
            borrada = self.remove_server_rule(ctx.guild.id, int(texto.strip()))
            if borrada:
                await ctx.send(f"🗑️ **Regla borrada:**\n> {borrada}")
            else:
                await ctx.send("❌ No encontré ninguna regla con ese número.")
        elif accion in ["lista", "list", "ver", ""]:
            reglas = self.get_server_rules(ctx.guild.id)
            if not reglas:
                return await ctx.send("📋 Este servidor no tiene reglas fijas todavía.\nUsa `!regla agregar <texto>` para crear una.")
            lineas = "\n".join(f"**#{r['id']}** — {r['texto']} _(agregada por {r.get('autor', '?')})_" for r in reglas)
            await ctx.send(f"📋 **Reglas fijas de este servidor:**\n{lineas}")
        else:
            await ctx.send("❓ Uso: `!regla agregar <texto>` / `!regla lista` / `!regla borrar <número>`")

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if ctx.author.bot: return
        if isinstance(error, commands.CommandNotFound):
            prompt = ctx.message.content[len(ctx.prefix):].strip()
            if not prompt and not ctx.message.attachments: return
            await self.process_ai_request(ctx.message, prompt)
        elif isinstance(error, commands.MissingPermissions): await ctx.send("🚫 No tienes permisos de admin.")
        else: raise error
