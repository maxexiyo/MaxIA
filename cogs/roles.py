import discord
from discord.ext import commands
import asyncio
import logging

try:
    from database import db
except Exception:
    from ..database import db

logger = logging.getLogger(__name__)

async def is_admin_or_owner(ctx) -> bool:
    """Verifica permisos de Administrador, Owner del Servidor u Owner del Bot"""
    if ctx.guild is None:
        return False
    if ctx.author.id == ctx.guild.owner_id or ctx.author.guild_permissions.administrator:
        return True
    return await ctx.bot.is_owner(ctx.author)


class ReactionRole(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.group(name='rr', invoke_without_command=True)
    async def rr_group(self, ctx):
        """Comando base de reaction roles"""
        await ctx.send("❓ Usa `!max rr make` para iniciar la creación de encuesta reactionrole.")

    @rr_group.command(name='make', aliases=['setup', 'crear'])
    async def rr_make(self, ctx):
        """Inicia el asistente interactivo paso a paso (estilo Carl-bot)"""
        if not await is_admin_or_owner(ctx):
            await ctx.send("❌ No tienes permisos de Administrador para configurar este panel.")
            return

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            # 📌 PASO 1: Selección del Canal
            await ctx.send(
                "📌 **Paso 1/4: Selección de Canal**\n"
                "¿En qué canal deseas publicar la encuesta reaction roles?\n"
                "Menciona el canal (ej: `#roles`) o escribe `aqui` para usar este mismo canal."
            )
            msg_channel = await self.bot.wait_for('message', check=check, timeout=120)
            
            if msg_channel.content.lower() in ['here', 'aqui', 'aquí']:
                target_channel = ctx.channel
            elif msg_channel.channel_mentions:
                target_channel = msg_channel.channel_mentions[0]
            elif msg_channel.content.isdigit():
                target_channel = ctx.guild.get_channel(int(msg_channel.content))
            else:
                await ctx.send("❌ Canal no válido. Proceso cancelado.")
                return

            if not isinstance(target_channel, discord.TextChannel):
                await ctx.send("❌ El canal debe ser un canal de texto. Proceso cancelado.")
                return

            # 📝 PASO 2: Título y Descripción
            await ctx.send(
                "📝 **Paso 2/4: Título y Descripción**\n"
                "Escribe el **Título | Descripción** del mensaje separados por una barra vertical `|`.\n"
                "*(Ejemplo: `Roles de Notificación | Reacciona para recibir tus roles`)*"
                "si no sabes como ponerlo o cual es simplemente copialo y pegalo de aqui."
            )
            await ctx.send("|")

            msg_title_desc = await self.bot.wait_for('message', check=check, timeout=120)
            
            if '|' in msg_title_desc.content:
                parts = msg_title_desc.content.split('|', 1)
                title = parts[0].strip()
                description = parts[1].strip()
            else:
                title = msg_title_desc.content.strip()
                description = "Reacciona con el emoji correspondiente para recibir el rol."

            # 🎨 PASO 3: Color del Embed
            await ctx.send(
                "🎨 **Paso 3/4: Color del Embed**\n"
                "Ingresa el código Hexadecimal del color (ejemplo: `#3498db` o `3498db`), "
                "o escribe `none` para usar el color por defecto."
            )
            msg_color = await self.bot.wait_for('message', check=check, timeout=120)
            color_text = msg_color.content.strip().lower()

            embed_color = discord.Color.blue()
            if color_text not in ['none', 'ninguno', 'no']:
                clean_hex = color_text.lstrip('#')
                try:
                    embed_color = discord.Color(int(clean_hex, 16))
                except ValueError:
                    await ctx.send("⚠️ Código Hexadecimal inválido, se usará el color por defecto.")

            # ✨ PASO 4: Vincular Emojis y Roles (Bucle interactivo)
            await ctx.send(
                "✨ **Paso 4/4: Vinculación de Emojis y Roles**\n"
                "Envía un mensaje por cada rol con el siguiente formato:\n"
                "`:emoji: @rol`  o  `:emoji: NombreDelRol`\n\n"
                "• *Ejemplo:* `🎮 @Gamer` o `:emoji_personalizado: @Anuncios`\n"
                "• El bot reaccionará con **✅** a cada par válido.\n"
                "• Cuando termines, escribe **`done`** o **`listo`** para publicar el panel."
            )

            reactions_list = []
            roles_list = []
            display_pairs = []

            while True:
                pair_msg = await self.bot.wait_for('message', check=check, timeout=300)
                content = pair_msg.content.strip()

                if content.lower() in ['done', 'listo', 'finalizar']:
                    break

                parts = content.split(maxsplit=1)
                if len(parts) < 2:
                    await pair_msg.add_reaction("❌")
                    await ctx.send("⚠️ Formato incorrecto. Debe ser: `:emoji: @rol`", delete_after=5)
                    continue

                raw_emoji, raw_role = parts[0].strip(), parts[1].strip()

                # Búsqueda/Validación del Rol
                role = None
                if pair_msg.role_mentions:
                    role = pair_msg.role_mentions[0]
                elif raw_role.isdigit():
                    role = ctx.guild.get_role(int(raw_role))
                else:
                    role = discord.utils.get(ctx.guild.roles, name=raw_role)

                if not role:
                    await pair_msg.add_reaction("❌")
                    await ctx.send(f"⚠️ No se encontró el rol `{raw_role}`.", delete_after=5)
                    continue

                # Validar Jerarquía de Roles
                if role.position >= ctx.guild.me.top_role.position:
                    await pair_msg.add_reaction("❌")
                    await ctx.send(f"⚠️ No puedo asignar {role.mention} porque está por encima de mi rol más alto.", delete_after=5)
                    continue

                # Confirmación en chat y almacenamiento
                await pair_msg.add_reaction("✅")
                reactions_list.append(raw_emoji)
                roles_list.append(str(role.id))
                display_pairs.append(f"{raw_emoji} ➔ {role.mention}")

            if not reactions_list:
                await ctx.send("❌ No agregaste ningún emoji/rol. Creación cancelada.")
                return

            # Publicar Embed final
            final_embed = discord.Embed(
                title=title,
                description=f"{description}\n\n" + "\n".join(display_pairs),
                color=embed_color
            )
            final_embed.set_footer(text="Reaction Roles")

            panel_msg = await target_channel.send(embed=final_embed)

            # Reaccionar en el mensaje publicado
            for emoji in reactions_list:
                try:
                    await panel_msg.add_reaction(emoji)
                except Exception as e:
                    logger.warning(f"No se pudo reaccionar con {emoji}: {e}")

            # Guardar en Base de Datos
            db.add_reaction_role(ctx.guild.id, panel_msg.id, reactions_list, roles_list)
            db.add_audit_log(ctx.guild.id, 'REACTION_ROLE_PANEL_CREATED', 0, ctx.author.id, f'Msg ID: {panel_msg.id}')

            await ctx.send(f"🎉 **¡Panel publicado en {target_channel.mention}!**\n🆔 ID del mensaje: `{panel_msg.id}`")

        except asyncio.TimeoutError:
            await ctx.send("⏰ Tiempo de espera agotado. Proceso cancelado.")
        except Exception as e:
            logger.error(f"Error en rr_make: {e}")
            await ctx.send(f"❌ Ocurrió un error: {e}")

    # ==========================================
    # LISTENERS DE REACCIONES
    # ==========================================

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return
        
        guild_id = payload.guild_id
        message_id = payload.message_id
        emoji = str(payload.emoji)
        
        rr = db.get_reaction_role(guild_id, message_id)
        if not rr or emoji not in rr['reactions']:
            return
        
        idx = rr['reactions'].index(emoji)
        role_id = int(rr['roles'][idx])
        
        try:
            guild = self.bot.get_guild(guild_id)
            user = guild.get_member(payload.user_id)
            role = guild.get_role(role_id)
            
            if user and role:
                await user.add_roles(role)
        except Exception as e:
            logger.error(f'Error al dar rol: {e}')

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return
        
        guild_id = payload.guild_id
        message_id = payload.message_id
        emoji = str(payload.emoji)
        
        rr = db.get_reaction_role(guild_id, message_id)
        if not rr or emoji not in rr['reactions']:
            return
        
        idx = rr['reactions'].index(emoji)
        role_id = int(rr['roles'][idx])
        
        try:
            guild = self.bot.get_guild(guild_id)
            user = guild.get_member(payload.user_id)
            role = guild.get_role(role_id)
            
            if user and role:
                await user.remove_roles(role)
        except Exception as e:
            logger.error(f'Error al quitar rol: {e}')


async def setup(bot):
    await bot.add_cog(ReactionRole(bot))