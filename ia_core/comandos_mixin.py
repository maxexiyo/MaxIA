"""
ComandosMixin: los comandos con prefijo (!remember_user, !apodo, !genero,
!personaje, !participar, !forget, !privado, !regla, !contexto) y el manejo
de errores de comandos (que redirige texto sin comando reconocido al chat
normal con la IA).
"""
import re
import discord
from discord.ext import commands


class ComandosMixin:
    @commands.command(name="contexto", aliases=["revisar", "entender"])
    async def check_context(self, ctx, cantidad: int = 30, *, duda: str = "Entiende el contexto del chat y responde"):
        await self.process_ai_request(ctx.message, duda, history_limit=min(cantidad, 100))

    @commands.command(name="remember_user", aliases=["perfil_usuario"])
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

    @commands.command(name="apodo", aliases=["nickname", "alias"])
    async def set_user_nickname(self, ctx, member: discord.Member = None, *, nickname: str = None):
        """Enseña al bot un apodo para un usuario. !apodo @usuario liko"""
        if not ctx.guild: return await ctx.send("❌ Solo funciona en servidores.")
        if not member or not nickname:
            return await ctx.send("❌ Uso: `!apodo @usuario apodo`\nEjemplo: `!apodo @Juan liki`")
        
        self.save_user_nickname(ctx.guild.id, member.display_name, nickname)
        existing_nicknames = self.get_user_nicknames(ctx.guild.id, member.display_name)
        nicknames_str = ", ".join(existing_nicknames) if existing_nicknames else nickname
        await ctx.send(f"✅ **Apodo guardado:** {member.display_name} también se le dice: `{nicknames_str}`")

    @commands.command(name="genero", aliases=["pronombre"])
    async def set_own_gender(self, ctx, *, valor: str = None):
        """El usuario declara su propio género/pronombre (nunca se infiere solo). !genero ella"""
        if not valor:
            return await ctx.send("❌ Uso: `!genero <valor>` (ej: `!genero él`, `!genero ella`, `!genero elle`)\nSolo puedes ponerlo para ti mismo/a.")
        self.save_user_gender(ctx.author.id, ctx.author.display_name, valor.strip())
        await ctx.send(f"✅ Listo, ahora sé que te trato como: **{valor.strip()}**")

    @commands.command(name="personaje", aliases=["personalidad", "character", "modo_personaje"])
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

    @commands.command(name="participar", aliases=["ambient", "modo_ambiental"])
    @commands.has_permissions(administrator=True)
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

    @commands.command(name="forget", aliases=["olvida", "reset_ia"])
    @commands.has_permissions(administrator=True)
    async def reset_memory(self, ctx, scope: str = "channel"):
        if not ctx.guild: return await ctx.send("❌ Solo en servidores.")
        if scope.lower() in ["all", "todo", "server"]:
            self.clear_guild_memory(ctx.guild.id)
            await ctx.send("🧹 **Memoria de chat de TODO el servidor formateada.**")
        else:
            self.clear_guild_memory(ctx.guild.id, ctx.channel.id)
            await ctx.send(f"🧹 **Memoria del canal #{ctx.channel.name} formateada.**")

    @commands.command(name="privado", aliases=["ignorar", "no_recordar"])
    @commands.has_permissions(administrator=True)
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

    @commands.command(name="regla", aliases=["reglas", "rule"])
    @commands.has_permissions(administrator=True)
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

