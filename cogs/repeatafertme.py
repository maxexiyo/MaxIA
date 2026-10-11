import discord
from discord.ext import commands
import logging

logger = logging.getLogger(__name__)

class Repeater(commands.Cog):
    """Cog para repetir texto y borrar el mensaje original del usuario"""
    
    def __init__(self, bot):
        self.bot = bot

    @commands.command(
        name='repeatafterme', 
        aliases=['repitedespuesemi', 'repiteesto', 'repetir']
    )
    async def repeat_after_me(self, ctx, *, text: str = None):
        """Repite lo que escriba el usuario en texto plano y borra su mensaje"""
        
        # Intentar borrar el mensaje del usuario primero
        try:
            await ctx.message.delete()
        except discord.Forbidden:
            logger.warning("Faltan permisos ('Manage Messages') para borrar el mensaje del usuario.")
        except discord.HTTPException as e:
            logger.error(f"Error al intentar borrar el mensaje: {e}")
        
        if not text:
            return
        
        # Enviar el texto repetido de forma plana
        try:
            await ctx.send(text)
            logger.info(f'Mensaje repetido para {ctx.author.name}: {text}')
        except Exception as e:
            logger.error(f'Error al enviar el mensaje repetido: {e}')

async def setup(bot):
    await bot.add_cog(Repeater(bot))