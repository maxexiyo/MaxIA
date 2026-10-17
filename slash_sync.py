"""
Slash commands del bot (/comando además del prefijo !max).

Qué hace este cog:
  · !sync / !sync aqui  (solo el dueño del bot): sube los slash commands a Discord. Hay que correrlo después de
    agregar, quitar o cambiar comandos. `!sync aqui` los pone al instante en el servidor donde lo uses (ideal para
    probar); `!sync` los pone en todos (puede tardar un rato en verse). Opcional: SLASH_AUTO_SYNC=1 en el .env
    sincroniza solo al arrancar el bot.
  · Defer automático: Discord solo da 3 segundos para responder un /comando. Para que ninguno falle por lentitud,
    se avisa "pensando..." antes de ejecutarlo (los comandos con prefijo no se enteran de nada).
  · Si un /comando truena, se contesta con un aviso en vez de dejarlo "pensando..." para siempre.
  · /max <mensaje>: hablar con la IA por slash (equivale a escribir "!max <mensaje>").
"""
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)

NO_DEFER = {"buscarm"}      # comandos que ya manejan su propio defer/respuesta


class SlashTools(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._ya_sincronizado = False
        # El bot solo admite UN hook global de before_invoke: se encadena con el que ya hubiera.
        self._hook_previo = getattr(bot, "_before_invoke", None)
        bot.before_invoke(self._antes_de_invocar)

    async def cog_unload(self):
        self.bot._before_invoke = self._hook_previo

    async def _antes_de_invocar(self, ctx):
        if self._hook_previo is not None:
            await self._hook_previo(ctx)
        if ctx.interaction is not None and ctx.command and ctx.command.qualified_name not in NO_DEFER \
                and not ctx.interaction.response.is_done():
            await ctx.defer()

    # ───────────── sincronizar ─────────────
    @commands.Cog.listener()
    async def on_ready(self):
        if self._ya_sincronizado or os.getenv("SLASH_AUTO_SYNC", "0") != "1":
            return
        self._ya_sincronizado = True
        try:
            cmds = await self.bot.tree.sync()
            logger.info(f"✅ Slash commands sincronizados al arrancar: {len(cmds)}")
        except Exception as e:
            logger.error(f"No se pudieron sincronizar los slash commands: {e!r}")

    @commands.command(name="sync")
    @commands.is_owner()
    async def sync_cmd(self, ctx, alcance: str = "global"):
        """(Dueño) Sincroniza los slash commands. !sync = todos los servidores · !sync aqui = solo este, al instante · !sync limpiar = quita los de este servidor"""
        alcance = alcance.lower()
        try:
            if alcance in ("aqui", "aquí", "here", "guild"):
                if ctx.guild is None:
                    return await ctx.send("❌ Eso solo se puede en un servidor.")
                self.bot.tree.copy_global_to(guild=ctx.guild)
                cmds = await self.bot.tree.sync(guild=ctx.guild)
                return await ctx.send(f"✅ {len(cmds)} slash commands sincronizados en este servidor (ya los puedes usar).")
            if alcance in ("limpiar", "clear"):
                if ctx.guild is None:
                    return await ctx.send("❌ Eso solo se puede en un servidor.")
                self.bot.tree.clear_commands(guild=ctx.guild)
                await self.bot.tree.sync(guild=ctx.guild)
                return await ctx.send("🧹 Quité los slash commands propios de este servidor (siguen los globales).")
            cmds = await self.bot.tree.sync()
            await ctx.send(f"✅ {len(cmds)} slash commands sincronizados globalmente (pueden tardar un rato en aparecer en todos lados).")
        except Exception as e:
            await ctx.send(f"❌ No pude sincronizar: {e}")

    # ───────────── errores de /comandos ─────────────
    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if ctx.interaction is None:
            return
        if isinstance(error, (commands.CommandNotFound, commands.MissingPermissions)):
            return          # ya los atiende el cog de IA
        if isinstance(error, commands.CommandInvokeError):
            logger.error(f"Error en /{ctx.command}: {error.original!r}")
            texto = "Algo salió mal ejecutando ese comando. Intenta de nuevo."
        elif isinstance(error, commands.CheckFailure):
            texto = "No puedes usar ese comando aquí."
        else:
            texto = str(error) or "No pude ejecutar ese comando."
        try:
            await ctx.send(f"❌ {texto}", ephemeral=True)
        except discord.HTTPException:
            pass

    # ───────────── /max ─────────────
    @app_commands.command(name="max", description="Habla con la IA (usa la personalidad activa del servidor)")
    @app_commands.describe(mensaje="Lo que quieres decirle o preguntarle", imagen="Una imagen para que la vea (opcional)")
    async def max_slash(self, interaction: discord.Interaction, mensaje: str, imagen: discord.Attachment = None):
        ia = self.bot.get_cog("AIModule")
        if ia is None:
            return await interaction.response.send_message("❌ El módulo de IA no está cargado.", ephemeral=True)
        await interaction.response.defer()
        from ia_core.slash_adapter import SlashMessage
        msg = SlashMessage.desde_interaction(interaction, mensaje, [imagen] if imagen else None)
        try:
            await ia.process_ai_request(msg, mensaje)
        except Exception as e:
            logger.error(f"Error en /max: {e!r}")
            await interaction.followup.send("❌ Algo salió mal, intenta de nuevo.", ephemeral=True)
        finally:
            await msg.cerrar()


async def setup(bot):
    await bot.add_cog(SlashTools(bot))
